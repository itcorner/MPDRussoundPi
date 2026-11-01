const {test} = require("node:test");
const assert = require("node:assert/strict");
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
  for (const version of ["v1.2.3", "01.2.3", "1.2", "1.2.3+build", "1.2.3-rc.01"]) {
    assert.throws(() => imagePolicy("stable", version, sha));
  }
  assert.throws(() => imagePolicy("stable", "1.2.3-rc.1", sha));
  assert.throws(() => imagePolicy("candidate", "1.2.3", sha));
  assert.throws(() => imagePolicy("other", "1.2.3", sha));
  assert.throws(() => imagePolicy("development", "", "short"));
});
