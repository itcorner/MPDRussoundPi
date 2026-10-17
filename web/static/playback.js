const health = document.getElementById("playbackHealth");
const message = document.getElementById("playbackMessage");
const outputsContainer = document.getElementById("playbackOutputs");
const configDialog = document.getElementById("playbackConfigDialog");
const configTitle = document.getElementById("playbackConfigTitle");
const configSubtitle = document.getElementById("playbackConfigSubtitle");
const configControls = document.getElementById("playbackConfigControls");
let activeOutputKey = null;
let playbackAvailable = false;

let eventSource = null;
let eventSourceRetryTimer = null;
let playbackPollTimer = null;
let playbackFetchInFlight = false;
let playbackFetchQueued = false;
let lastPlaybackSignature = null;
const outputControls = new Map();
const trackResizeObserver = new ResizeObserver((entries) => {
  for (const { target } of entries) {
    const line = target.className === "playback-track" ? target : target.parentElement;
    const distance = Math.max(0, line.firstElementChild.scrollWidth - line.clientWidth);
    line.dataset.overflow = String(distance > 0);
    line.style.setProperty("--track-offset", `${-distance}px`);
    line.style.setProperty("--track-duration", `${Math.max(6, distance / 25 + 4)}s`);
  }
});
const pendingControlRevisions = new Map();
const terminalOperationStatuses = new Set(["applied", "failed", "expired", "superseded", "outcome_unknown"]);
const pendingOperationStatuses = new Set(["queued", "running"]);
const operationPollLimit = 60;
const operationPollDeadlineMs = 90000;
const eventSourceRetryDelayMs = 30000;
const playbackPollIntervalMs = 5000;
const volatilePlaybackFields = new Set([
  "age_ms",
  "attempted_at",
  "elapsed_seconds",
  "last_attempt_at",
  "last_success_at",
  "observed_at",
  "revision",
  "snapshot_age_ms",
  "snapshot_at",
]);

// TuneIn hosts each station logo under one of several CDN paths; which one works varies per station.
function stationLogoCandidates(stationId) {
  return [
    `https://cdn-profiles.tunein.com/${stationId}/images/logoq.png`,
    `https://cdn-profiles.tunein.com/${stationId}/images/logoq.jpg`,
    `https://cdn-radiotime-logos.tunein.com/${stationId}q.png`,
  ];
}

// One element per output and station survives card re-renders, so logos neither reload nor retry failed URLs.
const stationLogos = new Map();

function stationLogo(outputKey, stationId) {
  if (typeof stationId !== "string" || !/^s[0-9]{1,12}$/.test(stationId)) return null;
  const key = JSON.stringify([outputKey, stationId]);
  let entry = stationLogos.get(key);
  if (!entry) {
    const element = document.createElement("img");
    element.className = "playback-logo";
    element.alt = "";
    element.referrerPolicy = "no-referrer";
    element.hidden = true;
    entry = { element, candidates: stationLogoCandidates(stationId), index: 0, failed: false };
    element.addEventListener("load", () => { element.hidden = false; });
    element.addEventListener("error", () => {
      element.hidden = true;
      entry.index += 1;
      if (entry.index < entry.candidates.length) element.src = entry.candidates[entry.index];
      else entry.failed = true;
    });
    element.src = entry.candidates[0];
    stationLogos.set(key, entry);
  }
  return entry.failed ? null : entry.element;
}

function apiFetch(path, options = {}) {
  return fetch(path, { ...options, credentials: "same-origin" });
}

function outputControlState(agent, output) {
  const key = JSON.stringify([agent.agent_id, output.id]);
  if (!outputControls.has(key)) {
    outputControls.set(key, {
      key, draft: "", dirty: false, stationQuery: "", stationResults: [], stationSearching: false,
      stationError: "", command: null,
    });
  }
  const state = outputControls.get(key);
  state.agent = agent;
  state.output = output;
  state.present = true;
  const pendingRevision = pendingControlRevisions.get(agent.agent_id);
  if (pendingRevision && output.control_revision !== pendingRevision) {
    pendingControlRevisions.delete(agent.agent_id);
  }
  if (state.savedGain !== undefined && !pendingControlRevisions.has(agent.agent_id)
    && output.gain?.configured_db === state.savedGain) {
    if (Number(state.draft) === state.savedGain) state.dirty = false;
    delete state.savedGain;
  }
  if (!state.dirty) {
    const gain = output.gain?.configured_db;
    state.draft = Number.isFinite(gain) ? String(gain) : "";
  }
  return state;
}

