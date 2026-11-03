import {appendFileSync} from "node:fs";
import {pathToFileURL} from "node:url";

export function validateRecoveryRun(run, repository) {
  if (run.repository?.full_name !== repository || run.event !== "push" ||
      run.head_branch !== "develop" || run.path !== ".github/workflows/images-build-only.yml" ||
      run.status !== "completed" || !["failure", "cancelled"].includes(run.conclusion) ||
      !/^[0-9a-f]{40}(?![\s\S])/.test(run.head_sha)) {
    throw new Error("Recovery requires a completed failed development image run from this repository");
  }
  return run.head_sha;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
if (process.env.GITHUB_EVENT_NAME !== "workflow_dispatch" || process.env.GITHUB_REF !== "refs/heads/main" ||
    process.env.RECOVERY_APPROVED !== "true" || !/^[1-9][0-9]*(?![\s\S])/.test(process.env.SOURCE_RUN_ID)) {
  throw new Error("Recovery requires explicit approval and a manual main dispatch with a source run ID");
}
const {GITHUB_REPOSITORY, GITHUB_TOKEN, SOURCE_RUN_ID, GITHUB_OUTPUT} = process.env;
const response = await fetch(`https://api.github.com/repos/${GITHUB_REPOSITORY}/actions/runs/${SOURCE_RUN_ID}`, {
  headers: {Authorization: `Bearer ${GITHUB_TOKEN}`, Accept: "application/vnd.github+json"},
  redirect: "error", signal: AbortSignal.timeout(30_000),
});
if (!response.ok) throw new Error(`Recovery source lookup failed: HTTP ${response.status}`);
appendFileSync(GITHUB_OUTPUT, `sha=${validateRecoveryRun(await response.json(), GITHUB_REPOSITORY)}\n`);
}
