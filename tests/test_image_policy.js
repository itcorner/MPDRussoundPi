const {test} = require("node:test");
const assert = require("node:assert/strict");
const {execFileSync, spawnSync} = require("node:child_process");
const {mkdtempSync, readFileSync, rmSync} = require("node:fs");
const {tmpdir} = require("node:os");
const path = require("node:path");
const policy = import("../tool/ci/image-policy.mjs");
const sha = "a".repeat(40);

test("development uses a write-once SHA tag and a development alias", async () => {
  const {imagePolicy} = await policy;
  assert.deepEqual(imagePolicy("development", "", sha), {exact: `dev-${sha}`, aliases: ["dev"]});
  assert.throws(() => imagePolicy("development", "", sha, [`dev-${sha}`]), /already exists/);
});

test("candidates never update stable aliases", async () => {
  const {imagePolicy} = await policy;
  assert.deepEqual(imagePolicy("candidate", "1.2.3-rc.1", sha), {exact: "1.2.3-rc.1", aliases: []});
});

test("stable aliases cannot move backward", async () => {
  const {imagePolicy} = await policy;
  assert.deepEqual(imagePolicy("stable", "1.2.3", sha), {exact: "1.2.3", aliases: ["1.2", "latest"]});
  assert.deepEqual(imagePolicy("stable", "1.2.3", sha, ["1.2.4"]), {exact: "1.2.3", aliases: []});
  assert.deepEqual(imagePolicy("stable", "1.2.3", sha, ["2.0.0"]), {exact: "1.2.3", aliases: ["1.2"]});
  assert.deepEqual(imagePolicy("stable", "1.2.3", sha, ["9.0.0-rc.1"]), {exact: "1.2.3", aliases: ["1.2", "latest"]});
  assert.throws(() => imagePolicy("stable", "1.2.3", sha, ["1.2.3"]), /already exists/);
});

test("invalid versions and channel combinations fail explicitly", async () => {
  const {imagePolicy} = await policy;
  for (const version of ["v1.2.3", "01.2.3", "1.2", "1.2.3+build", "1.2.3-rc.01", "1.2.3\n", "1.2.3-rc.1\n"]) {
    assert.throws(() => imagePolicy("stable", version, sha));
  }
  assert.throws(() => imagePolicy("stable", "1.2.3-rc.1", sha));
  assert.throws(() => imagePolicy("candidate", "1.2.3", sha));
  assert.throws(() => imagePolicy("other", "1.2.3", sha));
  assert.throws(() => imagePolicy("development", "", "short"));
  assert.throws(() => imagePolicy("development", "", `${sha}\n`), /full commit SHA/);
  assert.throws(() => imagePolicy("development", "1.2.3", sha), /version input/);
});

test("all exact image tags are write-once, including candidates", async () => {
  const {imagePolicy} = await policy;
  for (const [channel, version, exact] of [
    ["development", "", `dev-${sha}`],
    ["candidate", "1.2.3-rc.1", "1.2.3-rc.1"],
    ["stable", "1.2.3", "1.2.3"],
  ]) {
    assert.throws(() => imagePolicy(channel, version, sha, ["dev", "latest", exact]), /already exists/);
  }
});

test("stable alias decisions compare numeric versions and ignore non-stable tags", async () => {
  const {imagePolicy} = await policy;
  const cases = [
    ["1.2.10", ["1.2.9", "1.1.99"], ["1.2", "latest"]],
    ["1.2.9", ["1.2.10"], []],
    ["1.9.9", ["1.10.0"], ["1.9"]],
    ["9.9.9", ["10.0.0"], ["9.9"]],
    ["1.2.3", ["1.2.2", "dev", `dev-${sha}`, "latest", "1.2", "2.0.0-rc.1", "01.2.4", "2.0.0\n"], ["1.2", "latest"]],
    ["1.2.3", ["1.2.4", "2.0.0"], []],
    ["9007199254740993.0.0", ["9007199254740992.99.99"], ["9007199254740993.0", "latest"]],
  ];
  for (const [version, inventory, aliases] of cases) {
    assert.deepEqual(imagePolicy("stable", version, sha, inventory), {exact: version, aliases});
    assert.deepEqual(imagePolicy("stable", version, sha, [...inventory].reverse()), {exact: version, aliases});
  }
});