function canWrite(state, action) {
  return playbackAvailable && state.present && Array.isArray(state.agent.write_operations)
    && !pendingControlRevisions.has(state.agent.agent_id)
    && state.agent.write_operations.includes(action)
    && typeof state.agent.snapshot?.config_revision === "string"
    && typeof state.output.control_revision === "string"
    && state.agent.status === "ok"
    && !state.agent.snapshot.stale
    && !state.output.playback?.stale;
}

function hasStation(state) {
  return state.output.control?.has_station !== false;
}

function favoriteStations(state) {
  const stations = state.agent.snapshot?.favorites?.stations;
  return Array.isArray(stations) ? stations : [];
}

function favoriteKey(station) {
  return station.key || `tunein:${station.id}`;
}

function iconButton(parent, label, title, disabled, handler) {
  const button = commandButton(parent, label, disabled, handler);
  button.className = "playback-icon-button";
  button.title = title;
  button.setAttribute("aria-label", title);
  return button;
}

// ☆ adds a TuneIn station, ★ removes it; URL stations can only come from config.json.
function favoriteToggle(parent, state, station, blocked) {
  const key = favoriteKey(station);
  const favorites = favoriteStations(state);
  const existing = favorites.find((item) => item.key === key);
  if (existing) {
    return iconButton(parent, "★", `Remove ${existing.name} from favorites`,
      blocked || !canWrite(state, "remove_favorite"),
      () => submitOutputOperation(state, "remove_favorite", existing));
  }
  const full = favorites.length >= 20;
  const button = iconButton(parent, "☆", full ? "Favorites are full (20)" : `Add ${station.name} to favorites`,
    blocked || full || !station.id || !canWrite(state, "add_favorite"),
    () => submitOutputOperation(state, "add_favorite", station));
  return button;
}

function validGain(draft) {
  return draft.trim() !== "" && Number.isFinite(Number(draft)) && Number(draft) <= 0;
}

function commandButton(parent, label, disabled, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.disabled = disabled;
  button.addEventListener("click", handler);
  parent.appendChild(button);
  return button;
}

function dialogSection(parent, title) {
  const section = document.createElement("section");
  section.className = "playback-section";
  const heading = document.createElement("h3");
  heading.className = "playback-section-title";
  heading.textContent = title;
  section.appendChild(heading);
  parent.appendChild(section);
  return section;
}

function renderOutputControls(state) {
  const controls = document.createElement("div");
  controls.className = "playback-controls";

  const playbackSection = dialogSection(controls, "Playback");
  const nowPlaying = document.createElement("p");
  nowPlaying.className = "playback-section-detail";
  const transport = document.createElement("div");
  transport.className = "playback-transport";
  const buttons = {};
  for (const action of ["play", "pause", "stop"]) {
    buttons[action] = commandButton(transport, action[0].toUpperCase() + action.slice(1),
      true, () => submitOutputOperation(state, action));
    buttons[action].dataset.action = action;
  }
  const nowPlayingRow = document.createElement("div");
  nowPlayingRow.className = "playback-now-playing";
  const currentFavorite = document.createElement("span");
  currentFavorite.className = "playback-current-favorite";
  nowPlayingRow.append(nowPlaying, currentFavorite);
  playbackSection.append(nowPlayingRow, transport);

  const favoritesSection = dialogSection(controls, "Favorites");
  const favoritesList = document.createElement("div");
  favoritesList.className = "playback-favorites";
  favoritesSection.appendChild(favoritesList);

  const stationSection = dialogSection(controls, "Find a station");
  const searchRow = document.createElement("div");
  searchRow.className = "playback-search";
  const stationInput = document.createElement("input");
  stationInput.id = "playbackTuneInQuery";
  stationInput.type = "search";
  stationInput.maxLength = 80;
  stationInput.autocomplete = "off";
  stationInput.placeholder = "Station name or TuneIn ID (e.g. s25744)";
  stationInput.value = state.stationQuery;
  stationInput.setAttribute("aria-label", "Search TuneIn stations");
  searchRow.appendChild(stationInput);
  const searchStations = commandButton(
    searchRow, "Search", false, () => searchTuneInStations(state),
  );
  stationInput.addEventListener("input", () => {
    state.stationQuery = stationInput.value;
  });
  stationInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    if (!searchStations.disabled) searchTuneInStations(state);
  });
  const stationStatus = document.createElement("p");
  stationStatus.className = "playback-gain-hint";
  const stationResults = document.createElement("div");
  stationResults.className = "playback-station-results";
  stationSection.append(searchRow, stationStatus, stationResults);

  const gainSection = dialogSection(controls, "Calibration gain");
  const gainValue = document.createElement("output");
  gainValue.className = "playback-gain-value";
  gainSection.children[0].appendChild(gainValue);
  const slider = document.createElement("input");
  slider.id = "playbackGainSlider";
  slider.type = "range";
  slider.max = "0";
  slider.step = "0.1";
  slider.setAttribute("aria-label", "Calibration gain");
  gainValue.setAttribute("for", slider.id);
  slider.addEventListener("input", () => {
    state.draft = slider.value;
    state.dirty = true;
    updateOutputDialog(state);
  });
  const gainActions = document.createElement("div");
  gainActions.className = "playback-gain-actions";
  const validation = document.createElement("p");
  validation.className = "playback-gain-hint";
  gainActions.appendChild(validation);
  const apply = commandButton(gainActions, "Apply gain",
    true, () => submitOutputOperation(state, "set_gain"));
  gainSection.append(slider, gainActions);

  const availability = document.createElement("p");
  availability.className = "playback-meta";
  const status = document.createElement("p");
  const feedback = document.createElement("div");
  feedback.className = "playback-command-feedback";
  feedback.setAttribute("tabindex", "0");
  feedback.setAttribute("role", "region");
  feedback.setAttribute("aria-label", "Command feedback");
  feedback.append(availability, status);
  const retry = commandButton(feedback, "Retry identical submission", true, () => {
    const command = state.command;
    if (!command?.unknown || !state.present || !playbackAvailable) return;
    if (command.id) pollOutputOperation(state, command);
    else if (canWrite(state, command.body.action)) sendOutputOperation(state, command);
  });
  controls.appendChild(feedback);
  state.dialogElements = {
    buttons, nowPlaying, currentFavorite, favoritesList, slider, gainValue, apply, stationInput,
    searchStations, stationStatus, stationResults, validation, availability, status, retry, feedback,
  };
  updateOutputDialog(state);
  return controls;
}

