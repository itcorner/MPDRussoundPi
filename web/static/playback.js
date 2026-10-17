const health = document.getElementById("playbackHealth");
const message = document.getElementById("playbackMessage");
const outputsContainer = document.getElementById("playbackOutputs");

let eventSource = null;
let eventSourceRetryTimer = null;
let playbackPollTimer = null;
let playbackFetchInFlight = false;
let playbackFetchQueued = false;
let lastPlaybackSignature = null;
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

function apiFetch(path) {
  return fetch(path, { credentials: "same-origin" });
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
}

function renderPlaybackWarning(title, detail) {
  const warning = document.createElement("div");
  warning.className = "backend-warning-banner";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const description = document.createElement("span");
  description.textContent = detail;
  warning.append(heading, description);
  message.replaceChildren(warning);
  message.hidden = false;
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
    : effectiveAgeMs === null ? "Update age unknown" : `Updated ${Math.floor(effectiveAgeMs / 1000)}s ago`;
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
  outputsContainer.replaceChildren();
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
        outputsContainer,
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
      const observed = playbackProbe.value || playbackProbe.last_known?.value || {};
      const nowPlaying = observed.now_playing || {};
      const selection = output.selection?.label;
      const playbackState = observed.state || playbackProbe.status || "unknown";
      appendPlaybackText(card, "playback-state", `${playbackState}${selection ? ` · ${selection}` : ""}`);
      const track = [nowPlaying.artist, nowPlaying.title].filter((value) => typeof value === "string" && value).join(" — ");
      if (track) {
        appendPlaybackText(card, "playback-track", track);
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
      card.setAttribute("aria-label", `${agent.agent_id}, ${output.label}, ${playbackState}`);
      outputsContainer.appendChild(card);
    }
    if (agent.error?.message) {
      appendPlaybackWarning(
        outputsContainer,
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