test("manual branch dispatch preserves build-only channel simulations", async () => {
  const {imageEventPolicy} = await policy;
  for (const ref of ["refs/heads/main", "refs/heads/develop", "refs/heads/feature/image-policy"]) {
    for (const [channel, version, sourceBranch] of [
      ["development", "", "develop"], ["candidate", "1.2.3-rc.1", "develop"], ["stable", "1.2.3", "main"],
    ]) {
      assert.deepEqual(imageEventPolicy({eventName: "workflow_dispatch", ref, mode: "build-only", channel, version}),
        {channel, version, sourceBranch});
    }
  }
  for (const eventName of ["push", "pull_request", "pull_request_target", "workflow_run"]) {
    assert.throws(() => imageEventPolicy({eventName, ref: "refs/heads/develop",
      mode: "build-only", channel: "development"}), /manual dispatch/);
  }
  assert.throws(() => imageEventPolicy({eventName: "workflow_dispatch", ref: "refs/tags/v1.2.3",
    mode: "build-only", channel: "stable", version: "1.2.3"}), /manual dispatch/);
  assert.throws(() => imageEventPolicy({eventName: "workflow_dispatch", ref: "refs/heads/develop\n",
    mode: "build-only", channel: "development"}), /manual dispatch/);
});

test("future publication policy derives channels and versions only from allowed push refs", async () => {
  const {imageEventPolicy} = await policy;
  for (const [ref, channel, version, sourceBranch] of [
    ["refs/heads/develop", "development", "", "develop"],
    ["refs/tags/v1.2.3-rc.1", "candidate", "1.2.3-rc.1", "develop"],
    ["refs/tags/v1.2.3", "stable", "1.2.3", "main"],
  ]) {
    assert.deepEqual(imageEventPolicy({eventName: "push", ref, mode: "publish"}), {channel, version, sourceBranch});
  }
  for (const ref of ["refs/heads/main", "refs/heads/feature/test", "refs/tags/1.2.3",
    "refs/tags/v01.2.3", "refs/tags/v1.2", "refs/tags/v1.2.3-rc.01",
    "refs/tags/v1.2.3-beta.1", "refs/tags/v1.2.3+build", "refs/tags/v1.2.3\n"]) {
    assert.throws(() => imageEventPolicy({eventName: "push", ref, mode: "publish"}));
  }
  for (const eventName of ["workflow_dispatch", "pull_request", "pull_request_target", "workflow_run"]) {
    assert.throws(() => imageEventPolicy({eventName, ref: "refs/heads/develop", mode: "publish"}), /only push/);
  }
  assert.throws(() => imageEventPolicy({eventName: "push", ref: "refs/heads/develop", mode: "unknown"}), /only push/);
  assert.throws(() => imageEventPolicy({eventName: "push", ref: "refs/tags/v1.2.3",
    mode: "publish", deleted: true}), /Deleted refs/);
  assert.throws(() => imageEventPolicy({eventName: "push", mode: "publish"}), /GitHub ref/);
  for (const inputs of [{channel: "stable"}, {version: "1.2.3"}]) {
    assert.throws(() => imageEventPolicy({eventName: "push", ref: "refs/heads/develop",
      mode: "publish", ...inputs}), /derived from/);
  }
});

function sourceFixture(t) {
  const cwd = mkdtempSync(path.join(tmpdir(), "mpdrussoundpi-image-policy-"));
  t.after(() => rmSync(cwd, {recursive: true, force: true}));
  const git = (...args) => execFileSync("git", args, {cwd, encoding: "utf8",
    env: {...process.env, GIT_AUTHOR_NAME: "Policy Test", GIT_AUTHOR_EMAIL: "policy@example.invalid",
      GIT_COMMITTER_NAME: "Policy Test", GIT_COMMITTER_EMAIL: "policy@example.invalid"}}).trim();
  git("init", "--quiet");
  git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Stable baseline");
  const main = git("rev-parse", "HEAD");
  git("update-ref", "refs/remotes/origin/main", main);
  git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Development tip");
  const develop = git("rev-parse", "HEAD");
  git("update-ref", "refs/remotes/origin/develop", develop);
  git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Unintegrated feature");
  const feature = git("rev-parse", "HEAD");
  return {cwd, git, main, develop, feature};
}

