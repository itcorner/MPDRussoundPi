// Administrator mode on every page: amber frame, sticky strip with countdown and
// exit, and a row in the page menu (sign in / exit admin mode). See
// docs/plans/web_open_admin-mode-and-protected-settings.md. The login is a modal
// dialog opened from the header, the menu or any privileged action
// (window.adminLogin).
//
// Reading the session never extends it; only "Extend" and real administrator
// actions count as activity.

const ADMIN_POLL_MS = 15000;
const ADMIN_LOW_SECONDS = 60;
const SHORT_PASSWORD_RISK = "A password shorter than 12 characters is protected online only by the login lockout "
  + "(5 tries per 15 minutes, about 10 days on average for a 4-digit PIN), and it can be guessed offline within "
  + "seconds if the password file is ever copied, for example from a backup.";
const ADMIN_SETUP_COMMAND = "python -m web.admin_auth set --file <secrets directory>/russound-admin.scrypt";
// Resetting a lost password on the Pi: writes the page's password file in the web container
// (it wins over the deployment's secrets file), asks for the new password twice.
const ADMIN_RESET_COMMAND = "docker exec -it $(docker ps -qf name=russound-web) python -m web.admin_auth set --file /data/russound-admin.scrypt";

const SHIELD_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="currentColor" d="M12 1 3 5v6c0 5.5 3.8 10.7 9 12 5.2-1.3 9-6.5 9-12V5z"/></svg>';
const KEY_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><circle cx="8" cy="15" r="4" fill="none" stroke="currentColor" stroke-width="2.2"/><path d="M11 12l9-9M17 6l3 3M14 9l2 2" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>';
const LOCK_SVG = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><rect x="5" y="11" width="14" height="10" rx="2" fill="none" stroke="currentColor" stroke-width="2.2"/><path d="M8 11V8a4 4 0 0 1 8 0v3" fill="none" stroke="currentColor" stroke-width="2.2"/></svg>';

