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
const updatesState = document.getElementById("updatesState");
const restorePanel = document.getElementById("restorePanel");
const restoreState = document.getElementById("restoreState");
const restoreBox = document.getElementById("restoreBox");
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
    timeCell.className = "event-time";
    const timeBlock = document.createElement("div");
    timeBlock.className = "status-time";
    timeBlock.append(
      textElement("span", "status-time-date", timestamp.date),
      textElement("span", "status-time-value", timestamp.time),
    );
    timeCell.appendChild(timeBlock);

    const payloadCell = document.createElement("td");
    payloadCell.className = "event-payload";
    const payloadToggle = document.createElement("button");
    payloadToggle.className = "payload-toggle";
    payloadToggle.type = "button";
    payloadToggle.setAttribute("data-event-key", encodedKey);
    payloadToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    payloadToggle.title = expanded ? "Click to collapse payload" : "Click to expand payload";
    const payloadPreview = textElement("code", "payload-preview", payloadText);
    payloadToggle.appendChild(payloadPreview);
    payloadCell.appendChild(payloadToggle);

    row.append(timeCell, textElement("td", "event-ip", event.ip), textElement("td", "event-action", event.path),
      payloadCell);
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
  const pill = textElement("span", `status-pill host-level host-level-${hostLevelText[level] ? level : "unknown"}`,
    hostLevelText[level] || "Unknown");
  pill.title = pill.textContent;  // the icon-only pill on phones still names its level
  header.append(textElement(strong ? "strong" : "span", strong ? "" : "overview-group-title", title), pill);
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
// The session itself is shown by admin.js (strip, header, login dialog); this page
// only needs to know whether privileged requests can be sent.
function renderAdmin(session) {
  const authenticated = session.configured === true && session.authenticated === true;
  const token = authenticated && typeof session.csrf_token === "string" ? session.csrf_token : null;
  const changed = token !== adminCsrfToken;
  adminCsrfToken = token;
  if (changed) {
    fetchAdminUpdate();  // signed in or out: the update view (with or without a review) differs
  }
}

function showAdminMessage(text) {
  adminMessage.textContent = text || "";
  adminMessage.hidden = !text;
}

// Runs a privileged action, signing in first through the login dialog (admin.js).
function privilegedAction(reason, action) {
  return () => {
    if (adminCsrfToken || typeof window === "undefined" || typeof window.adminLogin !== "function") {
      action();
      return;
    }
    window.adminLogin({ reason, onSuccess: action });
  };
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
  return seconds < 60 ? "just now" : seconds < 7200 ? `${Math.round(seconds / 60)} min ago`
    : seconds < 172800 ? `${Math.round(seconds / 3600)} h ago` : `${Math.round(seconds / 86400)} days ago`;
}

function updateButton(label, handler, { primary = false, disabled = false, privileged = false } = {}) {
  const classes = (primary ? "compact-button compact-button-primary" : "compact-button") + (privileged ? " admin-action" : "");
  const button = textElement("button", classes, label);
  button.type = "button";
  button.disabled = disabled;
  button.addEventListener("click", handler);
  return button;
}

function updateAvailableVersion() {
  const result = latestUpdates && latestUpdates.result;
  return latestUpdates && latestUpdates.state === "update_available" && result && result.available
    ? result.available : failedUpdateRetry();
}

function isFailedCandidate(status, available) {
  const candidate = status && status.candidate_revision;
  return Boolean(candidate && available && (available.version === `dev-${candidate}`
    || (typeof available.revision === "string" && available.revision.length >= 7 && candidate.startsWith(available.revision))));
}

// A failed update can leave its candidate running. The check then compares the newest
// image with that candidate and says "up to date", while the updater still records the
// previous version as installed: the candidate stays on offer as a retry.
function failedUpdateRetry() {
  const status = adminUpdateState;
  const result = latestUpdates && latestUpdates.result;
  const available = result && result.available;
  if (!status || isRestoreKind(status) || status.phase !== "failed"
      || status.installed_revision === status.candidate_revision || !isFailedCandidate(status, available)) {
    return null;
  }
  return available;
}

