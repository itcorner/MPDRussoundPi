const saveConfigButton = document.getElementById("saveConfigButton");
const configEditorMessage = document.getElementById("configEditorMessage");
const configEmptyState = document.getElementById("configEmptyState");
const configEmptyMessage = document.getElementById("configEmptyMessage");
const controllerZones = document.getElementById("controllerZones");
const sourceConfigPanel = document.getElementById("sourceConfigPanel");
const sourceCountBadge = document.getElementById("sourceCountBadge");
const configSources = document.getElementById("configSources");
const controllerConfig = document.getElementById("controllerConfig");
const connectionConfig = document.getElementById("connectionConfig");
const backendMode = document.getElementById("backendMode");
const backendHost = document.getElementById("backendHost");
const backendPort = document.getElementById("backendPort");
const backendDevice = document.getElementById("backendDevice");
const backendBaud = document.getElementById("backendBaud");
const backendPollInterval = document.getElementById("backendPollInterval");
const backendConnectionStatus = document.getElementById("backendConnectionStatus");
const backendSummaryStatus = document.getElementById("backendSummaryStatus");
const backendOverrides = document.getElementById("backendOverrides");
const configShortcuts = document.getElementById("configShortcuts");
const shortcutCountBadge = document.getElementById("shortcutCountBadge");
const addShortcutButton = document.getElementById("addShortcutButton");

let configPayload = null;
let saveInFlight = false;
let baselineZoneSlotsHash = "";
let baselineSourceSlotsHash = "";
let hasUnsavedChanges = false;
let baselineConnectionHash = "";
let statusInFlight = false;
let baselineShortcutsHash = "";
const draftZones = new Map();

function collectShortcuts() {
  return Array.from(configShortcuts.querySelectorAll(".shortcut-editor")).map((editor) => ({
    ...JSON.parse(editor.dataset.shortcut),
    name: editor.querySelector('[data-field="shortcut-name"]').value,
    source: Number(editor.querySelector('[data-field="shortcut-source"]').value),
    zone_addresses: Array.from(editor.querySelectorAll('[data-field="shortcut-zone"]:checked')).map((checkbox) => ({
      controller: Number(checkbox.dataset.controller), zone: Number(checkbox.dataset.zone),
    })),
  }));
}

function renderShortcuts(shortcuts) {
  configShortcuts.replaceChildren();
  const sources = collectSourceSlots().filter((source) => source.enabled);
  const zones = collectZoneSlots().filter((zone) => zone.enabled);
  for (const shortcut of shortcuts) {
    const editor = document.createElement("article");
    editor.className = "shortcut-editor";
    editor.dataset.shortcut = JSON.stringify(shortcut);
    const fields = document.createElement("div");
    fields.className = "shortcut-fields";
    const nameLabel = document.createElement("label");
    nameLabel.className = "field config-name-field";
    nameLabel.textContent = "Name";
    const name = document.createElement("input");
    name.type = "text";
    name.dataset.field = "shortcut-name";
    name.value = shortcut.name || "";
    nameLabel.appendChild(name);
    fields.appendChild(nameLabel);
    const sourceLabel = document.createElement("label");
    sourceLabel.className = "field";
    sourceLabel.textContent = "Source";
    const select = document.createElement("select");
    select.dataset.field = "shortcut-source";
    select.add(new Option("Select source", "0"));
    for (const source of sources) select.add(new Option(source.name || `Source ${source.id}`, String(source.id)));
    if (shortcut.source && !sources.some((source) => source.id === shortcut.source)) {
      select.add(new Option(`Source ${shortcut.source} (disabled)`, String(shortcut.source)));
    }
    select.value = String(shortcut.source || 0);
    sourceLabel.appendChild(select);
    fields.appendChild(sourceLabel);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "power-button";
    remove.textContent = "Remove";
    remove.title = "Remove shortcut";
    remove.addEventListener("click", () => {
      editor.remove();
      shortcutCountBadge.textContent = `${collectShortcuts().length} shortcuts`;
      shortcutCountBadge.hidden = collectShortcuts().length === 0;
      recomputeUnsavedChanges();
    });
    fields.appendChild(remove);
    editor.appendChild(fields);
    const targets = document.createElement("fieldset");
    targets.className = "shortcut-targets";
    const legend = document.createElement("legend");
    legend.textContent = "Zones";
    targets.appendChild(legend);
    const addresses = shortcut.zone_addresses || [];
    const available = [...zones];
    for (const address of addresses) {
      if (!zones.some((zone) => zone.controller === address.controller && zone.zone === address.zone)) {
        available.push({ ...address, name: `Controller ${address.controller} Zone ${address.zone} (disabled)` });
      }
    }
    for (const zone of available) {
      const label = document.createElement("label");
      label.className = "config-check";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.dataset.field = "shortcut-zone";
      checkbox.dataset.controller = String(zone.controller);
      checkbox.dataset.zone = String(zone.zone);
      checkbox.checked = addresses.some((address) => address.controller === zone.controller && address.zone === zone.zone);
      label.append(checkbox, document.createTextNode(zone.name || `Controller ${zone.controller} Zone ${zone.zone}`));
      targets.appendChild(label);
    }
    editor.appendChild(targets);
    configShortcuts.appendChild(editor);
  }
  shortcutCountBadge.textContent = `${shortcuts.length} shortcuts`;
  shortcutCountBadge.hidden = shortcuts.length === 0;
}

