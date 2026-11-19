import {execFileSync} from "node:child_process";
import {readFileSync, appendFileSync} from "node:fs";
import {pathToFileURL} from "node:url";
import {imagePolicy} from "./image-policy.mjs";

const phases = new Set(["prepared", "verified-web", "verified-ser2net", "promoted-web-dev", "promoted-ser2net-dev"]);

export function verifyPublishedImages(journal, namespace, sha, run = execFileSync, {verifyTags = false} = {}) {
  const {expected, phase} = journal;
  if ((phase !== "complete" && !(verifyTags && phases.has(phase))) ||
      expected?.namespace !== namespace || expected.sha !== sha ||
      !/^[a-z0-9_-]+(?![\s\S])/.test(namespace) || expected.channel !== "development" ||
      expected.exact !== imagePolicy("development", "", sha).exact) {
    throw new Error("Published image verification requires a completed matching development journal");
  }
  const images = ["web", "ser2net"].map(image => {
    const digest = expected.digests?.[image];
    if (!/^sha256:[0-9a-f]{64}(?![\s\S])/.test(digest)) throw new Error("Invalid published digest");
    return {image, digest, repository: `${namespace}/mpdrussoundpi-${image}`};
  });
  if (verifyTags) {
    for (const {repository, digest} of images) {
      for (const tag of [expected.exact, "dev"]) {
        const tagged = `${repository}:${tag}`;
        const manifest = JSON.parse(run("docker", ["buildx", "imagetools", "inspect", tagged,
          "--format", "{{json .Manifest}}"], {encoding: "utf8", maxBuffer: 1024 * 1024, timeout: 30_000}));
        if (manifest?.digest !== digest) {
          throw new Error(`Registry tag ${tagged}: expected ${digest}, observed ${manifest?.digest ?? "no digest"}`);
        }
      }
    }
  }
  const reports = [];
  for (const {image, digest, repository} of images) {
    const reference = `${repository}@${digest}`;
    const index = JSON.parse(run("docker", ["buildx", "imagetools", "inspect", reference, "--raw"],
      {encoding: "utf8", maxBuffer: 1024 * 1024}));
    for (const arch of ["amd64", "arm64"]) {
      const platforms = index.manifests.filter(entry => entry.platform?.os === "linux" &&
        entry.platform?.architecture === arch);
      if (platforms.length !== 1 || !/^sha256:[0-9a-f]{64}(?![\s\S])/.test(platforms[0].digest)) {
        throw new Error(`Missing or ambiguous published linux/${arch} manifest`);
      }
      const platformReference = `${repository}@${platforms[0].digest}`;
      run("docker", ["pull", "--platform", `linux/${arch}`, platformReference], {stdio: "inherit"});
      run("bash", [`tool/ci/smoke-${image}-image.sh`, platformReference, arch], {stdio: "inherit"});
    }
    reports.push({image, reference, exact: expected.exact});
  }
  return reports;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const {PUBLICATION_JOURNAL, DOCKERHUB_NAMESPACE, PUBLICATION_SOURCE_SHA, GITHUB_SHA,
    GITHUB_STEP_SUMMARY, VERIFY_PUBLICATION_TAGS} = process.env;
  const reports = verifyPublishedImages(JSON.parse(readFileSync(PUBLICATION_JOURNAL, "utf8")),
    DOCKERHUB_NAMESPACE, PUBLICATION_SOURCE_SHA ?? GITHUB_SHA, execFileSync,
    {verifyTags: VERIFY_PUBLICATION_TAGS === "true"});
  if (GITHUB_STEP_SUMMARY) {
    for (const report of reports) appendFileSync(GITHUB_STEP_SUMMARY,
      `### Published ${report.image}\n\`${report.reference}\`\n\nExact tag: \`${report.exact}\`; alias: \`dev\`.\nAMD64 and ARM64 registry pull/runtime smoke checks passed.\n`);
  }
}
