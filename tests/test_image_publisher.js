const {test} = require("node:test");
const assert = require("node:assert/strict");
const {createHash} = require("node:crypto");
const {mkdtempSync, readFileSync, rmSync, mkdirSync, writeFileSync} = require("node:fs");
const {tmpdir} = require("node:os");
const path = require("node:path");
const {spawnSync, execFileSync} = require("node:child_process");
const publisher = import("../tool/ci/image-publisher.mjs");
const sha = "a".repeat(40);
const endpoint = process.env.REGISTRY_TEST_URL;

function readers(revision, version) {
  return Object.fromEntries(["amd64", "arm64"].map(arch => {
    const files = new Map();
    const put = (value, mediaType) => {
      const bytes = Buffer.from(JSON.stringify(value));
      const digest = `sha256:${createHash("sha256").update(bytes).digest("hex")}`;
      files.set(`blobs/sha256/${digest.slice(7)}`, bytes);
      return {digest, size: bytes.length, mediaType};
    };
    const config = put({architecture: arch, os: "linux", rootfs: {type: "layers", diff_ids: []},
      config: {Labels: {
        "org.opencontainers.image.source": "https://github.com/itcorner/MPDRussoundPi",
        "org.opencontainers.image.revision": revision, "org.opencontainers.image.version": version,
      }}}, "application/vnd.oci.image.config.v1+json");
    const runtime = put({schemaVersion: 2, mediaType: "application/vnd.oci.image.manifest.v1+json",
      config, layers: []}, "application/vnd.oci.image.manifest.v1+json");
    const attestationConfig = put({architecture: "unknown", os: "unknown",
      rootfs: {type: "layers", diff_ids: []}, config: {}}, "application/vnd.oci.image.config.v1+json");
    const layers = ["https://spdx.dev/Document", "https://slsa.dev/provenance/v0.2"].map(predicateType =>
      put({predicateType, subject: [{digest: {sha256: runtime.digest.slice(7)}}]},
        "application/vnd.in-toto+json"));
    const attestation = put({schemaVersion: 2, mediaType: "application/vnd.oci.image.manifest.v1+json",
      config: attestationConfig, layers}, "application/vnd.oci.image.manifest.v1+json");
    const index = {schemaVersion: 2, mediaType: "application/vnd.oci.image.index.v1+json", manifests: [
      {...runtime, platform: {os: "linux", architecture: arch}},
      {...attestation, platform: {os: "unknown", architecture: "unknown"}, annotations: {
        "vnd.docker.reference.type": "attestation-manifest", "vnd.docker.reference.digest": runtime.digest,
      }},
    ]};
    const root = put(index, index.mediaType);
    files.set("index.json", Buffer.from(JSON.stringify({schemaVersion: 2, manifests: [root]})));
    const read = name => {
      if (!files.has(name)) throw new Error(`Missing fixture blob: ${name}`);
      return files.get(name);
    };
    read.files = files;
    return [arch, read];
  }));
}

async function fixture(t, channel = "development", version = "", revision = sha) {
  const {LocalRegistry, assembleImage, publishImages} = await publisher;
  const {imagePolicy} = await import("../tool/ci/image-policy.mjs");
  const exact = imagePolicy(channel, version, revision).exact;
  const directory = mkdtempSync(path.join(tmpdir(), "mpdrussoundpi-publisher-"));
  t.after(() => rmSync(directory, {recursive: true, force: true}));
  const namespace = `test_${createHash("sha256").update(directory).digest("hex").slice(0, 16)}`;
  const registry = new LocalRegistry(endpoint);
  const image = assembleImage(readers(revision, exact), revision, exact);
  const options = {registry, namespace, channel, version, sha: revision, images: {web: image, ser2net: image},
    journalPath: path.join(directory, "journal.json"), checkSource: async () => {}};
  return {registry, image, options, publishImages, exact};
}

test("write transport refuses Docker Hub and non-loopback endpoints", async () => {
  const {LocalRegistry} = await publisher;
  for (const url of ["https://registry-1.docker.io", "http://example.com",
    "http://127.0.0.1/path", "http://user:placeholder@localhost:5000"]) {
    assert.throws(() => new LocalRegistry(url), /disposable local registry/);
  }
});

test("publisher CLI refuses production writes even when publish mode is requested", () => {
  const result = spawnSync(process.execPath, [path.resolve(__dirname, "../tool/ci/image-publisher.mjs")],
    {encoding: "utf8", env: {...process.env, MODE: "publish", LOCAL_REGISTRY_URL: "https://registry-1.docker.io"}});
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Docker Hub publication is disabled/);
});

