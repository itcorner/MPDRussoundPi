const {test} = require("node:test");
const assert = require("node:assert/strict");
const {spawnSync} = require("node:child_process");
const {createHash} = require("node:crypto");
const {mkdtempSync, writeFileSync, rmSync} = require("node:fs");
const {tmpdir} = require("node:os");
const path = require("node:path");
const transport = import("../tool/ci/image-publisher.mjs");
const repository = "itcornernet/mpdrussoundpi-web";
const token = `header.${Buffer.from(JSON.stringify({access: [
  {type: "repository", name: repository, actions: ["pull", "push"]},
]})).toString("base64url")}.signature`;

test("authenticated inventory is fresh on each call and paginated under the intended repository scope", async () => {
  const {DockerHubRegistry} = await transport;
  let inventories = 0;
  let grants = 0;
  const registry = new DockerHubRegistry("itcornernet", "itcornernet", "test-placeholder", async (url, options) => {
    if (url.startsWith("https://auth.docker.io/")) {
      grants++;
      assert.equal(new URL(url).searchParams.get("scope"), `repository:${repository}:pull,push`);
      return {ok: true, json: async () => ({token})};
    }
    inventories++;
    assert.equal(options.headers.Authorization, `Bearer ${token}`);
    assert.equal(options.redirect, "manual");
    const next = new URL(url).searchParams.has("last");
    return {ok: true, status: 200,
      headers: new Headers(next ? {} : {link: `</v2/${repository}/tags/list?n=100&last=dev>; rel="next"`}),
      json: async () => ({name: repository, tags: next ? ["dev-other"] : ["dev"]})};
  });
  assert.deepEqual(await registry.tags(repository), ["dev", "dev-other"]);
  assert.deepEqual(await registry.tags(repository), ["dev", "dev-other"]);
  assert.equal(grants, 1);
  assert.equal(inventories, 4);
});

test("blob CDN redirect receives no bearer credentials; foreign writes are rejected", async () => {
  const {DockerHubRegistry} = await transport;
  const registry = new DockerHubRegistry("itcornernet", "itcornernet", "test-placeholder", async (url, options) => {
    if (url.startsWith("https://auth.docker.io/")) return {ok: true, json: async () => ({token})};
    if (url.startsWith("https://registry-1.docker.io/")) {
      return {status: 307, headers: new Headers({location: "https://cdn.example.invalid/blob"})};
    }
    assert.equal(url, "https://cdn.example.invalid/blob");
    assert.equal(options.headers, undefined);
    return {ok: true, status: 200};
  });
  await registry.request(`/v2/${repository}/blobs/sha256:test`);
  await assert.rejects(registry.request("https://example.invalid/upload", {method: "PUT"}), /intended repositories/);
  await assert.rejects(registry.request("/v2/other/mpdrussoundpi-web/tags/list"), /intended repositories/);
  await assert.rejects(registry.request(`/v2/${repository}/blobs/uploads/`, {method: "PUT"}), /HTTP 307/);
});

test("missing tags require explicit registry missing-resource errors; auth errors never become empty inventories", async () => {
  const {DockerHubRegistry} = await transport;
  for (const [status, errors, expected] of [
    [404, [{code: "NAME_UNKNOWN"}], []], [404, [{code: "UNAUTHORIZED"}], null],
    [401, [], null], [429, [], null], [500, [], null],
  ]) {
    const registry = new DockerHubRegistry("itcornernet", "itcornernet", "test-placeholder", async url =>
      url.startsWith("https://auth.docker.io/") ? {ok: true, json: async () => ({token})}
        : {ok: false, status, json: async () => ({errors})});
    if (expected) assert.deepEqual(await registry.tags(repository), expected);
    else await assert.rejects(registry.tags(repository), /missing-resource|HTTP/);
  }
});

test("alias read-back tolerates stale and missing reads without repeating PUT", async t => {
  const {DockerHubRegistry} = await transport;
  const bytes = Buffer.from('{"schemaVersion":2}');
  const stale = Buffer.from('{"schemaVersion":2,"manifests":[]}');
  const digest = value => `sha256:${createHash("sha256").update(value).digest("hex")}`;
  const requests = [];
  let reads = 0;
  const registry = new DockerHubRegistry("itcornernet", "itcornernet", "test-placeholder", async (url, options) => {
    if (url.startsWith("https://auth.docker.io/")) return {ok: true, json: async () => ({token})};
    requests.push(options.method ?? "GET");
    if (options.method === "PUT") return new Response(null, {status: 201});
    if (++reads === 2) return Response.json({errors: [{code: "MANIFEST_UNKNOWN"}]}, {status: 404});
    const body = reads === 1 ? stale : bytes;
    return new Response(body, {headers: {"docker-content-digest": digest(body)}});
  });
  const warn = t.mock.method(console, "warn", () => {});
  await registry.put(repository, "dev", bytes, undefined, {mutableAlias: true});
  assert.deepEqual(requests, ["PUT", "GET", "GET", "GET"]);
  assert.match(warn.mock.calls[0].arguments[0], new RegExp(`expected ${digest(bytes)}, observed ${digest(stale)}`));
  assert.match(warn.mock.calls[1].arguments[0], /observed 404/);
  requests.length = 0;
  reads = 0;
  await assert.rejects(registry.put(repository, "dev-exact", bytes), /digest verification/);
  assert.deepEqual(requests, ["PUT", "GET"]);
});

