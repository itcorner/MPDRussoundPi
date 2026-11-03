const {test} = require("node:test");
const assert = require("node:assert/strict");
const {spawnSync} = require("node:child_process");
const path = require("node:path");
const inventory = import("../tool/ci/registry-inventory.mjs");
const sha = "a".repeat(40);
const endpoint = "https://hub.docker.com/v2/namespaces/itcornernet/repositories/mpdrussoundpi-web/tags";
const page = (names, count = names.length, next = null) => ({
  count, next, results: names.map(name => ({name})),
});
const response = body => ({ok: true, json: async () => body});

test("inventory follows every page using anonymous GET requests and returns sorted tags", async () => {
  const {dockerHubTags} = await inventory;
  const urls = [];
  const tags = await dockerHubTags("itcornernet", "mpdrussoundpi-web", async (url, options) => {
    urls.push(url);
    assert.equal(options.method, "GET");
    assert.equal(options.redirect, "error");
    assert.deepEqual(options.headers, {Accept: "application/json"});
    assert.ok(options.signal instanceof AbortSignal);
    return response(urls.length === 1
      ? page(["latest", "1.2.4"], 3, `${endpoint}?page=2&page_size=100`)
      : page(["1.2.3"], 3));
  });
  assert.deepEqual(tags, ["1.2.3", "1.2.4", "latest"]);
  assert.deepEqual(urls, [`${endpoint}?page_size=100`, `${endpoint}?page=2&page_size=100`]);
});

test("an explicitly empty successful inventory is allowed", async () => {
  const {dockerHubTags} = await inventory;
  assert.deepEqual(await dockerHubTags("itcornernet", "mpdrussoundpi-web", async () => response(page([]))), []);
});

test("HTTP, transport and invalid JSON errors never become an empty inventory", async () => {
  const {dockerHubTags} = await inventory;
  for (const status of [401, 403, 404, 429, 500]) {
    await assert.rejects(dockerHubTags("itcornernet", "mpdrussoundpi-web", async () => ({ok: false, status})),
      new RegExp(`HTTP ${status}`));
  }
  for (const message of ["Network failed", "Timeout", "Invalid JSON"]) {
    await assert.rejects(dockerHubTags("itcornernet", "mpdrussoundpi-web", async () => {
      if (message === "Invalid JSON") return {ok: true, json: async () => {throw new Error(message);}};
      throw new Error(message);
    }), new RegExp(message));
  }
});

test("invalid page shapes, names and incomplete counts fail explicitly", async () => {
  const {dockerHubTags} = await inventory;
  for (const body of [null, {}, {...page([]), count: -1}, {...page([]), count: 0.5},
    {...page([]), next: ""}, {...page([]), results: {}}, {...page([]), count: "0"},
    page(["1.2.3"], 2), page(["1.2.3"], 0), page(["1.2.3", "1.2.3"]),
    page(["bad tag"]), page(["tag\n"]), {...page([]), results: [null]},
    page([], 1, "?page=2"), page(["1.2.3"], 1, "?page=2")]) {
    await assert.rejects(dockerHubTags("itcornernet", "mpdrussoundpi-web", async () => response(body)),
      /Malformed|Incomplete|Inconsistent|duplicate/);
  }
});

test("pagination rejects count changes, duplicate tags and loops", async () => {
  const {dockerHubTags} = await inventory;
  for (const [first, second, error] of [
    [page(["1.2.3"], 2, "?page=2"), page(["1.2.4"], 3), /changed/],
    [page(["1.2.3"], 2, "?page=2"), page(["1.2.3"], 2), /duplicate/],
    [page(["1.2.3"], 3, "?page=2"), page(["1.2.4"], 3, "?page_size=100"), /repeated/],
  ]) {
    let calls = 0;
    await assert.rejects(dockerHubTags("itcornernet", "mpdrussoundpi-web", async () =>
      response(++calls === 1 ? first : second)), error);
    assert.equal(calls, 2);
  }
});

