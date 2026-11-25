// Decides whether a develop push publishes new development images
// (docs/plans/general_done_publish-only-changed-images.md).
//
// A push publishes when an image's build inputs changed since the revision the
// `dev` alias points to, when that image is older than MAX_AGE_DAYS (so base-image
// and Debian/Python security updates still arrive), when a commit since then asks
// for it with a `Publish-Images: force` line, or whenever the published state
// cannot be read with certainty. Docs, plans, tests and deployment tooling never
// reach an image, so a push that only touches them publishes nothing.
import {appendFileSync} from "node:fs";
import {execFileSync} from "node:child_process";
import {pathToFileURL} from "node:url";

// Everything a Dockerfile copies or that shapes the build. A test compares these
// with the COPY instructions so the lists cannot silently fall behind.
export const IMAGE_INPUTS = {
  web: ["Dockerfile", ".dockerignore", "web/", "mpdrussoundpi/__init__.py", "mpdrussoundpi/common/"],
  ser2net: ["Dockerfile.ser2net", "Dockerfile.ser2net.dockerignore", "docker/entrypoint-ser2net.sh"],
};
// Build arguments, labels and attestations come from the workflow itself.
export const BUILD_INPUTS = [".github/workflows/images-build-only.yml"];
export const MAX_AGE_DAYS = 7;
export const FORCE = /^Publish-Images:[ \t]*force[ \t]*$/im;
const REVISION = /^[0-9a-f]{40}$/;
const DAY = 24 * 60 * 60 * 1000;

// Never copied into either image (.dockerignore excludes it; ser2net copies one script).
export const NOT_IN_IMAGES = /\.md$/;

export function touches(file, inputs) {
  if (NOT_IN_IMAGES.test(file)) return false;
  return inputs.some(input => input.endsWith("/") ? file.startsWith(input) : file === input);
}

/**
 * Pure decision. `published` is {revision, created} of the `dev` images or null
 * when unknown; `ancestor` says whether that revision is in the pushed history.
 */
export function decide({head, published, ancestor, changedFiles = [], messages = "", now = Date.now(),
  maxAgeDays = MAX_AGE_DAYS}) {
  if (!published || !REVISION.test(published.revision ?? "")) {
    return {publish: true, reason: "The published development images could not be read; publishing to be safe."};
  }
  if (published.revision === head) {
    return {publish: false, reason: `The development images are already built from ${head.slice(0, 7)}.`};
  }
  if (!ancestor) {
    return {publish: true, reason: `The published revision ${published.revision.slice(0, 7)} is not in the pushed history.`};
  }
  const changed = Object.entries(IMAGE_INPUTS)
    .filter(([, inputs]) => changedFiles.some(file => touches(file, inputs))).map(([name]) => name);
  const build = changedFiles.filter(file => touches(file, BUILD_INPUTS));
  const since = published.revision.slice(0, 7);
  if (changed.length) {
    return {publish: true, reason: `Image inputs changed since ${since}: ${changed.join(", ")}.`, changed};
  }
  if (build.length) return {publish: true, reason: `The image build definition changed since ${since}.`};
  if (FORCE.test(messages)) return {publish: true, reason: `A commit since ${since} asks for Publish-Images: force.`};
  const created = Date.parse(published.created ?? "");
  if (!Number.isFinite(created)) return {publish: true, reason: "The published image has no build time; publishing to be safe."};
  const age = (now - created) / DAY;
  if (age >= maxAgeDays) {
    return {publish: true, reason: `The published images are ${Math.floor(age)} days old; refreshing the base images.`};
  }
  return {publish: false, reason: `No image input changed since ${since}; the published development images stay current.`};
}

async function pullToken(repository, fetchRequest) {
  const url = new URL("https://auth.docker.io/token");
  url.searchParams.set("service", "registry.docker.io");
  url.searchParams.set("scope", `repository:${repository}:pull`);
  const response = await fetchRequest(url.href, {redirect: "error", signal: AbortSignal.timeout(30_000)});
  if (!response.ok) throw new Error(`Anonymous Docker Hub token failed: HTTP ${response.status}`);
  const token = (await response.json())?.token;
  if (typeof token !== "string" || token.length === 0) throw new Error("Docker Hub returned no pull token");
  return token;
}

