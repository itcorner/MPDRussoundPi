const saveConfigButton = document.getElementById("saveConfigButton");
const discardConfigButton = document.getElementById("discardConfigButton");
const configSaveBar = document.getElementById("configSaveBar");
const configSaveText = document.getElementById("configSaveText");
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
let currentSoundZones = [];

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
  const savedBackend = configPayload?.config?.backend || {};
  const effective = configPayload?.effective_backend;
  const pollInterval = Number(backendPollInterval.value);
  const incompleteSavedEndpoint = configPayload?.config_required === false &&
    !savedBackend.device && !(savedBackend.host && savedBackend.port);
  if (effective?.mode?.source === "environment") {
    const backend = { ...savedBackend, poll_interval_seconds: pollInterval };
    if (!savedBackend.device) {
      if (effective.host?.source !== "environment" &&
          (!incompleteSavedEndpoint || backendHost.value.trim() !== effective.host?.value)) {
        backend.host = backendHost.value.trim();
      }
      if (effective.port?.source !== "environment" &&
          (!incompleteSavedEndpoint || Number(backendPort.value) !== effective.port?.value)) {
        backend.port = Number(backendPort.value);
      }
    }
    return backend;
  }
  if (incompleteSavedEndpoint && effective?.mode?.source === "default" &&
      backendMode.value === "tcp" && backendHost.value.trim() === effective.host?.value &&
      Number(backendPort.value) === effective.port?.value) {
    return { ...savedBackend, poll_interval_seconds: pollInterval };
  }
  const endpoint = backendMode.value === "serial"
    ? { device: backendDevice.value.trim(), baud: Number(backendBaud.value) }
    : { host: backendHost.value.trim(), port: Number(backendPort.value) };
  return { ...endpoint, poll_interval_seconds: pollInterval };
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

function backendSourcePresentation(source) {
  const labels = {
    environment: "Environment",
    saved: "Config file",
    default: "Default",
    cli: "Command line",
    command_line: "Command line",
  };
  return { label: labels[source] || "Config file" };
}

function setBackendSource(field, source) {
  const presentation = backendSourcePresentation(source);
  const badge = document.getElementById(`backend${field}Source`);
  badge.textContent = presentation.label;
  badge.dataset.source = source;
  badge.hidden = presentation.label === "Config file";
}

function renderBackendFields(payload) {
  const savedBackend = payload.config?.backend || {};
  const effective = payload.effective_backend;
  const modeSource = effective?.mode?.source || (savedBackend.device ? "saved" : "default");
  const hostSource = effective?.host?.source || (savedBackend.host ? "saved" : "default");
  const portSource = effective?.port?.source || (savedBackend.port ? "saved" : "default");
  const environmentMode = modeSource === "environment";
  const savedSerialOverridden = environmentMode && Boolean(savedBackend.device);
  const incompleteSavedEndpoint = payload.config_required === false &&
    !savedBackend.device && !(savedBackend.host && savedBackend.port);
  const missingSavedEndpoint = environmentMode && !savedBackend.device &&
    !(savedBackend.host && savedBackend.port);
  backendMode.value = environmentMode
    ? effective?.mode?.value || "tcp"
    : savedBackend.device ? "serial" : "tcp";
  backendHost.value = hostSource === "environment" || (hostSource === "default" && incompleteSavedEndpoint)
    ? effective?.host?.value ?? ""
    : savedBackend.host ?? effective?.host?.value ?? "";
  backendPort.value = portSource === "environment" || (portSource === "default" && incompleteSavedEndpoint)
    ? effective?.port?.value ?? 6666
    : savedBackend.port ?? effective?.port?.value ?? 6666;
  backendDevice.value = savedBackend.device || "";
  backendBaud.value = savedBackend.baud ?? 19200;
  backendPollInterval.value = savedBackend.poll_interval_seconds ?? 2;
  backendMode.disabled = environmentMode;
  backendHost.readOnly = hostSource === "environment" || savedSerialOverridden;
  backendPort.readOnly = portSource === "environment" || savedSerialOverridden;
  setBackendSource("Mode", modeSource);
  setBackendSource("Host", hostSource);
  setBackendSource("Port", portSource);
  const details = [];
  if (savedSerialOverridden) {
    details.push("The saved endpoint is serial; remove the environment override before editing TCP values.");
  }
  if (missingSavedEndpoint && payload.config_required !== false) {
    details.push("No saved endpoint exists yet; remove the environment override to complete initial setup.");
  } else if (missingSavedEndpoint) {
    const missingOverriddenValue = (hostSource === "environment" && !savedBackend.host) ||
      (portSource === "environment" && !savedBackend.port);
    details.push(missingOverriddenValue
      ? "The saved endpoint is incomplete; remove the environment override before completing it."
      : "The saved endpoint is incomplete; edit the unoverridden field to complete it.");
  } else if (incompleteSavedEndpoint) {
    details.push("The saved endpoint is incomplete; edit the connection fields before changing the poll interval.");
  }
  document.getElementById("backendSourceDetail").textContent =
    details.length ? ` ${details.join(" ")}` : "";
  updateConnectionFields();
}

