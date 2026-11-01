import {execFileSync} from "node:child_process";
import {appendFileSync} from "node:fs";
import {pathToFileURL} from "node:url";

export function imagePolicy(channel, version, sha, existing = []) {
  if (!/^[0-9a-f]{40}$/.test(sha)) throw new Error("Expected a full commit SHA");
  if (channel === "development") {
    const exact = `dev-${sha}`;
    if (existing.includes(exact)) throw new Error(`Write-once tag already exists: ${exact}`);
    return {exact, aliases: ["dev"]};
  }
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.(0|[1-9]\d*))?$/.exec(version);
  if (!match) throw new Error("Expected X.Y.Z or X.Y.Z-rc.N without a v prefix");
  if ((channel === "candidate") !== Boolean(match[4]) || !["candidate", "stable"].includes(channel)) {
    throw new Error("Version does not match the requested channel");
  }
  if (existing.includes(version)) throw new Error(`Write-once tag already exists: ${version}`);
  if (channel === "candidate") return {exact: version, aliases: []};
  const numbers = match.slice(1, 4).map(BigInt);
  const newer = existing.filter(tag => /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(tag))
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

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const {CHANNEL, VERSION = "", GITHUB_SHA, GITHUB_OUTPUT} = process.env;
  const branch = CHANNEL === "stable" ? "main" : "develop";
  execFileSync("git", ["merge-base", "--is-ancestor", GITHUB_SHA, `origin/${branch}`]);
  const policy = imagePolicy(CHANNEL, VERSION, GITHUB_SHA);
  console.log(JSON.stringify({channel: CHANNEL, sha: GITHUB_SHA, sourceBranch: branch, ...policy}));
  if (GITHUB_OUTPUT) {
    appendFileSync(GITHUB_OUTPUT, `version=${policy.exact}\n`);
  }
}
