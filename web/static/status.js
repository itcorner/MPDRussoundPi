const apiToken = document.querySelector('meta[name="russound-api-token"]')?.content || "";
const clientCount = document.getElementById("clientCount");
const clientsList = document.getElementById("clientsList");
const clientsEmpty = document.getElementById("clientsEmpty");
const eventsEmpty = document.getElementById("eventsEmpty");
const eventsTableBody = document.getElementById("eventsTableBody");

let eventSource = null;
let statusFetchInFlight = false;
let statusFetchQueued = false;

function authorizedFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (apiToken) {
    headers.set("X-Russound-Api-Token", apiToken);
  }
  return fetch(path, { ...options, headers });
}

function renderStatus(payload) {
  const clients = payload.connected_clients || [];
  const events = payload.recent_events || [];

  clientCount.textContent = String(clients.length);
  clientsEmpty.hidden = clients.length !== 0;
  clientsList.innerHTML = "";

  for (const client of clients) {
    const item = document.createElement("article");
    item.className = "status-list-item";
    item.innerHTML = `
      <div class="status-item-row">
        <strong>${client.ip}</strong>
        <span class="status-pill">Client ${client.id}</span>
      </div>
      <div class="status-meta">Connected: ${client.connected_at}</div>
      <div class="status-meta">User-Agent: ${client.user_agent || "Unknown"}</div>
    `;
    clientsList.appendChild(item);
  }

  eventsEmpty.hidden = events.length !== 0;
  eventsTableBody.innerHTML = "";
  for (const event of events) {
    const row = document.createElement("tr");
    row.innerHTML = `
      <td>${event.timestamp}</td>
      <td>${event.ip}</td>
      <td>${event.path}</td>
      <td><code>${JSON.stringify(event.payload)}</code></td>
    `;
    eventsTableBody.appendChild(row);
  }
}

async function fetchStatus() {
  if (statusFetchInFlight) {
    statusFetchQueued = true;
    return;
  }
  statusFetchInFlight = true;
  try {
    const response = await authorizedFetch("/api/status");
    if (!response.ok) {
      return;
    }
    const payload = await response.json();
    renderStatus(payload);
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
  const tokenQuery = apiToken ? `?token=${encodeURIComponent(apiToken)}` : "";
  eventSource = new EventSource(`/api/events${tokenQuery}`);
  eventSource.addEventListener("state-change", () => {
    fetchStatus();
  });
  eventSource.onerror = () => {};
}

fetchStatus();
startStatusSync();
setInterval(fetchStatus, 5000);