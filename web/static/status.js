const clientCount = document.getElementById("clientCount");
const clientsList = document.getElementById("clientsList");
const clientsEmpty = document.getElementById("clientsEmpty");
const eventsEmpty = document.getElementById("eventsEmpty");
const eventsCount = document.getElementById("eventsCount");
const eventsTableWrap = document.getElementById("eventsTableWrap");
const eventsTableBody = document.getElementById("eventsTableBody");
const hostList = document.getElementById("hostList");
const overviewList = document.getElementById("overviewList");
const overviewEmpty = document.getElementById("overviewEmpty");
const overviewLevel = document.getElementById("overviewLevel");
const overviewUpdate = document.getElementById("overviewUpdate");
const adminPanel = document.getElementById("adminPanel");
const adminState = document.getElementById("adminState");
const adminLoginForm = document.getElementById("adminLoginForm");
const adminPassword = document.getElementById("adminPassword");
const adminSession = document.getElementById("adminSession");
const adminSessionText = document.getElementById("adminSessionText");
const adminLogout = document.getElementById("adminLogout");
const adminSetup = document.getElementById("adminSetup");
const adminMessage = document.getElementById("adminMessage");

let eventSource = null;
let eventSourceRetryTimer = null;
let statusPollTimer = null;
const eventSourceRetryDelayMs = 30000;
const statusPollIntervalMs = 5000;
let statusFetchInFlight = false;
let statusFetchQueued = false;
let hostFetchInFlight = false;
let overviewFetchInFlight = false;
const expandedPayloadRows = new Set();

function formatTimestampLines(rawTimestamp) {
  const fallback = String(rawTimestamp || "");
  const parsed = new Date(fallback);

  if (Number.isNaN(parsed.getTime())) {
    const pieces = fallback.trim().split(/\s+/);
    if (pieces.length >= 2) {
      return { date: pieces[0], time: pieces.slice(1).join(" ") };
    }
    return { date: fallback || "-", time: "" };
  }

  const date = parsed.toLocaleDateString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const time = parsed.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  return { date, time };
}

function eventKey(event) {
  return `${event.timestamp}|${event.ip}|${event.path}|${JSON.stringify(event.payload)}`;
}

// Client and event fields come from arbitrary network peers (IP, User-Agent,
// request payloads), so they are only ever assigned as text, never as HTML.
function textElement(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  element.textContent = String(text ?? "");
  return element;
}

function renderStatus(payload, historyPayload) {
  const clients = payload.connected_clients || [];
  const events = historyPayload.recent_events || [];

  clientCount.textContent = String(clients.length);
  clientsEmpty.hidden = clients.length !== 0;
  clientsList.replaceChildren();

  for (const client of clients) {
    const item = document.createElement("article");
    item.className = "status-list-item";
    const header = document.createElement("div");
    header.className = "status-item-row";
    header.append(
      textElement("strong", "", client.ip),
      textElement("span", "status-pill", `Client ${client.id}`),
    );
    item.append(
      header,
      textElement("div", "status-meta", `Connected: ${client.connected_at}`),
      textElement("div", "status-meta", `Session: ${client.session_id || "—"}`),
      textElement("div", "status-meta", `Open streams: ${client.streams || 1}`),
      textElement("div", "status-meta", `User-Agent: ${client.user_agent || "Unknown"}`),
    );
    clientsList.appendChild(item);
  }

  eventsEmpty.hidden = events.length !== 0;
  eventsTableWrap.hidden = events.length === 0;
  eventsCount.textContent = String(events.length);
  eventsTableBody.replaceChildren();
  const visibleKeys = new Set();
  for (const event of events) {
    const key = eventKey(event);
    const encodedKey = encodeURIComponent(key);
    visibleKeys.add(encodedKey);
    const payloadText = JSON.stringify(event.payload);
    const expanded = expandedPayloadRows.has(encodedKey);
    const timestamp = formatTimestampLines(event.timestamp);
    const row = document.createElement("tr");

    const timeCell = document.createElement("td");
    const timeBlock = document.createElement("div");
    timeBlock.className = "status-time";
    timeBlock.append(
      textElement("span", "status-time-date", timestamp.date),
      textElement("span", "status-time-value", timestamp.time),
    );
    timeCell.appendChild(timeBlock);

    const payloadCell = document.createElement("td");
    const payloadToggle = document.createElement("button");
    payloadToggle.className = "payload-toggle";
    payloadToggle.type = "button";
    payloadToggle.setAttribute("data-event-key", encodedKey);
    payloadToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    payloadToggle.title = expanded ? "Click to collapse payload" : "Click to expand payload";
    const payloadPreview = textElement("code", "payload-preview", payloadText);
    payloadToggle.appendChild(payloadPreview);
    payloadCell.appendChild(payloadToggle);

    row.append(timeCell, textElement("td", "", event.ip), textElement("td", "", event.path), payloadCell);
    eventsTableBody.appendChild(row);

    // Always test truncation in collapsed mode so expanded rows are not misclassified.
    payloadToggle.setAttribute("aria-expanded", "false");
    // A collapsed panel has no layout (width 0): keep the toggle rather than judging from nothing.
    const isTruncated = !(payloadPreview.clientWidth > 0) || payloadPreview.scrollWidth > payloadPreview.clientWidth;
    payloadToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    if (isTruncated) {
      payloadToggle.setAttribute("data-payload-toggle", "");
    } else {
      expandedPayloadRows.delete(encodedKey);
      payloadToggle.replaceWith(textElement("code", "payload-preview payload-preview-static", payloadText));
    }
  }

  for (const key of Array.from(expandedPayloadRows)) {
    if (!visibleKeys.has(key)) {
      expandedPayloadRows.delete(key);
    }
  }
}

