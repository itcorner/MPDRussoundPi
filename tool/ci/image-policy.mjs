import {execFileSync, spawnSync} from "node:child_process";
import {appendFileSync} from "node:fs";
import {pathToFileURL} from "node:url";

function validateSha(sha) {
  if (!/^[0-9a-f]{40}(?![\s\S])/.test(sha)) throw new Error("Expected a full commit SHA");
}

function sourceBranch(channel) {
  if (!["development", "candidate", "stable"].includes(channel)) throw new Error("Unknown image channel");
  return channel === "stable" ? "main" : "develop";
}

function validateVersion(channel, version) {
  sourceBranch(channel);
  if (channel === "development") {
    if (version !== "") throw new Error("Development images do not accept a version input");
    return null;
  }
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.(0|[1-9]\d*))?(?![\s\S])/.exec(version);
  if (!match) throw new Error("Expected X.Y.Z or X.Y.Z-rc.N without a v prefix");
  if ((channel === "candidate") !== Boolean(match[4])) {
    throw new Error("Version does not match the requested channel");
  }
  return match;
}

export function imagePolicy(channel, version, sha, existing = []) {
  validateSha(sha);
  const match = validateVersion(channel, version);
  if (channel === "development") {
    const exact = `dev-${sha}`;
    if (existing.includes(exact)) throw new Error(`Write-once tag already exists: ${exact}`);
    return {exact, aliases: ["dev"]};
  }
  if (existing.includes(version)) throw new Error(`Write-once tag already exists: ${version}`);
  if (channel === "candidate") return {exact: version, aliases: []};
  const numbers = match.slice(1, 4).map(BigInt);
  const newer = existing.filter(tag => /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?![\s\S])/.test(tag))
    .map(tag => tag.split(".").map(BigInt))
    .filter(parts => {
      for (let i = 0; i < 3; i++) {
        if (parts[i] !== numbers[i]) return parts[i] > numbers[i];
      }
      return false;
    });
  const aliases = [];
  if (!newer.some(parts => parts[0] === numbers[0] && parts[1] === numbers[1])) {
    aliases.push(`${match[1]}.${match[2]}`);
  }
  if (!newer.length) aliases.push("latest");
  return {exact: version, aliases};
}

export function imageEventPolicy({eventName, ref, mode, channel = "", version = "", deleted = false}) {
  if (typeof ref !== "string") throw new Error("Expected a GitHub ref");
  if (deleted) throw new Error("Deleted refs cannot prepare or publish images");
  if (mode === "build-only") {
    if (eventName !== "workflow_dispatch" || !/^refs\/heads\/[^\s]+(?![\s\S])/.test(ref)) {
      throw new Error("Build-only simulations require a manual dispatch on a branch");
    }
    validateVersion(channel, version);
    return {channel, version, sourceBranch: sourceBranch(channel)};
  }
  if (mode !== "publish" || eventName !== "push") {
    throw new Error("Publication policy accepts only push events");
  }
  if (channel !== "" || version !== "") {
    throw new Error("Publication channel and version must be derived from the pushed ref");
  }
  if (ref === "refs/heads/develop") {
    return {channel: "development", version: "", sourceBranch: "develop"};
  }
  if (!ref.startsWith("refs/tags/v")) {
    throw new Error("Publication requires develop or a strict version tag");
  }
  const tagVersion = ref.slice("refs/tags/v".length);
  const tagChannel = tagVersion.includes("-rc.") ? "candidate" : "stable";
  validateVersion(tagChannel, tagVersion);
  return {channel: tagChannel, version: tagVersion, sourceBranch: sourceBranch(tagChannel)};
}

export function validateImageSource(channel, sha, {cwd = process.cwd(), requireTip = false} = {}) {
  validateSha(sha);
  const branch = sourceBranch(channel);
  const tip = execFileSync("git", ["rev-parse", "--verify", `refs/remotes/origin/${branch}^{commit}`],
    {cwd, encoding: "utf8"}).trim();
  const ancestry = spawnSync("git", ["merge-base", "--is-ancestor", sha, tip], {cwd, encoding: "utf8"});
  if (ancestry.error) throw ancestry.error;
  if (ancestry.status === 1) throw new Error(`Source commit is not reachable from origin/${branch}`);
  if (ancestry.status !== 0) throw new Error(`Git ancestry check failed: ${ancestry.stderr.trim()}`);
  if (requireTip && (channel !== "development" || sha !== tip)) {
    throw new Error("The dev alias requires the current origin/develop tip");
  }
  return branch;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const {MODE, CHANNEL, VERSION = "", GITHUB_SHA, GITHUB_OUTPUT, GITHUB_EVENT_NAME, GITHUB_REF} = process.env;
  if (MODE !== "build-only") throw new Error("Publication is disabled; only build-only is supported");
  const event = imageEventPolicy({eventName: GITHUB_EVENT_NAME, ref: GITHUB_REF,
    mode: MODE, channel: CHANNEL, version: VERSION});
  validateImageSource(event.channel, GITHUB_SHA);
  const policy = imagePolicy(event.channel, event.version, GITHUB_SHA);
  console.log(JSON.stringify({...event, sha: GITHUB_SHA, ...policy}));
  if (GITHUB_OUTPUT) {
    appendFileSync(GITHUB_OUTPUT, `version=${policy.exact}\n`);
  }
}
