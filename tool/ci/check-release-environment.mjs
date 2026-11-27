// Release publication runs in the GitHub environment "release", whose required reviewers
// approve each version tag before the job starts. GitHub creates a missing environment on
// first use without any protection, so the job checks the protection itself before it
// touches the registry token (docs/plans/general_new_release-channels.md, stage 1).
import {pathToFileURL} from "node:url";

export const ENVIRONMENT = "release";

export function checkReleaseEnvironment(body) {
  if (!body || body.name !== ENVIRONMENT || !Array.isArray(body.protection_rules)) {
    throw new Error(`The GitHub environment "${ENVIRONMENT}" could not be read`);
  }
  const reviewers = body.protection_rules.filter(rule => rule?.type === "required_reviewers")
    .flatMap(rule => Array.isArray(rule.reviewers) ? rule.reviewers : []);
  if (reviewers.length === 0) {
    throw new Error(`The GitHub environment "${ENVIRONMENT}" has no required reviewer; release publication stops`);
  }
  return reviewers.map(item => item?.reviewer?.login ?? item?.reviewer?.slug ?? "unknown");
}

export async function fetchEnvironment(repository, token, fetchRequest = fetch) {
  if (!/^(?!\.)[A-Za-z0-9_.-]+\/(?!\.)[A-Za-z0-9_.-]+(?![\s\S])/.test(repository ?? "")) {
    throw new Error("Unexpected repository");
  }
  const response = await fetchRequest(`https://api.github.com/repos/${repository}/environments/${ENVIRONMENT}`, {
    headers: {Accept: "application/vnd.github+json", ...(token ? {Authorization: `Bearer ${token}`} : {})},
    redirect: "error", signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`The GitHub environment "${ENVIRONMENT}" could not be read: HTTP ${response.status}`);
  return response.json();
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const {GITHUB_REPOSITORY, GITHUB_TOKEN} = process.env;
  const reviewers = checkReleaseEnvironment(await fetchEnvironment(GITHUB_REPOSITORY, GITHUB_TOKEN));
  console.log(`Environment "${ENVIRONMENT}" requires a review by: ${reviewers.join(", ")}`);
}