function updateCommandStatus(element, command) {
  element.hidden = !command;
  element.className = command?.error ? "playback-command-error" : "playback-command-status";
  element.setAttribute("role", command?.error ? "alert" : "status");
  element.textContent = command?.message || "";
}

function updateOutputDialog(state) {
  if (activeOutputKey !== state.key || !state.dialogElements) return;
  configTitle.textContent = state.output.label;
  configSubtitle.textContent = `${state.agent.agent_id} · ${state.output.id}`;
  const {
    buttons, nowPlaying, currentFavorite, favoritesList, slider, gainValue, apply, stationInput,
    searchStations, stationStatus, stationResults, validation, availability, status, retry, feedback,
  } = state.dialogElements;
  const blocked = Boolean(state.command?.pending || state.command?.unknown);
  for (const action of ["play", "pause", "stop"]) buttons[action].disabled = blocked || !canWrite(state, action);
  const playing = state.output.playback?.value?.state === "play";
  buttons.play.hidden = playing;
  buttons.pause.hidden = !playing;
  buttons.stop.disabled ||= state.output.playback?.value?.state === "stop";
  const stationless = !hasStation(state);
  buttons.play.disabled ||= stationless;
  buttons.play.title = stationless ? "Select a station first" : "";
  const observed = state.output.playback?.value || state.output.playback?.last_known?.value || {};
  const station = [state.output.selection?.station_name, observed.now_playing?.station]
    .find((value) => typeof value === "string" && value);
  const playbackState = observed.state || state.output.playback?.status || "unknown";
  nowPlaying.textContent = station ? `${station} · ${playbackState}`
    : stationless ? `No station · ${playbackState}` : playbackState;
  const selection = state.output.selection || {};
  currentFavorite.replaceChildren();
  if (typeof selection.key === "string" && selection.key) {
    const current = { key: selection.key, id: selection.provider === "tunein" ? selection.station_id : null,
      name: selection.station_name || selection.label || selection.key };
    favoriteToggle(currentFavorite, state, current, blocked);
  }
  const favorites = favoriteStations(state);
  favoritesList.replaceChildren();
  if (!favorites.length) {
    appendPlaybackText(favoritesList, "playback-gain-hint",
      state.agent.snapshot?.favorites ? "No favorites yet. Add stations with ☆." : "Favorites unavailable.");
  }
  favorites.forEach((favorite, index) => {
    const row = document.createElement("div");
    const isCurrent = favorite.key === selection.key;
    row.className = isCurrent ? "playback-favorite playback-favorite-current" : "playback-favorite";
    const logo = favorite.type === "tunein" ? stationLogo("favorites", favorite.station_id) : null;
    const name = document.createElement("span");
    name.className = "playback-station-name";
    name.textContent = isCurrent ? `${favorite.name} (current)` : favorite.name;
    const actions = document.createElement("div");
    actions.className = "playback-favorite-actions";
    const play = commandButton(actions, "Play", blocked || !canWrite(state, "select_favorite")
      || (isCurrent && playing), () => submitOutputOperation(state, "select_favorite", favorite));
    play.setAttribute("aria-label", `Play ${favorite.name}`);
    iconButton(actions, "↑", `Move ${favorite.name} up`, blocked || index === 0 || !canWrite(state, "move_favorite"),
      () => submitOutputOperation(state, "move_favorite", favorite, index - 1));
    iconButton(actions, "↓", `Move ${favorite.name} down`,
      blocked || index === favorites.length - 1 || !canWrite(state, "move_favorite"),
      () => submitOutputOperation(state, "move_favorite", favorite, index + 1));
    iconButton(actions, "✕", `Remove ${favorite.name} from favorites`,
      blocked || !canWrite(state, "remove_favorite"),
      () => submitOutputOperation(state, "remove_favorite", favorite));
    row.append(...(logo ? [logo] : []), name, actions);
    favoritesList.appendChild(row);
  });
  const valid = validGain(state.draft);
  // Include both configured and draft gain without rounding or clamping the saved value.
  const configuredGain = state.output.gain?.configured_db;
  slider.min = String(Math.min(-60, Number.isFinite(configuredGain) ? configuredGain : -60, valid ? Number(state.draft) : -60));
  slider.value = valid ? state.draft : slider.min;
  slider.disabled = blocked || !canWrite(state, "set_gain");
  stationInput.value = state.stationQuery;
  stationInput.disabled = false;
  searchStations.disabled = state.stationSearching;
  stationStatus.textContent = state.stationError || (state.stationSearching ? "Searching TuneIn…" : "");
  stationStatus.hidden = !stationStatus.textContent;
  stationStatus.setAttribute("role", state.stationError ? "alert" : "status");
  stationResults.replaceChildren();
  for (const station of state.stationResults) {
    const result = document.createElement("div");
    result.className = "playback-station-result";
    const details = document.createElement("div");
    details.className = "playback-station-details";
    const name = document.createElement("span");
    name.className = "playback-station-name";
    name.textContent = station.name;
    details.appendChild(name);
    const metadata = [station.description, station.formats, station.bitrate_kbps
      ? `${station.bitrate_kbps} kbps` : null].filter((value) => typeof value === "string" && value);
    if (metadata.length) {
      const meta = document.createElement("span");
      meta.className = "playback-station-meta";
      meta.textContent = metadata.join(" · ");
      details.appendChild(meta);
    }
    const actions = document.createElement("div");
    actions.className = "playback-favorite-actions";
    commandButton(
      actions, "Select and play", blocked || !canWrite(state, "set_tunein_station"),
      () => submitOutputOperation(state, "set_tunein_station", station),
    );
    favoriteToggle(actions, state, station, blocked);
    result.replaceChildren(details, actions);
    stationResults.appendChild(result);
  }
  gainValue.textContent = valid ? `${state.draft} dB` : "Gain unavailable";
  apply.disabled = slider.disabled || !valid;
  validation.textContent = valid
    ? "Unsaved draft. Select Apply gain to send."
    : "No valid configured gain. Move the slider to choose a draft.";
  availability.hidden = ["play", "pause", "stop", "set_gain", "set_tunein_station", "select_favorite"]
    .some((action) => canWrite(state, action));
  availability.textContent = pendingControlRevisions.has(state.agent.agent_id)
    ? "Waiting for updated playback state before another command can be sent."
    : "Read-only: output unavailable, stale, or write capabilities/current revisions unavailable.";
  updateCommandStatus(status, state.command);
  validation.hidden = valid && Number(state.draft) === configuredGain;
  if (state.command) availability.hidden = true;
  retry.hidden = !state.command?.unknown;
  feedback.hidden = availability.hidden && status.hidden && retry.hidden;
  retry.textContent = state.command?.id ? "Check operation status" : "Retry identical submission";
  retry.disabled = !state.present || !playbackAvailable
    || (!state.command?.id && !canWrite(state, state.command?.body.action));
}

