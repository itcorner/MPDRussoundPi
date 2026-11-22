const clientCount = document.getElementById("clientCount");
const clientsList = document.getElementById("clientsList");
const clientsEmpty = document.getElementById("clientsEmpty");
const eventsEmpty = document.getElementById("eventsEmpty");
const eventsCount = document.getElementById("eventsCount");
const eventsTableWrap = document.getElementById("eventsTableWrap");
const eventsTableBody = document.getElementById("eventsTableBody");
const hostList = document.getElementById("hostList");
const overviewList = document.getElementById("overviewList");
const overviewEmpty = document.getElementById("overviewEmpty");
const overviewLevel = document.getElementById("overviewLevel");
const overviewUpdate = document.getElementById("overviewUpdate");
const adminPanel = document.getElementById("adminPanel");
const adminState = document.getElementById("adminState");
const adminLoginForm = document.getElementById("adminLoginForm");
const adminPassword = document.getElementById("adminPassword");
const adminSession = document.getElementById("adminSession");
const adminSessionText = document.getElementById("adminSessionText");
const adminLogout = document.getElementById("adminLogout");
const adminSetup = document.getElementById("adminSetup");
const adminHttpWarning = document.getElementById("adminHttpWarning");
const adminHttps = document.getElementById("adminHttps");
const adminHttpsLink = document.getElementById("adminHttpsLink");
const adminMessage = document.getElementById("adminMessage");
const adminUpdate = document.getElementById("adminUpdate");

let eventSource = null;
let eventSourceRetryTimer = null;
let statusPollTimer = null;
const eventSourceRetryDelayMs = 30000;
const statusPollIntervalMs = 5000;
let statusFetchInFlight = false;
let statusFetchQueued = false;
let hostFetchInFlight = false;
let overviewFetchInFlight = false;
const expandedPayloadRows = new Set();

