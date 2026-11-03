import {appendFileSync} from "node:fs";
import {pathToFileURL} from "node:url";
import {imageEventPolicy, imagePolicy, validateImageSource} from "./image-policy.mjs";

export async function dockerHubTags(namespace, repository, fetchPage = fetch) {
  for (const component of [namespace, repository]) {
    if (typeof component !== "string" || !/^[a-z0-9][a-z0-9_-]*(?![\s\S])/.test(component)) {
      throw new Error("Expected a Docker Hub namespace and repository name");
    }
  }
  const endpoint = new URL(`https://hub.docker.com/v2/namespaces/${namespace}/repositories/${repository}/tags`);
  endpoint.searchParams.set("page_size", "100");
  const visited = new Set();
  const tags = new Set();
  let count;
  let url = endpoint;
  while (url) {
    if (url.origin !== endpoint.origin || url.pathname !== endpoint.pathname ||
        url.username || url.password || url.hash) {
      throw new Error("Docker Hub pagination left the expected repository endpoint");
    }
    if (visited.has(url.href)) throw new Error("Docker Hub pagination repeated a page");
    visited.add(url.href);
    const response = await fetchPage(url.href, {
      method: "GET",
      redirect: "error",
      signal: AbortSignal.timeout(30_000),
      headers: {Accept: "application/json"},
    });
    if (!response.ok) {
      throw new Error(`Docker Hub inventory failed for ${namespace}/${repository}: HTTP ${response.status}`);
    }
    const page = await response.json();
    if (!page || !Number.isSafeInteger(page.count) || page.count < 0 ||
        !Array.isArray(page.results) ||
        !(page.next === null || (typeof page.next === "string" && page.next.length > 0))) {
      throw new Error("Malformed Docker Hub inventory page");
    }
    if (count !== undefined && count !== page.count) throw new Error("Docker Hub inventory changed during pagination");
    count = page.count;
    for (const tag of page.results) {
      if (!tag || typeof tag.name !== "string" || !/^[\w][\w.-]{0,127}(?![\s\S])/.test(tag.name)) {
        throw new Error("Malformed Docker Hub tag name");
      }
      if (tags.has(tag.name)) throw new Error("Docker Hub inventory contains duplicate tags");
      tags.add(tag.name);
    }
    if (tags.size > count || (page.next !== null && (page.results.length === 0 || tags.size >= count))) {
      throw new Error("Inconsistent Docker Hub inventory pagination");
    }
    url = page.next === null ? null : new URL(page.next, url);
  }
  if (tags.size !== count) throw new Error("Incomplete Docker Hub inventory");
  return [...tags].sort();
}

export async function registryPolicy(namespace, channel, version, sha, fetchPage = fetch) {
  imagePolicy(channel, version, sha);
  const reports = [];
  for (const image of ["web", "ser2net"]) {
    const repository = `mpdrussoundpi-${image}`;
    const tags = await dockerHubTags(namespace, repository, fetchPage);
    reports.push({repository: `${namespace}/${repository}`, tags,
      ...imagePolicy(channel, version, sha, tags)});
  }
  return reports;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const {MODE, CHANNEL, VERSION = "", GITHUB_SHA, GITHUB_EVENT_NAME, GITHUB_REF,
    DOCKERHUB_NAMESPACE, GITHUB_STEP_SUMMARY} = process.env;
  if (MODE !== "build-only") throw new Error("Publication is disabled; only build-only is supported");
  const event = imageEventPolicy({mode: MODE, channel: CHANNEL, version: VERSION,
    eventName: GITHUB_EVENT_NAME, ref: GITHUB_REF});
  validateImageSource(event.channel, GITHUB_SHA);
  const reports = await registryPolicy(DOCKERHUB_NAMESPACE, event.channel, event.version, GITHUB_SHA);
  console.log(JSON.stringify(reports, null, 2));
  if (GITHUB_STEP_SUMMARY) {
    appendFileSync(GITHUB_STEP_SUMMARY,
      `### Read-only Docker Hub inventory\nNo login, token access or registry writes.\n\n\`\`\`json\n${JSON.stringify(reports, null, 2)}\n\`\`\`\n`);
  }
}
