import {appendFileSync} from "node:fs";
import {pathToFileURL} from "node:url";

export async function checkRegistryAccess(namespace, username, password, fetchRequest = fetch) {
  if (![namespace, username].every(value => typeof value === "string" &&
      /^[a-z0-9][a-z0-9_-]*(?![\s\S])/.test(value)) || typeof password !== "string" || password.length === 0) {
    throw new Error("Docker Hub namespace, username and token are required");
  }
  const reports = [];
  for (const image of ["web", "ser2net"]) {
    const repository = `${namespace}/mpdrussoundpi-${image}`;
    const url = new URL("https://auth.docker.io/token");
    url.searchParams.set("service", "registry.docker.io");
    url.searchParams.set("scope", `repository:${repository}:pull,push`);
    const response = await fetchRequest(url.href, {
      method: "GET", redirect: "error", signal: AbortSignal.timeout(30_000),
      headers: {Authorization: `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}`},
    });
    if (!response.ok) throw new Error(`Docker Hub authentication failed: HTTP ${response.status}`);
    // Never include authentication response bodies or token payloads in errors.
    let body;
    try {
      body = await response.json();
    } catch {
      throw new Error("Docker Hub authentication returned invalid JSON");
    }
    const token = body?.token;
    if (typeof token !== "string" || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![\s\S])/.test(token)) {
      throw new Error("Docker Hub authentication returned an invalid registry token");
    }
    let claims;
    try {
      claims = JSON.parse(Buffer.from(token.split(".")[1], "base64url").toString("utf8"));
    } catch {
      throw new Error("Docker Hub registry token has invalid claims");
    }
    const access = Array.isArray(claims?.access)
      ? claims.access.find(entry => entry?.type === "repository" && entry.name === repository) : undefined;
    if (!Array.isArray(access?.actions) || !["pull", "push"].every(action => access.actions.includes(action))) {
      throw new Error(`Docker Hub did not grant pull and push for ${repository}`);
    }
    if (access.actions.includes("delete")) throw new Error("Docker Hub granted unnecessary delete access");
    const registry = await fetchRequest("https://registry-1.docker.io/v2/", {
      method: "GET", redirect: "error", signal: AbortSignal.timeout(30_000),
      headers: {Authorization: `Bearer ${token}`},
    });
    if (!registry.ok) throw new Error(`Docker Hub registry rejected authentication: HTTP ${registry.status}`);
    reports.push({repository, granted: ["pull", "push"], registryAuthentication: "accepted"});
  }
  return reports;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const {GITHUB_EVENT_NAME, GITHUB_REF, DOCKERHUB_NAMESPACE, DOCKERHUB_USERNAME,
    DOCKERHUB_TOKEN, GITHUB_STEP_SUMMARY} = process.env;
  if (GITHUB_EVENT_NAME !== "workflow_dispatch" || GITHUB_REF !== "refs/heads/main") {
    throw new Error("Credential checks require a manual dispatch on main");
  }
  const reports = await checkRegistryAccess(DOCKERHUB_NAMESPACE, DOCKERHUB_USERNAME, DOCKERHUB_TOKEN);
  console.log(JSON.stringify(reports, null, 2));
  if (GITHUB_STEP_SUMMARY) appendFileSync(GITHUB_STEP_SUMMARY,
    `### Docker Hub credential check\nRegistry accepted authentication and granted pull/push scopes for both repositories.\nNo upload or tag write was attempted; actual publication remains untested.\n`);
}
