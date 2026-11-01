const {test} = require("node:test");
const assert = require("node:assert/strict");
const {createHash} = require("node:crypto");

test("OCI archive verification binds platform, labels, SBOM and provenance to runtime", async () => {
  const {verifyArchive} = await import("../tool/ci/verify-image-archive.mjs");
  const files = new Map();
  const put = value => {
    const data = Buffer.from(JSON.stringify(value));
    const hash = createHash("sha256").update(data).digest("hex");
    files.set(`blobs/sha256/${hash}`, data);
    return `sha256:${hash}`;
  };
  const revision = "a".repeat(40);
  const config = put({architecture: "arm64", os: "linux", config: {Labels: {
    "org.opencontainers.image.source": "https://github.com/itcorner/MPDRussoundPi",
    "org.opencontainers.image.revision": revision,
    "org.opencontainers.image.version": "1.0.0",
  }}});
  const runtime = put({config: {digest: config}});
  const layers = ["https://spdx.dev/Document", "https://slsa.dev/provenance/v0.2"].map(predicateType => ({
    digest: put({predicateType, subject: [{digest: {sha256: runtime.slice(7)}}]}),
  }));
  const attestation = put({layers});
  const index = {manifests: [
    {digest: runtime, platform: {os: "linux", architecture: "arm64"}},
    {digest: attestation, annotations: {
      "vnd.docker.reference.type": "attestation-manifest",
      "vnd.docker.reference.digest": runtime,
    }},
  ]};
  files.set("index.json", Buffer.from(JSON.stringify(index)));
  const read = name => files.get(name);
  assert.equal(verifyArchive(read, "arm64", revision, "1.0.0").digest, runtime);
  const nested = put(index);
  files.set("index.json", Buffer.from(JSON.stringify({manifests: [{
    mediaType: "application/vnd.oci.image.index.v1+json", digest: nested,
  }]})));
  assert.equal(verifyArchive(read, "arm64", revision, "1.0.0").digest, runtime);
  files.set("index.json", Buffer.from(JSON.stringify(index)));
  assert.throws(() => verifyArchive(read, "amd64", revision, "1.0.0"), /Missing/);
  assert.throws(() => verifyArchive(read, "arm64", revision, "2.0.0"), /labels/);
  const key = `blobs/sha256/${attestation.slice(7)}`;
  const original = files.get(key);
  files.set(key, Buffer.from("{}"));
  assert.throws(() => verifyArchive(read, "arm64", revision, "1.0.0"), /checksum/);
  files.set(key, original);
  index.manifests.pop();
  files.set("index.json", Buffer.from(JSON.stringify(index)));
  assert.throws(() => verifyArchive(read, "arm64", revision, "1.0.0"), /attestation/);
});