function adminCountdown(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${String(Math.floor(value / 60)).padStart(2, "0")}:${String(value % 60).padStart(2, "0")}`;
}

// Pure view model: everything the page shows follows from the session and the time left.
function adminView(session, secondsLeft) {
  const elevated = Boolean(session && session.configured === true && session.authenticated === true);
  const left = elevated ? Math.max(0, Math.floor(Number(secondsLeft) || 0)) : 0;
  return {
    elevated,
    configured: Boolean(session && session.configured === true),
    low: elevated && left <= ADMIN_LOW_SECONDS,
    countdown: adminCountdown(left),
    menuLabel: elevated ? "Exit admin mode" : "Sign in as administrator",
  };
}

const adminMode = {
  session: null,
  secondsLeft: 0,
  receivedAt: 0,
  elements: null,
  pollTimer: null,
  tickTimer: null,
  dialog: null,
  pending: null,
  reauth: false,
};

// What the login dialog shows for a session answer (pure, for tests).
function adminLoginView(session) {
  const configured = Boolean(session && session.configured === true);
  const secure = Boolean(session && session.secure === true);
  const httpsUrl = session && typeof session.https_url === "string" && session.https_url.startsWith("https://")
    ? session.https_url : null;
  const httpsOnly = Boolean(session && session.https_required === true && !secure && httpsUrl);
  const locked = configured && session && session.locked_for_seconds > 0 ? Math.ceil(session.locked_for_seconds / 60) : 0;
  return {
    mode: !configured ? "setup" : httpsOnly ? "https" : "password",
    httpsUrl: httpsOnly ? httpsUrl : null,
    plainHttpWarning: configured && !httpsOnly && !secure,
    lockedMessage: locked ? `Too many failed attempts. Login is locked for about ${locked === 1 ? "1 minute" : `${locked} minutes`}.` : "",
  };
}

function adminIcon(element, svg) {
  // Fixed markup from this file only; never session data.
  element.innerHTML = svg;
}

function buildAdminElements() {
  const header = document.querySelector(".system-panel .panel-header");
  const navigation = header ? header.querySelector(".page-navigation") : null;
  if (!header || !navigation) {
    return null;
  }
  const menu = navigation.querySelector(".page-menu");
  const divider = document.createElement("hr");
  divider.className = "page-menu-divider";
  const menuItem = document.createElement("button");
  menuItem.type = "button";
  menuItem.className = "page-menu-link page-menu-admin";
  const menuIcon = document.createElement("span");
  menuIcon.className = "page-menu-admin-icon";
  const menuText = document.createElement("span");
  menuItem.append(menuIcon, menuText);
  const passwordItem = document.createElement("button");
  passwordItem.type = "button";
  passwordItem.className = "page-menu-link page-menu-admin page-menu-password";
  const passwordIcon = document.createElement("span");
  passwordIcon.className = "page-menu-admin-icon";
  adminIcon(passwordIcon, KEY_SVG);
  const passwordText = document.createElement("span");
  passwordText.textContent = "Change password";
  passwordItem.append(passwordIcon, passwordText);
  passwordItem.hidden = true;
  if (menu) {
    menu.append(divider, passwordItem, menuItem);
  }
  passwordItem.addEventListener("click", () => {
    navigation.open = false;
    openPasswordChange();
  });

  const strip = document.createElement("div");
  strip.className = "admin-strip";
  strip.setAttribute("role", "status");
  strip.hidden = true;
  const stripIcon = document.createElement("span");
  stripIcon.className = "admin-strip-icon";
  adminIcon(stripIcon, SHIELD_SVG);
  const stripLong = document.createElement("span");
  stripLong.className = "admin-strip-long";
  stripLong.textContent = "Administrator mode";
  const stripShort = document.createElement("span");
  stripShort.className = "admin-strip-short";
  stripShort.textContent = "Admin mode";
  const spacer = document.createElement("span");
  spacer.className = "admin-strip-spacer";
  const stripTime = document.createElement("span");
  stripTime.className = "admin-strip-time admin-countdown";
  const extend = document.createElement("button");
  extend.type = "button";
  extend.className = "admin-strip-button";
  extend.textContent = "Extend";
  extend.hidden = true;
  const exit = document.createElement("button");
  exit.type = "button";
  exit.className = "admin-strip-button";
  exit.textContent = "Exit";
  strip.append(stripIcon, stripLong, stripShort, spacer, stripTime, extend, exit);
  document.body.prepend(strip);

  const frame = document.createElement("div");
  frame.className = "admin-frame";
  frame.setAttribute("aria-hidden", "true");
  document.body.append(frame);

  const onMenuAction = () => {
    navigation.open = false;
    if (adminMode.session && adminMode.session.authenticated) {
      exitAdminMode();
    } else {
      openAdminLogin();
    }
  };
  menuItem.addEventListener("click", onMenuAction);
  exit.addEventListener("click", exitAdminMode);
  extend.addEventListener("click", extendAdminMode);
  return { menuItem, menuIcon, menuText, passwordItem, strip, extend, frame, divider };
}

// ------------------------------------------------------------------ login dialog

function buildAdminDialog() {
  const dialog = document.createElement("dialog");
  dialog.className = "admin-dialog";
  dialog.setAttribute("aria-labelledby", "adminDialogTitle");
  const title = document.createElement("h2");
  title.id = "adminDialogTitle";
  title.className = "admin-dialog-title";
  const titleIcon = document.createElement("span");
  titleIcon.className = "admin-dialog-icon";
  adminIcon(titleIcon, SHIELD_SVG);
  const titleText = document.createElement("span");
  titleText.textContent = "Sign in as administrator";
  title.append(titleIcon, titleText);
  const reason = document.createElement("p");
  reason.className = "admin-dialog-reason";

  const form = document.createElement("form");
  form.className = "admin-dialog-form";
  form.method = "dialog";
  const label = document.createElement("label");
  label.htmlFor = "adminDialogPassword";
  label.textContent = "Password";
  const input = document.createElement("input");
  input.id = "adminDialogPassword";
  input.type = "password";
  input.autocomplete = "current-password";
  input.maxLength = 256;
  input.required = true;
  const message = document.createElement("p");
  message.className = "admin-dialog-message";
  message.setAttribute("role", "alert");
  message.hidden = true;
  const actions = document.createElement("div");
  actions.className = "admin-dialog-actions";
  const submit = document.createElement("button");
  submit.type = "submit";
  submit.className = "admin-dialog-submit";
  submit.textContent = "Sign in";
  const cancel = document.createElement("button");
  cancel.type = "button";
  cancel.className = "admin-dialog-cancel";
  cancel.textContent = "Cancel";
  actions.append(submit, cancel);
  const warning = document.createElement("p");
  warning.className = "admin-dialog-note";
  warning.textContent = "This page uses plain HTTP: on this network the password can be observed. Use it only on a network you trust.";
  form.append(label, input, message, actions, warning);

  const https = document.createElement("p");
  https.className = "admin-dialog-note";
  const httpsLead = document.createElement("span");
  httpsLead.textContent = "Sign in over HTTPS, where the password is encrypted: ";
  const httpsLink = document.createElement("a");
  https.append(httpsLead, httpsLink);
  const setup = document.createElement("p");
  setup.className = "admin-dialog-note";
  const setupLead = document.createElement("span");
  setupLead.textContent = "Administrator login is not set up on this installation. Set a password locally on the host with ";
  const setupCode = document.createElement("code");
  setupCode.textContent = ADMIN_SETUP_COMMAND;
  setup.append(setupLead, setupCode);
  const close = document.createElement("button");
  close.type = "button";
  close.className = "admin-dialog-cancel";
  close.textContent = "Close";

  // Changing the password: current, new and repeated, checked here before sending.
  const change = document.createElement("form");
  change.className = "admin-dialog-form";
  change.hidden = true;
  const changeFields = {};
  for (const [key, text, autocomplete] of [["current", "Current password", "current-password"],
    ["next", "New password", "new-password"], ["repeat", "Repeat the new password", "new-password"]]) {
    const fieldLabel = document.createElement("label");
    fieldLabel.htmlFor = `adminDialog-${key}`;
    fieldLabel.textContent = text;
    const field = document.createElement("input");
    field.id = `adminDialog-${key}`;
    field.type = "password";
    field.autocomplete = autocomplete;
    field.maxLength = 256;
    field.required = true;
    changeFields[key] = field;
    change.append(fieldLabel, field);
  }
  const changeRule = document.createElement("p");
  changeRule.className = "admin-dialog-note";
  changeRule.textContent = "At least 12 characters (a short password or PIN needs the risk accepted below). "
    + "All other signed-in devices are signed out.";
  // A short password (4–11 characters, e.g. a PIN) only after reading and accepting the risk.
  const shortBox = document.createElement("div");
  shortBox.className = "admin-dialog-risk";
  shortBox.hidden = true;
  const shortText = document.createElement("p");
  shortText.textContent = SHORT_PASSWORD_RISK;
  const shortLabel = document.createElement("label");
  shortLabel.className = "admin-dialog-check";
  const shortAccept = document.createElement("input");
  shortAccept.type = "checkbox";
  shortLabel.append(shortAccept, document.createTextNode(" I understand the risk and want to use a short password"));
  shortBox.append(shortText, shortLabel);
  changeFields.next.addEventListener("input", () => {
    const short = changeFields.next.value.length > 0 && changeFields.next.value.length < 12;
    shortBox.hidden = !short;
    if (!short) {
      shortAccept.checked = false;
    }
  });
  const changeMessage = document.createElement("p");
  changeMessage.className = "admin-dialog-message";
  changeMessage.setAttribute("role", "alert");
  changeMessage.hidden = true;
  const changeActions = document.createElement("div");
  changeActions.className = "admin-dialog-actions";
  const changeSubmit = document.createElement("button");
  changeSubmit.type = "submit";
  changeSubmit.className = "admin-dialog-submit";
  changeSubmit.textContent = "Change password";
  const changeCancel = document.createElement("button");
  changeCancel.type = "button";
  changeCancel.className = "admin-dialog-cancel";
  changeCancel.textContent = "Cancel";
  changeActions.append(changeSubmit, changeCancel);
  change.append(changeRule, shortBox, changeMessage, changeActions);

  // A forgotten password is reset on the Pi itself; the page can only say how.
  const help = document.createElement("details");
  help.className = "admin-dialog-help";
  const helpSummary = document.createElement("summary");
  helpSummary.textContent = "Forgot the password?";
  const helpText = document.createElement("p");
  helpText.textContent = "Set a new one on the Pi over SSH, then sign in with it:";
  const helpCode = document.createElement("code");
  helpCode.textContent = ADMIN_RESET_COMMAND;
  const helpCopy = document.createElement("button");
  helpCopy.type = "button";
  helpCopy.className = "admin-dialog-copy";
  helpCopy.textContent = "Copy";
  help.append(helpSummary, helpText, helpCode, helpCopy);
  helpCopy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(ADMIN_RESET_COMMAND);
      helpCopy.textContent = "Copied";
    } catch {
      helpCopy.textContent = "Select and copy";
    }
  });

  dialog.append(title, reason, form, change, help, https, setup, close);
  change.addEventListener("submit", (event) => {
    event.preventDefault();
    submitPasswordChange();
  });
  changeCancel.addEventListener("click", () => closeAdminLogin(false));
  document.body.append(dialog);

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    submitAdminLogin();
  });
  cancel.addEventListener("click", () => closeAdminLogin(false));
  close.addEventListener("click", () => closeAdminLogin(false));
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeAdminLogin(false);
  });
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      closeAdminLogin(false);  // a click on the backdrop
    }
  });
  return { dialog, titleText, reason, form, input, message, submit, warning, https, httpsLink, setup, close,
    change, changeFields, changeMessage, changeSubmit, help, shortBox, shortAccept };
}

function showAdminDialogMessage(text) {
  const parts = adminMode.dialog;
  parts.message.textContent = text || "";
  parts.message.hidden = !text;
}

function renderAdminDialog() {
  const parts = adminMode.dialog;
  const view = adminLoginView(adminMode.session);
  parts.form.hidden = view.mode !== "password";
  parts.change.hidden = true;
  // Only a sign-in can have forgotten the password; a signed-in confirmation cannot.
  parts.help.hidden = !(view.mode === "password" && !adminMode.reauth);
  parts.warning.hidden = !view.plainHttpWarning;
  parts.https.hidden = view.mode !== "https";
  parts.setup.hidden = view.mode !== "setup";
  parts.close.hidden = view.mode === "password";
  if (view.httpsUrl) {
    const target = view.httpsUrl + window.location.pathname.replace(/^\//, "");
    parts.httpsLink.href = target;
    parts.httpsLink.textContent = target;
  }
  if (view.lockedMessage) {
    showAdminDialogMessage(view.lockedMessage);
  }
  return view;
}

// Opens the login dialog. ``reason`` names what the user asked to do; ``onSuccess``
// runs once signed in (the action that needed the login). With ``reauth`` the signed-in
// administrator confirms the password again (a restore asks for it).
async function openAdminLogin({ reason = "", onSuccess = null, reauth = false } = {}) {
  if (!adminMode.dialog) {
    adminMode.dialog = buildAdminDialog();
  }
  const parts = adminMode.dialog;
  adminMode.pending = onSuccess;
  await refreshAdminMode();
  adminMode.reauth = Boolean(reauth && adminMode.session && adminMode.session.authenticated);
  parts.titleText.textContent = adminMode.reauth ? "Enter your password again" : "Sign in as administrator";
  parts.submit.textContent = adminMode.reauth ? "Confirm" : "Sign in";
  parts.reason.textContent = reason ? `You asked to: ${reason}` : "";
  parts.reason.hidden = !reason;
  parts.input.value = "";
  showAdminDialogMessage("");
  const view = renderAdminDialog();
  if (!parts.dialog.open) {
    parts.dialog.showModal();
  }
  (view.mode === "password" ? parts.input : parts.close).focus();
}

function closeAdminLogin(succeeded) {
  const parts = adminMode.dialog;
  const pending = adminMode.pending;
  adminMode.pending = null;
  if (parts && parts.dialog.open) {
    parts.dialog.close();
  }
  if (parts) {
    parts.input.value = "";
    for (const field of Object.values(parts.changeFields)) {
      field.value = "";
    }
    parts.shortAccept.checked = false;
    parts.shortBox.hidden = true;
  }
  if (succeeded && typeof pending === "function") {
    pending();
  }
}

// The password change dialog, for the signed-in administrator.
async function openPasswordChange() {
  if (!adminMode.dialog) {
    adminMode.dialog = buildAdminDialog();
  }
  const parts = adminMode.dialog;
  await refreshAdminMode();
  if (!(adminMode.session && adminMode.session.authenticated)) {
    openAdminLogin({ reason: "Change the administrator password", onSuccess: openPasswordChange });
    return;
  }
  adminMode.pending = null;
  parts.titleText.textContent = "Change the administrator password";
  parts.reason.hidden = true;
  for (const node of [parts.form, parts.help, parts.https, parts.setup, parts.close]) {
    node.hidden = true;
  }
  parts.warning.hidden = true;
  parts.change.hidden = false;
  parts.changeMessage.hidden = true;
  parts.changeMessage.classList.remove("is-success");
  if (!parts.dialog.open) {
    parts.dialog.showModal();
  }
  parts.changeFields.current.focus();
}

function showChangeMessage(text, success = false) {
  const parts = adminMode.dialog;
  parts.changeMessage.textContent = text;
  parts.changeMessage.hidden = !text;
  parts.changeMessage.classList.toggle("is-success", success);
}

async function submitPasswordChange() {
  const parts = adminMode.dialog;
  const { current, next, repeat } = parts.changeFields;
  const short = next.value.length < 12;
  if (next.value.length < 4) {
    showChangeMessage("Use at least 4 characters (12 or more is recommended).");
    next.focus();
    return;
  }
  if (short && !parts.shortAccept.checked) {
    showChangeMessage("Accept the risk of a short password, or use at least 12 characters.");
    parts.shortAccept.focus();
    return;
  }
  if (next.value !== repeat.value) {
    showChangeMessage("The new passwords differ.");
    repeat.focus();
    return;
  }
  parts.changeSubmit.disabled = true;
  try {
    const token = adminMode.session && adminMode.session.csrf_token;
    const response = await fetch("/api/admin/password", {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(typeof token === "string" ? { "X-CSRF-Token": token } : {}) },
      body: JSON.stringify({ current_password: current.value, new_password: next.value, allow_short: short }),
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (response.ok) {
      acceptAdminSession({ ...(adminMode.session || {}), ...payload });
      for (const field of Object.values(parts.changeFields)) {
        field.value = "";
      }
      parts.shortAccept.checked = false;
      parts.shortBox.hidden = true;
      showChangeMessage("Password changed. Other signed-in devices were signed out.", true);
      window.setTimeout(() => closeAdminLogin(false), 1800);
      return;
    }
    current.value = "";
    showChangeMessage(payload.error && payload.error.message ? payload.error.message : "The password was not changed.");
    current.focus();
  } catch {
    showChangeMessage("The server could not be reached.");
  } finally {
    parts.changeSubmit.disabled = false;
  }
}

async function submitAdminLogin() {
  const parts = adminMode.dialog;
  const password = parts.input.value;
  parts.input.value = "";
  showAdminDialogMessage("");
  parts.submit.disabled = true;
  try {
    const headers = { "Content-Type": "application/json" };
    if (adminMode.reauth && adminMode.session && typeof adminMode.session.csrf_token === "string") {
      headers["X-CSRF-Token"] = adminMode.session.csrf_token;
    }
    const response = await fetch(adminMode.reauth ? "/api/admin/reauthenticate" : "/api/admin/login", {
      method: "POST", credentials: "same-origin", headers, body: JSON.stringify({ password }),
    });
    let payload = {};
    try {
      payload = await response.json();
    } catch {
      payload = {};
    }
    if (response.ok) {
      acceptAdminSession({ ...(adminMode.session || {}), ...payload });
      announceAdminChange();
      closeAdminLogin(true);
      return;
    }
    showAdminDialogMessage(payload.error && payload.error.message ? payload.error.message : "Login failed.");
  } catch {
    showAdminDialogMessage("The server could not be reached.");
  } finally {
    parts.submit.disabled = false;
  }
  parts.input.focus();
}

function secondsLeftNow() {
  const elapsed = (Date.now() - adminMode.receivedAt) / 1000;
  return Math.max(0, adminMode.secondsLeft - elapsed);
}

function renderAdminMode() {
  const elements = adminMode.elements;
  if (!elements) {
    return;
  }
  const view = adminView(adminMode.session, secondsLeftNow());
  document.body.classList.toggle("admin-elevated", view.elevated);
  document.body.classList.toggle("admin-low", view.low);
  elements.strip.hidden = !view.elevated;
  elements.extend.hidden = !view.low;
  elements.menuItem.hidden = !view.configured;
  elements.passwordItem.hidden = !(view.elevated && adminMode.session && adminMode.session.password_changeable === true);
  elements.divider.hidden = !view.configured;
  if (elements.menuIcon.dataset.state !== String(view.elevated)) {
    adminIcon(elements.menuIcon, view.elevated ? SHIELD_SVG : LOCK_SVG);
    elements.menuIcon.dataset.state = String(view.elevated);
  }
  elements.menuText.textContent = view.menuLabel;
  document.querySelectorAll(".admin-countdown").forEach((node) => {
    node.textContent = view.countdown;
  });
}

function acceptAdminSession(session) {
  const before = adminView(adminMode.session, 0);
  adminMode.session = session;
  adminMode.secondsLeft = session && session.authenticated ? Number(session.idle_expires_in_seconds) || 0 : 0;
  adminMode.receivedAt = Date.now();
  renderAdminMode();
  const after = adminView(session, 0);
  if (before.elevated !== after.elevated || before.configured !== after.configured) {
    // Pages that lock editing for guests (the Configuration page) follow this.
    document.dispatchEvent(new CustomEvent("admin-mode-changed", { detail: adminModeState() }));
  }
}

// What other page scripts need: whether a login exists, whether it is active, and the token.
function adminModeState() {
  const view = adminView(adminMode.session, 0);
  return { known: adminMode.session !== null, configured: view.configured, elevated: view.elevated };
}

async function refreshAdminMode() {
  try {
    const response = await fetch("/api/admin/session", { credentials: "same-origin", cache: "no-store" });
    if (response.ok) {
      acceptAdminSession(await response.json());
    }
  } catch {
    // The next poll retries; the page keeps its last state.
  }
}

async function adminModePost(path) {
  const token = adminMode.session && adminMode.session.csrf_token;
  const headers = { "Content-Type": "application/json" };
  if (typeof token === "string") {
    headers["X-CSRF-Token"] = token;
  }
  return fetch(path, { method: "POST", credentials: "same-origin", headers, body: "{}" });
}

function announceAdminChange() {
  document.dispatchEvent(new CustomEvent("admin-session-changed", {
    detail: { source: "admin-mode", session: adminMode.session } }));
}

async function exitAdminMode() {
  try {
    await adminModePost("/api/admin/logout");
  } catch {
    // The refresh below shows the real state.
  }
  await refreshAdminMode();
  announceAdminChange();
}

async function extendAdminMode() {
  try {
    const response = await adminModePost("/api/admin/extend");
    if (response.ok) {
      acceptAdminSession(await response.json());
      return;
    }
  } catch {
    // Fall through to a plain refresh.
  }
  refreshAdminMode();
}

function tickAdminMode() {
  if (adminMode.session && adminMode.session.authenticated && secondsLeftNow() <= 0) {
    refreshAdminMode();  // the server has ended the session; show it right away
  }
  renderAdminMode();
}

function startAdminMode() {
  adminMode.elements = buildAdminElements();
  if (!adminMode.elements) {
    return;
  }
  renderAdminMode();
  refreshAdminMode();
  adminMode.pollTimer = setInterval(refreshAdminMode, ADMIN_POLL_MS);
  adminMode.tickTimer = setInterval(tickAdminMode, 1000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      refreshAdminMode();
    }
  });
  document.addEventListener("admin-session-changed", (event) => {
    if (!event.detail || event.detail.source !== "admin-mode") {
      refreshAdminMode();
    }
  });
}

if (typeof window !== "undefined") {
  window.adminLogin = openAdminLogin;
  window.adminReauthenticate = (options = {}) => openAdminLogin({ ...options, reauth: true });
  window.adminModeState = adminModeState;
  window.adminCsrfToken = () => (adminMode.session && typeof adminMode.session.csrf_token === "string"
    ? adminMode.session.csrf_token : null);
}

if (typeof document !== "undefined" && document.readyState !== "loading") {
  startAdminMode();
} else if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", startAdminMode);
}