function formatSoundValue(action, value) {
  const numericValue = Number(value);
  if (action === "turn_on_volume") return `${numericValue}%`;
  if (numericValue === 0) return "0";
  return numericValue > 0 ? `+${numericValue}` : String(numericValue);
}

function createSoundRange(zone, action, labelText, min, max, step, value, endpoints = null) {
  const zoneKey = `${zone.controller}-${zone.zone}`;
  const row = document.createElement("div");
  row.className = "control-row";
  const label = document.createElement("label");
  const input = document.createElement("input");
  input.type = "range";
  input.id = `sound-${action}-${zoneKey}`;
  input.min = String(min);
  input.max = String(max);
  input.step = String(step);
  input.value = String(value);
  input.dataset.action = action;
  input.dataset.controllerId = String(zone.controller);
  input.dataset.zoneNumber = String(zone.zone);
  label.htmlFor = input.id;
  label.append(document.createTextNode(`${labelText}: `));
  const output = document.createElement("span");
  output.className = "label-value";
  output.dataset.valueOutput = action;
  output.textContent = formatSoundValue(action, value);
  label.appendChild(output);
  row.appendChild(label);

  if (endpoints) {
    const range = document.createElement("div");
    range.className = "range-with-endpoints";
    for (const [index, endpoint] of endpoints.entries()) {
      if (index === 1) range.appendChild(input);
      const text = document.createElement("span");
      text.setAttribute("aria-hidden", "true");
      text.textContent = endpoint;
      range.appendChild(text);
    }
    row.appendChild(range);
  } else {
    row.appendChild(input);
  }
  return row;
}

function renderSoundControls(zones) {
  currentSoundZones = zones;
  for (const zone of zones) {
    const zoneKey = `${zone.controller}-${zone.zone}`;
    const row = controllerZones.querySelector(
      `.config-slot-row[data-controller="${zone.controller}"][data-zone="${zone.zone}"]`,
    );
    if (!row) continue;
    let section = row.querySelector(".config-slot-sound");
    if (!section) {
      section = document.createElement("details");
      section.className = "config-slot-sound";
      section.innerHTML = `
        <summary>Sound settings</summary>
        <div class="config-slot-sound-controls"></div>
        <p class="config-hint config-slot-sound-message" role="status" hidden></p>
      `;
      const controls = section.querySelector(".config-slot-sound-controls");
      controls.append(
        createSoundRange(zone, "turn_on_volume", "Turn-on volume", 0, 100, 2, zone.turn_on_volume ?? 0),
        createSoundRange(zone, "bass", "Bass", -10, 10, 1, zone.bass ?? 0, ["-10", "+10"]),
        createSoundRange(zone, "treble", "Treble", -10, 10, 1, zone.treble ?? 0, ["-10", "+10"]),
        createSoundRange(zone, "balance", "Balance", -10, 10, 1, zone.balance ?? 0, ["L", "R"]),
      );
      const loudness = document.createElement("label");
      loudness.className = "advanced-check";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.id = `sound-loudness-${zoneKey}`;
      checkbox.checked = Boolean(zone.loudness);
      checkbox.dataset.action = "loudness";
      checkbox.dataset.controllerId = String(zone.controller);
      checkbox.dataset.zoneNumber = String(zone.zone);
      const loudnessText = document.createElement("span");
      loudnessText.textContent = "Loudness";
      loudness.append(checkbox, loudnessText);
      controls.appendChild(loudness);
      row.appendChild(section);
    }
    for (const action of ["turn_on_volume", "bass", "treble", "balance"]) {
      const input = section.querySelector(`input[data-action="${action}"]`);
      if (document.activeElement !== input) {
        input.value = String(zone[action] ?? 0);
        section.querySelector(`[data-value-output="${action}"]`).textContent = formatSoundValue(action, input.value);
      }
    }
    const loudness = section.querySelector('input[data-action="loudness"]');
    if (document.activeElement !== loudness) loudness.checked = Boolean(zone.loudness);
  }
}

