const apiToken = document.querySelector('meta[name="russound-api-token"]')?.content || "";
const saveConfigButton = document.getElementById("saveConfigButton");
const configEditorMessage = document.getElementById("configEditorMessage");
const configEmptyState = document.getElementById("configEmptyState");
const configEmptyMessage = document.getElementById("configEmptyMessage");
const controllerZones = document.getElementById("controllerZones");
const sourceConfigPanel = document.getElementById("sourceConfigPanel");
const sourceCountBadge = document.getElementById("sourceCountBadge");
const configSources = document.getElementById("configSources");
const sessionStorageKey = "russound-session-id";

let configPayload = null;
let saveInFlight = false;
let baselineZoneSlotsHash = "";
let baselineSourceSlotsHash = "";
let hasUnsavedChanges = false;

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

function setMessage(text, isError = false) {
  if (!text) {
    configEditorMessage.hidden = true;
    configEditorMessage.textContent = "";
    configEditorMessage.classList.remove("is-error", "is-success");
    return;
  }
  configEditorMessage.hidden = false;
  configEditorMessage.textContent = text;
  configEditorMessage.classList.toggle("is-error", isError);
  configEditorMessage.classList.toggle("is-success", !isError);
}

function groupSlotsByController(zoneSlots) {
  const groups = new Map();
  for (const slot of zoneSlots) {
    const controllerId = slot.controller;
    if (!groups.has(controllerId)) {
      groups.set(controllerId, []);
    }
    groups.get(controllerId).push(slot);
  }
  return groups;
}

function normalizeZoneSlots(zoneSlots) {
  return zoneSlots
    .map((slot) => ({
      controller: Number(slot.controller),
      zone: Number(slot.zone),
      enabled: Boolean(slot.enabled),
      visible: Boolean(slot.visible),
      name: String(slot.name ?? ""),
    }))
    .sort((a, b) => {
      if (a.controller !== b.controller) {
        return a.controller - b.controller;
      }
      return a.zone - b.zone;
    });
}

function hashZoneSlots(zoneSlots) {
  return JSON.stringify(normalizeZoneSlots(zoneSlots));
}

function normalizeSourceSlots(sourceSlots) {
  return sourceSlots
    .map((slot) => ({
      id: Number(slot.id),
      name: String(slot.name ?? ""),
    }))
    .sort((a, b) => a.id - b.id);
}

function hashSourceSlots(sourceSlots) {
  return JSON.stringify(normalizeSourceSlots(sourceSlots));
}

function updateSaveButtonState() {
  if (saveInFlight || configPayload?.config_required) {
    saveConfigButton.disabled = true;
    return;
  }
  saveConfigButton.disabled = !hasUnsavedChanges;
}

function recomputeUnsavedChanges() {
  if (configPayload?.config_required) {
    hasUnsavedChanges = false;
    updateSaveButtonState();
    return;
  }

  const currentHash = hashZoneSlots(collectZoneSlots());
  const currentSourceHash = hashSourceSlots(collectSourceSlots());
  hasUnsavedChanges = currentHash !== baselineZoneSlotsHash || currentSourceHash !== baselineSourceSlotsHash;
  updateSaveButtonState();
}

function renderEditor(payload) {
  configPayload = payload;
  controllerZones.innerHTML = "";
  configSources.innerHTML = "";
  baselineZoneSlotsHash = hashZoneSlots(payload.zone_slots || []);
  baselineSourceSlotsHash = hashSourceSlots(payload.source_slots || []);
  hasUnsavedChanges = false;

  if (payload.config_required) {
    configEmptyState.hidden = false;
    controllerZones.hidden = true;
    sourceConfigPanel.hidden = true;
    updateSaveButtonState();
    configEmptyMessage.textContent = payload.message || "A config file is required.";
    return;
  }

  configEmptyState.hidden = true;
  controllerZones.hidden = false;
  sourceConfigPanel.hidden = false;
  updateSaveButtonState();

  const sourceSlots = payload.source_slots || [];
  sourceCountBadge.textContent = `${sourceSlots.length} available`;
  for (const sourceSlot of sourceSlots) {
    const row = document.createElement("div");
    row.className = "config-source-row";
    row.dataset.sourceId = String(sourceSlot.id);
    row.innerHTML = `
      <div class="config-slot-meta">
        <strong>Source ${sourceSlot.id}</strong>
      </div>
      <label class="field config-name-field">
        <span>Name</span>
        <input type="text" data-field="source-name" value="${sourceSlot.name}" />
      </label>
    `;
    configSources.appendChild(row);
  }

  const zoneSlots = payload.zone_slots || [];
  const groupedSlots = groupSlotsByController(zoneSlots);

  for (const controller of payload.config?.controllers || []) {
    const controllerSlots = groupedSlots.get(controller.id) || [];
    const enabledZones = controllerSlots.filter((slot) => slot.enabled).length;

    const card = document.createElement("details");
    card.className = "panel status-panel collapsible-panel controller-panel";
    card.open = true;
    card.innerHTML = `
      <summary class="collapsible-summary">
        <span class="section-title">Controller ${controller.id}</span>
        <span class="status-count">${enabledZones}/${controller.zone_count} zones enabled</span>
      </summary>
      <div class="collapsible-body">
        <div class="config-slot-list"></div>
      </div>
    `;
    const slotList = card.querySelector(".config-slot-list");
    for (const slot of controllerSlots) {
      const row = document.createElement("div");
      row.className = "config-slot-row";
      row.dataset.controller = String(slot.controller);
      row.dataset.zone = String(slot.zone);
      row.innerHTML = `
        <div class="config-slot-meta">
          <strong>Zone ${slot.zone}</strong>
        </div>
        <label class="config-check">
          <input type="checkbox" data-field="enabled" ${slot.enabled ? "checked" : ""} />
          <span>Enable</span>
        </label>
        <label class="config-check">
          <input type="checkbox" data-field="visible" ${slot.visible ? "checked" : ""} />
          <span>Show in overview</span>
        </label>
        <label class="field config-name-field">
          <span>Name</span>
          <input type="text" data-field="name" value="${slot.name}" />
        </label>
      `;
      slotList.appendChild(row);
    }
    controllerZones.appendChild(card);
  }
}