function escapeAttribute(value) {
  return String(value).replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
}

function collectBackend() {
  const endpoint = backendMode.value === "serial"
    ? { device: backendDevice.value.trim(), baud: Number(backendBaud.value) }
    : { host: backendHost.value.trim(), port: Number(backendPort.value) };
  return { ...endpoint, poll_interval_seconds: Number(backendPollInterval.value) };
}

function collectControllers() {
  return Array.from(controllerConfig.querySelectorAll(".setup-controller-row"))
    .filter((row) => row.querySelector("input").checked)
    .map((row) => ({ id: Number(row.dataset.controller), zone_count: Number(row.querySelector("select").value) }));
}

function connectionHash() {
  return JSON.stringify({ backend: collectBackend(), controllers: collectControllers() });
}

function updateConnectionFields() {
  const serial = backendMode.value === "serial";
  document.getElementById("backendHostField").hidden = serial;
  document.getElementById("backendPortField").hidden = serial;
  document.getElementById("backendDeviceField").hidden = !serial;
  document.getElementById("backendBaudField").hidden = !serial;
}

async function refreshBackendStatus() {
  if (statusInFlight || document.hidden) return;
  statusInFlight = true;
  try {
    const response = await apiFetch("/api/state");
    if (!response.ok) throw new Error("Status unavailable");
    const payload = await response.json();
    const backend = payload.config?.backend || {};
    const overrides = configPayload?.backend_overrides || {};
    const hasOverrides = Boolean(overrides.RUSSOUND_BACKEND_HOST || overrides.RUSSOUND_BACKEND_PORT);
    const host = overrides.RUSSOUND_BACKEND_HOST || backend.host;
    const port = Number(overrides.RUSSOUND_BACKEND_PORT || backend.port || 6666);
    const configured = !hasOverrides && backend.device
      ? Boolean(String(backend.device).trim())
      : typeof host === "string" && Boolean(host.trim()) && Number.isInteger(port) && port >= 1 && port <= 65535;
    if (payload.config_required || !configured) {
      setBackendConnectionStatus("incomplete", "Configuration incomplete", "Complete and save the backend configuration.");
    } else if (payload.backend_status?.connected) {
      setBackendConnectionStatus("connected", "Connected", "Backend connected");
    } else {
      setBackendConnectionStatus("disconnected", "Not connected", payload.backend_status?.message || "No backend connection established.");
    }
  } catch {
    setBackendConnectionStatus("unknown", "Status unavailable", "Status unavailable");
  } finally {
    statusInFlight = false;
  }
}

function setBackendConnectionStatus(state, label, message) {
  backendSummaryStatus.dataset.state = state;
  backendSummaryStatus.textContent = label;
  backendSummaryStatus.title = message;
  backendConnectionStatus.textContent = message;
}

function apiFetch(path, options = {}) {
  return fetch(path, { ...options, credentials: "same-origin" });
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
      keypad_id: Number(slot.keypad_id ?? 1),
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
      enabled: Boolean(slot.enabled),
    }))
    .sort((a, b) => a.id - b.id);
}

function hashSourceSlots(sourceSlots) {
  return JSON.stringify(normalizeSourceSlots(sourceSlots));
}

function updateSaveButtonState() {
  if (saveInFlight || !configPayload || configPayload.config_writable === false) {
    saveConfigButton.disabled = true;
    return;
  }
  saveConfigButton.disabled = !hasUnsavedChanges;
}

function recomputeUnsavedChanges() {
  const currentHash = hashZoneSlots(collectZoneSlots());
  const currentSourceHash = hashSourceSlots(collectSourceSlots());
  hasUnsavedChanges = Boolean(configPayload?.config_required) || currentHash !== baselineZoneSlotsHash || currentSourceHash !== baselineSourceSlotsHash || connectionHash() !== baselineConnectionHash || JSON.stringify(collectShortcuts()) !== baselineShortcutsHash;
  updateSaveButtonState();
}