async function updateSoundControl(controllerId, zoneNumber, action, value) {
  const row = controllerZones.querySelector(
    `.config-slot-row[data-controller="${controllerId}"][data-zone="${zoneNumber}"]`,
  );
  const message = row?.querySelector(".config-slot-sound-message");
  if (!message) return;
  message.hidden = false;
  message.classList.remove("is-error", "is-success");
  message.textContent = "Saving sound setting…";
  try {
    const response = await apiFetch(`/api/controller/${controllerId}/zone/${zoneNumber}/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [action]: value }),
    });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || `Sound setting could not be saved (${response.status}).`);
    }
    message.textContent = "Sound setting updated.";
    message.classList.add("is-success");
  } catch (error) {
    message.textContent = error instanceof Error
      ? `Unable to update sound setting: ${error.message}`
      : "Unable to update sound setting.";
    message.classList.add("is-error");
  }
}

async function refreshBackendStatus() {
  if (statusInFlight || document.hidden) return;
  statusInFlight = true;
  try {
    const response = await apiFetch("/api/config/state");
    if (!response.ok) throw new Error("Status unavailable");
    const payload = await response.json();
    renderSoundControls(payload.zones || []);
    if (configPayload) {
      configPayload.resolved_inputs = payload.resolved_inputs || [];
      for (const row of configSources.querySelectorAll(".config-source-row")) updatePlaybackPreview(row);
    }
    const configured = payload.effective_backend?.configured === true;
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
  backendConnectionStatus.hidden = state === "connected";
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
      playback_output: slot.playback_output
        ? { agent_id: slot.playback_output.agent_id, output_id: slot.playback_output.output_id }
        : null,
    }))
    .sort((a, b) => a.id - b.id);
}

function hashSourceSlots(sourceSlots) {
  return JSON.stringify(normalizeSourceSlots(sourceSlots));
}

// The save bar at the bottom of the page replaces a permanent save button: it shows
// while there are unsaved changes, while saving and with the result of a save. A
// success replaces the buttons and the bar leaves shortly after; a failure stays,
// with the buttons, until the next attempt or a discard.
const SAVE_SUCCESS_MS = 2500;
const SAVE_LEAVE_MS = 450;  // matches the slide-out transition in styles.css
let saveResult = null;
let saveResultTimer = null;

function setSaveResult(text, ok = false) {
  window.clearTimeout(saveResultTimer);
  saveResultTimer = null;
  configSaveBar.classList.remove("is-leaving");
  saveResult = text ? { text, ok } : null;
  if (saveResult && ok) {
    saveResultTimer = window.setTimeout(() => {
      // Slide down and fade, then hide for good.
      configSaveBar.classList.add("is-leaving");
      saveResultTimer = window.setTimeout(() => {
        configSaveBar.classList.remove("is-leaving");
        saveResult = null;
        updateSaveButtonState();
      }, SAVE_LEAVE_MS);
    }, SAVE_SUCCESS_MS);
  }
  updateSaveButtonState();
}

function updateSaveButtonState() {
  const writable = Boolean(configPayload) && configPayload.config_writable !== false;
  const succeeded = Boolean(saveResult && saveResult.ok && !hasUnsavedChanges && !saveInFlight);
  const failed = Boolean(saveResult && !saveResult.ok && !saveInFlight);
  const visible = writable && (hasUnsavedChanges || saveInFlight || succeeded || failed);
  configSaveBar.hidden = !visible;
  configSaveBar.classList.toggle("is-success", succeeded);
  configSaveBar.classList.toggle("is-error", failed);
  document.body.classList.toggle("has-save-bar", visible);
  // On success the buttons turn invisible but keep their space, so the bar keeps its size.
  discardConfigButton.hidden = Boolean(configPayload?.config_required);
  saveConfigButton.disabled = saveInFlight || !hasUnsavedChanges || !writable;
  discardConfigButton.disabled = saveInFlight;
  configSaveText.setAttribute("role", failed ? "alert" : "status");
  configSaveText.textContent = saveInFlight ? "Saving…"
    : succeeded || failed ? saveResult.text
      : configPayload?.config_required ? "The setup is not saved yet" : "Unsaved changes";
  saveConfigButton.textContent = configPayload?.config_required ? "Save setup" : "Save";
}

// Saving the configuration is an administrator action: a guest signs in first.
function requestSave() {
  const elevated = document.body.classList.contains("admin-elevated");
  if (!elevated && typeof window.adminLogin === "function") {
    window.adminLogin({ reason: "Save the configuration", onSuccess: saveConfig });
    return;
  }
  saveConfig();
}

function recomputeUnsavedChanges() {
  const currentHash = hashZoneSlots(collectZoneSlots());
  const currentSourceHash = hashSourceSlots(collectSourceSlots());
  hasUnsavedChanges = Boolean(configPayload?.config_required) || currentHash !== baselineZoneSlotsHash || currentSourceHash !== baselineSourceSlotsHash || connectionHash() !== baselineConnectionHash || JSON.stringify(collectShortcuts()) !== baselineShortcutsHash;
  if (hasUnsavedChanges && saveResult && saveResult.ok) {
    setSaveResult(null);  // a new edit after a save: back to the buttons
    return;
  }
  updateSaveButtonState();
}

function renderEditor(payload) {
  configPayload = payload;
  controllerZones.innerHTML = "";
  configSources.innerHTML = "";
  draftZones.clear();
  document.getElementById("configTitle").textContent = payload.config_required ? "Russound setup" : "Russound configuration";
  renderBackendFields(payload);
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
        <label class="config-check">
          <input type="checkbox" data-field="source-enabled" ${sourceSlot.enabled ? "checked" : ""} />
          <span>Enable</span>
        </label>
      </div>
      <label class="field config-name-field">
        Fallback name
        <input type="text" data-field="source-name" value="${escapeAttribute(sourceSlot.name)}" aria-label="Source ${sourceSlot.id} name" />
      </label>
      <label class="field">
        Stream mapping
        <span class="stream-mapping-select">
          <select data-field="playback-output" aria-label="Source ${sourceSlot.id} stream mapping"></select>
          <span class="stream-mapping-value" data-field="playback-preview" aria-hidden="true"></span>
        </span>
      </label>
    `;
    populatePlaybackOutputs(row, sourceSlot.playback_output);
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
  renderSoundControls(currentSoundZones);
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
  const backendDraft = [backendMode, backendHost, backendPort, backendDevice, backendBaud, backendPollInterval]
    .map((field) => field.value);
  renderEditor({ ...configPayload, config: { ...configPayload.config, controllers, shortcuts: collectShortcuts() }, zone_slots: zoneSlots, source_slots: collectSourceSlots() });
  [backendMode, backendHost, backendPort, backendDevice, backendBaud, backendPollInterval]
    .forEach((field, index) => { field.value = backendDraft[index]; });
  updateConnectionFields();
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
    playback_output: JSON.parse(row.querySelector('[data-field="playback-output"]').value || "null"),
  }));
}