function collectZoneSlots() {
  return Array.from(document.querySelectorAll(".config-slot-row")).map((row) => ({
    controller: Number(row.dataset.controller),
    zone: Number(row.dataset.zone),
    enabled: row.querySelector('[data-field="enabled"]').checked,
    visible: row.querySelector('[data-field="visible"]').checked,
    name: row.querySelector('[data-field="name"]').value,
  }));
}

function collectSourceSlots() {
  return Array.from(document.querySelectorAll(".config-source-row")).map((row) => ({
    id: Number(row.dataset.sourceId),
    name: row.querySelector('[data-field="source-name"]').value,
  }));
}

async function fetchConfigEditor() {
  const response = await authorizedFetch("/api/config");
  if (!response.ok) {
    setMessage("Failed to load configuration.", true);
    return;
  }
  const payload = await response.json();
  renderEditor(payload);
}

async function saveConfig() {
  if (saveInFlight || !hasUnsavedChanges) {
    return;
  }
  saveInFlight = true;
  updateSaveButtonState();
  setMessage("");
  try {
    const response = await authorizedFetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        zone_slots: collectZoneSlots(),
        source_slots: collectSourceSlots(),
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setMessage(payload.error || "Failed to save configuration.", true);
      return;
    }
    renderEditor(payload);
    setMessage("Configuration saved.");
  } finally {
    saveInFlight = false;
    updateSaveButtonState();
  }
}

function hasRelevantTarget(target) {
  if (!(target instanceof Element)) {
    return false;
  }
  return Boolean(target.closest(".config-slot-row, .config-source-row"));
}

function hasDirtyUnsavedState() {
  return hasUnsavedChanges && !saveInFlight;
}

function confirmDiscardUnsavedChanges() {
  return window.confirm("You have unsaved configuration changes. Leave without saving?");
}

function isModifiedPrimaryClick(event) {
  return event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
}

function handleNavigationGuard(event) {
  if (!hasDirtyUnsavedState() || !(event.target instanceof Element)) {
    return;
  }

  const link = event.target.closest("a[href]");
  if (!link || link.target === "_blank" || isModifiedPrimaryClick(event)) {
    return;
  }

  const href = link.getAttribute("href");
  if (!href || href.startsWith("#")) {
    return;
  }

  const currentUrl = new URL(window.location.href);
  const destinationUrl = new URL(href, window.location.href);
  const isSameDocument =
    destinationUrl.pathname === currentUrl.pathname &&
    destinationUrl.search === currentUrl.search &&
    destinationUrl.hash === currentUrl.hash;
  if (isSameDocument) {
    return;
  }

  if (!confirmDiscardUnsavedChanges()) {
    event.preventDefault();
  }
}

function handleEditorInteraction(event) {
  if (!hasRelevantTarget(event.target)) {
    return;
  }
  recomputeUnsavedChanges();
}

window.addEventListener("beforeunload", (event) => {
  if (!hasUnsavedChanges || saveInFlight) {
    return;
  }
  event.preventDefault();
  event.returnValue = "";
});

saveConfigButton.addEventListener("click", () => {
  saveConfig();
});

controllerZones.addEventListener("input", handleEditorInteraction);
controllerZones.addEventListener("change", handleEditorInteraction);
configSources.addEventListener("input", handleEditorInteraction);
configSources.addEventListener("change", handleEditorInteraction);
document.addEventListener("click", handleNavigationGuard);

fetchConfigEditor();