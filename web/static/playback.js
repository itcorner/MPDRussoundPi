const health = document.getElementById("playbackHealth");
const message = document.getElementById("playbackMessage");
const outputsContainer = document.getElementById("playbackOutputs");
const configDialog = document.getElementById("playbackConfigDialog");
const configTitle = document.getElementById("playbackConfigTitle");
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
const pendingConfigRevisions = new Map();
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

function apiFetch(path, options = {}) {
  return fetch(path, { ...options, credentials: "same-origin" });
}

function outputControlState(agent, output) {
  const key = JSON.stringify([agent.agent_id, output.id]);
  if (!outputControls.has(key)) {
    outputControls.set(key, { key, draft: "", dirty: false, command: null });
  }
  const state = outputControls.get(key);
  state.agent = agent;
  state.output = output;
  state.present = true;
  const pendingRevision = pendingConfigRevisions.get(agent.agent_id);
  if (pendingRevision && agent.snapshot.config_revision !== pendingRevision) {
    pendingConfigRevisions.delete(agent.agent_id);
  }
  if (state.savedGain !== undefined && !pendingConfigRevisions.has(agent.agent_id)
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
    && !pendingConfigRevisions.has(state.agent.agent_id)
    && state.agent.write_operations.includes(action)
    && typeof state.agent.snapshot?.config_revision === "string"
    && typeof state.output.control_revision === "string"
    && state.agent.status === "ok"
    && !state.agent.snapshot.stale
    && !state.output.playback?.stale;
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

function renderOutputControls(state) {
  const controls = document.createElement("div");
  controls.className = "playback-controls";
  const transport = document.createElement("div");
  transport.className = "playback-transport";
  const buttons = {};
  for (const action of ["play", "pause", "stop"]) {
    buttons[action] = commandButton(transport, action[0].toUpperCase() + action.slice(1),
      true, () => submitOutputOperation(state, action));
    buttons[action].dataset.action = action;
  }
  controls.appendChild(transport);

  const gainLabel = document.createElement("label");
  gainLabel.className = "playback-gain";
  gainLabel.textContent = "Calibration gain";
  gainLabel.setAttribute("for", "playbackGainSlider");
  const slider = document.createElement("input");
  slider.id = "playbackGainSlider";
  slider.type = "range";
  slider.max = "0";
  slider.step = "0.1";
  const gainValue = document.createElement("output");
  gainValue.setAttribute("for", slider.id);
  gainValue.className = "playback-gain-value";
  controls.append(gainLabel, slider, gainValue);
  const apply = commandButton(controls, "Apply gain",
    true, () => submitOutputOperation(state, "set_gain"));
  const validation = document.createElement("p");
  validation.className = "playback-gain-hint";
  slider.addEventListener("input", () => {
    state.draft = slider.value;
    state.dirty = true;
    updateOutputDialog(state);
  });
  const availability = document.createElement("p");
  availability.className = "playback-meta";
  const status = document.createElement("p");
  const feedback = document.createElement("div");
  feedback.className = "playback-command-feedback";
  feedback.setAttribute("tabindex", "0");
  feedback.setAttribute("role", "region");
  feedback.setAttribute("aria-label", "Command feedback");
  feedback.append(validation, availability, status);
  const retry = commandButton(feedback, "Retry identical submission", true, () => {
    const command = state.command;
    if (!command?.unknown || !state.present || !playbackAvailable) return;
    if (command.id) pollOutputOperation(state, command);
    else if (canWrite(state, command.body.action)) sendOutputOperation(state, command);
  });
  controls.appendChild(feedback);
  state.dialogElements = { buttons, slider, gainValue, apply, validation, availability, status, retry };
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
  configTitle.textContent = `Config · ${state.agent.agent_id} · ${state.output.label} (${state.output.id})`;
  const { buttons, slider, gainValue, apply, validation, availability, status, retry } = state.dialogElements;
  const blocked = Boolean(state.command?.pending || state.command?.unknown);
  for (const action of ["play", "pause", "stop"]) buttons[action].disabled = blocked || !canWrite(state, action);
  const playing = state.output.playback?.value?.state === "play";
  buttons.play.hidden = playing;
  buttons.pause.hidden = !playing;
  buttons.stop.disabled ||= state.output.playback?.value?.state === "stop";
  const valid = validGain(state.draft);
  // Include both configured and draft gain without rounding or clamping the saved value.
  const configuredGain = state.output.gain?.configured_db;
  slider.min = String(Math.min(-60, Number.isFinite(configuredGain) ? configuredGain : -60, valid ? Number(state.draft) : -60));
  slider.value = valid ? state.draft : slider.min;
  slider.disabled = blocked || !canWrite(state, "set_gain");
  gainValue.textContent = valid ? `${state.draft} dB` : "Gain unavailable";
  apply.disabled = slider.disabled || !valid;
  validation.textContent = valid
    ? "Draft only. Select Apply gain to send."
    : "No valid configured gain. Move the slider to choose a draft.";
  availability.hidden = ["play", "pause", "stop", "set_gain"].some((action) => canWrite(state, action));
  availability.textContent = pendingConfigRevisions.has(state.agent.agent_id)
    ? "Waiting for updated configuration before another command can be sent."
    : "Read-only: output unavailable, stale, or write capabilities/current revisions unavailable.";
  updateCommandStatus(status, state.command);
  validation.hidden = Boolean(state.command) || !availability.hidden;
  if (state.command) availability.hidden = true;
  retry.hidden = !state.command?.unknown;
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
    || operation.output_id !== state.output.id || operation.action !== command.body.action
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
  if (operation.status === "applied" && operation.applied === true && command.body.action === "set_gain"
    && Number(state.draft) === command.body.gain_db) {
    state.savedGain = command.body.gain_db;
    if (state.agent.snapshot.config_revision === command.body.expected_config_revision
      && state.output.gain?.configured_db !== command.body.gain_db) {
      pendingConfigRevisions.set(state.agent.agent_id, command.body.expected_config_revision);
    } else {
      state.dirty = false;
    }
  }
  const error = operation.error;
  const gainOnNextPlay = !command.error && command.body.action === "set_gain"
    && operation.gain_application === "on_next_play";
  command.message = `${command.error ? "Command result" : "Command confirmed"}: ${command.body.action} — ${operation.status}.`
    + (error ? ` ${error.code}: ${error.message} (retryable: ${error.retryable === true ? "yes" : "no"}).` : "")
    + ` Intention persisted: ${operation.requested_intention_persisted === true ? "yes" : operation.requested_intention_persisted === false ? "no" : "unknown"}.`
    + (gainOnNextPlay
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

function submitOutputOperation(state, action) {
  if (!canWrite(state, action) || state.command?.pending || state.command?.unknown
    || (action === "stop" && state.output.playback?.value?.state === "stop")
    || (action === "set_gain" && !validGain(state.draft))) {
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
  }
  state.command = {
    body,
    path: `/api/playback/agents/${encodeURIComponent(state.agent.agent_id)}/outputs/${encodeURIComponent(state.output.id)}/operations`,
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
      const title = document.createElement("h3");
      title.textContent = `${agent.agent_id} · ${output.label}`;
      card.appendChild(title);

      const playbackProbe = output.playback || {};
      const controlState = outputControlState(agent, output);
      const observed = playbackProbe.value || playbackProbe.last_known?.value || {};
      const nowPlaying = observed.now_playing || {};
      const selection = output.selection?.label;
      const playbackState = observed.state || playbackProbe.status || "unknown";
      appendPlaybackText(card, "playback-state", `${playbackState}${selection ? ` · ${selection}` : ""}`);
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
      if (!track && typeof nowPlaying.station === "string" && nowPlaying.station) {
        appendPlaybackText(card, "playback-meta", nowPlaying.station);
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
