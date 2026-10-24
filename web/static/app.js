const state = {
  view: null,
  lastRevision: null,
  lastEpoch: null,
};

const zonesContainer = document.getElementById("zones");
const systemButton = document.getElementById("systemPower");
const shortcutsContainer = document.getElementById("shortcuts");
const shortcutsHeading = document.getElementById("shortcutsHeading");
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
// Cards are kept across updates so open dropdowns, slider drags and taps survive live state changes.
const zoneCards = new Map();
// Controls the user is operating; live updates leave their value alone until the interaction ends.
const engagedControls = new Set();
let backendBanner = null;
let emptyState = null;
let shortcutsSignature = null;
let lastRenderSignature = null;

function zoneAddressKey(controllerId, zoneNumber) {
  return `${controllerId}-${zoneNumber}`;
}

function formatControlValue(value) {
  return `${value}%`;
}

function apiFetch(path, options = {}) {
  return fetch(path, { ...options, credentials: "same-origin" });
}

function inputDisplayName(input) {
  const unavailable = ["unavailable", "unmapped"].includes(input.playback_status);
  return `${input.display_name || input.name}${unavailable ? " (unavailable)" : ""}`;
}

// Projects a state payload onto the fields the Rooms page uses (the planned Rooms snapshot allowlist).
function toRoomsView(payload) {
  const inputs = payload.resolved_inputs || payload.config?.inputs || payload.state?.inputs || [];
  const backendStatus = payload.backend_status || { connected: true, message: "" };
  return {
    configRequired: Boolean(payload.config_required),
    message: payload.message || "",
    backendStatus: { connected: backendStatus.connected !== false, message: backendStatus.message || "" },
    systemPower: Boolean(payload.state?.system_power),
    zones: (payload.state?.zones || []).map((zone) => ({
      controller: zone.controller,
      zone: zone.zone,
      name: zone.name,
      power: Boolean(zone.power),
      source: zone.source,
      volume: zone.volume,
    })),
    inputs: inputs.filter((input) => input.enabled !== false).map((input) => ({
      id: input.id,
      name: input.name,
      display_name: input.display_name,
      playback_status: input.playback_status,
      playback_message: input.playback_message,
    })),
    shortcuts: (payload.config?.shortcuts || []).map((shortcut) => ({
      id: shortcut.id,
      name: shortcut.name,
      source: shortcut.source,
    })),
  };
}

function isStalePayload(payload) {
  if (!Number.isInteger(payload.revision)) {
    return false;
  }
  if (payload.epoch !== state.lastEpoch) {
    state.lastEpoch = payload.epoch;
  } else if (payload.revision < state.lastRevision) {
    return true;
  }
  state.lastRevision = payload.revision;
  return false;
}

function applyPendingZoneUpdates(view) {
  for (const pending of pendingZoneUpdates.values()) {
    const zone = view.zones.find((candidate) => candidate.controller === pending.controllerId && candidate.zone === pending.zoneNumber);
    if (zone) {
      zone[pending.action] = pending.value;
    }
  }
}

