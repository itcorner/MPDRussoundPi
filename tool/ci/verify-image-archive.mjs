import {execFileSync} from "node:child_process";
import {createHash} from "node:crypto";
import {pathToFileURL} from "node:url";

export function verifyArchive(read, arch, revision, version) {
  const json = name => JSON.parse(read(name));
  const blob = digest => {
    if (!/^sha256:[0-9a-f]{64}$/.test(digest)) throw new Error(`Invalid OCI digest: ${digest}`);
    const data = read(`blobs/sha256/${digest.slice(7)}`);
    if (`sha256:${createHash("sha256").update(data).digest("hex")}` !== digest) {
      throw new Error(`OCI blob checksum mismatch: ${digest}`);
    }
    return JSON.parse(data);
  };
  let index = json("index.json");
  if (index.manifests.length === 1
    && index.manifests[0].mediaType === "application/vnd.oci.image.index.v1+json") {
    index = blob(index.manifests[0].digest);
  }
  const runtime = index.manifests.find(entry =>
    entry.platform?.os === "linux" && entry.platform?.architecture === arch);
  if (!runtime) throw new Error(`Missing linux/${arch} runtime manifest`);
  const config = blob(blob(runtime.digest).config.digest);
  const labels = config.config.Labels;
  if (config.architecture !== arch || config.os !== "linux"
    || labels["org.opencontainers.image.revision"] !== revision
    || labels["org.opencontainers.image.version"] !== version
    || labels["org.opencontainers.image.source"] !== "https://github.com/itcorner/MPDRussoundPi") {
    throw new Error("OCI runtime platform or source labels do not match the requested build");
  }
  const attestation = index.manifests.find(entry =>
    entry.annotations?.["vnd.docker.reference.type"] === "attestation-manifest"
    && entry.annotations?.["vnd.docker.reference.digest"] === runtime.digest);
  if (!attestation) throw new Error("Missing attestation manifest for the runtime digest");
  const predicates = blob(attestation.digest).layers.map(layer => {
    const statement = blob(layer.digest);
    if (!statement.subject.some(subject => subject.digest?.sha256 === runtime.digest.slice(7))) {
      throw new Error("Attestation subject does not match the runtime digest");
    }
    return statement.predicateType;
  });
  if (!predicates.includes("https://spdx.dev/Document")
    || !predicates.some(type => type.startsWith("https://slsa.dev/provenance/"))) {
    throw new Error("Both SPDX SBOM and SLSA provenance are required");
  }
  return {platform: `linux/${arch}`, digest: runtime.digest, revision, version, predicates};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const [archive, arch, revision, version] = process.argv.slice(2);
  const read = name => execFileSync("tar", ["-xOf", archive, name], {maxBuffer: 32 * 1024 * 1024});
  console.log(JSON.stringify(verifyArchive(read, arch, revision, version)));
}