test("OCI assembly preserves both runtimes and attestations without rebuilding", async () => {
  const {assembleImage} = await publisher;
  const image = assembleImage(readers(sha, "1.2.3"), sha, "1.2.3");
  const index = JSON.parse(image.bytes);
  assert.equal(index.manifests.length, 4);
  assert.equal(index.manifests.filter(entry => entry.annotations).length, 2);
  assert.equal(image.digest, `sha256:${createHash("sha256").update(image.bytes).digest("hex")}`);
  assert.throws(() => assembleImage(readers(sha, "1.2.3"), sha, "1.2.4"), /labels/);
  const invalid = readers(sha, "1.2.3");
  const original = invalid.amd64;
  invalid.amd64 = name => name === "index.json" ? original(name) : Buffer.from("{}");
  assert.throws(() => assembleImage(invalid, sha, "1.2.3"), /checksum|size/);
});

test("local publication verifies both exact indices and blobs before promoting dev", {skip: !endpoint}, async t => {
  const {registry, options, publishImages, exact, image} = await fixture(t);
  const writes = [];
  const original = registry.put.bind(registry);
  registry.put = async (repository, reference, ...args) => {
    writes.push(reference);
    if (reference === "dev") {
      for (const name of ["web", "ser2net"]) {
        assert.equal((await registry.manifest(`${options.namespace}/mpdrussoundpi-${name}`, exact)).digest, image.digest);
      }
    }
    return original(repository, reference, ...args);
  };
  await publishImages(options);
  assert.equal(JSON.parse(readFileSync(options.journalPath)).phase, "complete");
  assert.equal(writes.filter(ref => ref === "dev").length, 2);
  for (const name of ["web", "ser2net"]) {
    const repository = `${options.namespace}/mpdrussoundpi-${name}`;
    await registry.verify(repository, image, "dev");
    assert.deepEqual((await registry.tags(repository)).sort(), ["dev", exact].sort());
  }
});

test("exact collision in either repository fails before any upload", {skip: !endpoint}, async t => {
  const {registry, options, publishImages, exact, image} = await fixture(t);
  const repository = `${options.namespace}/mpdrussoundpi-ser2net`;
  await registry.upload(repository, image);
  await registry.put(repository, exact, image.bytes);
  registry.upload = async () => assert.fail("Unexpected upload after collision");
  await assert.rejects(publishImages(options), /already exists/);
  assert.equal(await registry.manifest(`${options.namespace}/mpdrussoundpi-web`, exact), null);
});

test("partial failure requires approved matching-journal recovery without overwriting exact tags", {skip: !endpoint}, async t => {
  const {registry, options, publishImages, exact} = await fixture(t);
  const upload = registry.upload.bind(registry);
  registry.upload = async (repository, image) => {
    if (repository.endsWith("ser2net")) throw new Error("Injected upload failure");
    return upload(repository, image);
  };
  await assert.rejects(publishImages(options), /Injected upload failure/);
  assert.equal(await registry.manifest(`${options.namespace}/mpdrussoundpi-web`, "dev"), null);
  const existing = await registry.manifest(`${options.namespace}/mpdrussoundpi-web`, exact);
  registry.upload = upload;
  await assert.rejects(publishImages(options), /EEXIST/);
  const put = registry.put.bind(registry);
  registry.put = async (repository, reference, ...args) => {
    assert.ok(!(repository.endsWith("web") && reference === exact), "Recovery overwrote exact tag");
    return put(repository, reference, ...args);
  };
  await publishImages({...options, recoveryApproved: true});
  assert.equal((await registry.manifest(`${options.namespace}/mpdrussoundpi-web`, exact)).digest, existing.digest);
});

test("recovery refuses mismatched expected evidence", {skip: !endpoint}, async t => {
  const {options, publishImages} = await fixture(t);
  await publishImages(options);
  await assert.rejects(publishImages({...options, namespace: `${options.namespace}_other`, recoveryApproved: true}),
    /journal does not match/);
});

test("stale development tip stops aliases while preserving exact tags", {skip: !endpoint}, async t => {
  const {registry, options, publishImages, exact} = await fixture(t);
  options.checkSource = async requireTip => {
    if (requireTip) throw new Error("Development tip advanced");
  };
  await assert.rejects(publishImages(options), /tip advanced/);
  for (const image of ["web", "ser2net"]) {
    assert.ok(await registry.manifest(`${options.namespace}/mpdrussoundpi-${image}`, exact));
    assert.equal(await registry.manifest(`${options.namespace}/mpdrussoundpi-${image}`, "dev"), null);
  }
});

test("older stable publication cannot regress latest or minor aliases", {skip: !endpoint}, async t => {
  const {registry, options, publishImages} = await fixture(t, "stable", "1.2.4");
  await publishImages(options);
  const {assembleImage} = await publisher;
  const older = assembleImage(readers(sha, "1.2.3"), sha, "1.2.3");
  await publishImages({...options, version: "1.2.3", images: {web: older, ser2net: older},
    journalPath: `${options.journalPath}.older`});
  for (const name of ["web", "ser2net"]) {
    const repository = `${options.namespace}/mpdrussoundpi-${name}`;
    assert.equal((await registry.manifest(repository, "latest")).digest, options.images[name].digest);
    assert.equal((await registry.manifest(repository, "1.2")).digest, options.images[name].digest);
  }
});