test("production CLI gate rejects unapproved, manual, stable and main publication before secret access", () => {
  for (const overrides of [
    {DEVELOPMENT_PUBLICATION_ENABLED: "false"}, {GITHUB_EVENT_NAME: "workflow_dispatch"},
    {GITHUB_REF: "refs/heads/main"}, {GITHUB_REF: "refs/tags/v1.2.3"}, {CHANNEL: "stable"}, {VERSION: "1.2.3"},
  ]) {
    const result = spawnSync(process.execPath, [path.resolve(__dirname, "../tool/ci/image-publisher.mjs")],
      {encoding: "utf8", env: {...process.env, MODE: "publish-development",
        DEVELOPMENT_PUBLICATION_ENABLED: "true", CHANNEL: "development", VERSION: "",
        GITHUB_EVENT_NAME: "push", GITHUB_REF: "refs/heads/develop", ...overrides}});
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /disabled|requires develop|only push|Only develop/);
  }
});

test("release CLI gate requires the enable variable and a version tag matching channel and version", () => {
  const run = overrides => spawnSync(process.execPath, [path.resolve(__dirname, "../tool/ci/image-publisher.mjs")],
    {encoding: "utf8", env: {...process.env, MODE: "publish-release", RELEASE_PUBLICATION_ENABLED: "true",
      CHANNEL: "candidate", VERSION: "0.9.0-rc.1", GITHUB_EVENT_NAME: "push", GITHUB_REF: "refs/tags/v0.9.0-rc.1",
      RECOVERY_APPROVED: "false", DOCKERHUB_NAMESPACE: "", DOCKERHUB_USERNAME: "", DOCKERHUB_TOKEN: "", ...overrides}});
  for (const overrides of [{RELEASE_PUBLICATION_ENABLED: "false"}, {RELEASE_PUBLICATION_ENABLED: ""},
    {RECOVERY_APPROVED: "true"}, {DEVELOPMENT_PUBLICATION_ENABLED: "true", RELEASE_PUBLICATION_ENABLED: ""}]) {
    const result = run(overrides);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /publication is disabled/, JSON.stringify(overrides));
  }
  for (const overrides of [{GITHUB_REF: "refs/heads/develop"}, {CHANNEL: "stable"}, {VERSION: "0.9.0-rc.2"},
    {GITHUB_REF: "refs/tags/v0.9.0"}, {GITHUB_EVENT_NAME: "workflow_dispatch"}, {CHANNEL: "development", VERSION: ""}]) {
    const result = run(overrides);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /version tag push matching|only push|Release publication requires/, JSON.stringify(overrides));
  }
  // Past the gate, the registry transport is created: here it stops for the missing credentials.
  for (const overrides of [{}, {CHANNEL: "stable", VERSION: "0.9.0", GITHUB_REF: "refs/tags/v0.9.0"}]) {
    assert.match(run(overrides).stderr, /namespace, username and token are required/);
  }
});

test("the release environment must name a required reviewer", async () => {
  const {checkReleaseEnvironment, fetchEnvironment} = await import("../tool/ci/check-release-environment.mjs");
  const reviewer = {type: "User", reviewer: {login: "maintainer"}};
  assert.deepEqual(checkReleaseEnvironment({name: "release", protection_rules: [
    {type: "branch_policy"}, {type: "required_reviewers", reviewers: [reviewer]}]}), ["maintainer"]);
  for (const body of [null, {name: "other", protection_rules: []}, {name: "release"},
    {name: "release", protection_rules: []}, {name: "release", protection_rules: [{type: "wait_timer", wait_timer: 5}]},
    {name: "release", protection_rules: [{type: "required_reviewers", reviewers: []}]}]) {
    assert.throws(() => checkReleaseEnvironment(body), /release/);
  }
  const requests = [];
  const body = await fetchEnvironment("itcorner/MPDRussoundPi", "token", async (url, init) => {
    requests.push([url, init.headers.Authorization, init.redirect]);
    return {ok: true, json: async () => ({name: "release"})};
  });
  assert.equal(body.name, "release");
  assert.deepEqual(requests, [["https://api.github.com/repos/itcorner/MPDRussoundPi/environments/release",
    "Bearer token", "error"]]);
  await assert.rejects(fetchEnvironment("itcorner/MPDRussoundPi", null, async () => ({ok: false, status: 404})),
    /HTTP 404/);
  for (const repository of ["../x", "a/..", "a/b/c", "", undefined]) {
    await assert.rejects(fetchEnvironment(repository, null, async () => assert.fail("no request")), /Unexpected repository/);
  }
});

