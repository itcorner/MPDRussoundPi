import {execFileSync} from "node:child_process";
import {readFileSync, appendFileSync} from "node:fs";
import {pathToFileURL} from "node:url";
import {imagePolicy} from "./image-policy.mjs";

export function verifyPublishedImages(journal, namespace, sha, run = execFileSync) {
  const {expected, phase} = journal;
  if (phase !== "complete" || expected.namespace !== namespace || expected.sha !== sha ||
      !/^[a-z0-9_-]+(?![\s\S])/.test(namespace) || expected.channel !== "development" ||
      expected.exact !== imagePolicy("development", "", sha).exact) {
    throw new Error("Published image verification requires a completed matching development journal");
  }
  const reports = [];
  for (const image of ["web", "ser2net"]) {
    const digest = expected.digests[image];
    if (!/^sha256:[0-9a-f]{64}(?![\s\S])/.test(digest)) throw new Error("Invalid published digest");
    const reference = `${namespace}/mpdrussoundpi-${image}@${digest}`;
    const index = JSON.parse(run("docker", ["buildx", "imagetools", "inspect", reference, "--raw"],
      {encoding: "utf8", maxBuffer: 1024 * 1024}));
    for (const arch of ["amd64", "arm64"]) {
      const platforms = index.manifests.filter(entry => entry.platform?.os === "linux" &&
        entry.platform?.architecture === arch);
      if (platforms.length !== 1 || !/^sha256:[0-9a-f]{64}(?![\s\S])/.test(platforms[0].digest)) {
        throw new Error(`Missing or ambiguous published linux/${arch} manifest`);
      }
      const platformReference = `${namespace}/mpdrussoundpi-${image}@${platforms[0].digest}`;
      run("docker", ["pull", "--platform", `linux/${arch}`, platformReference], {stdio: "inherit"});
      run("bash", [`tool/ci/smoke-${image}-image.sh`, platformReference, arch], {stdio: "inherit"});
    }
    reports.push({image, reference, exact: expected.exact});
  }
  return reports;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const {PUBLICATION_JOURNAL, DOCKERHUB_NAMESPACE, GITHUB_SHA, GITHUB_STEP_SUMMARY} = process.env;
  const reports = verifyPublishedImages(JSON.parse(readFileSync(PUBLICATION_JOURNAL, "utf8")),
    DOCKERHUB_NAMESPACE, GITHUB_SHA);
  if (GITHUB_STEP_SUMMARY) {
    for (const report of reports) appendFileSync(GITHUB_STEP_SUMMARY,
      `### Published ${report.image}\n\`${report.reference}\`\n\nExact tag: \`${report.exact}\`; alias: \`dev\`.\nAMD64 and ARM64 registry pull/runtime smoke checks passed.\n`);
  }
}