function outputCard(state) {
  return Array.from(outputsContainer.querySelectorAll(".playback-card"))
    .find((element) => element.dataset.outputKey === state.key);
}

function openOutputConfig(state) {
  if (!state.present) return;
  if (activeOutputKey !== state.key) {
    activeOutputKey = state.key;
    configControls.replaceChildren(renderOutputControls(state));
  }
  updateOutputDialog(state);
  if (!configDialog.open) {
    configDialog.showModal();
    document.activeElement?.blur();
  }
}

document.getElementById("playbackConfigClose").addEventListener("click", () => configDialog.close());
configDialog.addEventListener("close", () => {
  if (configDialog.open) return;
  const state = outputControls.get(activeOutputKey);
  activeOutputKey = null;
  if (state) outputCard(state)?.querySelector(".playback-config-button")?.focus();
});

function refreshOutputControls(state) {
  updateOutputDialog(state);
  const activeState = outputControls.get(activeOutputKey);
  if (activeState && activeState !== state) updateOutputDialog(activeState);
}

function operationUnknown(state, command, detail) {
  command.pending = false;
  command.unknown = true;
  command.error = true;
  command.message = `Command outcome unknown: ${detail} No new command was sent. ${
    command.id ? "Check this operation again." : "Retry only the identical submission with its original key."}`;
  refreshOutputControls(state);
}