test("inventory CLI rejects publication and non-manual events before any registry request", () => {
  for (const overrides of [{MODE: "publish"}, {GITHUB_EVENT_NAME: "push"},
    {GITHUB_REF: "refs/tags/v1.2.3"}]) {
    const result = spawnSync(process.execPath, [path.resolve(__dirname, "../tool/ci/registry-inventory.mjs")],
      {encoding: "utf8", env: {...process.env, MODE: "build-only", CHANNEL: "development", VERSION: "",
        GITHUB_EVENT_NAME: "workflow_dispatch", GITHUB_REF: "refs/heads/develop",
        DOCKERHUB_NAMESPACE: "itcornernet", ...overrides}});
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Publication is disabled|manual dispatch/);
    assert.equal(result.stdout, "");
  }
});

test("pagination cannot change host, repository, scheme or embed credentials", async () => {
  const {dockerHubTags} = await inventory;
  for (const next of ["https://example.invalid/tags", endpoint.replace("web", "ser2net"),
    endpoint.replace("https:", "http:"), endpoint.replace("https://", "https://user:pass@"), `${endpoint}#fragment`]) {
    let calls = 0;
    await assert.rejects(dockerHubTags("itcornernet", "mpdrussoundpi-web", async () => {
      calls++;
      return response(page(["1.2.3"], 2, next));
    }), /expected repository endpoint/);
    assert.equal(calls, 1);
  }
});

test("invalid repository components fail before making requests", async () => {
  const {dockerHubTags} = await inventory;
  for (const component of ["", undefined, "../other", "name/path", "name\n", "Name"]) {
    const fetchPage = async () => assert.fail("Unexpected request");
    await assert.rejects(dockerHubTags(component, "mpdrussoundpi-web", fetchPage), /namespace and repository/);
    await assert.rejects(dockerHubTags("itcornernet", component, fetchPage), /namespace and repository/);
  }
});

test("both image inventories drive independent write-once and alias decisions", async () => {
  const {registryPolicy} = await inventory;
  const reports = await registryPolicy("itcornernet", "stable", "1.2.3", sha, async url =>
    response(page(url.includes("mpdrussoundpi-web") ? ["1.2.4"] : ["2.0.0"])));
  assert.deepEqual(reports, [
    {repository: "itcornernet/mpdrussoundpi-web", tags: ["1.2.4"], exact: "1.2.3", aliases: []},
    {repository: "itcornernet/mpdrussoundpi-ser2net", tags: ["2.0.0"], exact: "1.2.3", aliases: ["1.2"]},
  ]);
  for (const collisionImage of ["web", "ser2net"]) {
    await assert.rejects(registryPolicy("itcornernet", "stable", "1.2.3", sha, async url =>
      response(page(url.includes(`mpdrussoundpi-${collisionImage}`) ? ["1.2.3"] : []))), /already exists/);
  }
  await assert.rejects(registryPolicy("itcornernet", "stable", "1.2.3", sha, async url =>
    url.includes("ser2net") ? {ok: false, status: 404} : response(page([]))), /HTTP 404/);
  await assert.rejects(registryPolicy("itcornernet", "stable", "invalid", sha,
    async () => assert.fail("Unexpected request")), /Expected X.Y.Z/);
});

test("later inventory pages affect alias policy and cannot hide collisions or HTTP errors", async () => {
  const {registryPolicy} = await inventory;
  const paginated = later => async url => response(new URL(url).searchParams.has("page")
    ? page([later], 2)
    : page(["dev"], 2, "?page=2&page_size=100"));
  const reports = await registryPolicy("itcornernet", "stable", "1.2.3", sha, paginated("1.2.4"));
  assert.ok(reports.every(report => report.aliases.length === 0));
  for (const [channel, version, exact] of [
    ["development", "", `dev-${sha}`], ["candidate", "1.2.3-rc.1", "1.2.3-rc.1"],
    ["stable", "1.2.3", "1.2.3"],
  ]) {
    await assert.rejects(registryPolicy("itcornernet", channel, version, sha, paginated(exact)), /already exists/);
  }
  await assert.rejects(registryPolicy("itcornernet", "stable", "1.2.3", sha, async url =>
    new URL(url).searchParams.has("page")
      ? {ok: false, status: 429}
      : response(page(["dev"], 2, "?page=2"))), /HTTP 429/);
});