function populatePlaybackOutputs(row, binding) {
  const select = row.querySelector('[data-field="playback-output"]');
  select.add(new Option("Not mapped", "null"));
  for (const output of configPayload.playback_outputs || []) {
    const value = JSON.stringify({ agent_id: output.agent_id, output_id: output.output_id });
    select.add(new Option(`${output.station_name || "No station selected"} [${output.agent_id} / ${output.output_id}]${output.available ? "" : " (unavailable)"}`, value));
  }
  const value = binding ? JSON.stringify({ agent_id: binding.agent_id, output_id: binding.output_id }) : "null";
  if (!Array.from(select.options).some((option) => option.value === value)) {
    select.add(new Option(`Unavailable [${binding.agent_id} / ${binding.output_id}]`, value));
  }
  select.value = value;
  updatePlaybackPreview(row);
}

function updatePlaybackPreview(row) {
  const select = row.querySelector('[data-field="playback-output"]');
  const binding = JSON.parse(select.value || "null");
  const preview = row.querySelector('[data-field="playback-preview"]');
  const output = (configPayload.playback_outputs || []).find((output) =>
    output.agent_id === binding?.agent_id && output.output_id === binding?.output_id);
  const resolved = (configPayload.resolved_inputs || []).find((input) =>
    input.id === Number(row.dataset.sourceId) &&
    input.playback_output?.agent_id === binding?.agent_id &&
    input.playback_output?.output_id === binding?.output_id);
  const station = resolved?.playback_status === "no_station" ? "No station selected"
    : resolved && !["unmapped"].includes(resolved.playback_status) ? resolved.display_name
    : output?.station_name || (output ? "No station selected" : "Unavailable");
  preview.textContent = binding ? station : "Not mapped";
  select.title = !binding ? "" : resolved?.playback_message ||
    (!output ? "Playback output unavailable; fallback name will be used."
      : !output.available ? "Playback unavailable; showing the last known station." : "");
  if (binding) {
    const option = Array.from(select.options).find((option) => option.value === select.value);
    if (option) {
      option.textContent = `${station} [${binding.agent_id} / ${binding.output_id}]${select.title && resolved?.playback_status !== "no_station" ? " (unavailable)" : ""}`;
    }
  }
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
  const savedBackend = configPayload?.config?.backend || {};
  const effective = configPayload?.effective_backend;
  const savedMode = savedBackend.device ? "serial" : "tcp";
  const persistenceMode = effective?.mode?.source === "environment" ? savedMode : backendMode.value;
  const preserveIncompleteEndpoint = configPayload?.config_required === false &&
    !savedBackend.device && !(savedBackend.host && savedBackend.port) &&
    !backend.device && !(backend.host && backend.port) &&
    ["host", "port", "device", "baud"].every((key) => backend[key] === savedBackend[key]);
  if (preserveIncompleteEndpoint &&
      backend.poll_interval_seconds !== (savedBackend.poll_interval_seconds ?? 2)) {
    setSaveResult("Complete the saved backend endpoint before changing the poll interval.", false);
    return;
  }
  if (!preserveIncompleteEndpoint && effective?.mode?.source === "environment" &&
      !savedBackend.device && !(savedBackend.host && savedBackend.port) &&
      (!backend.host || !backend.port)) {
    setSaveResult(
      "No saved backend endpoint is available to complete setup. Remove or change the environment override, restart the web service, then save the endpoint in the config file.",
      false,
    );
    return;
  }
  if (!Number.isFinite(backend.poll_interval_seconds) || backend.poll_interval_seconds < 1) {
    setSaveResult("Enter a poll interval of at least 1 second.", false);
    return;
  }
  if (!preserveIncompleteEndpoint && persistenceMode === "tcp" && (!backend.host || !Number.isInteger(backend.port) || backend.port < 1 || backend.port > 65535)) {
    setSaveResult("Enter a gateway host and a TCP port from 1 to 65535.", false);
    return;
  }
  if (persistenceMode === "serial" && (!backend.device || !Number.isInteger(backend.baud) || backend.baud < 1 || backend.baud > 4000000)) {
    setSaveResult("Enter a serial device and a baud rate from 1 to 4000000.", false);
    return;
  }
  if (!collectControllers().length) {
    setSaveResult("Enable at least one controller.", false);
    return;
  }
  saveInFlight = true;
  updateSaveButtonState();
  setSaveResult(null);
  try {
    // The revision makes the server reject the save if the config changed since
    // this page loaded it (409), instead of overwriting newer edits.
    const revision = configPayload?.configuration?.revision;
    const response = await apiFetch("/api/config", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(revision ? { "X-Config-Revision": revision } : {}),
      },
      body: JSON.stringify({
        ...(preserveIncompleteEndpoint ? {} : { backend }),
        controllers: collectControllers(),
        zone_slots: collectZoneSlots(),
        source_slots: collectSourceSlots(),
        shortcuts: collectShortcuts(),
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setSaveResult(payload.error || "Failed to save configuration.", false);
      return;
    }
    renderEditor(payload);
    setSaveResult("Configuration saved.", true);
  } catch {
    setSaveResult("Failed to save configuration. Check the connection and retry.", false);
  } finally {
    saveInFlight = false;
    updateSaveButtonState();
  }
}

function hasRelevantTarget(target) {
  if (!(target instanceof Element)) {
    return false;
  }
  if (target.closest(".config-slot-sound")) {
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

saveConfigButton.addEventListener("click", requestSave);
discardConfigButton.addEventListener("click", () => {
  setSaveResult(null);
  fetchConfigEditor();  // the saved configuration replaces every edit
});

controllerZones.addEventListener("input", (event) => {
  const control = event.target.closest('input[type="range"][data-action]');
  if (!control) return;
  const output = control.closest(".control-row")?.querySelector("[data-value-output]");
  if (output) output.textContent = formatSoundValue(control.dataset.action, control.value);
});
controllerZones.addEventListener("change", (event) => {
  const control = event.target.closest("input[data-action]");
  if (!control) return;
  const value = control.type === "checkbox" ? control.checked : Number(control.value);
  updateSoundControl(
    Number(control.dataset.controllerId),
    Number(control.dataset.zoneNumber),
    control.dataset.action,
    value,
  );
});

controllerZones.addEventListener("input", handleEditorInteraction);
controllerZones.addEventListener("change", handleEditorInteraction);
configSources.addEventListener("input", handleEditorInteraction);
configSources.addEventListener("change", handleEditorInteraction);
configSources.addEventListener("change", (event) => {
  const row = event.target.closest(".config-source-row");
  if (row) updatePlaybackPreview(row);
});
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
  container.addEventListener("change", (event) => {
    if (event.target instanceof Element && event.target.closest(".config-slot-sound")) {
      return;
    }
    renderShortcuts(collectShortcuts());
    recomputeUnsavedChanges();
  });
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshBackendStatus(); });
window.setInterval(refreshBackendStatus, 5000);

fetchConfigEditor();