function dockerHubLink(available) {
  if (!available || typeof available.link !== "string" || !available.link.startsWith("https://hub.docker.com/r/")) {
    return null;
  }
  const link = textElement("a", "status-meta", "Docker Hub");
  link.setAttribute("href", available.link);
  link.setAttribute("target", "_blank");
  link.setAttribute("rel", "noopener noreferrer");
  return link;
}

function renderUpdateInfo(box) {
  box.replaceChildren();
  const updates = latestUpdates;
  if (!updates) {
    box.appendChild(textElement("p", "status-meta", "Loading…"));
    return;
  }
  const list = document.createElement("dl");
  list.className = "host-rows";
  const add = (label, value) => {
    const dd = typeof value === "string" ? textElement("dd", "", value) : value;
    list.append(textElement("dt", "", label), dd);
  };
  if (updates.installed) {
    add("Installed", shortVersion(updates.installed.version));
  }
  const result = updates.result || null;
  const available = result && result.available ? result.available : null;
  if (available) {
    const published = ageText(available.published_at);
    const dd = textElement("dd", "update-inline",
      shortVersion(available.version) + (published ? ` (published ${published})` : ""));
    const link = updateAvailableVersion() ? dockerHubLink(available) : null;
    if (link) {
      dd.append(" ", link);
    }
    add("Newest published", dd);
  }
  const state = failedUpdateRetry() ? "Update failed" : updateStateText[updates.state] || "Unknown";
  add("Status", state + (updates.stale ? " (last known result)" : ""));
  if (updates.state !== "unsupported" && updates.state !== "disabled") {
    // Label, age and button form one group: on wide screens a row of the list (the age in a
    // fixed-width slot, so the button never moves); on phones the button sits to the right of
    // label and age together, like a merged cell.
    const manual = updates.manual_check || {};
    const group = document.createElement("div");
    group.className = "update-check-group";
    const cells = document.createElement("div");
    cells.className = "update-check-cells";
    const action = document.createElement("div");
    action.className = "update-check-action";
    action.appendChild(updateButton("Check now", requestUpdateCheck,
      { disabled: !manual.available || updateCheckInFlight }));
    if (!manual.available && manual.retry_after_seconds > 0) {
      action.appendChild(textElement("span", "status-meta", `again in ${manual.retry_after_seconds} s`));
    }
    cells.append(textElement("dd", "update-check-age",
      updates.checked_at ? ageText(updates.checked_at) || "unknown" : "not yet"), action);
    group.append(textElement("dt", "", "Last check"), cells);
    list.appendChild(group);
  } else if (updates.checked_at) {
    add("Last check", ageText(updates.checked_at) || "unknown");
  }
  if (updates.check_failed && updates.error) {
    add("Check failed", updates.error);
  }
  if (updates.reason) {
    add("Note", updates.reason);
  }
  box.appendChild(list);
}

function cleanupText(cleanup) {
  if (!cleanup || !Number.isInteger(cleanup.removed) || cleanup.removed <= 0) {
    return "";
  }
  const megabytes = Math.round((cleanup.freed_bytes || 0) / 1e6);
  return `Removed ${cleanup.removed} old image${cleanup.removed === 1 ? "" : "s"} (${megabytes} MB).`;
}

// "itcornernet/mpdrussoundpi-web@sha256:2777aa50ce6f…" → "2777aa50ce6f": 12 characters
// identify an image on one Pi; the full reference stays in the tooltip.
function shortImage(reference) {
  const text = String(reference || "");
  const digest = text.match(/sha256:([0-9a-f]{12})[0-9a-f]*/);
  if (digest) {
    return digest[1];
  }
  const tag = text.split("/").pop().split(":")[1];
  return tag ? tag.slice(0, 24) : text.slice(0, 24);
}