test("concurrent publications for the same registry namespace are rejected", {skip: !endpoint}, async t => {
  const {options, publishImages} = await fixture(t);
  let release;
  let started;
  const entered = new Promise(resolve => {started = resolve;});
  const wait = new Promise(resolve => {release = resolve;});
  options.checkSource = async () => {started(); await wait;};
  const running = publishImages(options);
  await entered;
  try {
    await assert.rejects(publishImages({...options, journalPath: `${options.journalPath}.other`}), /EEXIST/);
  } finally {
    release();
    await running;
  }
});

test("candidate publication never promotes stable or dev aliases", {skip: !endpoint}, async t => {
  const {registry, options, publishImages, exact} = await fixture(t, "candidate", "1.2.3-rc.1");
  await publishImages(options);
  for (const image of ["web", "ser2net"]) {
    assert.deepEqual(await registry.tags(`${options.namespace}/mpdrussoundpi-${image}`), [exact]);
  }
});

test("recovery rejects registry exact-tag digest mismatches", {skip: !endpoint}, async t => {
  const {registry, options, publishImages, exact} = await fixture(t);
  await publishImages(options);
  const {assembleImage} = await publisher;
  const changed = assembleImage(readers("c".repeat(40), exact), "c".repeat(40), exact);
  const repository = `${options.namespace}/mpdrussoundpi-web`;
  await registry.upload(repository, changed);
  await registry.put(repository, exact, changed.bytes);
  await assert.rejects(publishImages({...options, recoveryApproved: true}), /digest mismatch/);
});

test("local CLI publishes checksummed tested archives and refreshes real Git ancestry", {skip: !endpoint}, async t => {
  const {verifyArchive} = await import("../tool/ci/verify-image-archive.mjs");
  const {LocalRegistry} = await publisher;
  const directory = mkdtempSync(path.join(tmpdir(), "mpdrussoundpi-publisher-cli-"));
  t.after(() => rmSync(directory, {recursive: true, force: true}));
  const git = (...args) => execFileSync("git", args, {cwd: directory, encoding: "utf8",
    env: {...process.env, GIT_AUTHOR_NAME: "Policy Test", GIT_AUTHOR_EMAIL: "policy@example.invalid",
      GIT_COMMITTER_NAME: "Policy Test", GIT_COMMITTER_EMAIL: "policy@example.invalid"}}).trim();
  git("init", "--quiet");
  git("-c", "commit.gpgsign=false", "commit", "--quiet", "--allow-empty", "-m", "Test source");
  const revision = git("rev-parse", "HEAD");
  git("update-ref", "refs/heads/develop", revision);
  git("remote", "add", "origin", directory);
  const version = `dev-${revision}`;
  const root = path.join(directory, "prepared");
  const input = readers(revision, version);
  for (const image of ["web", "ser2net"]) {
    for (const arch of ["amd64", "arm64"]) {
      const target = path.join(root, `prepared-${image}-${arch}`);
      const unpacked = path.join(target, "oci");
      mkdirSync(path.join(unpacked, "blobs/sha256"), {recursive: true});
      for (const [name, bytes] of input[arch].files) writeFileSync(path.join(unpacked, name), bytes);
      const archive = path.join(target, "image.tar");
      execFileSync("tar", ["-cf", archive, "-C", unpacked, "index.json", "blobs"]);
      const hash = createHash("sha256").update(readFileSync(archive)).digest("hex");
      writeFileSync(`${archive}.sha256`, `${hash}  image.tar\n`);
      writeFileSync(`${archive}.metadata.json`, JSON.stringify(verifyArchive(input[arch], arch, revision, version)));
    }
  }
  const namespace = `cli_${createHash("sha256").update(directory).digest("hex").slice(0, 16)}`;
  const result = spawnSync(process.execPath, [path.resolve(__dirname, "../tool/ci/image-publisher.mjs")],
    {cwd: directory, encoding: "utf8", env: {...process.env, MODE: "local-test", CHANNEL: "development",
      VERSION: "", GITHUB_SHA: revision, DOCKERHUB_NAMESPACE: namespace, LOCAL_REGISTRY_URL: endpoint,
      ARTIFACT_ROOT: root, PUBLICATION_JOURNAL: path.join(directory, "journal.json"), RECOVERY_APPROVED: "false"}});
  assert.equal(result.status, 0, result.stderr);
  const registry = new LocalRegistry(endpoint);
  const output = JSON.parse(result.stdout);
  for (const image of ["web", "ser2net"]) {
    const exact = await registry.manifest(`${namespace}/mpdrussoundpi-${image}`, version);
    assert.equal(exact.digest, output.digests[image]);
    assert.equal((await registry.manifest(`${namespace}/mpdrussoundpi-${image}`, "dev")).digest, exact.digest);
  }
});