// Host facts: every string below comes from validated server fields and is
// assigned as text only. The server decides labels and builds the detail rows.
const hostLevelText = { ok: "OK", warning: "Warning", problem: "Problem", unknown: "Unknown" };

function hostFreshness(host) {
  if (host.status === "unsupported") {
    return "This agent does not report host information yet. Update the agent and the playback proxy to enable it.";
  }
  if (host.status === "pending") {
    return "Waiting for the first host report.";
  }
  if (host.status !== "ok" || !host.host) {
    return `Host information unavailable${host.error && host.error.message ? `: ${host.error.message}` : "."}`;
  }
  const ages = Object.values(host.host.sections)
    .filter((section) => section.status === "ok" && Number.isFinite(section.age_ms))
    .map((section) => section.age_ms);
  const seconds = ages.length ? Math.round(Math.min(...ages) / 1000) : null;
  if (host.stale) {
    return seconds === null ? "Data is old." : `Data is old (last update ${seconds} s ago).`;
  }
  return seconds === null ? "Updated." : seconds < 4 ? "Updated now." : `Updated ${seconds} s ago.`;
}

function renderHosts(payload) {
  const hosts = payload.hosts || [];
  hostList.replaceChildren();
  for (const host of hosts) {
    const view = host.host ? host.host.view : null;
    hostList.appendChild(overviewCard({
      id: `host:${host.agent_id}`,
      title: `Audio host (${host.agent_id})`,
      level: view ? view.overall : "unknown",
      summary: view && view.summary.length ? view.summary.join(" · ") : "",
      note: hostFreshness(host),
      rows: [],
      groups: view ? (view.groups || []) : [],
    }));
  }
}

// System overview: sections and groups come from the server as plain text with
// the level spelled out; nothing here is ever treated as markup.
const openOverviewDetails = new Set();

function overviewRows(rows) {
  const list = document.createElement("dl");
  list.className = "host-rows";
  for (const row of rows) {
    const flagged = hostLevelText[row.level] && row.level !== "ok";
    const value = textElement("dd", flagged ? `host-row-${row.level}` : "", flagged ? `${row.value} (${hostLevelText[row.level]})` : row.value);
    if (typeof row.link === "string" && row.link.startsWith("https://hub.docker.com/r/")) {
      // Only a Docker Hub address from the server becomes a link; the text stays text.
      const anchor = textElement("a", "", row.value);
      anchor.setAttribute("href", row.link);
      anchor.setAttribute("target", "_blank");
      anchor.setAttribute("rel", "noopener noreferrer");
      value.replaceChildren(anchor);
    }
    list.append(textElement("dt", "", row.label), value);
  }
  return list;
}

function overviewHeader(title, level, strong) {
  const header = document.createElement("div");
  header.className = "status-item-row";
  header.append(
    textElement(strong ? "strong" : "span", strong ? "" : "overview-group-title", title),
    textElement("span", `status-pill host-level host-level-${hostLevelText[level] ? level : "unknown"}`, hostLevelText[level] || "Unknown"),
  );
  return header;
}

function overviewDetails(key, label) {
  const details = document.createElement("details");
  details.className = "host-details";
  details.open = openOverviewDetails.has(key);
  details.addEventListener("toggle", () => {
    if (details.open) {
      openOverviewDetails.add(key);
    } else {
      openOverviewDetails.delete(key);
    }
  });
  details.appendChild(textElement("summary", "", label));
  return details;
}