function imagesRow(images) {
  const value = document.createElement("dd");
  value.className = "review-images";
  const entries = Object.entries(images || {});
  entries.forEach(([name, reference], index) => {
    const part = document.createElement("span");
    part.className = "review-image";
    part.title = String(reference || "");
    part.append(document.createTextNode(`${name} `), textElement("code", "", shortImage(reference)));
    value.appendChild(part);
    if (index < entries.length - 1) {
      value.appendChild(document.createTextNode(" · "));
    }
  });
  return value;
}

function retentionText(retention) {
  if (!retention || !Number.isInteger(retention.operations) || retention.operations <= 0) {
    return "";
  }
  const megabytes = Math.round((retention.freed_bytes || 0) / 1e6);
  return `Removed the backups of ${retention.operations} older update${retention.operations === 1 ? "" : "s"} (${megabytes} MB).`;
}

function renderReview(review) {
  const box = document.createElement("div");
  box.className = "admin-review";
  const isRestore = review.action === "restore";
  box.appendChild(textElement("strong", "", isRestore ? "Review: restore backup"
    : `Review: update to dev-${shortRevision(review.revision)}`));
  const rows = [["Interruption", review.interruption || ""], ["Backup", review.backup || ""]];
  if (review.cleanup) {
    rows.push(["Old images", review.cleanup]);
  }
  if (review.retention) {
    rows.push(["Old backups", review.retention]);
  }
  if (review.layout_change) {
    rows.push(["Layout change", review.layout_change]);
  }
  const list = document.createElement("dl");
  list.className = "host-rows";
  for (const [label, value] of rows) {
    list.append(textElement("dt", "", label), textElement("dd", "", value));
  }
  if (Object.keys(review.images || {}).length) {
    list.append(textElement("dt", "", "Images"), imagesRow(review.images));
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
    () => runUpdateAction("approve", { operation: review.operation, digest: review.digest }),
    { primary: true, disabled: true, privileged: true });
  input.addEventListener("input", () => {
    confirm.disabled = input.value.trim().toLowerCase() !== word;
  });
  row.append(input, confirm, updateButton("Cancel", () => runUpdateAction("cancel", { operation: review.operation })));
  box.appendChild(row);
  box.countdown = countdown;
  return box;
}

// A restore is its own section ("Previous version"), so updating and going back are
// never two buttons side by side. Messages and reviews go to the section of their kind.
function isRestoreKind(status) {
  return Boolean(status && (status.kind === "restore" || ["restoring", "restored", "restore_awaiting_approval"]
    .includes(status.phase) || (status.review && status.review.action === "restore")));
}

function renderReviewInto(box, review, keyName) {
  const key = `${review.operation}/${review.digest}`;
  if (box[keyName] === key && box.reviewCard) {
    // Same review: only the countdown changes, the typed confirmation stays.
    box.reviewCard.countdown.textContent = `${review.expires_in_seconds} s`;
    return;
  }
  box[keyName] = key;
  box.reviewCard = renderReview(review);
  box.replaceChildren(box.reviewCard);
}

function clearReview(box, keyName) {
  box[keyName] = null;
  box.reviewCard = null;
  box.replaceChildren();
}

function phaseMessage(box, status) {
  const phase = status.phase;
  if (!phase || phase === "idle" || !updatePhaseText[phase]) {
    return;
  }
  // The updater's error is a complete sentence for this exact outcome (a failed
  // restore also ends in "failed"); the phase text is only the fallback, so the
  // same failure is never stated twice.
  const detailed = status.error && !["cancelled", "up_to_date"].includes(phase);
  box.appendChild(textElement("p", phase === "failed" || phase === "outcome_unknown" ? "admin-message" : "status-meta",
    detailed ? status.error : updatePhaseText[phase]));
  if (phase === "succeeded") {
    for (const line of [cleanupText(status.cleanup), retentionText(status.retention)]) {
      if (line) {
        box.appendChild(textElement("p", "status-meta", line));
      }
    }
  }
}

