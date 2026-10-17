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
let eventSource = null;
let eventSourceRetryTimer = null;
let statePollTimer = null;
const eventSourceRetryDelayMs = 30000;
const statePollIntervalMs = 5000;
let stateFetchInFlight = false;
let stateFetchQueued = false;
// Zone changes still awaiting the backend; stale state payloads must not revert them in the UI.
const pendingZoneUpdates = new Map();
let zoneUpdateRevision = 0;
const advancedStateStorageKey = "russound-advanced-open-zones-v1";
const advancedOpenZones = loadAdvancedOpenZones();

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

function apiFetch(path, options = {}) {
  return fetch(path, { ...options, credentials: "same-origin" });
}

function inputDisplayName(input) {
  const unavailable = ["unavailable", "unmapped"].includes(input.playback_status);
  return `${input.display_name || input.name}${unavailable ? " (unavailable)" : ""}`;
}

function applyInputNames(payload) {
  const inputs = payload.resolved_inputs || payload.config?.inputs || payload.state?.inputs || [];
  state.inputs = inputs.filter((input) => input.enabled !== false);
}

async function refreshInputNames() {
  try {
    const response = await apiFetch("/api/state");
    if (!response.ok) throw new Error(`Source names unavailable (${response.status})`);
    applyInputNames(await response.json());
  } catch (error) {
    console.error("Unable to refresh playback input names", error);
    return;
  }
  for (const select of zonesContainer.querySelectorAll('select[data-action="source"]')) {
    for (const option of select.options) {
      const input = state.inputs.find((input) => input.id === Number(option.value));
      if (input) {
        option.textContent = inputDisplayName(input);
        option.title = input.playback_message || "";
      }
      for (const button of shortcutsContainer.querySelectorAll("button[data-shortcut-id]")) {
        const shortcut = state.shortcuts.find((shortcut) => shortcut.id === button.dataset.shortcutId);
        const input = state.inputs.find((input) => input.id === shortcut?.source);
        button.title = input ? inputDisplayName(input) : "";
      }
    }
  }
}

function applyPayload(payload) {
  if (payload.config_required) {
    window.location.replace("/config");
    return;
  }
  state.config = payload.config || null;
  state.configRequired = Boolean(payload.config_required);
  state.configMessage = payload.message || "";
  state.backendStatus = payload.backend_status || { connected: true, message: "" };
  state.zones = payload.state?.zones || [];
  for (const pending of pendingZoneUpdates.values()) {
    const zone = state.zones.find((candidate) => candidate.controller === pending.controllerId && candidate.zone === pending.zoneNumber);
    if (zone) {
      zone[pending.action] = pending.value;
    }
  }
  state.systemPower = Boolean(payload.state?.system_power);
  applyInputNames(payload);
  state.shortcuts = state.config?.shortcuts || [];
  render();
}

