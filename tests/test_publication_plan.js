const {test} = require("node:test");
const assert = require("node:assert/strict");
const {spawnSync, execFileSync} = require("node:child_process");
const {createHash} = require("node:crypto");
const {mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync} = require("node:fs");
const {tmpdir} = require("node:os");
const path = require("node:path");
const planning = import("../tool/ci/publication-plan.mjs");
const sha = "a".repeat(40);
const digest = `sha256:${"b".repeat(64)}`;
const artifacts = () => ["web", "ser2net"].flatMap(image =>
  ["amd64", "arm64"].map(arch => ({image, platform: `linux/${arch}`, digest, revision: sha, version: "1.2.3"})));
const repositories = () => ["web", "ser2net"].map(image => ({
  repository: `itcornernet/mpdrussoundpi-${image}`, tags: [], exact: "1.2.3", aliases: ["1.2", "latest"],
}));

test("publication dry run requires complete platforms and moves aliases only after exact tags", async () => {
  const {buildPublicationPlan} = await planning;
  const plan = buildPublicationPlan("stable", "1.2.3", sha, artifacts(), repositories());
  assert.equal(plan.mode, "dry-run");
  assert.match(plan.order.at(-1), /aliases last/);
  assert.match(plan.recovery, /Never overwrite/);
});

test("planning rejects missing, duplicate, mismatched or stale artifacts and inventory decisions", async () => {
  const {buildPublicationPlan} = await planning;
  assert.throws(() => buildPublicationPlan("stable", "1.2.3", sha, artifacts().slice(1), repositories()), /all four/);
  for (const patch of [{revision: "c".repeat(40)}, {version: "1.2.2"}, {digest: "invalid"},
    {platform: "linux/arm64"}]) {
    const entries = artifacts();
    Object.assign(entries[0], patch);
    assert.throws(() => buildPublicationPlan("stable", "1.2.3", sha, entries, repositories()), /Platform artifacts/);
  }
  for (const patch of [{tags: ["1.2.3"]}, {tags: ["2.0.0"]}, {exact: "wrong"},
    {repository: "itcornernet/other"}]) {
    const reports = repositories();
    Object.assign(reports[0], patch);
    assert.throws(() => buildPublicationPlan("stable", "1.2.3", sha, artifacts(), reports),
      /already exists|do not match|Missing image/);
  }
});

test("recovery refuses overwrite, missing digest evidence and unapproved promotion", async () => {
  const {recoveryGate} = await planning;
  assert.throws(() => recoveryGate(digest, undefined, true), /manifest digests/);
  assert.throws(() => recoveryGate(digest, `sha256:${"c".repeat(64)}`, true), /never overwrite/);
  assert.throws(() => recoveryGate(digest, digest), /explicit approval/);
  assert.throws(() => recoveryGate(digest, digest, "true"), /explicit approval/);
  assert.deepEqual(recoveryGate(digest, digest, true), {reuseExact: true, overwriteExact: false});
});