function overviewCard(section) {
  const item = document.createElement("article");
  item.className = "status-list-item host-card";
  item.appendChild(overviewHeader(section.title, section.level, true));
  if (section.summary) {
    item.appendChild(textElement("div", "status-meta host-summary", section.summary));
  }
  if (section.note) {
    item.appendChild(textElement("div", "status-meta", section.note));
  }
  if (section.action && section.action.id === "check-updates") {
    item.appendChild(updateCheckControl(section.action));
  }
  const groups = section.groups || [];
  if (section.rows.length || groups.length) {
    const details = overviewDetails(section.id, "Details");
    if (section.rows.length) {
      details.appendChild(overviewRows(section.rows));
    }
    for (const group of groups) {
      const box = document.createElement("div");
      box.className = "overview-group";
      box.appendChild(overviewHeader(group.title, group.level, false));
      box.appendChild(textElement("div", "status-meta", group.summary));
      const inner = overviewDetails(`${section.id}/${group.id}`, "Show");
      inner.appendChild(overviewRows(group.rows));
      box.appendChild(inner);
      details.appendChild(box);
    }
    item.appendChild(details);
  }
  return item;
}

// Administrator session. The CSRF token stays in memory and is sent with every
// privileged request; the password field is cleared after each attempt.
let adminCsrfToken = null;
let adminShownState = null;

function minutesText(seconds) {
  const minutes = Math.max(1, Math.round(seconds / 60));
  return minutes === 1 ? "1 minute" : `${minutes} minutes`;
}

function renderAdmin(session) {
  const configured = session.configured === true;
  const authenticated = configured && session.authenticated === true;
  adminCsrfToken = authenticated && typeof session.csrf_token === "string" ? session.csrf_token : null;
  const [text, level] = !configured ? ["Not set up", "unknown"]
    : authenticated ? ["Logged in", "ok"]
      : session.locked_for_seconds > 0 ? ["Locked", "warning"] : ["Not logged in", "unknown"];
  adminState.textContent = text;
  adminState.className = `status-pill host-level host-level-${level}`;
  // Open the panel when a login is possible but not done; collapse it once logged
  // in or when nothing can be done here. Only on a change of state, so a panel the
  // user opened or closed stays that way across refreshes.
  const shownState = !configured ? "setup" : authenticated ? "in" : "out";
  if (shownState !== adminShownState) {
    adminPanel.open = shownState === "out";
    adminShownState = shownState;
  }
  adminSetup.hidden = configured;
  adminLoginForm.hidden = !configured || authenticated;
  adminSession.hidden = !authenticated;
  if (authenticated) {
    adminSessionText.textContent = `Logged in. The session ends after ${minutesText(session.idle_expires_in_seconds)} without activity`
      + ` and at the latest in ${minutesText(session.expires_in_seconds)}.`;
  }
  if (configured && !authenticated && session.locked_for_seconds > 0) {
    showAdminMessage(`Too many failed attempts. Login is locked for about ${minutesText(session.locked_for_seconds)}.`);
  }
}

function showAdminMessage(text) {
  adminMessage.textContent = text || "";
  adminMessage.hidden = !text;
}

async function fetchAdminSession() {
  try {
    const response = await apiFetch("/api/admin/session");
    if (response.ok) {
      renderAdmin(await response.json());
    }
  } catch {
    // The next poll retries.
  }
}

async function adminRequest(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (adminCsrfToken) {
    headers["X-CSRF-Token"] = adminCsrfToken;
  }
  const response = await apiFetch(path, { method: "POST", headers, body: JSON.stringify(body || {}) });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  return { response, payload };
}

async function submitAdminLogin(event) {
  event.preventDefault();
  const password = adminPassword.value;
  adminPassword.value = "";
  showAdminMessage("");
  try {
    const { response, payload } = await adminRequest("/api/admin/login", { password });
    if (response.ok) {
      renderAdmin(payload);
    } else {
      showAdminMessage(payload.error && payload.error.message ? payload.error.message : "Login failed.");
    }
  } catch {
    showAdminMessage("The server could not be reached.");
  }
  fetchAdminSession();
}

async function submitAdminLogout() {
  try {
    await adminRequest("/api/admin/logout");
  } catch {
    // The session state below shows the result.
  }
  showAdminMessage("");
  fetchAdminSession();
}

// The only action the overview offers: ask the server to check for updates now.
// The server coalesces and rate-limits requests; nothing is installed.
let updateCheckInFlight = false;

function updateCheckControl(action) {
  const row = document.createElement("div");
  row.className = "overview-action";
  const button = textElement("button", "compact-button", action.label);
  button.type = "button";
  button.disabled = !action.available || updateCheckInFlight;
  button.addEventListener("click", () => requestUpdateCheck());
  row.appendChild(button);
  if (action.note) {
    row.appendChild(textElement("span", "status-meta", action.note));
  }
  return row;
}

