const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const path = require("node:path");
const changes = import("../tool/ci/image-changes.mjs");

const root = path.resolve(__dirname, "..");
const head = "b".repeat(40);
const published = {revision: "a".repeat(40), created: "2026-11-20T10:00:00Z"};
const now = Date.parse("2026-11-22T10:00:00Z");
const base = {head, published, ancestor: true, now};

test("a push that only touches docs, plans, tests or tooling publishes nothing", async () => {
  const {decide} = await changes;
  const result = decide({...base, changedFiles: ["docs/plans/x.md", "README.md", "tests/test_x.py",
    "tool/deployment_updater.py", "ansible/playbooks/deploy-updater.yml", ".github/workflows/validation.yml"]});
  assert.equal(result.publish, false);
  assert.match(result.reason, /No image input changed since aaaaaaa/);
});

test("a change to either image's inputs publishes and names the image", async () => {
  const {decide} = await changes;
  for (const [file, image] of [["web/static/status.js", "web"], ["mpdrussoundpi/common/imageregistry.py", "web"],
    ["Dockerfile", "web"], [".dockerignore", "web"], ["mpdrussoundpi/__init__.py", "web"],
    ["docker/entrypoint-ser2net.sh", "ser2net"], ["Dockerfile.ser2net", "ser2net"],
    ["Dockerfile.ser2net.dockerignore", "ser2net"]]) {
    const result = decide({...base, changedFiles: ["docs/x.md", file]});
    assert.equal(result.publish, true, file);
    assert.deepEqual(result.changed, [image], file);
  }
  assert.equal(decide({...base, changedFiles: ["mpdrussoundpi/agent/x.py", "webhooks.md"]}).publish, false,
    "prefixes match whole directories only");
  assert.equal(decide({...base, changedFiles: ["web/API.md", "mpdrussoundpi/common/README.md"]}).publish, false,
    "documentation is not in the images");
  assert.match(readFileSync(path.join(root, ".dockerignore"), "utf8"), /^\*\*\/\*\.md$/m,
    "the web build context really leaves documentation out");
  assert.equal(decide({...base, changedFiles: [".github/workflows/images-build-only.yml"]}).publish, true,
    "the build definition shapes the images too");
});

test("force line, age and uncertainty publish; the same revision never does", async () => {
  const {decide, MAX_AGE_DAYS} = await changes;
  assert.equal(decide({...base, messages: "Docs\n\nPublish-Images: force\n"}).publish, true);
  assert.equal(decide({...base, messages: "mentions Publish-Images: force inline"}).publish, false);
  const old = {...published, created: new Date(now - MAX_AGE_DAYS * 24 * 3600 * 1000).toISOString()};
  assert.match(decide({...base, published: old}).reason, /7 days old/);
  assert.equal(decide({...base, published: null}).publish, true);
  assert.equal(decide({...base, published: {revision: "nope"}}).publish, true);
  assert.equal(decide({...base, published: {revision: published.revision}}).publish, true, "no build time");
  assert.equal(decide({...base, ancestor: false}).publish, true, "history rewritten");
  assert.equal(decide({...base, published: {...published, revision: head}}).publish, false);
});

test("the published state is read anonymously, without sending the token to the blob CDN", async () => {
  const {publishedDevelopment} = await changes;
  const seen = [];
  const json = (body, status = 200, headers = {}) => ({ok: status < 300, status, json: async () => body,
    headers: {get: name => headers[name.toLowerCase()] ?? null}});
  const fetchRequest = async (url, init = {}) => {
    seen.push({url, auth: init.headers?.Authorization});
    const {hostname, pathname} = new URL(url);
    if (hostname === "auth.docker.io") {
      assert.equal(init.headers, undefined, "anonymous token request");
      return json({token: "t"});
    }
    if (hostname === "cdn.example") return json({created: "2026-11-21T00:00:00Z",
      config: {Labels: {"org.opencontainers.image.revision": published.revision}}});
    if (pathname.endsWith("/manifests/dev")) return json({manifests: [
      {digest: "sha256:arm", platform: {os: "linux", architecture: "arm64"}},
      {digest: "sha256:amd", platform: {os: "linux", architecture: "amd64"}}]});
    if (pathname.endsWith("/manifests/sha256:amd")) return json({config: {digest: "sha256:cfg"}});
    if (pathname.endsWith("/blobs/sha256:cfg")) return json(null, 307, {location: "https://cdn.example/blob"});
    throw new Error(`unexpected ${url}`);
  };
  const result = await publishedDevelopment("itcornernet", fetchRequest);
  assert.deepEqual(result, {revision: published.revision, created: "2026-11-21T00:00:00.000Z"});
  assert.ok(seen.filter(entry => entry.url.startsWith("https://cdn.example")).every(entry => entry.auth === undefined));
  assert.ok(seen.some(entry => entry.url.includes("mpdrussoundpi-ser2net")), "both images are read");
});

test("evaluate compares against the published revision in git and publishes when the registry is unreadable", async () => {
  const {evaluate} = await changes;
  const calls = [];
  const run = args => {
    calls.push(args.join(" "));
    if (args[0] === "diff") return "docs/a.md\ntests/b.py\n";
    if (args[0] === "log") return "Docs only\n";
    return "";
  };
  const registry = async () => { throw new Error("offline"); };
  const offline = await evaluate({namespace: "itcornernet", head, fetchRequest: registry, run, now});
  assert.equal(offline.publish, true);
  assert.deepEqual(calls, [], "no git work without a published revision");
  await assert.rejects(evaluate({namespace: "itcornernet", head: "HEAD", run}), /full pushed commit/);
});

test("the input lists cover everything the Dockerfiles copy", async () => {
  const {IMAGE_INPUTS, touches} = await changes;
  for (const [image, dockerfile] of [["web", "Dockerfile"], ["ser2net", "Dockerfile.ser2net"]]) {
    const sources = readFileSync(path.join(root, dockerfile), "utf8").split("\n")
      .filter(line => /^COPY\s/.test(line) && !line.includes("--from="))
      .flatMap(line => line.trim().split(/\s+/).slice(1, -1).filter(part => !part.startsWith("--")));
    assert.ok(sources.length > 0, dockerfile);
    for (const source of sources) {
      assert.ok(touches(source, IMAGE_INPUTS[image]) || touches(source.replace(/\/?$/, "/"), IMAGE_INPUTS[image]),
        `${dockerfile} copies ${source}, which ${image}'s inputs do not cover`);
    }
    assert.ok(IMAGE_INPUTS[image].includes(dockerfile));
  }
});

test("the workflow builds and publishes only after the decision", () => {
  const workflow = readFileSync(path.join(root, ".github/workflows/images-build-only.yml"), "utf8");
  assert.match(workflow, /\n  images:\n    needs: \[validation, policy, changes\]\n    if: needs\.changes\.outputs\.build == 'true'\n/);
  assert.match(workflow, /\n  publish-development:\n    needs: \[policy, images\]\n/);
  const changesJob = workflow.slice(workflow.indexOf("\n  changes:"), workflow.indexOf("\n  images:"));
  assert.match(changesJob, /node tool\/ci\/image-changes\.mjs/);
  assert.doesNotMatch(changesJob, /\$\{\{\s*secrets\./);
});