function formatTimestampLines(rawTimestamp) {
  const fallback = String(rawTimestamp || "");
  const parsed = new Date(fallback);

  if (Number.isNaN(parsed.getTime())) {
    const pieces = fallback.trim().split(/\s+/);
    if (pieces.length >= 2) {
      return { date: pieces[0], time: pieces.slice(1).join(" ") };
    }
    return { date: fallback || "-", time: "" };
  }

  const date = parsed.toLocaleDateString(undefined, {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  const time = parsed.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
  return { date, time };
}

function eventKey(event) {
  return `${event.timestamp}|${event.ip}|${event.path}|${JSON.stringify(event.payload)}`;
}

// Client and event fields come from arbitrary network peers (IP, User-Agent,
// request payloads), so they are only ever assigned as text, never as HTML.
function textElement(tagName, className, text) {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  element.textContent = String(text ?? "");
  return element;
}

function renderStatus(payload, historyPayload) {
  const clients = payload.connected_clients || [];
  const events = historyPayload.recent_events || [];

  clientCount.textContent = String(clients.length);
  clientsEmpty.hidden = clients.length !== 0;
  clientsList.replaceChildren();

  for (const client of clients) {
    const item = document.createElement("article");
    item.className = "status-list-item";
    const header = document.createElement("div");
    header.className = "status-item-row";
    header.append(
      textElement("strong", "", client.ip),
      textElement("span", "status-pill", `Client ${client.id}`),
    );
    item.append(
      header,
      textElement("div", "status-meta", `Connected: ${client.connected_at}`),
      textElement("div", "status-meta", `Session: ${client.session_id || "—"}`),
      textElement("div", "status-meta", `Open streams: ${client.streams || 1}`),
      textElement("div", "status-meta", `User-Agent: ${client.user_agent || "Unknown"}`),
    );
    clientsList.appendChild(item);
  }

  eventsEmpty.hidden = events.length !== 0;
  eventsTableWrap.hidden = events.length === 0;
  eventsCount.textContent = String(events.length);
  eventsTableBody.replaceChildren();
  const visibleKeys = new Set();
  for (const event of events) {
    const key = eventKey(event);
    const encodedKey = encodeURIComponent(key);
    visibleKeys.add(encodedKey);
    const payloadText = JSON.stringify(event.payload);
    const expanded = expandedPayloadRows.has(encodedKey);
    const timestamp = formatTimestampLines(event.timestamp);
    const row = document.createElement("tr");

    const timeCell = document.createElement("td");
    const timeBlock = document.createElement("div");
    timeBlock.className = "status-time";
    timeBlock.append(
      textElement("span", "status-time-date", timestamp.date),
      textElement("span", "status-time-value", timestamp.time),
    );
    timeCell.appendChild(timeBlock);

    const payloadCell = document.createElement("td");
    const payloadToggle = document.createElement("button");
    payloadToggle.className = "payload-toggle";
    payloadToggle.type = "button";
    payloadToggle.setAttribute("data-event-key", encodedKey);
    payloadToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    payloadToggle.title = expanded ? "Click to collapse payload" : "Click to expand payload";
    const payloadPreview = textElement("code", "payload-preview", payloadText);
    payloadToggle.appendChild(payloadPreview);
    payloadCell.appendChild(payloadToggle);

    row.append(timeCell, textElement("td", "", event.ip), textElement("td", "", event.path), payloadCell);
    eventsTableBody.appendChild(row);

    // Always test truncation in collapsed mode so expanded rows are not misclassified.
    payloadToggle.setAttribute("aria-expanded", "false");
    // A collapsed panel has no layout (width 0): keep the toggle rather than judging from nothing.
    const isTruncated = !(payloadPreview.clientWidth > 0) || payloadPreview.scrollWidth > payloadPreview.clientWidth;
    payloadToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    if (isTruncated) {
      payloadToggle.setAttribute("data-payload-toggle", "");
    } else {
      expandedPayloadRows.delete(encodedKey);
      payloadToggle.replaceWith(textElement("code", "payload-preview payload-preview-static", payloadText));
    }
  }

  for (const key of Array.from(expandedPayloadRows)) {
    if (!visibleKeys.has(key)) {
      expandedPayloadRows.delete(key);
    }
  }
}

// Host facts: every string below comes from validated server fields and is
// assigned as text only. The server decides labels and builds the detail rows.
const hostLevelText = { ok: "OK", warning: "Warning", problem: "Problem", unknown: "Unknown" };

function hostFreshness(host) {
  if (host.status === "unsupported") {
    return "This agent does not report host information yet. Update the agent and the playback proxy to enable it.";
  }
  if (host.status === "pending") {
    return "Waiting for the first host report.";
  }
  if (host.status !== "ok" || !host.host) {
    return `Host information unavailable${host.error && host.error.message ? `: ${host.error.message}` : "."}`;
  }
  const ages = Object.values(host.host.sections)
    .filter((section) => section.status === "ok" && Number.isFinite(section.age_ms))
    .map((section) => section.age_ms);
  const seconds = ages.length ? Math.round(Math.min(...ages) / 1000) : null;
  if (host.stale) {
    return seconds === null ? "Data is old." : `Data is old (last update ${seconds} s ago).`;
  }
  return seconds === null ? "Updated." : seconds < 4 ? "Updated now." : `Updated ${seconds} s ago.`;
}

function renderHosts(payload) {
  const hosts = payload.hosts || [];
  hostList.replaceChildren();
  for (const host of hosts) {
    const view = host.host ? host.host.view : null;
    hostList.appendChild(overviewCard({
      id: `host:${host.agent_id}`,
      title: `Audio host (${host.agent_id})`,
      level: view ? view.overall : "unknown",
      summary: view && view.summary.length ? view.summary.join(" · ") : "",
      note: hostFreshness(host),
      rows: [],
      groups: view ? (view.groups || []) : [],
    }));
  }
}

// System overview: sections and groups come from the server as plain text with
// the level spelled out; nothing here is ever treated as markup.
const openOverviewDetails = new Set();

function overviewRows(rows) {
  const list = document.createElement("dl");
  list.className = "host-rows";
  for (const row of rows) {
    const flagged = hostLevelText[row.level] && row.level !== "ok";
    const value = textElement("dd", flagged ? `host-row-${row.level}` : "", flagged ? `${row.value} (${hostLevelText[row.level]})` : row.value);
    if (typeof row.link === "string" && row.link.startsWith("https://hub.docker.com/r/")) {
      // Only a Docker Hub address from the server becomes a link; the text stays text.
      const anchor = textElement("a", "", row.value);
      anchor.setAttribute("href", row.link);
      anchor.setAttribute("target", "_blank");
      anchor.setAttribute("rel", "noopener noreferrer");
      value.replaceChildren(anchor);
    }
    list.append(textElement("dt", "", row.label), value);
  }
  return list;
}

function overviewHeader(title, level, strong) {
  const header = document.createElement("div");
  header.className = "status-item-row";
  header.append(
    textElement(strong ? "strong" : "span", strong ? "" : "overview-group-title", title),
    textElement("span", `status-pill host-level host-level-${hostLevelText[level] ? level : "unknown"}`, hostLevelText[level] || "Unknown"),
  );
  return header;
}

function overviewDetails(key, label) {
  const details = document.createElement("details");
  details.className = "host-details";
  details.open = openOverviewDetails.has(key);
  details.addEventListener("toggle", () => {
    if (details.open) {
      openOverviewDetails.add(key);
    } else {
      openOverviewDetails.delete(key);
    }
  });
  details.appendChild(textElement("summary", "", label));
  return details;
}

function overviewCard(section) {
  const item = document.createElement("article");
  item.className = "status-list-item host-card";
  item.appendChild(overviewHeader(section.title, section.level, true));
  if (section.summary) {
    item.appendChild(textElement("div", "status-meta host-summary", section.summary));
  }
  if (section.note) {
    item.appendChild(textElement("div", "status-meta", section.note));
  }
  const groups = section.groups || [];
  if (section.rows.length || groups.length) {
    const details = overviewDetails(section.id, "Details");
    if (section.rows.length) {
      details.appendChild(overviewRows(section.rows));
    }
    for (const group of groups) {
      const box = document.createElement("div");
      box.className = "overview-group";
      box.appendChild(overviewHeader(group.title, group.level, false));
      box.appendChild(textElement("div", "status-meta", group.summary));
      const inner = overviewDetails(`${section.id}/${group.id}`, "Show");
      inner.appendChild(overviewRows(group.rows));
      box.appendChild(inner);
      details.appendChild(box);
    }
    item.appendChild(details);
  }
  return item;
}

// Administrator session. The CSRF token stays in memory and is sent with every
// privileged request; the password field is cleared after each attempt.
let adminCsrfToken = null;
let adminShownState = null;

function minutesText(seconds) {
  const minutes = Math.max(1, Math.round(seconds / 60));
  return minutes === 1 ? "1 minute" : `${minutes} minutes`;
}

function renderAdmin(session) {
  const configured = session.configured === true;
  const authenticated = configured && session.authenticated === true;
  adminCsrfToken = authenticated && typeof session.csrf_token === "string" ? session.csrf_token : null;
  const [text, level] = !configured ? ["Not set up", "unknown"]
    : authenticated ? ["Logged in", "ok"]
      : session.locked_for_seconds > 0 ? ["Locked", "warning"] : ["Not logged in", "unknown"];
  adminState.textContent = text;
  adminState.className = `status-pill host-level host-level-${level}`;
  // Open the panel when a login is possible but not done; collapse it once logged
  // in or when nothing can be done here. Only on a change of state, so a panel the
  // user opened or closed stays that way across refreshes.
  // On page load: open when a login is possible but not done. Later, logging in
  // keeps the panel open; logging out or an expired session opens it again.
  const shownState = !configured ? "setup" : authenticated ? "in" : "out";
  if (shownState !== adminShownState) {
    if (adminShownState === null) {
      adminPanel.open = shownState === "out";
    } else if (shownState === "out") {
      adminPanel.open = true;
    } else if (shownState === "setup") {
      adminPanel.open = false;
    }
    adminShownState = shownState;
  }
  // Behind the HTTPS proxy the server refuses logins over plain HTTP; offer the HTTPS address instead.
  const secure = session.secure === true;
  const httpsUrl = typeof session.https_url === "string" && session.https_url.startsWith("https://")
    ? session.https_url : null;
  const httpsOnly = session.https_required === true && !secure && httpsUrl !== null;
  adminSetup.hidden = configured;
  adminLoginForm.hidden = !configured || authenticated || httpsOnly;
  adminHttpWarning.hidden = secure;
  adminHttps.hidden = !configured || authenticated || !httpsOnly;
  if (httpsOnly) {
    adminHttpsLink.href = `${httpsUrl}status`;
    adminHttpsLink.textContent = `${httpsUrl}status`;
  }
  adminSession.hidden = !authenticated;
  if (authenticated) {
    adminSessionText.textContent = `Logged in. The session ends after ${minutesText(session.idle_expires_in_seconds)} without activity`
      + ` and at the latest in ${minutesText(session.expires_in_seconds)}.`;
  }
  if (configured && !authenticated && session.locked_for_seconds > 0) {
    showAdminMessage(`Too many failed attempts. Login is locked for about ${minutesText(session.locked_for_seconds)}.`);
  }
}

function showAdminMessage(text) {
  adminMessage.textContent = text || "";
  adminMessage.hidden = !text;
}

function showAdminFailure(text) {
  showAdminMessage(text);
  // Back to the field, so the next attempt can be typed right away.
  adminPassword.focus();
}

async function fetchAdminSession() {
  try {
    const response = await apiFetch("/api/admin/session");
    if (response.ok) {
      renderAdmin(await response.json());
    }
  } catch {
    // The next poll retries.
  }
}

async function adminRequest(path, body) {
  const headers = { "Content-Type": "application/json" };
  if (adminCsrfToken) {
    headers["X-CSRF-Token"] = adminCsrfToken;
  }
  const response = await apiFetch(path, { method: "POST", headers, body: JSON.stringify(body || {}) });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    payload = {};
  }
  return { response, payload };
}

async function submitAdminLogin(event) {
  event.preventDefault();
  const password = adminPassword.value;
  adminPassword.value = "";
  showAdminMessage("");
  try {
    const { response, payload } = await adminRequest("/api/admin/login", { password });
    if (response.ok) {
      renderAdmin(payload);
      fetchAdminUpdate();
    } else {
      showAdminFailure(payload.error && payload.error.message ? payload.error.message : "Login failed.");
    }
  } catch {
    showAdminFailure("The server could not be reached.");
  }
  fetchAdminSession();
}

async function submitAdminLogout() {
  try {
    await adminRequest("/api/admin/logout");
  } catch {
    // The session state below shows the result.
  }
  showAdminMessage("");
  fetchAdminSession();
}

// Software updates in the Administrator panel. Everyone sees what is installed,
// what is newest and can ask for a check; a logged-in administrator can install
// the available update. The review card is built once per review, so a refresh
// never clears what is being typed into its confirmation field.
const updateStateText = {
  up_to_date: "Up to date",
  update_available: "Update available",
  pending: "Not checked yet",
  checking: "Checking…",
  check_failed: "Check failed",
  unsupported: "Not checked",
  disabled: "Turned off",
};
const updatePhaseText = {
  resolving: "Preparing the update…",
  approved: "Confirmed. Starting…",
  running: "Updating. The page loses its connection for a few minutes and reconnects by itself.",
  succeeded: "The last update finished.",
  up_to_date: "The newest image is already installed.",
  cancelled: "The last update was not started.",
  failed: "The last update failed after the services were stopped.",
  failed_before_interruption: "The last update stopped before anything was interrupted.",
  outcome_unknown: "The outcome of the last update is unknown. Check the deployment on the Pi before trying again.",
  restoring: "Restoring the previous version. The page reconnects by itself.",
  restored: "The previous version was restored.",
};
let latestUpdates = null;
let pageBuild = null;
let adminUpdateState = null;
let adminUpdateReconnecting = false;
let adminUpdateTimer = null;
let pendingReauthAction = null;
let renderedReviewKey = null;
let updateCheckInFlight = false;

function shortVersion(version) {
  if (typeof version !== "string") {
    return "unknown";
  }
  return version.startsWith("dev-") ? version.slice(0, 11) : version;
}

function shortRevision(revision) {
  return typeof revision === "string" && revision.length >= 7 ? revision.slice(0, 7) : "unknown";
}

function ageText(iso) {
  const moment = Date.parse(iso || "");
  if (!Number.isFinite(moment)) {
    return null;
  }
  const seconds = Math.max(0, Math.round((Date.now() - moment) / 1000));
  return seconds < 60 ? "just now" : seconds < 7200 ? `${Math.round(seconds / 60)} min ago` : `${Math.round(seconds / 3600)} h ago`;
}

function updateButton(label, handler, { primary = false, disabled = false } = {}) {
  const button = textElement("button", primary ? "compact-button compact-button-primary" : "compact-button", label);
  button.type = "button";
  button.disabled = disabled;
  button.addEventListener("click", handler);
  return button;
}

function updateAvailableVersion() {
  const result = latestUpdates && latestUpdates.result;
  return latestUpdates && latestUpdates.state === "update_available" && result && result.available
    ? result.available : null;
}

function renderUpdateInfo(box) {
  box.replaceChildren();
  const updates = latestUpdates;
  if (!updates) {
    box.appendChild(textElement("p", "status-meta", "Loading…"));
    return;
  }
  const rows = [];
  if (updates.installed) {
    rows.push(["Installed", shortVersion(updates.installed.version)]);
  }
  const result = updates.result || null;
  const available = result && result.available ? result.available : null;
  if (available) {
    const published = ageText(available.published_at);
    rows.push(["Newest published", shortVersion(available.version) + (published ? ` (published ${published})` : "")]);
  }
  const state = updateStateText[updates.state] || "Unknown";
  rows.push(["Status", state + (updates.stale ? " (last known result)" : "")]);
  if (updates.check_failed && updates.error) {
    rows.push(["Last check", updates.error]);
  } else if (updates.checked_at) {
    rows.push(["Last check", ageText(updates.checked_at) || "unknown"]);
  }
  if (updates.reason) {
    rows.push(["Note", updates.reason]);
  }
  const list = document.createElement("dl");
  list.className = "host-rows";
  for (const [label, value] of rows) {
    list.append(textElement("dt", "", label), textElement("dd", "", value));
  }
  box.appendChild(list);
  if (updates.state !== "unsupported" && updates.state !== "disabled") {
    const manual = updates.manual_check || {};
    const row = document.createElement("div");
    row.className = "admin-form-row";
    row.appendChild(updateButton("Check now", requestUpdateCheck, { disabled: !manual.available || updateCheckInFlight }));
    if (available && updates.state === "update_available" && typeof available.link === "string"
        && available.link.startsWith("https://hub.docker.com/r/")) {
      const link = textElement("a", "status-meta", "Docker Hub");
      link.setAttribute("href", available.link);
      link.setAttribute("target", "_blank");
      link.setAttribute("rel", "noopener noreferrer");
      row.appendChild(link);
    }
    if (!manual.available && manual.retry_after_seconds > 0) {
      row.appendChild(textElement("span", "status-meta", `Check again in ${manual.retry_after_seconds} s`));
    }
    box.appendChild(row);
  }
}

function cleanupText(cleanup) {
  if (!cleanup || !Number.isInteger(cleanup.removed) || cleanup.removed <= 0) {
    return "";
  }
  const megabytes = Math.round((cleanup.freed_bytes || 0) / 1e6);
  return `Removed ${cleanup.removed} old image${cleanup.removed === 1 ? "" : "s"} (${megabytes} MB).`;
}

function renderReview(review) {
  const box = document.createElement("div");
  box.className = "admin-review";
  const isRestore = review.action === "restore";
  box.appendChild(textElement("strong", "", isRestore ? "Review: restore the previous version"
    : `Review: update to dev-${shortRevision(review.revision)}`));
  const rows = [["Interruption", review.interruption || ""], ["Backup", review.backup || ""]];
  if (review.cleanup) {
    rows.push(["Old images", review.cleanup]);
  }
  if (review.layout_change) {
    rows.push(["Layout change", review.layout_change]);
  }
  for (const [name, reference] of Object.entries(review.images || {})) {
    rows.push([`Image (${name})`, reference || ""]);
  }
  const list = document.createElement("dl");
  list.className = "host-rows";
  for (const [label, value] of rows) {
    list.append(textElement("dt", "", label), textElement("dd", "", value));
  }
  const countdown = textElement("dd", "review-countdown", `${review.expires_in_seconds} s`);
  list.append(textElement("dt", "", "Confirm within"), countdown);
  box.appendChild(list);
  const word = isRestore ? "restore" : "update";
  const row = document.createElement("div");
  row.className = "admin-form-row";
  const input = document.createElement("input");
  input.type = "text";
  input.autocomplete = "off";
  input.className = "admin-confirm-input";
  input.setAttribute("aria-label", `Type ${word} to confirm`);
  input.placeholder = `Type "${word}" to confirm`;
  const confirm = updateButton(isRestore ? "Confirm restore" : "Confirm update",
    () => runUpdateAction("approve", { operation: review.operation, digest: review.digest }), { primary: true, disabled: true });
  input.addEventListener("input", () => {
    confirm.disabled = input.value.trim().toLowerCase() !== word;
  });
  row.append(input, confirm, updateButton("Cancel", () => runUpdateAction("cancel", { operation: review.operation })));
  box.appendChild(row);
  box.countdown = countdown;
  return box;
}

function renderInstall(box) {
  const status = adminUpdateState;
  const review = status && status.review;
  const awaiting = status && (status.phase === "awaiting_approval" || status.phase === "restore_awaiting_approval");
  if (awaiting && review && adminCsrfToken) {
    const key = `${review.operation}/${review.digest}`;
    if (key === renderedReviewKey && box.reviewCard) {
      // Same review: only the countdown changes, the typed confirmation stays.
      box.reviewCard.countdown.textContent = `${review.expires_in_seconds} s`;
      return;
    }
    renderedReviewKey = key;
    box.reviewCard = renderReview(review);
    box.replaceChildren(box.reviewCard);
    return;
  }
  renderedReviewKey = null;
  box.reviewCard = null;
  box.replaceChildren();
  if (adminUpdateReconnecting) {
    box.appendChild(textElement("p", "status-meta", "Waiting for the web application to come back…"));
    return;
  }
  const available = updateAvailableVersion();
  if (!status || status.configured === false) {
    if (available && adminCsrfToken) {
      box.appendChild(textElement("p", "status-meta", "Installing updates from this page is not set up here. It needs "
        + "the updater service and works only when the containers run on one Pi."));
    }
    return;
  }
  const phase = status.phase;
  if (phase && phase !== "idle" && updatePhaseText[phase]) {
    // The updater's error is a complete sentence for this exact outcome (a failed
    // restore also ends in "failed"); the phase text is only the fallback, so the
    // same failure is never stated twice.
    const detailed = status.error && !["cancelled", "up_to_date"].includes(phase);
    box.appendChild(textElement("p", phase === "failed" || phase === "outcome_unknown" ? "admin-message" : "status-meta",
      detailed ? status.error : updatePhaseText[phase]));
    const removed = cleanupText(status.cleanup);
    if (phase === "succeeded" && removed) {
      box.appendChild(textElement("p", "status-meta", removed));
    }
  }
  const row = document.createElement("div");
  row.className = "admin-form-row";
  if (adminCsrfToken && status.restore_available) {
    row.appendChild(updateButton("Restore previous version",
      () => runUpdateAction("restore", { operation: status.operation }), { primary: true }));
  }
  if (available && !status.busy && phase !== "outcome_unknown") {
    if (adminCsrfToken) {
      row.appendChild(updateButton(`Update to ${shortVersion(available.version)}`,
        () => runUpdateAction("review", {}), { primary: true }));
    } else {
      box.appendChild(textElement("p", "status-meta", "Log in as administrator to install it."));
    }
  }
  if (row.children.length) {
    box.appendChild(row);
  }
}

function buildIdentity(build) {
  if (!build || typeof build.revision !== "string" || build.revision === "") {
    return "";
  }
  return `${build.revision}@${typeof build.built_at === "string" ? build.built_at : ""}`;
}

// After an update the web application comes back as a new build, but this page
// still runs the old code. Reload once so the new page shows the result.
function reloadForNewBuild(build) {
  const identity = buildIdentity(build);
  if (!identity) {
    return false; // builds without an identity never trigger a reload
  }
  if (pageBuild === null) {
    pageBuild = identity;
    return false;
  }
  if (identity === pageBuild) {
    return false;
  }
  pageBuild = identity; // never twice from this page
  try {
    if (window.sessionStorage.getItem("russoundReloadedForBuild") === identity) {
      return false; // already reloaded for this build (for example a cached page): no loop
    }
    window.sessionStorage.setItem("russoundReloadedForBuild", identity);
  } catch {
    // Storage blocked: the page-level guard above still allows only one reload.
  }
  window.location.reload();
  return true;
}

function renderSoftwareUpdates() {
  if (!adminUpdate.infoBox) {
    adminUpdate.replaceChildren();
    adminUpdate.appendChild(textElement("h3", "overview-subtitle", "Software updates"));
    adminUpdate.infoBox = document.createElement("div");
    adminUpdate.installBox = document.createElement("div");
    adminUpdate.append(adminUpdate.infoBox, adminUpdate.installBox);
  }
  renderUpdateInfo(adminUpdate.infoBox);
  renderInstall(adminUpdate.installBox);
}

function scheduleAdminUpdate(delayMs) {
  if (adminUpdateTimer !== null) {
    window.clearTimeout(adminUpdateTimer);
  }
  adminUpdateTimer = window.setTimeout(() => {
    adminUpdateTimer = null;
    fetchAdminUpdate();
  }, delayMs);
}

async function fetchAdminUpdate() {
  const wasBusy = Boolean(adminUpdateState && adminUpdateState.busy);
  try {
    const checked = await apiFetch("/api/status/updates");
    if (checked.ok) {
      latestUpdates = await checked.json();
      if (reloadForNewBuild(latestUpdates.build)) {
        return;
      }
    }
    const response = await apiFetch(adminCsrfToken ? "/api/admin/update" : "/api/status/update-operation");
    const body = await response.json();
    adminUpdateReconnecting = false;
    adminUpdateState = response.ok || body.configured === false ? body : { configured: true, phase: "idle" };
    renderSoftwareUpdates();
    if (adminUpdateState.busy) {
      scheduleAdminUpdate(2000);
    }
  } catch {
    // During an update the web restarts: keep waiting instead of showing an error.
    adminUpdateReconnecting = wasBusy || adminUpdateReconnecting;
    renderSoftwareUpdates();
    if (adminUpdateReconnecting) {
      scheduleAdminUpdate(3000);
    }
  }
}

async function requestUpdateCheck() {
  if (updateCheckInFlight) {
    return;
  }
  updateCheckInFlight = true;
  try {
    await apiFetch("/api/status/updates/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } catch {
    // The block shows the result or the failure on the next refresh.
  } finally {
    updateCheckInFlight = false;
    fetchAdminUpdate();
  }
}

async function runUpdateAction(action, body) {
  showAdminMessage("");
  try {
    const { response, payload } = await adminRequest(`/api/admin/update/${action}`, body);
    if (response.status === 403 && payload.error && payload.error.code === "reauthenticate") {
      pendingReauthAction = [action, body];
      showReauthPrompt();
      return;
    }
    if (!response.ok) {
      showAdminMessage(payload.error && payload.error.message ? payload.error.message : "The request failed.");
    }
  } catch {
    showAdminMessage("The server could not be reached.");
  }
  fetchAdminUpdate();
}

function showReauthPrompt() {
  renderedReviewKey = null;
  const box = adminUpdate.installBox || adminUpdate;
  box.reviewCard = null;
  box.replaceChildren();
  box.appendChild(textElement("p", "status-meta", "Enter the administrator password again to continue."));
  const form = document.createElement("form");
  form.className = "admin-form-row";
  const input = document.createElement("input");
  input.type = "password";
  input.autocomplete = "current-password";
  input.className = "admin-confirm-input";
  input.setAttribute("aria-label", "Administrator password");
  form.append(input, textElement("button", "compact-button compact-button-primary", "Continue"));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const password = input.value;
    input.value = "";
    const { response, payload } = await adminRequest("/api/admin/reauthenticate", { password });
    if (!response.ok) {
      showAdminMessage(payload.error && payload.error.message ? payload.error.message : "Login failed.");
      return;
    }
    const retry = pendingReauthAction;
    pendingReauthAction = null;
    if (retry) {
      runUpdateAction(retry[0], retry[1]);
    }
  });
  box.appendChild(form);
  input.focus();
}