async function requestUpdateCheck() {
  if (updateCheckInFlight) {
    return;
  }
  updateCheckInFlight = true;
  try {
    await apiFetch("/api/status/updates/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } catch {
    // The overview shows the result or the failure on the next refresh.
  } finally {
    updateCheckInFlight = false;
    fetchOverview();
  }
}

function renderOverview(payload) {
  const sections = payload.sections || [];
  overviewEmpty.hidden = sections.length !== 0;
  const overall = hostLevelText[payload.overall] ? payload.overall : "unknown";
  overviewLevel.textContent = hostLevelText[overall];
  overviewLevel.className = `status-pill host-level host-level-${overall}`;
  overviewUpdate.hidden = payload.update_available !== true;
  overviewList.replaceChildren();
  for (const section of sections) {
    overviewList.appendChild(overviewCard(section));
  }
}

eventsTableBody.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-payload-toggle]");
  if (!toggle) {
    return;
  }

  const isExpanded = toggle.getAttribute("aria-expanded") === "true";
  const key = toggle.getAttribute("data-event-key");
  toggle.setAttribute("aria-expanded", isExpanded ? "false" : "true");
  toggle.title = isExpanded ? "Click to expand payload" : "Click to collapse payload";
  if (!key) {
    return;
  }
  if (isExpanded) {
    expandedPayloadRows.delete(key);
  } else {
    expandedPayloadRows.add(key);
  }
});

async function fetchStatus() {
  if (statusFetchInFlight) {
    statusFetchQueued = true;
    return;
  }
  statusFetchInFlight = true;
  try {
    const [clientsResponse, historyResponse] = await Promise.all([
      apiFetch("/api/status/clients"),
      apiFetch("/api/status/history"),
    ]);
    if (!clientsResponse.ok || !historyResponse.ok) {
      return;
    }
    const clientPayload = await clientsResponse.json();
    const historyPayload = await historyResponse.json();
    renderStatus(clientPayload, historyPayload);
  } finally {
    statusFetchInFlight = false;
    if (statusFetchQueued) {
      statusFetchQueued = false;
      fetchStatus();
    }
  }
}

async function fetchHost() {
  if (hostFetchInFlight) {
    return;
  }
  hostFetchInFlight = true;
  try {
    const response = await apiFetch("/api/status/host");
    if (response.ok) {
      renderHosts(await response.json());
    }
  } catch {
    // The next poll retries; the previous card stays until then.
  } finally {
    hostFetchInFlight = false;
  }
}

async function fetchOverview() {
  if (overviewFetchInFlight) {
    return;
  }
  overviewFetchInFlight = true;
  try {
    const response = await apiFetch("/api/status/overview");
    if (response.ok) {
      renderOverview(await response.json());
    }
  } catch {
    // The next poll retries; the previous overview stays until then.
  } finally {
    overviewFetchInFlight = false;
  }
}

function startStatusSync() {
  if (!window.EventSource || eventSource || document.hidden) {
    return;
  }
  eventSource = new EventSource("/api/events");
  eventSource.addEventListener("state-change", () => {
    fetchStatus();
  });
  eventSource.onopen = () => {
    fetchStatus();
  };
  eventSource.onerror = () => {
    // EventSource gives up for good on non-200 responses (e.g. 503 when the server stream limit is reached).
    if (eventSource && eventSource.readyState === EventSource.CLOSED) {
      stopStatusSync();
      eventSourceRetryTimer = window.setTimeout(startStatusSync, eventSourceRetryDelayMs);
    }
  };
}

function stopStatusSync() {
  if (eventSourceRetryTimer !== null) {
    window.clearTimeout(eventSourceRetryTimer);
    eventSourceRetryTimer = null;
  }
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

function startStatusPolling() {
  if (statusPollTimer === null && !document.hidden) {
    statusPollTimer = window.setInterval(() => {
      fetchStatus();
      fetchHost();
      fetchOverview();
      fetchAdminSession();
    }, statusPollIntervalMs);
  }
}

function stopStatusPolling() {
  if (statusPollTimer !== null) {
    window.clearInterval(statusPollTimer);
    statusPollTimer = null;
  }
}

fetchStatus();
fetchHost();
fetchOverview();
fetchAdminSession();
adminLoginForm.addEventListener("submit", submitAdminLogin);
adminLogout.addEventListener("click", submitAdminLogout);
startStatusSync();
startStatusPolling();

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopStatusSync();
    stopStatusPolling();
    return;
  }
  fetchStatus();
  fetchHost();
  fetchOverview();
  fetchAdminSession();
  startStatusSync();
  startStatusPolling();
});