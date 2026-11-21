const clientCount = document.getElementById("clientCount");
const clientsList = document.getElementById("clientsList");
const clientsEmpty = document.getElementById("clientsEmpty");
const eventsEmpty = document.getElementById("eventsEmpty");
const eventsTableBody = document.getElementById("eventsTableBody");
const hostList = document.getElementById("hostList");
const hostEmpty = document.getElementById("hostEmpty");
const hostCount = document.getElementById("hostCount");

let eventSource = null;
let eventSourceRetryTimer = null;
let statusPollTimer = null;
const eventSourceRetryDelayMs = 30000;
const statusPollIntervalMs = 5000;
let statusFetchInFlight = false;
let statusFetchQueued = false;
let hostFetchInFlight = false;
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
    const isTruncated = payloadPreview.scrollWidth > payloadPreview.clientWidth;
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
const openHostDetails = new Set();
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
  hostCount.textContent = String(hosts.length);
  hostEmpty.hidden = hosts.length !== 0;
  hostList.replaceChildren();
  for (const host of hosts) {
    const item = document.createElement("article");
    item.className = "status-list-item host-card";
    const view = host.host ? host.host.view : null;
    const level = view ? view.overall : "unknown";
    const header = document.createElement("div");
    header.className = "status-item-row";
    header.append(
      textElement("strong", "", `Audio host (${host.agent_id})`),
      textElement("span", `status-pill host-level host-level-${hostLevelText[level] ? level : "unknown"}`,
        hostLevelText[level] || "Unknown"),
    );
    item.appendChild(header);
    if (view && view.summary.length) {
      item.appendChild(textElement("div", "status-meta host-summary", view.summary.join(" · ")));
    }
    item.appendChild(textElement("div", "status-meta", hostFreshness(host)));
    if (view && view.rows.length) {
      const details = document.createElement("details");
      details.className = "host-details";
      details.open = openHostDetails.has(host.agent_id);
      details.addEventListener("toggle", () => {
        if (details.open) {
          openHostDetails.add(host.agent_id);
        } else {
          openHostDetails.delete(host.agent_id);
        }
      });
      details.appendChild(textElement("summary", "", "Details"));
      const list = document.createElement("dl");
      list.className = "host-rows";
      for (const row of view.rows) {
        const flagged = hostLevelText[row.level] && row.level !== "ok";
        list.append(
          textElement("dt", "", row.label),
          textElement("dd", flagged ? `host-row-${row.level}` : "", flagged ? `${row.value} (${hostLevelText[row.level]})` : row.value),
        );
      }
      details.appendChild(list);
      item.appendChild(details);
    }
    hostList.appendChild(item);
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
  startStatusSync();
  startStatusPolling();
});