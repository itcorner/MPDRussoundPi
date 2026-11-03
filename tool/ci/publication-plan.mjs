import {execFileSync} from "node:child_process";
import {createHash} from "node:crypto";
import {createReadStream, readFileSync, appendFileSync} from "node:fs";
import {pathToFileURL} from "node:url";
import {imagePolicy, validateImageSource} from "./image-policy.mjs";
import {registryPolicy} from "./registry-inventory.mjs";
import {verifyArchive} from "./verify-image-archive.mjs";

export function recoveryGate(expectedDigest, actualDigest, approved = false) {
  const digestPattern = /^sha256:[0-9a-f]{64}(?![\s\S])/;
  if (!digestPattern.test(expectedDigest) || !digestPattern.test(actualDigest)) {
    throw new Error("Recovery requires recorded and registry manifest digests");
  }
  if (expectedDigest !== actualDigest) throw new Error("Recovery digest mismatch; never overwrite the exact tag");
  if (approved !== true) throw new Error("Recovery requires separate explicit approval");
  return {reuseExact: true, overwriteExact: false};
}

export function buildPublicationPlan(channel, version, sha, artifacts, repositories) {
  const policy = imagePolicy(channel, version, sha);
  if (artifacts.length !== 4 || repositories.length !== 2) {
    throw new Error("Publication planning requires both images and all four platform artifacts");
  }
  for (const image of ["web", "ser2net"]) {
    const repository = repositories.find(report => report.repository.endsWith(`/mpdrussoundpi-${image}`));
    if (!repository) throw new Error("Missing image repository inventory");
    const freshPolicy = imagePolicy(channel, version, sha, repository.tags);
    for (const arch of ["amd64", "arm64"]) {
      const matching = artifacts.filter(entry => entry.image === image && entry.platform === `linux/${arch}`);
      if (matching.length !== 1 || matching[0].revision !== sha || matching[0].version !== policy.exact ||
          !/^sha256:[0-9a-f]{64}(?![\s\S])/.test(matching[0].digest)) {
        throw new Error("Platform artifacts must match the planned version and source revision");
      }
    }
    if (JSON.stringify(repository.aliases) !== JSON.stringify(freshPolicy.aliases) ||
        repository.exact !== freshPolicy.exact) throw new Error("Inventory policy decisions do not match");
  }
  return {mode: "dry-run", artifacts, repositories,
    order: ["Upload verified OCI artifacts preserving attestations",
      "Assemble and verify both multi-platform exact tags",
      "Record manifest digests", "Refresh inventory and branch tip", "Promote eligible aliases last"],
    recovery: "Stop on partial writes; require matching recorded manifest digests and separate approval. Never overwrite exact tags."};
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const {MODE, CHANNEL, VERSION = "", GITHUB_SHA, DOCKERHUB_NAMESPACE, ARTIFACT_ROOT,
    GITHUB_STEP_SUMMARY, GITHUB_EVENT_NAME} = process.env;
  if (MODE !== "build-only" || GITHUB_EVENT_NAME !== "workflow_dispatch") {
    throw new Error("Only manual build-only publication planning is enabled");
  }
  if (!ARTIFACT_ROOT) throw new Error("The tested artifact directory is required");
  const policy = imagePolicy(CHANNEL, VERSION, GITHUB_SHA);
  const artifacts = [];
  for (const image of ["web", "ser2net"]) {
    for (const arch of ["amd64", "arm64"]) {
      const archive = `${ARTIFACT_ROOT}/prepared-${image}-${arch}/image.tar`;
      const checksum = /^([0-9a-f]{64}) [ *][^\r\n]+\n?(?![\s\S])/.exec(readFileSync(`${archive}.sha256`, "utf8"));
      if (!checksum) throw new Error("Malformed artifact archive checksum");
      const hash = createHash("sha256");
      for await (const chunk of createReadStream(archive)) hash.update(chunk);
      if (hash.digest("hex") !== checksum[1]) throw new Error("Artifact archive checksum mismatch");
      const metadata = verifyArchive(name => execFileSync("tar", ["-xOf", archive, name],
        {maxBuffer: 32 * 1024 * 1024}), arch, GITHUB_SHA, policy.exact);
      const recorded = JSON.parse(readFileSync(`${archive}.metadata.json`, "utf8"));
      if (recorded.digest !== metadata.digest) throw new Error("Artifact metadata digest mismatch");
      artifacts.push({image, ...metadata});
    }
  }
  const branch = validateImageSource(CHANNEL, GITHUB_SHA);
  execFileSync("git", ["fetch", "--no-tags", "origin",
    `+refs/heads/${branch}:refs/remotes/origin/${branch}`], {stdio: "pipe"});
  validateImageSource(CHANNEL, GITHUB_SHA, {requireTip: CHANNEL === "development"});
  const repositories = await registryPolicy(DOCKERHUB_NAMESPACE, CHANNEL, VERSION, GITHUB_SHA);
  const plan = buildPublicationPlan(CHANNEL, VERSION, GITHUB_SHA, artifacts, repositories);
  console.log(JSON.stringify(plan, null, 2));
  if (GITHUB_STEP_SUMMARY) appendFileSync(GITHUB_STEP_SUMMARY,
    `### Serialized publication dry run\nNo registry writes. Artifact verification, fresh inventory and source checks passed.\n\n\`\`\`json\n${JSON.stringify(plan, null, 2)}\n\`\`\`\n`);
}