function renderOverview(payload) {
  const sections = payload.sections || [];
  overviewEmpty.hidden = sections.length !== 0;
  const overall = hostLevelText[payload.overall] ? payload.overall : "unknown";
  overviewLevel.textContent = hostLevelText[overall];
  overviewLevel.className = `status-pill host-level host-level-${overall}`;
  overviewUpdate.hidden = payload.update_available !== true;
  overviewList.replaceChildren();
  for (const section of sections) {
    overviewList.appendChild(overviewCard(section));
  }
}

eventsTableBody.addEventListener("click", (event) => {
  const toggle = event.target.closest("[data-payload-toggle]");
  if (!toggle) {
    return;
  }

  const isExpanded = toggle.getAttribute("aria-expanded") === "true";
  const key = toggle.getAttribute("data-event-key");
  toggle.setAttribute("aria-expanded", isExpanded ? "false" : "true");
  toggle.title = isExpanded ? "Click to expand payload" : "Click to collapse payload";
  if (!key) {
    return;
  }
  if (isExpanded) {
    expandedPayloadRows.delete(key);
  } else {
    expandedPayloadRows.add(key);
  }
});

async function fetchStatus() {
  if (statusFetchInFlight) {
    statusFetchQueued = true;
    return;
  }
  statusFetchInFlight = true;
  try {
    const [clientsResponse, historyResponse] = await Promise.all([
      apiFetch("/api/status/clients"),
      apiFetch("/api/status/history"),
    ]);
    if (!clientsResponse.ok || !historyResponse.ok) {
      return;
    }
    const clientPayload = await clientsResponse.json();
    const historyPayload = await historyResponse.json();
    renderStatus(clientPayload, historyPayload);
  } finally {
    statusFetchInFlight = false;
    if (statusFetchQueued) {
      statusFetchQueued = false;
      fetchStatus();
    }
  }
}