async function registryJson(path, token, accept, fetchRequest) {
  const url = new URL(path, "https://registry-1.docker.io");
  let response = await fetchRequest(url.href, {redirect: "manual", signal: AbortSignal.timeout(30_000),
    headers: {Authorization: `Bearer ${token}`, Accept: accept}});
  if ([302, 307].includes(response.status)) {
    // Blobs come from a CDN; never send the registry token there.
    const location = new URL(response.headers.get("location"), url);
    if (location.protocol !== "https:" || location.username || location.password) throw new Error("Unsafe blob redirect");
    response = await fetchRequest(location.href, {redirect: "error", signal: AbortSignal.timeout(30_000)});
  }
  if (!response.ok) throw new Error(`Docker Hub read failed for ${url.pathname}: HTTP ${response.status}`);
  return response.json();
}

/** Revision label and build time of `<repository>:dev` for linux/amd64 (read-only, anonymous). */
export async function publishedImage(repository, fetchRequest = fetch) {
  const token = await pullToken(repository, fetchRequest);
  const index = await registryJson(`/v2/${repository}/manifests/dev`, token,
    "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json", fetchRequest);
  const platform = (index.manifests ?? []).find(entry =>
    entry?.platform?.os === "linux" && entry.platform.architecture === "amd64");
  if (!platform?.digest) throw new Error(`${repository}:dev has no linux/amd64 image`);
  const manifest = await registryJson(`/v2/${repository}/manifests/${platform.digest}`, token,
    "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json", fetchRequest);
  const config = await registryJson(`/v2/${repository}/blobs/${manifest.config?.digest}`, token,
    "application/vnd.oci.image.config.v1+json, application/json", fetchRequest);
  const labels = config.config?.Labels ?? {};
  // The label is the build time; a fully cached image keeps an old config time.
  return {revision: labels["org.opencontainers.image.revision"],
    created: labels["org.opencontainers.image.created"] ?? config.created};
}

/** Both images must agree; anything uncertain returns null, which publishes. */
export async function publishedDevelopment(namespace, fetchRequest = fetch) {
  const [web, ser2net] = await Promise.all(["web", "ser2net"].map(image =>
    publishedImage(`${namespace}/mpdrussoundpi-${image}`, fetchRequest)));
  if (web.revision !== ser2net.revision) return null;
  // Both are published together, so the newer time is the publication time.
  const created = [web.created, ser2net.created].map(Date.parse).filter(Number.isFinite);
  return {revision: web.revision, created: created.length ? new Date(Math.max(...created)).toISOString() : null};
}

function git(args) {
  return execFileSync("git", args, {encoding: "utf8", maxBuffer: 16 * 1024 * 1024});
}

export async function evaluate({namespace, head, fetchRequest = fetch, run = git, now = Date.now()}) {
  if (!REVISION.test(head ?? "")) throw new Error("Expected the full pushed commit SHA");
  let published = null;
  try {
    published = await publishedDevelopment(namespace, fetchRequest);
  } catch (error) {
    console.warn(`Reading the published development images failed: ${error.message}`);
  }
  if (!published || !REVISION.test(published.revision ?? "") || published.revision === head) {
    return decide({head, published, now});
  }
  let ancestor = false;
  try {
    run(["merge-base", "--is-ancestor", published.revision, head]);
    ancestor = true;
  } catch {
    return decide({head, published, ancestor, now});
  }
  const changedFiles = run(["diff", "--name-only", published.revision, head]).split("\n").filter(Boolean);
  const messages = run(["log", "--format=%B", `${published.revision}..${head}`]);
  return {...decide({head, published, ancestor, changedFiles, messages, now}), since: published.revision};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const {GITHUB_EVENT_NAME, GITHUB_SHA, DOCKERHUB_NAMESPACE, GITHUB_OUTPUT, GITHUB_STEP_SUMMARY} = process.env;
  const result = GITHUB_EVENT_NAME === "push"
    ? await evaluate({namespace: DOCKERHUB_NAMESPACE, head: GITHUB_SHA})
    : {publish: true, reason: "Manual build-only run: images are always built and never published."};
  console.log(JSON.stringify(result));
  if (GITHUB_OUTPUT) appendFileSync(GITHUB_OUTPUT, `build=${result.publish}\nreason=${result.reason}\n`);
  if (GITHUB_STEP_SUMMARY) {
    appendFileSync(GITHUB_STEP_SUMMARY, `### Image publication decision\n${result.publish
      ? "**Build and publish.**" : "**Nothing to build or publish.**"} ${result.reason}\n`);
  }
}