async function fetchState() {
  if (stateFetchInFlight) {
    stateFetchQueued = true;
    return;
  }
  stateFetchInFlight = true;
  const revisionAtRequest = zoneUpdateRevision;
  try {
    const response = await apiFetch("/api/state");
    if (!response.ok) {
      return;
    }
    const payload = await response.json();
    if (revisionAtRequest !== zoneUpdateRevision) {
      return;
    }
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
  if (!window.EventSource || eventSource || document.hidden) {
    return;
  }
  eventSource = new EventSource("/api/events");
  eventSource.addEventListener("playback-change", refreshInputNames);
  eventSource.addEventListener("state-change", (event) => {
    try {
      const data = JSON.parse(event.data || "{}");
      if (data && typeof data === "object" && data.payload && typeof data.payload === "object") {
        applyPayload(data.payload);
        return;
      }
    } catch {
      // Fall back to explicit state fetch when event payload cannot be parsed.
    }
    fetchState();
  });
  // Catch up on changes missed while the stream was reconnecting.
  eventSource.onopen = () => {
    fetchState();
  };
  eventSource.onerror = () => {
    // EventSource gives up for good on non-200 responses (e.g. 503 when the server stream limit is reached).
    if (eventSource && eventSource.readyState === EventSource.CLOSED) {
      stopStateSync();
      eventSourceRetryTimer = window.setTimeout(startStateSync, eventSourceRetryDelayMs);
    }
  };
}

function stopStateSync() {
  if (eventSourceRetryTimer !== null) {
    window.clearTimeout(eventSourceRetryTimer);
    eventSourceRetryTimer = null;
  }
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

function startStatePolling() {
  if (statePollTimer === null && !document.hidden) {
    statePollTimer = window.setInterval(fetchState, statePollIntervalMs);
  }
}

function stopStatePolling() {
  if (statePollTimer !== null) {
    window.clearInterval(statePollTimer);
    statePollTimer = null;
  }
}

function render() {
  document.getElementById("shortcutsHeading").hidden = state.shortcuts.length === 0;
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
    const input = state.inputs.find((input) => input.id === shortcut.source);
    button.title = input ? inputDisplayName(input) : "";
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
    const turnOnVolume = Number(zone.turn_on_volume ?? 0);
    const loudness = Boolean(zone.loudness);
    const card = document.createElement("article");
    card.className = "zone-card";
    card.dataset.zoneKey = zoneDomId;
    card.classList.toggle("is-flipped", isAdvancedOpen);
    card.innerHTML = `
      <div class="zone-card-face zone-card-front">
        <div class="zone-header">
          <h2 class="zone-name">${zone.name}</h2>
          <button class="zone-toggle ${zone.power ? "" : "is-off"}" type="button" data-action="power" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}">
            ${zone.power ? "On" : "Off"}
          </button>
        </div>
        <div class="zone-controls">
          <div class="control-row">
            <label for="source-${zoneDomId}">Source</label>
            <select id="source-${zoneDomId}" data-action="source" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}">
            </select>
          </div>
          <div class="control-row">
            <label for="volume-${zoneDomId}">Volume: <span class="label-value" data-value-output="volume">${zone.volume}%</span></label>
            <input id="volume-${zoneDomId}" type="range" min="0" max="100" step="2" value="${zone.volume}" data-action="volume" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" ${zone.power ? "" : "disabled"} />
          </div>
        </div>
        <button class="zone-advanced-toggle" type="button" data-action="toggle-advanced" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}">
          Advanced sound
        </button>
      </div>
      <div class="zone-card-face zone-card-back">
        <div class="zone-card-back-header">
          <button class="zone-advanced-toggle zone-advanced-toggle--back" type="button" data-action="toggle-advanced" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}">
            Back
          </button>
          <h3 class="zone-card-back-title">Advanced sound</h3>
        </div>
        <div class="zone-advanced-grid">
          <div class="control-row">
            <label for="turn-on-volume-${zoneDomId}">Turn-on volume: <span class="label-value" data-value-output="turn_on_volume">${turnOnVolume}%</span></label>
            <input id="turn-on-volume-${zoneDomId}" type="range" min="0" max="100" step="2" value="${turnOnVolume}" data-action="turn_on_volume" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" />
          </div>
          <div class="control-row">
            <label for="bass-${zoneDomId}">Bass: <span class="label-value" data-value-output="bass">${formatSignedValue(bass)}</span></label>
            <div class="range-with-endpoints">
              <span aria-hidden="true">-10</span>
              <input id="bass-${zoneDomId}" type="range" min="-10" max="10" step="1" value="${bass}" data-action="bass" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" />
              <span aria-hidden="true">+10</span>
            </div>
          </div>
          <div class="control-row">
            <label for="treble-${zoneDomId}">Treble: <span class="label-value" data-value-output="treble">${formatSignedValue(treble)}</span></label>
            <div class="range-with-endpoints">
              <span aria-hidden="true">-10</span>
              <input id="treble-${zoneDomId}" type="range" min="-10" max="10" step="1" value="${treble}" data-action="treble" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" />
              <span aria-hidden="true">+10</span>
            </div>
          </div>
          <div class="control-row">
            <label for="balance-${zoneDomId}">Balance: <span class="label-value" data-value-output="balance">${formatSignedValue(balance)}</span></label>
            <div class="range-with-endpoints">
              <span aria-hidden="true">L</span>
              <input id="balance-${zoneDomId}" type="range" min="-10" max="10" step="1" value="${balance}" data-action="balance" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" />
              <span aria-hidden="true">R</span>
            </div>
          </div>
          <label class="advanced-check" for="loudness-${zoneDomId}">
            <input id="loudness-${zoneDomId}" type="checkbox" data-action="loudness" data-controller-id="${zone.controller}" data-zone-number="${zone.zone}" ${loudness ? "checked" : ""} />
            <span>Loudness</span>
          </label>
        </div>
      </div>
    `;

    const sourceSelect = card.querySelector('select[data-action="source"]');
    for (const input of state.inputs) {
      const option = new Option(inputDisplayName(input), String(input.id), false, input.id === zone.source);
      option.title = input.playback_message || "";
      sourceSelect.add(option);
    }
    zonesContainer.appendChild(card);
  }
}

async function updateZone(controllerId, zoneNumber, action, value) {
  const pendingKey = `${zoneAddressKey(controllerId, zoneNumber)}-${action}`;
  const pending = { controllerId, zoneNumber, action, value };
  pendingZoneUpdates.set(pendingKey, pending);
  zoneUpdateRevision += 1;
  let payload = null;
  try {
    const response = await apiFetch(`/api/controller/${controllerId}/zone/${zoneNumber}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [action]: value }),
    });
    if (response.ok) {
      payload = await response.json();
    }
  } finally {
    if (pendingZoneUpdates.get(pendingKey) === pending) {
      pendingZoneUpdates.delete(pendingKey);
    }
    zoneUpdateRevision += 1;
  }
  if (payload) {
    applyPayload(payload);
  } else {
    fetchState();
  }
}

async function updateSharedSource(source) {
  const response = await apiFetch("/api/source", {
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
  const response = await apiFetch(`/api/shortcuts/${shortcutId}/activate`, {
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
  const response = await apiFetch("/api/system/power", {
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
  if (action === "volume" || action === "turn_on_volume" || action === "bass" || action === "treble" || action === "balance") {
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
  if (action === "turn_on_volume") {
    updateZone(controllerId, zoneNumber, "turn_on_volume", Number(event.target.value));
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
startStatePolling();

// Hidden pages (sleeping phones, background tabs) release their stream instead of leaving a dead connection behind.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopStateSync();
    stopStatePolling();
    return;
  }
  fetchState();
  startStateSync();
  startStatePolling();
});