function renderEditor(payload) {
  configPayload = payload;
  controllerZones.innerHTML = "";
  configSources.innerHTML = "";
  draftZones.clear();
  document.getElementById("configTitle").textContent = payload.config_required ? "Russound setup" : "Russound configuration";
  backendMode.value = payload.config?.backend?.device ? "serial" : "tcp";
  backendHost.value = payload.config?.backend?.host || "";
  backendPort.value = payload.config?.backend?.port ?? 6666;
  backendDevice.value = payload.config?.backend?.device || "";
  backendBaud.value = payload.config?.backend?.baud ?? 19200;
  backendPollInterval.value = payload.config?.backend?.poll_interval_seconds ?? 2;
  updateConnectionFields();
  controllerConfig.innerHTML = "";
  for (let controllerId = 1; controllerId <= 6; controllerId += 1) {
    const configured = payload.config?.controllers?.find((controller) => controller.id === controllerId);
    const row = document.createElement("div");
    row.className = "setup-controller-row";
    row.dataset.controller = String(controllerId);
    row.innerHTML = `<label class="config-check"><input type="checkbox" ${configured ? "checked" : ""} />Controller ${controllerId}</label>
      <select aria-label="Controller ${controllerId} zone count">${[1, 2, 3, 4, 5, 6].map((count) => `<option value="${count}" ${count === (configured?.zone_count ?? 6) ? "selected" : ""}>${count} zones</option>`).join("")}</select>`;
    controllerConfig.appendChild(row);
  }
  document.getElementById("controllerCountBadge").textContent = `${collectControllers().length} enabled`;
  baselineConnectionHash = connectionHash();
  const overrides = Object.entries(payload.backend_overrides || {});
  backendOverrides.hidden = overrides.length === 0;
  backendOverrides.textContent = overrides.length ? `Environment overrides: ${overrides.map(([name, value]) => `${name}=${value}`).join(", ")}` : "";
  baselineZoneSlotsHash = hashZoneSlots(payload.zone_slots || []);
  baselineSourceSlotsHash = hashSourceSlots(payload.source_slots || []);
  hasUnsavedChanges = false;

  if (payload.config_required) {
    configEmptyState.hidden = false;
    configEmptyMessage.textContent = payload.config_writable === false
      ? "No configuration path is set. Restart the server with --config or RUSSOUND_CONFIG pointing to a writable file."
      : payload.message || "A config file is required.";
  } else {
    configEmptyState.hidden = true;
  }

  controllerZones.hidden = false;
  sourceConfigPanel.hidden = false;
  updateSaveButtonState();

  const sourceSlots = payload.source_slots || [];
  const enabledSources = sourceSlots.filter((slot) => slot.enabled).length;
  sourceCountBadge.textContent = `${enabledSources}/${sourceSlots.length} enabled`;
  for (const sourceSlot of sourceSlots) {
    const row = document.createElement("div");
    row.className = "config-source-row";
    row.dataset.sourceId = String(sourceSlot.id);
    row.innerHTML = `
      <div class="config-slot-meta">
        <strong>Source ${sourceSlot.id}</strong>
      </div>
      <label class="config-check">
        <input type="checkbox" data-field="source-enabled" ${sourceSlot.enabled ? "checked" : ""} />
        <span>Enable</span>
      </label>
      <label class="field config-name-field">
        <input type="text" data-field="source-name" value="${escapeAttribute(sourceSlot.name)}" aria-label="Source ${sourceSlot.id} name" />
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
    card.open = false;
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
      row.dataset.keypadId = String(slot.keypad_id ?? 1);
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
          <input type="text" data-field="name" value="${escapeAttribute(slot.name)}" aria-label="Controller ${slot.controller} zone ${slot.zone} name" />
        </label>
      `;
      slotList.appendChild(row);
    }
    controllerZones.appendChild(card);
  }
  if (payload.config_required) {
    sourceConfigPanel.open = true;
    controllerZones.querySelectorAll("details").forEach((panel) => { panel.open = true; });
  }
  renderShortcuts(payload.config?.shortcuts || []);
  baselineShortcutsHash = JSON.stringify(collectShortcuts());
  recomputeUnsavedChanges();
  refreshBackendStatus();
}