test("release journals are verified by their exact version and never by the dev alias", async () => {
  const {verifyPublishedImages} = await import("../tool/ci/verify-published-images.mjs");
  const sha = "a".repeat(40);
  const digest = `sha256:${"b".repeat(64)}`;
  const child = arch => `sha256:${(arch === "amd64" ? "c" : "d").repeat(64)}`;
  const journal = {phase: "complete", expected: {namespace: "itcornernet", sha, channel: "stable", version: "0.9.0",
    exact: "0.9.0", digests: {web: digest, ser2net: digest}}};
  const inspected = [];
  const run = (command, args) => {
    if (args[0] === "buildx") {
      inspected.push(args[3]);
      return args.includes("--raw") ? JSON.stringify({manifests: ["amd64", "arm64"].map(arch => ({
        platform: {os: "linux", architecture: arch}, digest: child(arch)}))}) : JSON.stringify({digest});
    }
    return "";
  };
  assert.deepEqual(verifyPublishedImages(journal, "itcornernet", sha, run, {verifyTags: true}).map(report => report.exact),
    ["0.9.0", "0.9.0"]);
  assert.ok(inspected.every(reference => !reference.endsWith(":dev")));
  for (const expected of [{...journal.expected, exact: "0.9.1"}, {...journal.expected, version: "0.9.0-rc.1"},
    {...journal.expected, channel: "beta"}]) {
    assert.throws(() => verifyPublishedImages({...journal, expected}, "itcornernet", sha, run), /completed matching/);
  }
  assert.throws(() => verifyPublishedImages({...journal, phase: "verified-web"}, "itcornernet", sha, run,
    {verifyTags: true}), /completed matching/, "no partial-journal check for releases");
});

test("recovery metadata allows only original failed develop push workflows in this repository", async () => {
  const {validateRecoveryRun} = await import("../tool/ci/recovery-source.mjs");
  const run = {repository: {full_name: "itcorner/MPDRussoundPi"}, event: "push", head_branch: "develop",
    path: ".github/workflows/images-build-only.yml", status: "completed", conclusion: "failure",
    head_sha: "a".repeat(40)};
  assert.equal(validateRecoveryRun(run, "itcorner/MPDRussoundPi"), run.head_sha);
  for (const patch of [{event: "pull_request"}, {head_branch: "main"}, {conclusion: "success"},
    {status: "in_progress"}, {path: "other.yml"}, {head_sha: "short"},
    {repository: {full_name: "other/repo"}}]) {
    assert.throws(() => validateRecoveryRun({...run, ...patch}, "itcorner/MPDRussoundPi"), /failed development/);
  }
});

