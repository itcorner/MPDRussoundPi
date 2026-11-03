import {createHash} from "node:crypto";
import {writeFileSync, readFileSync, unlinkSync, renameSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {execFileSync} from "node:child_process";
import {pathToFileURL} from "node:url";
import {imagePolicy, validateImageSource} from "./image-policy.mjs";
import {recoveryGate, loadPreparedArtifacts} from "./publication-plan.mjs";
import {verifyArchive} from "./verify-image-archive.mjs";

const indexType = "application/vnd.oci.image.index.v1+json";
const manifestType = "application/vnd.oci.image.manifest.v1+json";
const digestOf = bytes => `sha256:${createHash("sha256").update(bytes).digest("hex")}`;

export function assembleImage(readers, sha, version) {
  const blobs = new Map();
  const manifests = new Map();
  const descriptors = [];
  function visit(read, descriptor) {
    if (!/^sha256:[0-9a-f]{64}(?![\s\S])/.test(descriptor.digest)) throw new Error("Invalid OCI descriptor digest");
    const bytes = read(`blobs/sha256/${descriptor.digest.slice(7)}`);
    if (digestOf(bytes) !== descriptor.digest || bytes.length !== descriptor.size) {
      throw new Error("OCI descriptor checksum or size mismatch");
    }
    if ([indexType, manifestType].includes(descriptor.mediaType)) {
      const value = JSON.parse(bytes);
      for (const child of value.manifests ?? [value.config, ...value.layers]) visit(read, child);
      manifests.set(descriptor.digest, {bytes, mediaType: descriptor.mediaType});
    } else {
      blobs.set(descriptor.digest, bytes);
    }
  }
  for (const arch of ["amd64", "arm64"]) {
    const read = readers[arch];
    if (typeof read !== "function") throw new Error(`Missing ${arch} archive`);
    verifyArchive(read, arch, sha, version);
    let index = JSON.parse(read("index.json"));
    if (index.manifests.length === 1 && index.manifests[0].mediaType === indexType) {
      const root = index.manifests[0];
      visit(read, root);
      index = JSON.parse(manifests.get(root.digest).bytes);
    }
    if (index.manifests.length !== 2) throw new Error("Expected one runtime and one attestation per platform");
    for (const descriptor of index.manifests) {
      visit(read, descriptor);
      descriptors.push(descriptor);
    }
  }
  const bytes = Buffer.from(JSON.stringify({schemaVersion: 2, mediaType: indexType, manifests: descriptors}));
  return {bytes, digest: digestOf(bytes), blobs, manifests, sha, version};
}

// Until Stage 5 approval, the concrete write transport is restricted to loopback.
export class LocalRegistry {
  constructor(endpoint) {
    this.endpoint = new URL(endpoint);
    if (this.endpoint.protocol !== "http:" || !["127.0.0.1", "localhost"].includes(this.endpoint.hostname) ||
        this.endpoint.username || this.endpoint.password || this.endpoint.pathname !== "/") {
      throw new Error("Registry writes are restricted to a disposable local registry");
    }
  }

  async request(path, options = {}, allowMissing = false) {
    const url = new URL(path, this.endpoint);
    if (url.origin !== this.endpoint.origin) throw new Error("Registry request left the local endpoint");
    const response = await fetch(url, {...options, redirect: "error", signal: AbortSignal.timeout(120_000)});
    if (allowMissing && response.status === 404) return null;
    if (!response.ok) throw new Error(`Local registry request failed: HTTP ${response.status}`);
    return response;
  }

  path(repository, resource) {
    if (!/^[a-z0-9_-]+\/mpdrussoundpi-(web|ser2net)(?![\s\S])/.test(repository)) {
      throw new Error("Unexpected image repository");
    }
    return `/v2/${repository}/${resource}`;
  }

  async tags(repository) {
    const first = this.path(repository, "tags/list?n=100");
    let next = first;
    const visited = new Set();
    const tags = new Set();
    while (next) {
      const url = new URL(next, this.endpoint);
      if (url.pathname !== new URL(first, this.endpoint).pathname || visited.has(url.href)) {
        throw new Error("Invalid registry pagination");
      }
      visited.add(url.href);
      const response = await this.request(url.href, {}, true);
      if (!response) {
        if (visited.size !== 1) throw new Error("Registry inventory disappeared during pagination");
        return [];
      }
      const body = await response.json();
      if (body.name !== repository || !(body.tags === null || Array.isArray(body.tags))) {
        throw new Error("Malformed registry inventory");
      }
      for (const tag of body.tags ?? []) {
        if (typeof tag !== "string" || tags.has(tag)) throw new Error("Invalid or duplicate registry tag");
        tags.add(tag);
      }
      const link = response.headers.get("link");
      if (link && !/^<[^>]+>;\s*rel="next"$/.test(link)) throw new Error("Invalid registry pagination link");
      next = link ? /^<([^>]+)>/.exec(link)[1] : null;
    }
    return [...tags];
  }

  async manifest(repository, reference) {
    const response = await this.request(this.path(repository, `manifests/${encodeURIComponent(reference)}`),
      {headers: {Accept: `${indexType}, ${manifestType}`}}, true);
    if (!response) return null;
    const bytes = Buffer.from(await response.arrayBuffer());
    const digest = digestOf(bytes);
    if (response.headers.get("docker-content-digest") !== digest) throw new Error("Registry manifest digest mismatch");
    return {bytes, digest};
  }

  async upload(repository, image) {
    for (const [digest, bytes] of image.blobs) {
      const exists = await this.request(this.path(repository, `blobs/${digest}`), {method: "HEAD"}, true);
      if (!exists) {
        const upload = await this.request(this.path(repository, "blobs/uploads/"), {method: "POST"});
        const location = upload.headers.get("location");
        if (!location) throw new Error("Missing registry upload location");
        const url = new URL(location, this.endpoint);
        url.searchParams.set("digest", digest);
        await this.request(url.href, {method: "PUT", headers: {"Content-Type": "application/octet-stream"}, body: bytes});
      }
    }
    for (const [digest, manifest] of image.manifests) {
      await this.put(repository, digest, manifest.bytes, manifest.mediaType);
    }
  }

  async put(repository, reference, bytes, mediaType = indexType) {
    await this.request(this.path(repository, `manifests/${encodeURIComponent(reference)}`),
      {method: "PUT", headers: {"Content-Type": mediaType}, body: bytes});
    const actual = await this.manifest(repository, reference);
    if (actual?.digest !== digestOf(bytes)) throw new Error("Written registry manifest failed digest verification");
  }

  async verify(repository, image, reference) {
    if ((await this.manifest(repository, reference))?.digest !== image.digest) throw new Error("Exact manifest mismatch");
    for (const [digest] of image.manifests) {
      if ((await this.manifest(repository, digest))?.digest !== digest) throw new Error("Missing child manifest");
    }
    for (const [digest] of image.blobs) {
      const response = await this.request(this.path(repository, `blobs/${digest}`));
      if (digestOf(Buffer.from(await response.arrayBuffer())) !== digest) throw new Error("Registry blob checksum mismatch");
    }
  }
}

async function publishLocked({registry, namespace, channel, version, sha, images, journalPath,
  checkSource, recoveryApproved = false}) {
  const exact = imagePolicy(channel, version, sha).exact;
  if (typeof recoveryApproved !== "boolean") throw new Error("Recovery approval must be an explicit boolean");
  if (!journalPath || typeof checkSource !== "function") throw new Error("Journal and source checks are required");
  for (const image of ["web", "ser2net"]) {
    if (!images[image] || images[image].sha !== sha || images[image].version !== exact ||
        digestOf(images[image].bytes) !== images[image].digest) throw new Error("Missing or invalid assembled image");
  }
  const expected = {namespace, channel, version, sha, exact,
    digests: Object.fromEntries(["web", "ser2net"].map(image => [image, images[image].digest]))};
  if (recoveryApproved) {
    const journal = JSON.parse(readFileSync(journalPath, "utf8"));
    if (JSON.stringify(journal.expected) !== JSON.stringify(expected)) throw new Error("Recovery journal does not match artifacts");
  } else {
    writeFileSync(journalPath, JSON.stringify({expected, phase: "prepared"}, null, 2), {flag: "wx", flush: true});
  }
  const save = phase => {
    writeFileSync(`${journalPath}.next`, JSON.stringify({expected, phase}, null, 2), {flag: "wx", flush: true});
    renameSync(`${journalPath}.next`, journalPath);
  };
  await checkSource(false);
  // Preflight both repositories before the first upload.
  for (const image of ["web", "ser2net"]) {
    const repository = `${namespace}/mpdrussoundpi-${image}`;
    const actual = await registry.manifest(repository, exact);
    const tags = await registry.tags(repository);
    if (actual && recoveryApproved) recoveryGate(images[image].digest, actual.digest, true);
    else imagePolicy(channel, version, sha, actual ? [...tags, exact] : tags);
  }
  for (const image of ["web", "ser2net"]) {
    const repository = `${namespace}/mpdrussoundpi-${image}`;
    const assembled = images[image];
    const actual = await registry.manifest(repository, exact);
    if (actual) recoveryGate(assembled.digest, actual.digest, recoveryApproved);
    else {
      await registry.upload(repository, assembled);
      if (await registry.manifest(repository, exact)) throw new Error("Exact tag appeared during upload; recovery required");
      await checkSource(false);
      await registry.put(repository, exact, assembled.bytes);
    }
    await registry.verify(repository, assembled, exact);
    save(`verified-${image}`);
  }
  for (const image of ["web", "ser2net"]) {
    const repository = `${namespace}/mpdrussoundpi-${image}`;
    await registry.verify(repository, images[image], exact);
    const tags = await registry.tags(repository);
    const aliases = imagePolicy(channel, version, sha, tags.filter(tag => tag !== exact)).aliases;
    for (const alias of aliases) {
      const freshTags = await registry.tags(repository);
      if (!imagePolicy(channel, version, sha, freshTags.filter(tag => tag !== exact)).aliases.includes(alias)) {
        throw new Error("Alias eligibility changed during promotion");
      }
      await checkSource(channel === "development");
      await registry.put(repository, alias, images[image].bytes);
      save(`promoted-${image}-${alias}`);
    }
  }
  save("complete");
  return expected;
}

export async function publishImages(options) {
  const {registry, namespace} = options;
  if (!(registry instanceof LocalRegistry) || !/^[a-z0-9_-]+(?![\s\S])/.test(namespace)) {
    throw new Error("Publication requires the local registry transport and a valid namespace");
  }
  const key = createHash("sha256").update(`${registry.endpoint.href}/${namespace}`).digest("hex");
  const lock = join(tmpdir(), `mpdrussoundpi-publication-${key}.lock`);
  // A crashed process leaves evidence: never silently steal its publication lock.
  writeFileSync(lock, JSON.stringify({pid: process.pid, journal: options.journalPath}), {flag: "wx"});
  try {
    return await publishLocked(options);
  } finally {
    unlinkSync(lock);
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const {MODE, LOCAL_REGISTRY_URL, DOCKERHUB_NAMESPACE, CHANNEL, VERSION = "", GITHUB_SHA,
    ARTIFACT_ROOT, PUBLICATION_JOURNAL, RECOVERY_APPROVED} = process.env;
  if (MODE !== "local-test") throw new Error("Docker Hub publication is disabled; only local-test mode is supported");
  const registry = new LocalRegistry(LOCAL_REGISTRY_URL);
  const exact = imagePolicy(CHANNEL, VERSION, GITHUB_SHA).exact;
  const {readers} = await loadPreparedArtifacts(ARTIFACT_ROOT, GITHUB_SHA, exact);
  const images = Object.fromEntries(["web", "ser2net"].map(image =>
    [image, assembleImage(readers[image], GITHUB_SHA, exact)]));
  const branch = CHANNEL === "stable" ? "main" : "develop";
  const checkSource = async requireTip => {
    execFileSync("git", ["fetch", "--no-tags", "origin",
      `+refs/heads/${branch}:refs/remotes/origin/${branch}`], {stdio: "pipe"});
    validateImageSource(CHANNEL, GITHUB_SHA, {requireTip});
  };
  console.log(JSON.stringify(await publishImages({registry, namespace: DOCKERHUB_NAMESPACE,
    channel: CHANNEL, version: VERSION, sha: GITHUB_SHA, images, journalPath: PUBLICATION_JOURNAL,
    checkSource, recoveryApproved: RECOVERY_APPROVED === "true"}), null, 2));
}