function rebuildZoneSlots() {
  for (const slot of collectZoneSlots()) draftZones.set(`${slot.controller}/${slot.zone}`, slot);
  const controllers = collectControllers();
  const zoneSlots = [];
  for (const controller of controllers) {
    for (let zoneNumber = 1; zoneNumber <= controller.zone_count; zoneNumber += 1) {
      zoneSlots.push(draftZones.get(`${controller.id}/${zoneNumber}`) || {
        controller: controller.id, zone: zoneNumber, keypad_id: 1,
        name: `Controller ${controller.id} Zone ${zoneNumber}`, enabled: false, visible: true,
      });
    }
  }
  const savedDrafts = new Map(draftZones);
  const savedBaselines = [baselineZoneSlotsHash, baselineSourceSlotsHash, baselineConnectionHash, baselineShortcutsHash];
  renderEditor({ ...configPayload, config: { ...configPayload.config, backend: collectBackend(), controllers, shortcuts: collectShortcuts() }, zone_slots: zoneSlots, source_slots: collectSourceSlots() });
  for (const [address, slot] of savedDrafts) draftZones.set(address, slot);
  [baselineZoneSlotsHash, baselineSourceSlotsHash, baselineConnectionHash, baselineShortcutsHash] = savedBaselines;
  recomputeUnsavedChanges();
}

function collectZoneSlots() {
  return Array.from(document.querySelectorAll(".config-slot-row")).map((row) => ({
    controller: Number(row.dataset.controller),
    zone: Number(row.dataset.zone),
    keypad_id: Number(row.dataset.keypadId),
    enabled: row.querySelector('[data-field="enabled"]').checked,
    visible: row.querySelector('[data-field="visible"]').checked,
    name: row.querySelector('[data-field="name"]').value,
  }));
}

function collectSourceSlots() {
  return Array.from(document.querySelectorAll(".config-source-row")).map((row) => ({
    id: Number(row.dataset.sourceId),
    name: row.querySelector('[data-field="source-name"]').value,
    enabled: row.querySelector('[data-field="source-enabled"]').checked,
  }));
}

async function fetchConfigEditor() {
  try {
  const response = await apiFetch("/api/config");
  if (!response.ok) {
    setMessage("Failed to load configuration.", true);
    return;
  }
  const payload = await response.json();
  renderEditor(payload);
  } catch {
    setMessage("Failed to load configuration. Check the connection and retry.", true);
  }
}

async function saveConfig() {
  if (saveInFlight || !hasUnsavedChanges) {
    return;
  }
  const backend = collectBackend();
  if (!Number.isFinite(backend.poll_interval_seconds) || backend.poll_interval_seconds < 1) {
    setMessage("Enter a poll interval of at least 1 second.", true);
    return;
  }
  if (backendMode.value === "tcp" && (!backend.host || !Number.isInteger(backend.port) || backend.port < 1 || backend.port > 65535)) {
    setMessage("Enter a gateway host and a TCP port from 1 to 65535.", true);
    return;
  }
  if (backendMode.value === "serial" && (!backend.device || !Number.isInteger(backend.baud) || backend.baud < 1 || backend.baud > 4000000)) {
    setMessage("Enter a serial device and a baud rate from 1 to 4000000.", true);
    return;
  }
  if (!collectControllers().length) {
    setMessage("Enable at least one controller.", true);
    return;
  }
  saveInFlight = true;
  updateSaveButtonState();
  setMessage("");
  try {
    const response = await apiFetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        backend,
        controllers: collectControllers(),
        zone_slots: collectZoneSlots(),
        source_slots: collectSourceSlots(),
        shortcuts: collectShortcuts(),
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setMessage(payload.error || "Failed to save configuration.", true);
      return;
    }
    renderEditor(payload);
    setMessage("Configuration saved.");
  } catch {
    setMessage("Failed to save configuration. Check the connection and retry.", true);
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
connectionConfig.addEventListener("input", () => { updateConnectionFields(); recomputeUnsavedChanges(); });
connectionConfig.addEventListener("change", () => { updateConnectionFields(); recomputeUnsavedChanges(); });
controllerConfig.addEventListener("change", rebuildZoneSlots);
configShortcuts.addEventListener("input", recomputeUnsavedChanges);
configShortcuts.addEventListener("change", recomputeUnsavedChanges);
addShortcutButton.addEventListener("click", () => {
  const shortcuts = collectShortcuts();
  const shortcutId = `shortcut-${Array.from(crypto.getRandomValues(new Uint32Array(4)), (value) => value.toString(16).padStart(8, "0")).join("")}`;
  shortcuts.push({ id: shortcutId, name: "", source: 0, zone_addresses: [] });
  renderShortcuts(shortcuts);
  recomputeUnsavedChanges();
  configShortcuts.lastElementChild.querySelector('[data-field="shortcut-name"]').focus();
});
for (const container of [controllerZones, configSources]) {
  container.addEventListener("change", () => {
    renderShortcuts(collectShortcuts());
    recomputeUnsavedChanges();
  });
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshBackendStatus(); });
window.setInterval(refreshBackendStatus, 5000);

fetchConfigEditor();