const state = {
  config: null,
  configRequired: false,
  configMessage: "",
  backendStatus: { connected: true, message: "" },
  zones: [],
  systemPower: false,
  inputs: [],
  shortcuts: [],
};

const zonesContainer = document.getElementById("zones");
const systemButton = document.getElementById("systemPower");
const shortcutsContainer = document.getElementById("shortcuts");
const configHint = document.getElementById("configHint");
const appShell = document.querySelector("main.app-shell");
const apiToken = document.querySelector('meta[name="russound-api-token"]')?.content || "";
let eventSource = null;
let stateFetchInFlight = false;
let stateFetchQueued = false;
const advancedStateStorageKey = "russound-advanced-open-zones-v1";
const advancedOpenZones = loadAdvancedOpenZones();
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

function zoneAddressKey(controllerId, zoneNumber) {
  return `${controllerId}-${zoneNumber}`;
}

function loadAdvancedOpenZones() {
  try {
    const savedState = window.localStorage.getItem(advancedStateStorageKey);
    if (!savedState) {
      return new Set();
    }
    const parsed = JSON.parse(savedState);
    if (!Array.isArray(parsed)) {
      return new Set();
    }
    return new Set(parsed.filter((entry) => typeof entry === "string"));
  } catch {
    return new Set();
  }
}

function saveAdvancedOpenZones() {
  try {
    window.localStorage.setItem(advancedStateStorageKey, JSON.stringify(Array.from(advancedOpenZones)));
  } catch {
    // Ignore storage errors so UI interactions still work in restricted environments.
  }
}

function syncAdvancedPanelState(zoneKey, isOpen) {
  if (!zoneKey) {
    return;
  }
  if (isOpen) {
    advancedOpenZones.add(zoneKey);
  } else {
    advancedOpenZones.delete(zoneKey);
  }
  saveAdvancedOpenZones();
}

function formatSignedValue(value) {
  const numericValue = Number(value);
  if (!Number.isFinite(numericValue) || numericValue === 0) {
    return "0";
  }
  return numericValue > 0 ? `+${numericValue}` : String(numericValue);
}

function formatControlValue(action, value) {
  if (action === "volume") {
    return `${value}%`;
  }
  if (action === "bass" || action === "treble" || action === "balance") {
    return formatSignedValue(value);
  }
  return String(value);
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

function applyPayload(payload) {
  state.config = payload.config || null;
  state.configRequired = Boolean(payload.config_required);
  state.configMessage = payload.message || "";
  state.backendStatus = payload.backend_status || { connected: true, message: "" };
  state.zones = payload.state?.zones || [];
  state.systemPower = Boolean(payload.state?.system_power);
  state.inputs = state.config?.inputs || payload.state?.inputs || [];
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
    fetchState();
  });
  eventSource.onerror = () => {};
}

function render() {
  if (appShell) {
    let banner = appShell.querySelector(".backend-warning-banner");
    if (!state.backendStatus.connected) {
      if (!banner) {
        banner = document.createElement("div");
        banner.className = "backend-warning-banner";
        appShell.insertBefore(banner, appShell.firstChild);
      }
      banner.innerHTML = `<strong>Hardware communication unavailable</strong><span>${state.backendStatus.message || "Communication with Russound hardware is currently unavailable."}</span>`;
    } else if (banner) {
      banner.remove();
    }
  }

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
    const zoneDomId = zoneAddressKey(zone.controller, zone.zone);
    const isAdvancedOpen = advancedOpenZones.has(zoneDomId);
    const bass = Number(zone.bass ?? 0);
    const treble = Number(zone.treble ?? 0);
    const balance = Number(zone.balance ?? 0);
    const loudness = Boolean(zone.loudness);
    const card = document.createElement("article");
    card.className = "zone-card";
    card.dataset.zoneKey = zoneDomId;
    card.classList.toggle("is-flipped", isAdvancedOpen);
    card.innerHTML = `
      <div class="zone-card-face zone-card-front">
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
            <label for="volume-${zoneDomId}">Volume: <span class="label-value" data-value-output="volume">${zone.volume}%</span></label>
            <input id="volume-${zoneDomId}" type="range" min="0" max="100" step="1" value="${zone.volume}" data-action="volume" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" />
          </div>
        </div>
        <button class="zone-advanced-toggle" type="button" data-action="toggle-advanced" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}">
          Advanced sound
        </button>
      </div>
      <div class="zone-card-face zone-card-back">
        <div class="zone-card-back-header">
          <button class="zone-advanced-toggle zone-advanced-toggle--back" type="button" data-action="toggle-advanced" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}">
            Back to normal
          </button>
          <h3 class="zone-card-back-title">Advanced sound</h3>
        </div>
        <div class="zone-advanced-grid">
          <div class="control-row">
            <label for="bass-${zoneDomId}">Bass: <span class="label-value" data-value-output="bass">${formatSignedValue(bass)}</span></label>
            <input id="bass-${zoneDomId}" type="range" min="-10" max="10" step="1" value="${bass}" data-action="bass" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" />
            <div class="range-endpoints" aria-hidden="true">
              <span>-10</span>
              <span>+10</span>
            </div>
          </div>
          <div class="control-row">
            <label for="treble-${zoneDomId}">Treble: <span class="label-value" data-value-output="treble">${formatSignedValue(treble)}</span></label>
            <input id="treble-${zoneDomId}" type="range" min="-10" max="10" step="1" value="${treble}" data-action="treble" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" />
            <div class="range-endpoints" aria-hidden="true">
              <span>-10</span>
              <span>+10</span>
            </div>
          </div>
          <div class="control-row">
            <label for="balance-${zoneDomId}">Balance: <span class="label-value" data-value-output="balance">${formatSignedValue(balance)}</span></label>
            <input id="balance-${zoneDomId}" type="range" min="-10" max="10" step="1" value="${balance}" data-action="balance" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" />
            <div class="range-endpoints" aria-hidden="true">
              <span>Left</span>
              <span>Right</span>
            </div>
          </div>
          <label class="advanced-check" for="loudness-${zoneDomId}">
            <input id="loudness-${zoneDomId}" type="checkbox" data-action="loudness" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" ${loudness ? "checked" : ""} />
            <span>Loudness</span>
          </label>
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
  if (action === "toggle-advanced") {
    const zoneCard = button.closest(".zone-card");
    if (!zoneCard) {
      return;
    }
    const zoneKey = zoneCard.dataset.zoneKey;
    const nextOpenState = !zoneCard.classList.contains("is-flipped");
    zoneCard.classList.toggle("is-flipped", nextOpenState);
    syncAdvancedPanelState(zoneKey, nextOpenState);
    return;
  }
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
  if (action === "volume" || action === "bass" || action === "treble" || action === "balance") {
    const value = Number(control.value);
    const row = control.closest(".control-row");
    const valueElement = row?.querySelector("[data-value-output]");
    if (valueElement) {
      valueElement.textContent = formatControlValue(action, value);
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
    return;
  }
  if (action === "bass") {
    updateZone(controllerId, zoneNumber, "bass", Number(event.target.value));
    return;
  }
  if (action === "treble") {
    updateZone(controllerId, zoneNumber, "treble", Number(event.target.value));
    return;
  }
  if (action === "balance") {
    updateZone(controllerId, zoneNumber, "balance", Number(event.target.value));
    return;
  }
  if (action === "loudness") {
    updateZone(controllerId, zoneNumber, "loudness", Boolean(event.target.checked));
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