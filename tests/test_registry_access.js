const {test} = require("node:test");
const assert = require("node:assert/strict");
const {spawnSync} = require("node:child_process");
const path = require("node:path");
const access = import("../tool/ci/check-registry-access.mjs");
const token = claims => `header.${Buffer.from(JSON.stringify(claims)).toString("base64url")}.signature`;
const granted = (repository, actions = ["pull", "push"]) => ({
  access: [{type: "repository", name: repository, actions}],
});
const response = body => ({ok: true, json: async () => body});

test("credential check requests and verifies both repository scopes using GET only", async () => {
  const {checkRegistryAccess} = await access;
  let requests = 0;
  const reports = await checkRegistryAccess("itcornernet", "itcornernet", "test-placeholder", async (url, options) => {
    requests++;
    assert.equal(options.method, "GET");
    assert.equal(options.redirect, "error");
    if (url.startsWith("https://auth.docker.io/token")) {
      assert.match(options.headers.Authorization, /^Basic /);
      const scope = new URL(url).searchParams.get("scope");
      const repository = scope.slice("repository:".length, -":pull,push".length);
      return response({token: token(granted(repository))});
    }
    assert.equal(url, "https://registry-1.docker.io/v2/");
    assert.match(options.headers.Authorization, /^Bearer /);
    return {ok: true};
  });
  assert.equal(requests, 4);
  assert.deepEqual(reports.map(report => report.repository),
    ["itcornernet/mpdrussoundpi-web", "itcornernet/mpdrussoundpi-ser2net"]);
  assert.ok(reports.every(report => report.registryAuthentication === "accepted"));
  assert.ok(!JSON.stringify(reports).includes("test-placeholder"));
});

test("authentication errors and insufficient grants fail without exposing token payloads", async () => {
  const {checkRegistryAccess} = await access;
  const repository = "itcornernet/mpdrussoundpi-web";
  for (const body of [null, {}, {token: "private-response-body"},
    {token: "header.invalid.signature"}, {token: token({})}, {token: token({access: {}})},
    {token: token(granted("other/repository"))},
    {token: token(granted(repository, ["pull"]))},
    {token: token(granted(repository, ["pull", "push", "delete"]))}]) {
    await assert.rejects(checkRegistryAccess("itcornernet", "itcornernet", "test-placeholder",
      async () => response(body)), error => {
      assert.doesNotMatch(error.message, /private-response-body|test-placeholder/);
      return true;
    });
  }
  await assert.rejects(checkRegistryAccess("itcornernet", "itcornernet", "test-placeholder",
    async () => ({ok: false, status: 401})), /HTTP 401/);
  await assert.rejects(checkRegistryAccess("itcornernet", "itcornernet", "test-placeholder",
    async url => url.startsWith("https://auth.docker.io/")
      ? response({token: token(granted(repository))}) : {ok: false, status: 401}), /registry rejected/);
  await assert.rejects(checkRegistryAccess("itcornernet", "itcornernet", "test-placeholder",
    async () => ({ok: true, json: async () => {throw new Error("private-response-body");}})),
  /returned invalid JSON/);
});

test("missing configuration fails before requesting authentication", async () => {
  const {checkRegistryAccess} = await access;
  for (const args of [["", "itcornernet", "test-placeholder"],
    ["itcornernet", "", "test-placeholder"], ["itcornernet", "itcornernet", ""]]) {
    await assert.rejects(checkRegistryAccess(...args, async () => assert.fail("Unexpected request")), /required/);
  }
});

test("credential CLI accepts neither PRs nor manual dispatches outside main", () => {
  for (const [event, ref] of [["pull_request", "refs/heads/main"],
    ["workflow_dispatch", "refs/heads/develop"], ["push", "refs/heads/main"]]) {
    const result = spawnSync(process.execPath, [path.resolve(__dirname, "../tool/ci/check-registry-access.mjs")],
      {encoding: "utf8", env: {...process.env, GITHUB_EVENT_NAME: event, GITHUB_REF: ref}});
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /manual dispatch on main/);
    assert.equal(result.stdout, "");
  }
});