test("source ancestry uses the required branch and rejects unintegrated commits", async t => {
  const {validateImageSource} = await policy;
  const {cwd, main, develop, feature} = sourceFixture(t);
  assert.equal(validateImageSource("stable", main, {cwd}), "main");
  for (const channel of ["development", "candidate"]) {
    assert.equal(validateImageSource(channel, main, {cwd}), "develop");
    assert.equal(validateImageSource(channel, develop, {cwd}), "develop");
    assert.throws(() => validateImageSource(channel, feature, {cwd}), /not reachable from origin\/develop/);
  }
  assert.throws(() => validateImageSource("stable", develop, {cwd}), /not reachable from origin\/main/);
  assert.throws(() => validateImageSource("development", "short", {cwd}), /full commit SHA/);
  assert.throws(() => validateImageSource("unknown", main, {cwd}), /channel/);
});

test("dev alias promotion requires the current branch tip, even after the branch advances", async t => {
  const {validateImageSource} = await policy;
  const {cwd, git, main, develop, feature} = sourceFixture(t);
  assert.equal(validateImageSource("development", develop, {cwd, requireTip: true}), "develop");
  assert.throws(() => validateImageSource("development", main, {cwd, requireTip: true}), /current origin\/develop tip/);
  git("update-ref", "refs/remotes/origin/develop", feature);
  assert.throws(() => validateImageSource("development", develop, {cwd, requireTip: true}), /current origin\/develop tip/);
  assert.equal(validateImageSource("development", feature, {cwd, requireTip: true}), "develop");
  assert.throws(() => validateImageSource("stable", main, {cwd, requireTip: true}), /dev alias/);
});

test("missing Git refs and unknown source commits fail rather than bypass ancestry", async t => {
  const {validateImageSource} = await policy;
  const {cwd, git, main} = sourceFixture(t);
  assert.throws(() => validateImageSource("development", sha, {cwd}), /Git ancestry check failed/);
  git("update-ref", "-d", "refs/remotes/origin/main");
  assert.throws(() => validateImageSource("stable", main, {cwd}), /rev-parse/);
});

test("CLI checks manual event and ancestry, emits the exact tag, and cannot enable publication", async t => {
  const {cwd, main, develop} = sourceFixture(t);
  const output = path.join(cwd, "github-output");
  const run = overrides => spawnSync(process.execPath, [path.resolve(__dirname, "../tool/ci/image-policy.mjs")],
    {cwd, encoding: "utf8", env: {...process.env, MODE: "build-only", CHANNEL: "development", VERSION: "",
      GITHUB_SHA: develop, GITHUB_OUTPUT: output, GITHUB_EVENT_NAME: "workflow_dispatch",
      GITHUB_REF: "refs/heads/develop", ...overrides}});
  for (const [channel, version, source, exact] of [
    ["development", "", develop, `dev-${develop}`],
    ["candidate", "1.2.3-rc.1", develop, "1.2.3-rc.1"],
    ["stable", "1.2.3", main, "1.2.3"],
  ]) {
    const result = run({CHANNEL: channel, VERSION: version, GITHUB_SHA: source});
    assert.equal(result.status, 0, result.stderr);
    assert.equal(JSON.parse(result.stdout).exact, exact);
  }
  const expectedOutput = readFileSync(output, "utf8");
  assert.equal(expectedOutput, `version=dev-${develop}\nversion=1.2.3-rc.1\nversion=1.2.3\n`);
  for (const overrides of [{MODE: "publish", GITHUB_EVENT_NAME: "push"},
    {MODE: ""}, {GITHUB_EVENT_NAME: "push"}, {CHANNEL: "stable", VERSION: "1.2.3"}]) {
    const result = run(overrides);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /Publication is disabled|manual dispatch|not reachable/);
    assert.equal(readFileSync(output, "utf8"), expectedOutput);
  }
});