function applyPayload(payload) {
  if (!payload || typeof payload !== "object") {
    return;
  }
  if (payload.config_required) {
    window.location.replace("/config");
    return;
  }
  if (isStalePayload(payload)) {
    return;
  }
  const view = toRoomsView(payload);
  applyPendingZoneUpdates(view);
  state.view = view;
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
  // Playback availability changes the resolved source names, which are part of the state payload.
  eventSource.addEventListener("playback-change", () => fetchState());
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

function setText(element, text) {
  if (element.textContent !== text) {
    element.textContent = text;
  }
}

function createElement(tagName, className, attributes = {}) {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  for (const [name, value] of Object.entries(attributes)) {
    element.setAttribute(name, value);
  }
  return element;
}

function renderBackendBanner(status) {
  if (!appShell) {
    return;
  }
  if (status.connected) {
    backendBanner?.remove();
    backendBanner = null;
    return;
  }
  if (!backendBanner) {
    backendBanner = createElement("div", "backend-warning-banner");
    const title = createElement("strong");
    title.textContent = "Hardware communication unavailable";
    backendBanner.append(title, createElement("span"));
    appShell.insertBefore(backendBanner, appShell.firstChild);
  }
  setText(backendBanner.lastChild, status.message || "Communication with Russound hardware is currently unavailable.");
}

function renderShortcuts(view) {
  shortcutsHeading.hidden = view.shortcuts.length === 0;
  const signature = JSON.stringify(view.shortcuts.map((shortcut) => [shortcut.id, shortcut.name]));
  if (signature !== shortcutsSignature) {
    shortcutsSignature = signature;
    for (const button of Array.from(shortcutsContainer.querySelectorAll("button[data-shortcut-id]"))) {
      button.remove();
    }
    if (systemButton.parentNode !== shortcutsContainer) {
      shortcutsContainer.appendChild(systemButton);
    }
    for (const shortcut of view.shortcuts) {
      const button = createElement("button", "shortcut-button", { type: "button" });
      button.textContent = shortcut.name;
      button.dataset.shortcutId = shortcut.id;
      shortcutsContainer.insertBefore(button, systemButton);
    }
  }
  for (const button of shortcutsContainer.querySelectorAll("button[data-shortcut-id]")) {
    const shortcut = view.shortcuts.find((candidate) => candidate.id === button.dataset.shortcutId);
    const input = view.inputs.find((candidate) => candidate.id === shortcut?.source);
    const title = input ? inputDisplayName(input) : "";
    if (button.title !== title) {
      button.title = title;
    }
  }
}

function renderSystemButton(text, isOff, disabled) {
  setText(systemButton, text);
  systemButton.classList.toggle("is-off", isOff);
  if (systemButton.disabled !== disabled) {
    systemButton.disabled = disabled;
  }
}

function renderConfigRequired(view) {
  renderSystemButton("Config required", true, true);
  for (const button of Array.from(shortcutsContainer.querySelectorAll("button[data-shortcut-id]"))) {
    button.remove();
  }
  shortcutsSignature = null;
  for (const refs of zoneCards.values()) {
    refs.card.remove();
  }
  zoneCards.clear();
  if (!emptyState) {
    emptyState = createElement("article", "empty-state");
    const eyebrow = createElement("p", "empty-state-eyebrow");
    eyebrow.textContent = "Configuration required";
    const title = createElement("h2");
    title.textContent = "Provide a Russound config file";
    emptyState.append(eyebrow, title, createElement("p"));
    zonesContainer.appendChild(emptyState);
  }
  setText(emptyState.lastChild, view.message || "Copy web/config_example.json to your own config file and launch the server with --config.");
  if (configHint) {
    setText(configHint, view.message);
    configHint.hidden = false;
  }
}

function createZoneCard(zone) {
  const zoneDomId = zoneAddressKey(zone.controller, zone.zone);
  const address = { "data-controller-id": String(zone.controller), "data-zone-number": String(zone.zone) };
  const card = createElement("article", "zone-card");
  const header = createElement("div", "zone-header");
  const name = createElement("h2", "zone-name");
  const power = createElement("button", "zone-toggle", { type: "button", "data-action": "power", ...address });
  header.append(name, power);

  const controls = createElement("div", "zone-controls");
  const sourceRow = createElement("div", "control-row");
  const sourceLabel = createElement("label", "", { for: `source-${zoneDomId}` });
  sourceLabel.textContent = "Source";
  const select = createElement("select", "", { id: `source-${zoneDomId}`, "data-action": "source", ...address });
  sourceRow.append(sourceLabel, select);

  const volumeRow = createElement("div", "control-row");
  const volumeLabel = createElement("label", "", { for: `volume-${zoneDomId}` });
  const volumeValue = createElement("span", "label-value", { "data-value-output": "volume" });
  volumeLabel.append("Volume: ", volumeValue);
  const volume = createElement("input", "", {
    id: `volume-${zoneDomId}`, type: "range", min: "0", max: "100", step: "2", "data-action": "volume", ...address,
  });
  volumeRow.append(volumeLabel, volume);
  controls.append(sourceRow, volumeRow);
  card.append(header, controls);
  return { card, name, power, select, volume, volumeValue, inputsSignature: null };
}

function updateSourceOptions(refs, inputs, inputsSignature) {
  if (refs.inputsSignature === inputsSignature) {
    return;
  }
  refs.inputsSignature = inputsSignature;
  const options = inputs.map((input) => {
    const option = new Option(inputDisplayName(input), String(input.id));
    option.title = input.playback_message || "";
    return option;
  });
  refs.select.replaceChildren(...options);
}

function updateZoneCard(refs, zone, view, inputsSignature) {
  setText(refs.name, zone.name);
  setText(refs.power, zone.power ? "On" : "Off");
  refs.power.classList.toggle("is-off", !zone.power);

  if (!engagedControls.has(refs.select)) {
    updateSourceOptions(refs, view.inputs, inputsSignature);
    const sourceIndex = view.inputs.findIndex((input) => input.id === zone.source);
    // Unknown sources fall back to the first option, as a freshly built select would.
    const selectedIndex = sourceIndex >= 0 ? sourceIndex : refs.select.options.length > 0 ? 0 : -1;
    if (refs.select.selectedIndex !== selectedIndex) {
      refs.select.selectedIndex = selectedIndex;
    }
  }

  if (refs.volume.disabled !== !zone.power) {
    refs.volume.disabled = !zone.power;
  }
  if (!engagedControls.has(refs.volume)) {
    const volume = String(zone.volume);
    if (refs.volume.value !== volume) {
      refs.volume.value = volume;
    }
    setText(refs.volumeValue, formatControlValue(zone.volume));
  }
}

function renderZones(view) {
  emptyState?.remove();
  emptyState = null;
  const inputsSignature = JSON.stringify(view.inputs.map((input) => [input.id, inputDisplayName(input), input.playback_message || ""]));
  const presentKeys = new Set();
  let previousCard = null;
  for (const zone of view.zones) {
    const key = zoneAddressKey(zone.controller, zone.zone);
    presentKeys.add(key);
    let refs = zoneCards.get(key);
    if (!refs) {
      refs = createZoneCard(zone);
      zoneCards.set(key, refs);
    }
    updateZoneCard(refs, zone, view, inputsSignature);
    // Only move cards whose position changed; moving a card would still close its open dropdown.
    const expected = previousCard ? previousCard.nextSibling : zonesContainer.firstChild;
    if (refs.card !== expected) {
      zonesContainer.insertBefore(refs.card, expected);
    }
    previousCard = refs.card;
  }
  for (const [key, refs] of zoneCards) {
    if (!presentKeys.has(key)) {
      engagedControls.delete(refs.select);
      engagedControls.delete(refs.volume);
      refs.card.remove();
      zoneCards.delete(key);
    }
  }
}

function render({ force = false } = {}) {
  const view = state.view;
  if (!view) {
    return;
  }
  const signature = JSON.stringify(view);
  if (!force && signature === lastRenderSignature) {
    return;
  }
  lastRenderSignature = signature;
  renderBackendBanner(view.backendStatus);

  if (view.configRequired) {
    renderConfigRequired(view);
    return;
  }

  renderSystemButton("System off", !view.systemPower, !view.systemPower);
  if (configHint) {
    setText(configHint, "");
    configHint.hidden = true;
  }
  renderShortcuts(view);
  renderZones(view);
}

function engageControl(control) {
  if (control && ["source", "volume"].includes(control.dataset.action)) {
    engagedControls.add(control);
  }
}

function releaseControl(control) {
  if (control && engagedControls.delete(control)) {
    // Catch up on updates that were held back while the control was in use.
    render({ force: true });
  }
}

async function updateZone(controllerId, zoneNumber, action, value) {
  const pendingKey = `${zoneAddressKey(controllerId, zoneNumber)}-${action}`;
  const pending = { controllerId, zoneNumber, action, value };
  pendingZoneUpdates.set(pendingKey, pending);
  zoneUpdateRevision += 1;
  if (state.view) {
    applyPendingZoneUpdates(state.view);
    render();
  }
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
  if (action === "power") {
    const zone = state.view?.zones.find((candidate) => candidate.controller === controllerId && candidate.zone === zoneNumber);
    if (zone) {
      updateZone(controllerId, zoneNumber, "power", !zone.power);
    }
  }
});

zonesContainer.addEventListener("pointerdown", (event) => {
  engageControl(event.target.closest("[data-action]"));
});

zonesContainer.addEventListener("keydown", (event) => {
  engageControl(event.target.closest("[data-action]"));
});

// A slider click that does not change the value fires no change event.
for (const type of ["pointerup", "pointercancel"]) {
  zonesContainer.addEventListener(type, (event) => {
    const control = event.target.closest('[data-action="volume"]');
    if (control) {
      window.setTimeout(() => releaseControl(control), 0);
    }
  });
}

zonesContainer.addEventListener("focusout", (event) => {
  releaseControl(event.target.closest("[data-action]"));
});

zonesContainer.addEventListener("input", (event) => {
  const control = event.target.closest("[data-action]");
  if (!control) {
    return;
  }
  const action = control.dataset.action;
  if (action === "volume") {
    engageControl(control);
    const value = Number(control.value);
    const row = control.closest(".control-row");
    const valueElement = row?.querySelector("[data-value-output]");
    if (valueElement) {
      valueElement.textContent = formatControlValue(value);
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
    releaseControl(control);
    return;
  }
  if (action === "volume") {
    updateZone(controllerId, zoneNumber, "volume", Number(event.target.value));
    releaseControl(control);
    return;
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