async function operationRequest(path, options) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 10000);
  try {
    const response = await apiFetch(path, { ...options, signal: controller.signal });
    const envelope = await response.json();
    return { response, envelope };
  } finally {
    window.clearTimeout(timer);
  }
}

function operationFromEnvelope(envelope, state, command) {
  const operation = envelope?.operation;
  if (envelope?.api_version !== 1 || envelope.agent_id !== state.agent.agent_id
    || typeof operation?.id !== "string" || !operation.id
    || (command.id && command.id !== operation.id)
    || operation.output_id !== (command.favorites ? null : state.output.id)
    || operation.action !== command.body.action
    || (!terminalOperationStatuses.has(operation.status) && !pendingOperationStatuses.has(operation.status))) {
    throw new Error("Invalid operation response.");
  }
  return operation;
}

function acceptOperation(state, command, operation) {
  command.id = operation.id;
  if (!terminalOperationStatuses.has(operation.status)) {
    command.message = `${command.body.action}: ${operation.status}. Waiting for confirmed outcome…`;
    command.error = false;
    refreshOutputControls(state);
    return false;
  }
  command.pending = false;
  command.unknown = operation.status === "outcome_unknown";
  command.error = operation.status !== "applied" || Boolean(operation.error);
  if (operation.status === "applied" && operation.applied === true
    && (command.body.action === "set_tunein_station" || command.body.action === "select_favorite")) {
    state.stationResults = [];
  }
  if (operation.status === "applied" && operation.applied === true && command.body.action === "set_gain"
    && Number(state.draft) === command.body.gain_db) {
    state.savedGain = command.body.gain_db;
    if (state.output.control_revision === command.body.expected_control_revision
      && state.output.gain?.configured_db !== command.body.gain_db) {
      pendingControlRevisions.set(state.agent.agent_id, command.body.expected_control_revision);
    } else {
      state.dirty = false;
    }
  }
  const error = operation.error;
  const gainOnNextPlay = !command.error && command.body.action === "set_gain"
    && operation.gain_application === "on_next_play";
  command.message = `${command.error ? "Command result" : "Command confirmed"}: ${command.body.action} — ${operation.status}.`
    + (error ? ` ${error.code}: ${error.message} (retryable: ${error.retryable === true ? "yes" : "no"}).` : "")
    + (command.body.action === "set_tunein_station" || command.body.action === "select_favorite"
      ? ` Desired station persisted: ${operation.requested_source_persisted === true ? "yes" : operation.requested_source_persisted === false ? "no" : "unknown"}.`
      : "")
    + (command.favorites ? "" : ` Intention persisted: ${operation.requested_intention_persisted === true ? "yes" : operation.requested_intention_persisted === false ? "no" : "unknown"}.`)
    + (command.favorites ? "" : gainOnNextPlay
      ? " Calibration saved; applies on next play (not applied to the live mixer)."
      : ` Applied: ${operation.applied === true ? "yes" : operation.applied === false ? "no" : "unknown"}.`);
  if (command.unknown) {
    command.message += " Outcome unknown; check operation status before issuing another command.";
  }
  refreshOutputControls(state);
  fetchPlayback();
  return true;
}