async function fetchHost() {
  if (hostFetchInFlight) {
    return;
  }
  hostFetchInFlight = true;
  try {
    const response = await apiFetch("/api/status/host");
    if (response.ok) {
      renderHosts(await response.json());
    }
  } catch {
    // The next poll retries; the previous card stays until then.
  } finally {
    hostFetchInFlight = false;
  }
}

async function fetchOverview() {
  if (overviewFetchInFlight) {
    return;
  }
  overviewFetchInFlight = true;
  try {
    const response = await apiFetch("/api/status/overview");
    if (response.ok) {
      renderOverview(await response.json());
    }
  } catch {
    // The next poll retries; the previous overview stays until then.
  } finally {
    overviewFetchInFlight = false;
  }
}

function startStatusSync() {
  if (!window.EventSource || eventSource || document.hidden) {
    return;
  }
  eventSource = new EventSource("/api/events");
  eventSource.addEventListener("state-change", () => {
    fetchStatus();
  });
  eventSource.onopen = () => {
    fetchStatus();
  };
  eventSource.onerror = () => {
    // EventSource gives up for good on non-200 responses (e.g. 503 when the server stream limit is reached).
    if (eventSource && eventSource.readyState === EventSource.CLOSED) {
      stopStatusSync();
      eventSourceRetryTimer = window.setTimeout(startStatusSync, eventSourceRetryDelayMs);
    }
  };
}

function stopStatusSync() {
  if (eventSourceRetryTimer !== null) {
    window.clearTimeout(eventSourceRetryTimer);
    eventSourceRetryTimer = null;
  }
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

function startStatusPolling() {
  if (statusPollTimer === null && !document.hidden) {
    statusPollTimer = window.setInterval(() => {
      fetchStatus();
      fetchHost();
      fetchOverview();
      fetchAdminSession();
      if (adminUpdateTimer === null && !pendingReauthAction) {
        fetchAdminUpdate();
      }
    }, statusPollIntervalMs);
  }
}

function stopStatusPolling() {
  if (statusPollTimer !== null) {
    window.clearInterval(statusPollTimer);
    statusPollTimer = null;
  }
}

fetchStatus();
fetchHost();
fetchOverview();
fetchAdminSession();
fetchAdminUpdate();
adminLoginForm.addEventListener("submit", submitAdminLogin);
adminLogout.addEventListener("click", submitAdminLogout);
startStatusSync();
startStatusPolling();

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopStatusSync();
    stopStatusPolling();
    return;
  }
  fetchStatus();
  fetchHost();
  fetchOverview();
  fetchAdminSession();
  startStatusSync();
  startStatusPolling();
});