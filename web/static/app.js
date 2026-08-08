const state = {
  config: null,
  configRequired: false,
  configMessage: "",
  zones: [],
  systemPower: false,
  inputs: [],
  shortcuts: [],
};

const zonesContainer = document.getElementById("zones");
const systemButton = document.getElementById("systemPower");
const shortcutsContainer = document.getElementById("shortcuts");
const configHint = document.getElementById("configHint");
const apiToken = document.querySelector('meta[name="russound-api-token"]')?.content || "";
let eventSource = null;
let stateFetchInFlight = false;
let stateFetchQueued = false;

function authorizedFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (apiToken) {
    headers.set("X-Russound-Api-Token", apiToken);
  }
  return fetch(path, { ...options, headers });
}

function applyPayload(payload) {
  state.config = payload.config || null;
  state.configRequired = Boolean(payload.config_required);
  state.configMessage = payload.message || "";
  state.zones = payload.state.zones || [];
  state.systemPower = payload.state.system_power;
  state.inputs = payload.state.inputs || [];
  state.shortcuts = state.config?.shortcuts || [];
  render();
}

async function fetchState() {
  if (stateFetchInFlight) {
    stateFetchQueued = true;
    return;
  }
  stateFetchInFlight = true;
  try {
    const response = await authorizedFetch("/api/state");
    if (!response.ok) {
      return;
    }
    const payload = await response.json();
    applyPayload(payload);
  } finally {
    stateFetchInFlight = false;
    if (stateFetchQueued) {
      stateFetchQueued = false;
      fetchState();
    }
  }
}

function startStateSync() {
  if (!window.EventSource || eventSource) {
    return;
  }
  const tokenQuery = apiToken ? `?token=${encodeURIComponent(apiToken)}` : "";
  eventSource = new EventSource(`/api/events${tokenQuery}`);
  eventSource.addEventListener("state-change", () => {
    fetchState();
  });
  eventSource.onerror = () => {};
}

function render() {
  if (state.configRequired) {
    systemButton.textContent = "Config required";
    systemButton.classList.add("is-off");
    systemButton.disabled = true;
    shortcutsContainer.replaceChildren(systemButton);
    zonesContainer.innerHTML = `
      <article class="empty-state">
        <p class="empty-state-eyebrow">Configuration required</p>
        <h2>Provide a Russound config file</h2>
        <p>${state.configMessage || "Copy web/config_example.json to your own config file and launch the server with --config."}</p>
      </article>
    `;
    if (configHint) {
      configHint.textContent = state.configMessage || "";
      configHint.hidden = false;
    }
    return;
  }

  systemButton.textContent = "System off";
  systemButton.classList.toggle("is-off", !state.systemPower);
  systemButton.disabled = !state.systemPower;
  if (configHint) {
    configHint.textContent = "";
    configHint.hidden = true;
  }

  shortcutsContainer.innerHTML = "";
  for (const shortcut of state.shortcuts) {
    const button = document.createElement("button");
    button.className = "shortcut-button";
    button.type = "button";
    button.textContent = shortcut.name;
    button.dataset.shortcutId = shortcut.id;
    shortcutsContainer.appendChild(button);
  }
  shortcutsContainer.appendChild(systemButton);

  zonesContainer.innerHTML = "";
  for (const zone of state.zones) {
    const zoneDomId = `${zone.controller}-${zone.zone}`;
    const card = document.createElement("article");
    card.className = "zone-card";
    card.innerHTML = `
      <div class="zone-header">
        <div>
          <h2 class="zone-name">${zone.name}</h2>
          <div class="zone-state">${zone.power ? "On" : "Off"}</div>
        </div>
        <button class="zone-toggle ${zone.power ? "" : "is-off"}" type="button" data-action="power" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}">
          ${zone.power ? "On" : "Off"}
        </button>
      </div>
      <div class="zone-controls">
        <div class="control-row">
          <label for="source-${zoneDomId}">Source</label>
          <select id="source-${zoneDomId}" data-action="source" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}">
            ${state.inputs.map((input) => `<option value="${input.id}" ${input.id === zone.source ? "selected" : ""}>${input.name}</option>`).join("")}
          </select>
        </div>
        <div class="control-row">
          <label for="volume-${zoneDomId}">Volume</label>
          <input id="volume-${zoneDomId}" type="range" min="0" max="100" step="1" value="${zone.volume}" data-action="volume" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" />
          <div class="volume-value">${zone.volume}%</div>
        </div>
      </div>
    `;
    zonesContainer.appendChild(card);
  }
}

async function updateZone(controllerId, zoneNumber, action, value) {
  const response = await authorizedFetch(`/api/controller/${controllerId}/zone/${zoneNumber}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ [action]: value }),
  });
  if (!response.ok) {
    return;
  }
  const payload = await response.json();
  applyPayload(payload);
}

async function updateSharedSource(source) {
  const response = await authorizedFetch("/api/source", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
  });
  if (!response.ok) {
    return;
  }
  const payload = await response.json();
  applyPayload(payload);
}

async function activateShortcut(shortcutId) {
  const response = await authorizedFetch(`/api/shortcuts/${shortcutId}/activate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  if (!response.ok) {
    return;
  }
  const payload = await response.json();
  applyPayload(payload);
}

async function updateSystemPower(power) {
  const response = await authorizedFetch("/api/system/power", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ power }),
  });
  if (!response.ok) {
    return;
  }
  const payload = await response.json();
  applyPayload(payload);
}

zonesContainer.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) {
    return;
  }
  const controllerId = Number(button.dataset.controllerId);
  const zoneNumber = Number(button.dataset.zoneNumber);
  const action = button.dataset.action;
  if (action === "power") {
    const zone = state.zones.find((candidate) => candidate.controller === controllerId && candidate.zone === zoneNumber);
    if (zone) {
      updateZone(controllerId, zoneNumber, "power", !zone.power);
    }
  }
});

zonesContainer.addEventListener("input", (event) => {
  const control = event.target.closest("[data-action]");
  if (!control) {
    return;
  }
  const action = control.dataset.action;
  if (action === "volume") {
    const value = Number(control.value);
    const row = control.closest(".control-row");
    const valueElement = row?.querySelector(".volume-value");
    if (valueElement) {
      valueElement.textContent = `${value}%`;
    }
  }
});

zonesContainer.addEventListener("change", (event) => {
  const control = event.target.closest("[data-action]");
  if (!control) {
    return;
  }
  const controllerId = Number(control.dataset.controllerId);
  const zoneNumber = Number(control.dataset.zoneNumber);
  const action = control.dataset.action;
  if (action === "source") {
    updateZone(controllerId, zoneNumber, "source", Number(event.target.value));
    return;
  }
  if (action === "volume") {
    updateZone(controllerId, zoneNumber, "volume", Number(event.target.value));
  }
});

shortcutsContainer.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-shortcut-id]");
  if (!button) {
    return;
  }
  activateShortcut(button.dataset.shortcutId);
});

systemButton.addEventListener("click", () => {
  updateSystemPower(false);
});

fetchState();
startStateSync();
setInterval(fetchState, 5000);