async function pollOutputOperation(state, command) {
  if (command.polling) {
    return;
  }
  command.polling = true;
  command.pending = true;
  command.unknown = false;
  command.error = false;
  command.message = `${command.body.action}: checking operation status…`;
  refreshOutputControls(state);
  const deadline = Date.now() + operationPollDeadlineMs;
  try {
    for (let attempt = 0; attempt < operationPollLimit && Date.now() < deadline; attempt += 1) {
      try {
        const { response, envelope } = await operationRequest(
          `/api/playback/agents/${encodeURIComponent(state.agent.agent_id)}/operations/${encodeURIComponent(command.id)}`);
        if (response.status !== 200) {
          throw new Error(`Operation status request failed (${response.status}).`);
        }
        if (acceptOperation(state, command, operationFromEnvelope(envelope, state, command))) {
          return;
        }
      } catch (error) {
        command.error = true;
        command.message = `Command status check failed: ${error.message || "Unable to check status."} Retrying this operation only…`;
        refreshOutputControls(state);
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
    operationUnknown(state, command, "Status polling reached its time or attempt limit.");
  } finally {
    command.polling = false;
  }
}

async function sendOutputOperation(state, command) {
  command.pending = true;
  command.unknown = false;
  command.error = false;
  command.message = `${command.body.action}: submitting…`;
  refreshOutputControls(state);
  try {
    const { response, envelope } = await operationRequest(command.path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(command.body),
    });
    if (response.status !== 202) {
      const detail = envelope?.error;
      const description = `Submission failed (${response.status})${detail?.message ? `: ${detail.message}` : "."}`;
      if (response.status >= 500) {
        operationUnknown(state, command, description);
      } else {
        command.pending = false;
        command.error = true;
        command.message = `Command rejected: ${description}${detail?.code ? ` [${detail.code}]` : ""}`;
        refreshOutputControls(state);
      }
      return;
    }
    const operation = operationFromEnvelope(envelope, state, command);
    if (!acceptOperation(state, command, operation)) {
      await pollOutputOperation(state, command);
    }
  } catch (error) {
    operationUnknown(state, command, error.message || "Submission response was lost.");
  }
}

async function searchTuneInStations(state) {
  if (!state.dialogElements || state.stationSearching) return;
  const query = state.stationQuery.trim();
  if (query.length < 2 || query.length > 80) {
    state.stationError = "Enter at least two characters to search.";
    updateOutputDialog(state);
    return;
  }
  state.stationSearching = true;
  state.stationError = "";
  state.stationResults = [];
  updateOutputDialog(state);
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 5000);
  try {
    const response = await apiFetch(
      `/api/playback/catalogs/tunein/search?q=${encodeURIComponent(query)}`,
      { signal: controller.signal },
    );
    const envelope = await response.json();
    if (response.status !== 200 || envelope?.api_version !== 1
      || envelope.provider !== "tunein" || !Array.isArray(envelope.stations)) {
      throw new Error(envelope?.error?.message || "TuneIn search failed.");
    }
    state.stationResults = envelope.stations.filter((station) =>
      station && typeof station.id === "string" && /^s[0-9]{1,12}$/.test(station.id)
      && typeof station.name === "string" && station.name.length > 0 && station.name.length <= 120);
    if (!state.stationResults.length) state.stationError = "No playable stations found.";
  } catch (error) {
    state.stationError = error.message || "TuneIn search failed.";
  } finally {
    window.clearTimeout(timer);
    state.stationSearching = false;
    updateOutputDialog(state);
  }
}

const favoriteActions = new Set(["add_favorite", "remove_favorite", "move_favorite"]);