test("only approved recovery selects explicit source; ordinary publication cannot override push SHA", () => {
  const directory = mkdtempSync(path.join(tmpdir(), "recovery-source-"));
  const env = {...process.env, DEVELOPMENT_PUBLICATION_ENABLED: "true",
    CHANNEL: "development", VERSION: "", DOCKERHUB_NAMESPACE: "itcornernet",
    DOCKERHUB_USERNAME: "itcornernet", DOCKERHUB_TOKEN: "test-placeholder",
    ARTIFACT_ROOT: directory, GITHUB_SHA: "invalid",
    PUBLICATION_SOURCE_SHA: "a".repeat(40)};
  const script = path.resolve(__dirname, "../tool/ci/image-publisher.mjs");
  try {
    const recover = spawnSync(process.execPath, [script], {encoding: "utf8", env: {
      ...env, MODE: "recover-development", GITHUB_EVENT_NAME: "workflow_dispatch",
      GITHUB_REF: "refs/heads/main", RECOVERY_APPROVED: "true",
    }});
    assert.notEqual(recover.status, 0);
    assert.match(recover.stderr, /ENOENT/);
    assert.doesNotMatch(recover.stderr, /Expected a full commit SHA/);
    const publish = spawnSync(process.execPath, [script], {encoding: "utf8", env: {
      ...env, MODE: "publish-development", GITHUB_EVENT_NAME: "push",
      GITHUB_REF: "refs/heads/develop", RECOVERY_APPROVED: "false",
    }});
    assert.notEqual(publish.status, 0);
    assert.match(publish.stderr, /Expected a full commit SHA/);
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});

test("published runtime verification pulls distinct platform child digests rather than reusing the index", async () => {
  const {verifyPublishedImages} = await import("../tool/ci/verify-published-images.mjs");
  const sha = "a".repeat(40);
  const root = `sha256:${"b".repeat(64)}`;
  const child = arch => `sha256:${(arch === "amd64" ? "c" : "d").repeat(64)}`;
  const journal = {phase: "complete", expected: {namespace: "itcornernet", sha, channel: "development",
    exact: `dev-${sha}`, digests: {web: root, ser2net: root}}};
  const pulls = [];
  const reports = verifyPublishedImages(journal, "itcornernet", sha, (command, args) => {
    if (args[0] === "buildx") return JSON.stringify({manifests: ["amd64", "arm64"].map(arch => ({
      platform: {os: "linux", architecture: arch}, digest: child(arch),
    }))});
    if (command === "docker") {
      assert.equal(args[0], "pull");
      assert.ok(args.at(-1).endsWith(child(args[2].slice("linux/".length))));
      assert.ok(!args.at(-1).endsWith(root));
      pulls.push(args.at(-1));
    } else assert.equal(command, "bash");
  });

  assert.equal(pulls.length, 4);
  assert.equal(reports.length, 2);
  assert.throws(() => verifyPublishedImages({...journal, phase: "prepared"}, "itcornernet", sha), /completed matching/);
});

test("no-write verification checks all four tags before accepting an incomplete journal", async () => {
  const {verifyPublishedImages} = await import("../tool/ci/verify-published-images.mjs");
  const sha = "a".repeat(40);
  const digest = `sha256:${"b".repeat(64)}`;
  const exact = `dev-${sha}`;
  const journal = {phase: "promoted-web-dev", expected: {namespace: "itcornernet", sha,
    channel: "development", exact, digests: {web: digest, ser2net: digest}}};
  const tags = ["web", "ser2net"].flatMap(image =>
    [exact, "dev"].map(tag => `itcornernet/mpdrussoundpi-${image}:${tag}`));
  let inspected = [];
  let smokeTests = 0;
  let mismatch = true;
  const run = (command, args) => {
    if (args[4] === "--format") {
      inspected.push(args[3]);
      return JSON.stringify({digest: mismatch && args[3] === tags[3] ? `sha256:${"c".repeat(64)}` : digest});
    }
    assert.deepEqual(inspected, tags);
    if (command === "bash") smokeTests++;
    if (args[0] === "buildx") return JSON.stringify({manifests: ["amd64", "arm64"].map(architecture => ({
      platform: {os: "linux", architecture}, digest,
    }))});
  };
  assert.throws(() => verifyPublishedImages(journal, "itcornernet", sha, run, {verifyTags: true}),
    /mpdrussoundpi-ser2net:dev: expected .* observed/);
  assert.equal(smokeTests, 0);
  inspected = [];
  mismatch = false;
  assert.equal(verifyPublishedImages(journal, "itcornernet", sha, run, {verifyTags: true}).length, 2);
  assert.equal(smokeTests, 4);
});

test("verification CLI uses explicit original source rather than the trusted workflow commit", () => {
  const directory = mkdtempSync(path.join(tmpdir(), "published-verification-"));
  const sha = "a".repeat(40);
  const root = `sha256:${"b".repeat(64)}`;
  const index = {manifests: ["amd64", "arm64"].map(arch => ({
    platform: {os: "linux", architecture: arch},
    digest: `sha256:${(arch === "amd64" ? "c" : "d").repeat(64)}`,
  }))};
  const journal = path.join(directory, "journal.json");
  try {
    writeFileSync(journal, JSON.stringify({phase: "complete", expected: {
      namespace: "itcornernet", sha, channel: "development", exact: `dev-${sha}`,
      digests: {web: root, ser2net: root},
    }}));
    writeFileSync(path.join(directory, "docker"),
      `#!${process.execPath}\nif (process.argv[2] === "buildx") console.log(${JSON.stringify(JSON.stringify(index))});\n`,
      {mode: 0o755});
    writeFileSync(path.join(directory, "bash"), `#!${process.execPath}\n`, {mode: 0o755});
    const env = {...process.env, PATH: directory, GITHUB_SHA: "e".repeat(40),
      PUBLICATION_SOURCE_SHA: sha, DOCKERHUB_NAMESPACE: "itcornernet",
      PUBLICATION_JOURNAL: journal};
    delete env.GITHUB_STEP_SUMMARY;
    const result = spawnSync(process.execPath,
      [path.resolve(__dirname, "../tool/ci/verify-published-images.mjs")], {encoding: "utf8", env});
    assert.equal(result.status, 0, result.stderr);
    delete env.PUBLICATION_SOURCE_SHA;
    const mismatch = spawnSync(process.execPath,
      [path.resolve(__dirname, "../tool/ci/verify-published-images.mjs")], {encoding: "utf8", env});
    assert.notEqual(mismatch.status, 0);
    assert.match(mismatch.stderr, /completed matching journal/);
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
});
