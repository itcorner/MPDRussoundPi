const apiToken = document.querySelector('meta[name="russound-api-token"]')?.content || "";
const clientCount = document.getElementById("clientCount");
const clientsList = document.getElementById("clientsList");
const clientsEmpty = document.getElementById("clientsEmpty");
const eventsEmpty = document.getElementById("eventsEmpty");
const eventsTableBody = document.getElementById("eventsTableBody");

let eventSource = null;
let statusFetchInFlight = false;
let statusFetchQueued = false;
const expandedPayloadRows = new Set();
const sessionStorageKey = "russound-session-id";

function getSessionId() {
  try {
    const existing = window.sessionStorage.getItem(sessionStorageKey);
    if (existing) {
      return existing;
    }
    const generated = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    window.sessionStorage.setItem(sessionStorageKey, generated);
    return generated;
  } catch {
    return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
}

const clientSessionId = getSessionId();

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

function authorizedFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (apiToken) {
    headers.set("X-Russound-Api-Token", apiToken);
  }
  if (clientSessionId) {
    headers.set("X-Russound-Session-Id", clientSessionId);
  }
  return fetch(path, { ...options, headers });
}

function renderStatus(payload, historyPayload) {
  const clients = payload.connected_clients || [];
  const events = historyPayload.recent_events || [];

  clientCount.textContent = String(clients.length);
  clientsEmpty.hidden = clients.length !== 0;
  clientsList.innerHTML = "";

  for (const client of clients) {
    const item = document.createElement("article");
    item.className = "status-list-item";
    const sessionValue = client.session_id || "—";
    item.innerHTML = `
      <div class="status-item-row">
        <strong>${client.ip}</strong>
        <span class="status-pill">Client ${client.id}</span>
      </div>
      <div class="status-meta">Connected: ${client.connected_at}</div>
      <div class="status-meta">Session: ${sessionValue}</div>
      <div class="status-meta">User-Agent: ${client.user_agent || "Unknown"}</div>
    `;
    clientsList.appendChild(item);
  }

  eventsEmpty.hidden = events.length !== 0;
  eventsTableBody.innerHTML = "";
  const visibleKeys = new Set();
  for (const event of events) {
    const key = eventKey(event);
    const encodedKey = encodeURIComponent(key);
    visibleKeys.add(encodedKey);
    const payloadText = JSON.stringify(event.payload);
    const expanded = expandedPayloadRows.has(encodedKey);
    const timestamp = formatTimestampLines(event.timestamp);
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>
        <div class="status-time">
          <span class="status-time-date">${timestamp.date}</span>
          <span class="status-time-value">${timestamp.time}</span>
        </div>
      </td>
      <td>${event.ip}</td>
      <td>${event.path}</td>
      <td>
        <button class="payload-toggle" type="button" data-event-key="${encodedKey}" aria-expanded="${expanded ? "true" : "false"}" title="${expanded ? "Click to collapse payload" : "Click to expand payload"}">
          <code class="payload-preview">${payloadText}</code>
        </button>
      </td>
    `;
    eventsTableBody.appendChild(row);

    const payloadToggle = row.querySelector(".payload-toggle");
    const payloadPreview = row.querySelector(".payload-preview");
    if (payloadToggle && payloadPreview) {
      const shouldBeExpanded = expanded;
      // Always test truncation in collapsed mode so expanded rows are not misclassified.
      payloadToggle.setAttribute("aria-expanded", "false");
      const isTruncated = payloadPreview.scrollWidth > payloadPreview.clientWidth;
      payloadToggle.setAttribute("aria-expanded", shouldBeExpanded ? "true" : "false");
      if (isTruncated) {
        payloadToggle.setAttribute("data-payload-toggle", "");
        payloadToggle.title = shouldBeExpanded ? "Click to collapse payload" : "Click to expand payload";
      } else {
        expandedPayloadRows.delete(encodedKey);
        const staticPreview = document.createElement("code");
        staticPreview.className = "payload-preview payload-preview-static";
        staticPreview.textContent = payloadText;
        payloadToggle.replaceWith(staticPreview);
      }
    }
  }

  for (const key of Array.from(expandedPayloadRows)) {
    if (!visibleKeys.has(key)) {
      expandedPayloadRows.delete(key);
    }
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
      authorizedFetch("/api/status/clients"),
      authorizedFetch("/api/status/history"),
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

function startStatusSync() {
  if (!window.EventSource || eventSource) {
    return;
  }
  const params = new URLSearchParams();
  if (apiToken) {
    params.set("token", apiToken);
  }
  if (clientSessionId) {
    params.set("sessionId", clientSessionId);
  }
  const query = params.toString();
  eventSource = new EventSource(`/api/events${query ? `?${query}` : ""}`);
  eventSource.addEventListener("state-change", () => {
    fetchStatus();
  });
  eventSource.onerror = () => {};
}

fetchStatus();
startStatusSync();
setInterval(fetchStatus, 5000);