function submitOutputOperation(state, action, station, index) {
  if (!canWrite(state, action) || state.command?.pending || state.command?.unknown
    || (action === "stop" && state.output.playback?.value?.state === "stop")
    || (action === "play" && !hasStation(state))
    || (action === "set_gain" && !validGain(state.draft))
    || (["set_tunein_station", "add_favorite"].includes(action) && !station?.id)
    || (["select_favorite", "remove_favorite", "move_favorite"].includes(action) && !station?.key)) {
    return;
  }
  const randomKey = globalThis.crypto?.randomUUID?.()
    || `${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  const body = {
    action,
    expected_config_revision: state.agent.snapshot.config_revision,
    expected_control_revision: state.output.control_revision,
    idempotency_key: randomKey,
  };
  if (action === "set_gain") {
    body.gain_db = Number(state.draft);
  } else if (action === "set_tunein_station" || action === "add_favorite") {
    body.station_id = station.id;
    body.station_name = station.name;
  } else if (action !== "play" && action !== "pause" && action !== "stop") {
    body.key = station.key;
    if (action === "move_favorite") body.index = index;
  }
  const favorites = favoriteActions.has(action);
  const agentPath = `/api/playback/agents/${encodeURIComponent(state.agent.agent_id)}`;
  state.command = {
    body,
    favorites,
    path: favorites
      ? `${agentPath}/favorites/operations`
      : `${agentPath}/outputs/${encodeURIComponent(state.output.id)}/operations`,
  };
  sendOutputOperation(state, state.command);
}

function appendPlaybackText(parent, className, text) {
  const element = document.createElement("p");
  element.className = className;
  element.textContent = text;
  parent.appendChild(element);
}

function appendPlaybackWarning(parent, title, detail) {
  const warning = document.createElement("div");
  warning.className = "backend-warning-banner";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const description = document.createElement("span");
  description.textContent = detail;
  warning.append(heading, description);
  parent.appendChild(warning);
  parent.hidden = false;
}

function renderPlaybackWarning(title, detail) {
  message.replaceChildren();
  appendPlaybackWarning(message, title, detail);
}

function meaningfulPlaybackValue(value) {
  if (Array.isArray(value)) {
    return value.map(meaningfulPlaybackValue);
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.keys(value)
        .filter((key) => !volatilePlaybackFields.has(key))
        .sort()
        .map((key) => [key, meaningfulPlaybackValue(value[key])]),
    );
  }
  return value;
}

function playbackSignature(playback) {
  return JSON.stringify(meaningfulPlaybackValue(playback));
}

function playbackFreshness(playbackProbe, agent) {
  const ageMs = playbackProbe.age_ms ?? agent.snapshot.snapshot_age_ms;
  const successTime = Date.parse(agent.last_success_at || "");
  const elapsedSinceSuccess = Number.isFinite(successTime) ? Date.now() - successTime : null;
  const knownAgeMs = Number.isFinite(ageMs) ? Math.max(0, ageMs) : null;
  const effectiveAgeMs = elapsedSinceSuccess === null
    ? knownAgeMs
    : knownAgeMs === null ? elapsedSinceSuccess : Math.max(knownAgeMs, elapsedSinceSuccess);
  const isStale = agent.status !== "ok" || playbackProbe.stale || agent.snapshot.stale;
  return isStale
    ? `Stale${effectiveAgeMs === null ? "" : ` · last data ${Math.floor(effectiveAgeMs / 1000)}s old`}`
    : effectiveAgeMs === null ? "Update age unknown"
      : effectiveAgeMs < 4000 ? "Updated now" : `Updated ${Math.floor(effectiveAgeMs / 1000)}s ago`;
}

function updatePlaybackFreshness(playback) {
  const cards = new Map(
    Array.from(outputsContainer.querySelectorAll(".playback-card"))
      .map((card) => [card.dataset.outputKey, card]),
  );
  for (const agent of playback.agents || []) {
    for (const output of agent.snapshot?.outputs || []) {
      const key = JSON.stringify([agent.agent_id, output.id]);
      const card = cards.get(key);
      const freshness = card?.querySelector(".playback-freshness");
      if (freshness) {
        freshness.textContent = playbackFreshness(output.playback || {}, agent);
      }
    }
  }
}

function renderPlayback(playback) {
  trackResizeObserver.disconnect();
  playbackAvailable = true;
  for (const state of outputControls.values()) state.present = false;
  const presentKeys = new Set((playback.configured ? playback.agents || [] : [])
    .flatMap((agent) => (agent.snapshot?.outputs || []).map((output) => JSON.stringify([agent.agent_id, output.id]))));
  if (activeOutputKey && !presentKeys.has(activeOutputKey)) configDialog.close();
  outputsContainer.replaceChildren();
  message.replaceChildren();
  message.hidden = true;

  if (!playback.configured) {
    health.textContent = "Not configured";
    health.dataset.status = "unavailable";
    renderPlaybackWarning("Playback unavailable", "No playback agents are configured.");
    return;
  }

  const agents = playback.agents || [];
  const allHealthy = agents.length > 0 && agents.every((agent) => agent.status === "ok");
  health.textContent = allHealthy ? "Connected" : "One or more agents unavailable";
  health.dataset.status = allHealthy ? "ok" : "unavailable";
  for (const agent of agents) {
    const outputs = agent.snapshot?.outputs || [];
    if (outputs.length === 0) {
      appendPlaybackWarning(
        message,
        "Playback agent unavailable",
        `${agent.agent_id}: ${agent.error?.message || "Waiting for stream status."}`,
      );
      continue;
    }
    for (const output of outputs) {
      const card = document.createElement("article");
      card.className = "playback-card";
      card.dataset.outputKey = JSON.stringify([agent.agent_id, output.id]);
      const playbackProbe = output.playback || {};
      const controlState = outputControlState(agent, output);
      const observed = playbackProbe.value || playbackProbe.last_known?.value || {};
      const nowPlaying = observed.now_playing || {};
      const stationId = output.selection?.provider === "tunein" ? output.selection.station_id : null;
      const logo = stationLogo(card.dataset.outputKey, stationId);
      if (logo) card.appendChild(logo);
      const title = document.createElement("h3");
      title.textContent = `${agent.agent_id} · ${output.label}`;
      card.appendChild(title);

      const station = [output.selection?.station_name, nowPlaying.station]
        .find((value) => typeof value === "string" && value);
      const playbackState = observed.state || playbackProbe.status || "unknown";
      const stationText = station || (output.control?.has_station === false ? "No station" : "");
      appendPlaybackText(card, "playback-state", `${playbackState}${stationText ? ` · ${stationText}` : ""}`);
      const track = [nowPlaying.artist, nowPlaying.title].filter((value) => typeof value === "string" && value).join(" — ");
      if (track) {
        const line = document.createElement("p");
        line.className = "playback-track";
        line.title = track;
        const text = document.createElement("span");
        text.textContent = track;
        line.appendChild(text);
        card.appendChild(line);
        trackResizeObserver.observe(line);
        trackResizeObserver.observe(text);
      }
      if (typeof nowPlaying.album === "string" && nowPlaying.album) {
        appendPlaybackText(card, "playback-meta", nowPlaying.album);
      }
      if (typeof output.gain?.configured_db === "number" && Number.isFinite(output.gain.configured_db)) {
        appendPlaybackText(card, "playback-meta", `Configured gain: ${output.gain.configured_db.toFixed(1)} dB`);
      }
      appendPlaybackText(card, "playback-meta playback-freshness", playbackFreshness(playbackProbe, agent));
      if (playbackProbe.error?.message) {
        appendPlaybackText(card, "playback-error", playbackProbe.error.message);
      }
      const config = commandButton(card, "Config", false, () => openOutputConfig(controlState));
      config.className = "playback-config-button";
      config.setAttribute("aria-haspopup", "dialog");
      config.setAttribute("aria-label", `Config ${agent.agent_id}, ${output.label}, ${output.id}`);
      updateOutputDialog(controlState);
      card.setAttribute("aria-label", `${agent.agent_id}, ${output.label}, ${playbackState}`);
      outputsContainer.appendChild(card);
    }
    if (agent.error?.message) {
      appendPlaybackWarning(
        message,
        "Playback agent unavailable",
        `${agent.agent_id}: ${agent.error.message}`,
      );
    }
  }
}

async function fetchPlayback() {
  if (playbackFetchInFlight) {
    playbackFetchQueued = true;
    return;
  }
  playbackFetchInFlight = true;
  try {
    const response = await apiFetch("/api/playback");
    if (!response.ok) {
      throw new Error(`Playback status request failed (${response.status}).`);
    }
    const playback = await response.json();
    const signature = playbackSignature(playback);
    if (signature !== lastPlaybackSignature) {
      lastPlaybackSignature = signature;
      renderPlayback(playback);
    } else {
      updatePlaybackFreshness(playback);
    }
  } catch (error) {
    playbackAvailable = false;
    for (const state of outputControls.values()) updateOutputDialog(state);
    lastPlaybackSignature = null;
    health.textContent = "Unavailable";
    health.dataset.status = "unavailable";
    renderPlaybackWarning(
      "Playback status unavailable",
      error instanceof Error ? error.message : "Unable to load playback status.",
    );
  } finally {
    playbackFetchInFlight = false;
    if (playbackFetchQueued) {
      playbackFetchQueued = false;
      fetchPlayback();
    }
  }
}

function startPlaybackSync() {
  if (!window.EventSource || eventSource || document.hidden) {
    return;
  }
  eventSource = new EventSource("/api/events");
  eventSource.addEventListener("playback-change", fetchPlayback);
  eventSource.onopen = fetchPlayback;
  eventSource.onerror = () => {
    if (eventSource && eventSource.readyState === EventSource.CLOSED) {
      stopPlaybackSync();
      eventSourceRetryTimer = window.setTimeout(startPlaybackSync, eventSourceRetryDelayMs);
    }
  };
}

function stopPlaybackSync() {
  if (eventSourceRetryTimer !== null) {
    window.clearTimeout(eventSourceRetryTimer);
    eventSourceRetryTimer = null;
  }
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

function startPlaybackPolling() {
  if (playbackPollTimer === null && !document.hidden) {
    playbackPollTimer = window.setInterval(fetchPlayback, playbackPollIntervalMs);
  }
}

function stopPlaybackPolling() {
  if (playbackPollTimer !== null) {
    window.clearInterval(playbackPollTimer);
    playbackPollTimer = null;
  }
}

fetchPlayback();
startPlaybackSync();
startPlaybackPolling();

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPlaybackSync();
    stopPlaybackPolling();
    return;
  }
  fetchPlayback();
  startPlaybackSync();
  startPlaybackPolling();
});