function renderInstall(box) {
  const status = adminUpdateState;
  const review = status && status.review;
  const awaiting = status && (status.phase === "awaiting_approval" || status.phase === "restore_awaiting_approval");
  if (awaiting && review && review.action !== "restore" && adminCsrfToken) {
    renderReviewInto(box, review, "reviewKey");
    return;
  }
  clearReview(box, "reviewKey");
  if (adminUpdateReconnecting && !isRestoreKind(status)) {
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
  if (!isRestoreKind(status)) {
    phaseMessage(box, status);
  }
  // After a failed (or unknown) update the same version is offered as a retry; the restore
  // has its own section. A newer version is a normal update again.
  const updateFailed = ["failed", "outcome_unknown"].includes(status.phase) && !isRestoreKind(status);
  const sameAsFailed = updateFailed && (!status.candidate_revision || isFailedCandidate(status, available));
  const reviewPending = ["awaiting_approval", "restore_awaiting_approval"].includes(status.phase);
  // An unknown outcome is checked on the Pi first (see its message): no retry from here.
  if (available && !status.busy && !reviewPending && status.phase !== "outcome_unknown") {
    const version = shortVersion(available.version);
    const row = document.createElement("div");
    row.className = "admin-form-row";
    row.appendChild(updateButton(sameAsFailed ? `Retry update to ${version}` : `Update to ${version}`,
      privilegedAction(sameAsFailed ? `Retry the update to ${version}` : `Install the update to ${version}`,
        () => runUpdateAction("review", {})),
      { primary: true, privileged: true }));
    box.appendChild(row);
  }
}

let restoreOpenedFor = null;

function renderRestore() {
  const status = adminUpdateState;
  const restoring = isRestoreKind(status);
  const updateFailed = Boolean(status && !restoring && ["failed", "outcome_unknown"].includes(status.phase));
  const shown = Boolean(status && status.configured !== false && (status.restore_available || restoring));
  restorePanel.hidden = !shown;
  if (!shown) {
    clearReview(restoreBox, "reviewKey");
    return;
  }
  // A failed update opens this section once per operation; the user may close it again.
  if (updateFailed && status.restore_available && restoreOpenedFor !== status.operation) {
    restorePanel.open = true;
    restoreOpenedFor = status.operation;
  }
  const review = status.review;
  const awaiting = status.phase === "awaiting_approval" || status.phase === "restore_awaiting_approval";
  const restoreReview = Boolean(awaiting && review && review.action === "restore" && adminCsrfToken);
  const [text, level] = restoring && status.busy ? ["Restoring", "update"]
    : restoreReview ? ["Confirm restore", "update"]
      : updateFailed ? ["Update failed", "warning"] : ["Available", "unknown"];
  restoreState.textContent = text;
  restoreState.title = text;
  restoreState.className = `status-pill host-level host-level-${level}`;
  if (restoreReview) {
    restorePanel.open = true;  // the review is the next step; never hide it in a closed section
    renderReviewInto(restoreBox, review, "reviewKey");
    return;
  }
  clearReview(restoreBox, "reviewKey");
  if (restoring) {
    if (adminUpdateReconnecting) {
      restoreBox.appendChild(textElement("p", "status-meta", "Waiting for the web application to come back…"));
      return;
    }
    phaseMessage(restoreBox, status);
  }
  if (status.restore_available && !status.busy) {
    const backups = Array.isArray(status.backups) && status.backups.length
      ? status.backups : [{ operation: status.operation }];
    const list = document.createElement("div");
    list.className = "backup-list";
    for (const backup of backups) {
      list.appendChild(backupEntry(backup));
    }
    restoreBox.appendChild(list);
  }
}

function backupTime(value) {
  const moment = typeof value === "string" ? new Date(value) : null;
  if (!moment || Number.isNaN(moment.getTime())) {
    return null;
  }
  const date = moment.toLocaleDateString(undefined, { year: "numeric", month: "2-digit", day: "2-digit" });
  const time = moment.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  return { text: `${date}, ${time}`, ago: ageText(value) || "" };
}

function backupSize(bytes) {
  if (typeof bytes !== "number" || bytes < 0) {
    return null;
  }
  return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.max(1, Math.round(bytes / 1e6))} MB`;
}

// One restorable backup. Today there is only the failed update's own backup; the list
// is ready for more once more backups are kept.
function backupEntry(backup) {
  const item = document.createElement("article");
  item.className = "backup-entry";
  const taken = backupTime(backup.taken_at);
  const title = taken ? `Backup from ${taken.text}` : "Backup from the last update";
  item.appendChild(textElement("strong", "backup-title", title));
  const facts = document.createElement("dl");
  facts.className = "backup-facts";
  const rows = [
    ["Taken", taken ? `${taken.ago}${backup.reason ? `, ${backup.reason}` : ""}` : backup.reason || "before the last update"],
    ["Version", backup.revision ? `dev-${shortRevision(backup.revision)}` : "not recorded"],
  ];
  const size = backupSize(backup.size_bytes);
  if (size) {
    rows.push(["Size", size]);
  }
  for (const [label, value] of rows) {
    facts.append(textElement("dt", "", label), textElement("dd", "", value));
  }
  item.appendChild(facts);
  const row = document.createElement("div");
  row.className = "admin-form-row";
  const operation = typeof backup.operation === "string" ? backup.operation : adminUpdateState.operation;
  row.appendChild(updateButton("Restore this backup",
    privilegedAction(taken ? `Restore the backup from ${taken.text}` : "Restore the backup",
      () => runUpdateAction("restore", { operation })), { primary: true, privileged: true }));
  item.appendChild(row);
  return item;
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

function renderUpdatesState() {
  const state = latestUpdates && latestUpdates.state;
  const [text, level] = adminUpdateState && adminUpdateState.busy ? ["Updating", "update"]
    : failedUpdateRetry() ? ["Update failed", "problem"]
    : state === "update_available" ? ["Update available", "update"]
      : state === "up_to_date" ? ["Up to date", "ok"]
        : state ? ["Not checked", "unknown"] : ["Checking…", "unknown"];
  updatesState.textContent = text;
  updatesState.title = text;
  updatesState.className = `status-pill host-level host-level-${level}`;
}

function renderSoftwareUpdates() {
  renderUpdatesState();
  if (!adminUpdate.infoBox) {
    adminUpdate.replaceChildren();
    adminUpdate.infoBox = document.createElement("div");
    adminUpdate.installBox = document.createElement("div");
    adminUpdate.append(adminUpdate.infoBox, adminUpdate.installBox);
  }
  renderUpdateInfo(adminUpdate.infoBox);
  renderInstall(adminUpdate.installBox);
  renderRestore();
  // While a review (or the password prompt that may follow) is open, it is the only thing
  // shown: the version overview and "Check now" return after cancel or completion.
  adminUpdate.infoBox.hidden = Boolean(adminUpdate.installBox.reviewCard);
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
    // The password and its errors belong to the dialog (admin.js), never to this section.
    const code = payload.error && payload.error.code;
    const again = () => runUpdateAction(action, body);
    const reason = action === "restore" ? "Restore the previous version" : "Install the update";
    if (response.status === 403 && code === "reauthenticate" && typeof window.adminReauthenticate === "function") {
      window.adminReauthenticate({ reason, onSuccess: again });
      return;
    }
    if (response.status === 401 && typeof window.adminLogin === "function") {
      window.adminLogin({ reason, onSuccess: again });  // the session ended meanwhile
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

function renderOverview(payload) {
  const sections = payload.sections || [];
  overviewEmpty.hidden = sections.length !== 0;
  const overall = hostLevelText[payload.overall] ? payload.overall : "unknown";
  overviewLevel.textContent = hostLevelText[overall];
  overviewLevel.title = hostLevelText[overall];
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
      if (adminUpdateTimer === null) {
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
// A login or exit in admin.js (dialog, strip or menu) carries the new session, so a
// pending privileged action already finds the token when it runs.
document.addEventListener("admin-session-changed", (event) => {
  if (event.detail && event.detail.session) {
    renderAdmin(event.detail.session);
  } else {
    fetchAdminSession();
  }
  fetchAdminUpdate();
});
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