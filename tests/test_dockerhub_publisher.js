const {test} = require("node:test");
const assert = require("node:assert/strict");
const {spawnSync} = require("node:child_process");
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
