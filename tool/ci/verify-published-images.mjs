import {execFileSync} from "node:child_process";
import {readFileSync, appendFileSync} from "node:fs";
import {imagePolicy} from "./image-policy.mjs";

const {PUBLICATION_JOURNAL, DOCKERHUB_NAMESPACE, GITHUB_SHA, GITHUB_STEP_SUMMARY} = process.env;
const {expected, phase} = JSON.parse(readFileSync(PUBLICATION_JOURNAL, "utf8"));
if (phase !== "complete" || expected.namespace !== DOCKERHUB_NAMESPACE || expected.sha !== GITHUB_SHA ||
    expected.channel !== "development" || expected.exact !== imagePolicy("development", "", GITHUB_SHA).exact) {
  throw new Error("Published image verification requires a completed matching development journal");
}
for (const image of ["web", "ser2net"]) {
  const digest = expected.digests[image];
  if (!/^sha256:[0-9a-f]{64}(?![\s\S])/.test(digest)) throw new Error("Invalid published digest");
  const reference = `${DOCKERHUB_NAMESPACE}/mpdrussoundpi-${image}@${digest}`;
  for (const arch of ["amd64", "arm64"]) {
    execFileSync("docker", ["pull", "--platform", `linux/${arch}`, reference], {stdio: "inherit"});
    execFileSync("bash", [`tool/ci/smoke-${image}-image.sh`, reference, arch], {stdio: "inherit"});
  }
  if (GITHUB_STEP_SUMMARY) appendFileSync(GITHUB_STEP_SUMMARY,
    `### Published ${image}\n\`${reference}\`\n\nExact tag: \`${expected.exact}\`; alias: \`dev\`.\nAMD64 and ARM64 registry pull/runtime smoke checks passed.\n`);
}