test("planner CLI verifies four OCI archives, refreshes Git and uses fresh inventory without writes", t => {
  const cwd = mkdtempSync(path.join(tmpdir(), "mpdrussoundpi-publication-plan-"));
  t.after(() => rmSync(cwd, {recursive: true, force: true}));
  const git = (...args) => execFileSync("git", args, {cwd, encoding: "utf8", env: {
    ...process.env, GIT_AUTHOR_NAME: "Policy Test", GIT_AUTHOR_EMAIL: "policy@example.invalid",
    GIT_COMMITTER_NAME: "Policy Test", GIT_COMMITTER_EMAIL: "policy@example.invalid",
  }}).trim();
  git("init", "--quiet");
  git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Development baseline");
  const revision = git("rev-parse", "HEAD");
  git("update-ref", "refs/heads/develop", revision);
  git("update-ref", "refs/remotes/origin/develop", revision);
  git("remote", "add", "origin", cwd);
  const version = `dev-${revision}`;
  const root = path.join(cwd, "prepared");
  for (const image of ["web", "ser2net"]) {
    for (const arch of ["amd64", "arm64"]) {
      const directory = path.join(root, `prepared-${image}-${arch}`);
      const oci = path.join(directory, "oci");
      mkdirSync(path.join(oci, "blobs/sha256"), {recursive: true});
      const put = value => {
        const bytes = Buffer.from(JSON.stringify(value));
        const hash = createHash("sha256").update(bytes).digest("hex");
        writeFileSync(path.join(oci, "blobs/sha256", hash), bytes);
        return `sha256:${hash}`;
      };
      const config = put({architecture: arch, os: "linux", config: {Labels: {
        "org.opencontainers.image.source": "https://github.com/itcorner/MPDRussoundPi",
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.version": version,
      }}});
      const runtime = put({config: {digest: config}});
      const layers = ["https://spdx.dev/Document", "https://slsa.dev/provenance/v0.2"].map(predicateType => ({
        digest: put({predicateType, subject: [{digest: {sha256: runtime.slice(7)}}]}),
      }));
      const attestation = put({layers});
      writeFileSync(path.join(oci, "index.json"), JSON.stringify({manifests: [
        {digest: runtime, platform: {os: "linux", architecture: arch}},
        {digest: attestation, annotations: {
          "vnd.docker.reference.type": "attestation-manifest", "vnd.docker.reference.digest": runtime,
        }},
      ]}));
      execFileSync("tar", ["-cf", path.join(directory, "image.tar"), "-C", oci, "index.json", "blobs"]);
      const archiveHash = createHash("sha256").update(readFileSync(path.join(directory, "image.tar"))).digest("hex");
      writeFileSync(path.join(directory, "image.tar.sha256"), `${archiveHash}  /original-runner/image.tar\n`);
      writeFileSync(path.join(directory, "image.tar.metadata.json"), JSON.stringify({digest: runtime}));
    }
  }
  const mock = path.join(cwd, "mock-fetch.mjs");
  writeFileSync(mock, `
globalThis.fetch = async (url, options) => {
  if (options.method !== "GET" || !url.startsWith("https://hub.docker.com/v2/namespaces/itcornernet/")) {
    throw new Error("Unexpected registry operation");
  }
  return {ok: true, json: async () => ({count: 0, next: null, results: []})};
};
`);
  const summary = path.join(cwd, "summary");
  const run = () => spawnSync(process.execPath, ["--import", mock,
    path.resolve(__dirname, "../tool/ci/publication-plan.mjs")], {cwd, encoding: "utf8",
    env: {...process.env, MODE: "build-only", CHANNEL: "development", VERSION: "",
      GITHUB_SHA: revision, GITHUB_EVENT_NAME: "workflow_dispatch", DOCKERHUB_NAMESPACE: "itcornernet",
      ARTIFACT_ROOT: root, GITHUB_STEP_SUMMARY: summary}});
  const success = run();
  assert.equal(success.status, 0, success.stderr);
  assert.equal(JSON.parse(success.stdout).artifacts.length, 4);
  assert.match(readFileSync(summary, "utf8"), /No registry writes/);
  writeFileSync(path.join(root, "prepared-web-amd64/image.tar.metadata.json"), JSON.stringify({digest}));
  const mismatch = run();
  assert.notEqual(mismatch.status, 0);
  assert.match(mismatch.stderr, /metadata digest mismatch/);
  writeFileSync(path.join(root, "prepared-web-amd64/image.tar.sha256"), `${"0".repeat(64)}  image.tar\n`);
  const corrupted = run();
  assert.notEqual(corrupted.status, 0);
  assert.match(corrupted.stderr, /archive checksum mismatch/);
});

test("publication planner CLI cannot activate writes", () => {
  const result = spawnSync(process.execPath, [path.resolve(__dirname, "../tool/ci/publication-plan.mjs")],
    {encoding: "utf8", env: {...process.env, MODE: "publish", GITHUB_EVENT_NAME: "workflow_dispatch"}});
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Only manual build-only/);
});
