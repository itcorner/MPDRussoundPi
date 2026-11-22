const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../web/static/status.js"), "utf8");
const rendering = source.slice(
  source.indexOf("function formatTimestampLines("),
  source.indexOf("eventsTableBody.addEventListener("),
);

// Minimal DOM: any innerHTML/outerHTML write fails the test, so rendered values
// can only reach the page as text.
class FakeElement {
  constructor(tagName) {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = {};
    this.className = "";
    this.textContent = "";
    this.hidden = false;
    this.parent = null;
    this.scrollWidth = 0;
    this.clientWidth = 0;
    this.listeners = {};
  }

  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }

  fire(type) {
    for (const handler of this.listeners[type] || []) handler();
  }

  set innerHTML(_value) {
    throw new Error("innerHTML must not be used for status rendering");
  }

  set outerHTML(_value) {
    throw new Error("outerHTML must not be used for status rendering");
  }

  append(...nodes) {
    for (const node of nodes) this.appendChild(node);
  }

  appendChild(node) {
    node.parent = this;
    this.children.push(node);
    return node;
  }

  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }

  replaceWith(node) {
    const siblings = this.parent.children;
    siblings[siblings.indexOf(this)] = node;
    node.parent = this.parent;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  allText() {
    return [this.textContent, ...this.children.map((child) => child.allText())].join(" ");
  }

  find(predicate) {
    if (predicate(this)) return this;
    for (const child of this.children) {
      const match = child.find(predicate);
      if (match) return match;
    }
    return null;
  }
}

function render(clients, events, options = {}) {
  const elements = {
    clientCount: new FakeElement("span"),
    clientsList: new FakeElement("div"),
    clientsEmpty: new FakeElement("p"),
    eventsEmpty: new FakeElement("p"),
    eventsCount: new FakeElement("span"),
    eventsTableWrap: new FakeElement("div"),
    eventsTableBody: new FakeElement("tbody"),
  };
  const context = {
    ...elements,
    expandedPayloadRows: new Set(),
    document: {
      createElement: (tagName) => {
        const element = new FakeElement(tagName);
        if (tagName === "code") element.clientWidth = options.laidOut === false ? 0 : 200;
        return element;
      },
    },
  };
  vm.createContext(context);
  vm.runInContext(rendering, context);
  context.renderStatus({ connected_clients: clients }, { recent_events: events });
  return elements;
}

const markup = '<img src=x onerror="alert(1)">';

test("status page renders peer-controlled client fields as text", () => {
  const { clientsList, clientCount } = render([{
    id: 3,
    ip: markup,
    connected_at: "2026-11-20T10:00:00+00:00",
    session_id: markup,
    streams: 2,
    user_agent: `Mozilla ${markup}`,
  }], []);

  assert.equal(clientCount.textContent, "1");
  const text = clientsList.allText();
  assert.match(text, /User-Agent: Mozilla <img src=x onerror="alert\(1\)">/);
  assert.match(text, /Session: <img/);
  assert.equal(clientsList.find((node) => node.tagName === "STRONG").textContent, markup);
  assert.equal(clientsList.find((node) => node.tagName === "IMG"), null);
});

test("status page renders event path, IP and payload as text", () => {
  const { eventsTableBody } = render([], [{
    timestamp: "2026-11-20T10:00:00+00:00",
    ip: markup,
    path: `/api/config${markup}`,
    payload: { zones: [{ name: markup }] },
  }]);

  const row = eventsTableBody.children[0];
  const cells = row.children;
  assert.equal(cells[1].textContent, markup);
  assert.equal(cells[2].textContent, `/api/config${markup}`);
  // Not truncated in the fake DOM, so the toggle collapses to a static preview.
  const preview = cells[3].children[0];
  assert.equal(preview.className, "payload-preview payload-preview-static");
  assert.equal(preview.textContent, JSON.stringify({ zones: [{ name: markup }] }));
  assert.equal(eventsTableBody.find((node) => node.tagName === "IMG"), null);
});

test("the events count is the number shown and the empty table is hidden", () => {
  const empty = render([], []);
  assert.equal(empty.eventsCount.textContent, "0");
  assert.equal(empty.eventsTableWrap.hidden, true);
  assert.equal(empty.eventsEmpty.hidden, false);
  const events = [1, 2, 3].map((n) => ({ timestamp: "2026-11-20T10:00:00+00:00", ip: "192.0.2.4", path: `/api/${n}`, payload: {} }));
  const filled = render([], events);
  assert.equal(filled.eventsCount.textContent, "3");
  assert.equal(filled.eventsTableWrap.hidden, false);
  assert.equal(filled.eventsEmpty.hidden, true);
});

test("a collapsed panel has no layout, so payloads keep their toggle instead of becoming static", () => {
  const { eventsTableBody } = render([], [{ timestamp: "2026-11-20T10:00:00+00:00", ip: "192.0.2.4", path: "/api/x", payload: { a: 1 } }],
    { laidOut: false });
  assert.equal(eventsTableBody.children[0].children[3].children[0].tagName, "BUTTON");
});

test("truncated payloads keep the expand toggle", () => {
  const elements = {
    clientCount: new FakeElement("span"),
    clientsList: new FakeElement("div"),
    clientsEmpty: new FakeElement("p"),
    eventsEmpty: new FakeElement("p"),
    eventsCount: new FakeElement("span"),
    eventsTableWrap: new FakeElement("div"),
    eventsTableBody: new FakeElement("tbody"),
  };
  const context = {
    ...elements,
    expandedPayloadRows: new Set(),
    document: {
      createElement: (tagName) => {
        const element = new FakeElement(tagName);
        if (tagName === "code") { element.scrollWidth = 500; element.clientWidth = 200; }
        return element;
      },
    },
  };
  vm.createContext(context);
  vm.runInContext(rendering, context);
  context.renderStatus({ connected_clients: [] }, { recent_events: [{
    timestamp: "2026-11-20T10:00:00+00:00", ip: "192.0.2.4", path: "/api/source", payload: { source: 2 },
  }] });

  const toggle = elements.eventsTableBody.children[0].children[3].children[0];
  assert.equal(toggle.tagName, "BUTTON");
  assert.equal(toggle.getAttribute("data-payload-toggle"), "");
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
});


// ---------------------------------------------------------------- Host card

const reference = {
  agent_id: "musicpi", status: "ok", error: null, stale: false, last_success_at: "2026-11-21T10:00:00Z",
  host: {
    sections: {
      cpu: { status: "ok", age_ms: 2000, stale: false, error: null, value: {} },
      memory: { status: "ok", age_ms: 7400, stale: false, error: null, value: {} },
      power: { status: "unsupported", age_ms: null, stale: true, error: null, value: null },
    },
    assessment: {},
    view: {
      overall: "warning",
      summary: ["Wi-Fi weak (-76 dBm)", "CPU 33 %", "Memory 51 % used", "44 °C"],
      rows: [],
      groups: [
        { id: "network", title: "Network", level: "warning", summary: "Wi-Fi weak (-76 dBm)", rows: [
          { label: "Wi-Fi link", value: "-76 dBm (weak), 270 Mbit/s, 5 GHz", level: "warning" },
          { label: "Addresses", value: "192.168.1.105/24", level: "" },
        ] },
        { id: "processor", title: "Processor and memory", level: "ok", summary: "CPU 33 % · Memory 51 % used", rows: [
          { label: "Memory", value: "452 MiB used of 906 MiB", level: "ok" },
        ] },
      ],
    },
  },
};

function renderHostPayload(hosts, existing) {
  const elements = existing || {
    hostList: new FakeElement("div"),
    clientCount: new FakeElement("span"), clientsList: new FakeElement("div"), clientsEmpty: new FakeElement("p"),
    eventsEmpty: new FakeElement("p"), eventsTableBody: new FakeElement("tbody"),
  };
  const context = existing ? existing.context : {
    ...elements,
    expandedPayloadRows: new Set(),
    document: { createElement: (tagName) => new FakeElement(tagName) },
  };
  if (!existing) {
    vm.createContext(context);
    vm.runInContext(rendering, context);
    elements.context = context;
  }
  context.renderHosts({ hosts });
  return elements;
}

test("host card shows the level in words, the summary and collapsed details", () => {
  const { hostList } = renderHostPayload([reference]);
  assert.equal(hostList.children.length, 1);
  const card = hostList.children[0];
  const pill = card.find((node) => node.className.includes("host-level"));
  assert.equal(pill.textContent, "Warning");
  assert.match(pill.className, /host-level-warning/);
  assert.equal(card.find((node) => node.className.includes("host-summary")).textContent,
    "Wi-Fi weak (-76 dBm) · CPU 33 % · Memory 51 % used · 44 °C");
  assert.match(card.allText(), /Updated now\./, "under four seconds reads as now, like the playback page");
  const older = JSON.parse(JSON.stringify(reference));
  older.host.sections.cpu.age_ms = 7400;
  assert.match(renderHostPayload([older]).hostList.allText(), /Updated 7 s ago\./);
  const details = card.find((node) => node.tagName === "DETAILS");
  assert.equal(details.open, false);
  const text = details.allText();
  assert.match(text, /Network/);
  assert.match(text, /Processor and memory/);
  assert.match(text, /Wi-Fi link -76 dBm \(weak\), 270 Mbit\/s, 5 GHz \(Warning\)/);
  assert.match(text, /192\.168\.1\.105\/24/);
  assert.match(text, /Memory 452 MiB used of 906 MiB/);
});

test("expanded details stay open across refreshes", () => {
  const first = renderHostPayload([reference]);
  const details = first.hostList.children[0].find((node) => node.tagName === "DETAILS");
  details.open = true;
  details.fire("toggle");
  renderHostPayload([reference], first);
  assert.equal(first.hostList.children[0].find((node) => node.tagName === "DETAILS").open, true);
  const reopened = first.hostList.children[0].find((node) => node.tagName === "DETAILS");
  reopened.open = false;
  reopened.fire("toggle");
  renderHostPayload([reference], first);
  assert.equal(first.hostList.children[0].find((node) => node.tagName === "DETAILS").open, false);
});

test("unsupported, pending, unavailable and stale hosts explain themselves", () => {
  const cases = [
    [{ agent_id: "a", status: "unsupported", host: null, error: null, stale: false }, /does not report host information/],
    [{ agent_id: "a", status: "pending", host: null, error: null, stale: false }, /Waiting for the first host report/],
    [{ agent_id: "a", status: "unavailable", host: null, error: { code: "agent_timeout", message: "Playback agent timed out." }, stale: false },
      /unavailable: Playback agent timed out\./],
    [{ ...reference, stale: true }, /Data is old \(last update 2 s ago\)/],
  ];
  for (const [host, expected] of cases) {
    const { hostList } = renderHostPayload([host]);
    const card = hostList.children[0];
    assert.match(card.allText(), expected);
    if (!host.host) {
      assert.equal(card.find((node) => node.className.includes("host-level")).textContent, "Unknown");
      assert.equal(card.find((node) => node.tagName === "DETAILS"), null);
    }
  }
});

test("host text is only ever assigned as text, even for hostile labels", () => {
  const hostile = JSON.parse(JSON.stringify(reference));
  const markup = '<img src=x onerror="alert(1)">';
  hostile.agent_id = markup;
  hostile.host.view.summary = [markup];
  hostile.host.view.groups = [{ id: "g", title: markup, level: "ok", summary: markup, rows: [{ label: markup, value: markup, level: "problem" }] }];
  const { hostList } = renderHostPayload([hostile]);
  assert.equal(hostList.find((node) => node.tagName === "IMG"), null);
  assert.match(hostList.allText(), /<img src=x onerror="alert\(1\)"> \(Problem\)/);
});

test("no configured agent renders no host card", () => {
  assert.equal(renderHostPayload([]).hostList.children.length, 0);
});

function renderOverviewPayload(payload, existing) {
  const elements = existing || {
    overviewList: new FakeElement("div"), overviewEmpty: new FakeElement("p"), overviewLevel: new FakeElement("span"),
    overviewUpdate: new FakeElement("span"),
  };
  const context = existing ? existing.context : {
    ...elements,
    document: { createElement: (tagName) => new FakeElement(tagName) },
  };
  if (!existing) {
    vm.createContext(context);
    vm.runInContext(rendering, context);
    elements.context = context;
  }
  context.renderOverview(payload);
  return elements;
}

const overviewPayload = {
  overall: "problem",
  sections: [
    { id: "web", title: "Web application", level: "ok", summary: "Running · 1 h", rows: [{ label: "Status", value: "Running", level: "ok" }], groups: [] },
    {
      id: "audio:musicpi", title: "Audio system (musicpi)", level: "problem", summary: "1 of 1 outputs playing", rows: [],
      groups: [{ id: "front", title: "Front <b>stream</b>", level: "problem", summary: "Playing",
        rows: [{ label: "Audio route", value: "Wrong: expected a, actual b", level: "problem" }] }],
    },
  ],
};

test("overview shows levels in words, nested groups and text-only values", () => {
  const view = renderOverviewPayload(overviewPayload);
  assert.equal(view.overviewLevel.textContent, "Problem");
  assert.match(view.overviewLevel.className, /host-level-problem/);
  assert.equal(view.overviewEmpty.hidden, true);
  assert.equal(view.overviewList.children.length, 2);
  const text = view.overviewList.allText();
  assert.match(text, /Front <b>stream<\/b>/, "markup in labels stays text");
  assert.match(text, /Wrong: expected a, actual b \(Problem\)/);
  assert.equal(view.overviewList.find((node) => node.tagName === "B"), null);
});

test("overview details keep their open state across refreshes", () => {
  const first = renderOverviewPayload(overviewPayload);
  const card = () => first.overviewList.children[1];
  const outer = card().find((node) => node.tagName === "DETAILS");
  outer.open = true;
  outer.fire("toggle");
  renderOverviewPayload(overviewPayload, first);
  assert.equal(card().find((node) => node.tagName === "DETAILS").open, true);
});

test("a Docker Hub link is rendered as a link, any other address stays text", () => {
  const link = (url) => ({ overall: "ok", sections: [{ id: "web", title: "Web", level: "ok", summary: "", groups: [],
    rows: [{ label: "Docker Hub", value: "Open this tag", level: "", link: url }] }] });
  const good = renderOverviewPayload(link("https://hub.docker.com/r/ns/mpdrussoundpi-web/tags?name=1.0.0"));
  const anchor = good.overviewList.find((node) => node.tagName === "A");
  assert.equal(anchor.getAttribute("href"), "https://hub.docker.com/r/ns/mpdrussoundpi-web/tags?name=1.0.0");
  assert.equal(anchor.getAttribute("rel"), "noopener noreferrer");
  assert.equal(anchor.textContent, "Open this tag");
  for (const bad of ["javascript:alert(1)", "https://evil.example/r/x", "http://hub.docker.com/r/x"]) {
    const view = renderOverviewPayload(link(bad));
    assert.equal(view.overviewList.find((node) => node.tagName === "A"), null, bad);
  }
});

test("the overview header shows only a small update icon", () => {
  const view = renderOverviewPayload({ overall: "ok", update_available: true, sections: [] });
  assert.equal(view.overviewUpdate.hidden, false);
  assert.equal(renderOverviewPayload({ overall: "ok", update_available: false, sections: [] }).overviewUpdate.hidden, true);
});

test("an empty overview shows the placeholder and an unknown level", () => {
  const view = renderOverviewPayload({ overall: "weird", sections: [] });
  assert.equal(view.overviewEmpty.hidden, false);
  assert.equal(view.overviewLevel.textContent, "Unknown");
});


function renderAdminState(session) {
  const elements = {
    adminPanel: new FakeElement("details"), adminState: new FakeElement("span"), adminLoginForm: new FakeElement("form"), adminPassword: new FakeElement("input"),
    adminSession: new FakeElement("div"), adminSessionText: new FakeElement("span"), adminLogout: new FakeElement("button"),
    adminSetup: new FakeElement("p"), adminMessage: new FakeElement("p"), adminUpdate: new FakeElement("section"),
  };
  elements.adminPassword.focus = () => { elements.adminPassword.focused = true; };
  const context = { ...elements, document: { createElement: (tagName) => new FakeElement(tagName) } };
  vm.createContext(context);
  vm.runInContext(rendering, context);
  context.renderAdmin(session);
  return { ...elements, context };
}

test("administrator panel opens only when a login is possible and keeps the user's choice", () => {
  const view = renderAdminState({ configured: true, authenticated: false, locked_for_seconds: 0 });
  assert.equal(view.adminPanel.open, true, "set up but not logged in: open");
  view.adminPanel.open = false;  // the user closes it
  view.context.renderAdmin({ configured: true, authenticated: false, locked_for_seconds: 0 });
  assert.equal(view.adminPanel.open, false, "a refresh with the same state keeps the user's choice");
  view.adminPanel.open = true;  // the user opens it again and logs in
  view.context.renderAdmin({ configured: true, authenticated: true, csrf_token: "t", idle_expires_in_seconds: 900, expires_in_seconds: 28800 });
  assert.equal(view.adminPanel.open, true, "logging in keeps the panel open");
  view.context.renderAdmin({ configured: true, authenticated: false, locked_for_seconds: 0 });
  assert.equal(view.adminPanel.open, true, "after logout or expiry it opens again");
  assert.equal(renderAdminState({ configured: false, authenticated: false, locked_for_seconds: 0 }).adminPanel.open, false,
    "not set up: collapsed");
  assert.equal(renderAdminState({ configured: true, authenticated: true, csrf_token: "t", idle_expires_in_seconds: 900,
    expires_in_seconds: 28800 }).adminPanel.open, false, "already logged in when the page loads: collapsed");
});

test("a failed login shows the message and returns to the password field", () => {
  const view = renderAdminState({ configured: true, authenticated: false, locked_for_seconds: 0 });
  view.context.showAdminFailure("Login failed.");
  assert.equal(view.adminMessage.hidden, false);
  assert.equal(view.adminMessage.textContent, "Login failed.");
  assert.equal(view.adminPassword.focused, true);
  view.context.showAdminMessage("");
  assert.equal(view.adminMessage.hidden, true);
});

test("administrator panel shows setup, login, locked and logged-in states in words", () => {
  const setup = renderAdminState({ configured: false, authenticated: false, locked_for_seconds: 0 });
  assert.equal(setup.adminState.textContent, "Not set up");
  assert.equal(setup.adminSetup.hidden, false);
  assert.equal(setup.adminLoginForm.hidden, true);
  const out = renderAdminState({ configured: true, authenticated: false, locked_for_seconds: 0 });
  assert.equal(out.adminState.textContent, "Not logged in");
  assert.equal(out.adminLoginForm.hidden, false);
  assert.equal(out.adminSession.hidden, true);
  const locked = renderAdminState({ configured: true, authenticated: false, locked_for_seconds: 600 });
  assert.equal(locked.adminState.textContent, "Locked");
  assert.match(locked.adminMessage.textContent, /locked for about 10 minutes/);
  const inside = renderAdminState({ configured: true, authenticated: true, csrf_token: "token-1",
    idle_expires_in_seconds: 900, expires_in_seconds: 28800, recently_authenticated: true });
  assert.equal(inside.adminState.textContent, "Logged in");
  assert.equal(inside.adminLoginForm.hidden, true);
  assert.equal(inside.adminSession.hidden, false);
  assert.match(inside.adminSessionText.textContent, /15 minutes without activity and at the latest in 480 minutes/);
  assert.equal(vm.runInContext("adminCsrfToken", inside.context), "token-1");
});


const availableUpdates = {
  state: "update_available", installed: { version: "dev-" + "a".repeat(40) }, stale: false, check_failed: false,
  checked_at: new Date().toISOString(), manual_check: { available: true, retry_after_seconds: 0 },
  result: { state: "update_available", available: { version: "dev-" + "c".repeat(40), published_at: new Date().toISOString(),
    link: "https://hub.docker.com/r/itcornernet/mpdrussoundpi-web/tags?name=dev-c" } },
};
const pendingReview = { configured: true, phase: "awaiting_approval", busy: true, review: {
  operation: "op-1", digest: "sha256:abc", action: "update", revision: "c".repeat(40), expires_in_seconds: 95,
  images: { web: "itcornernet/mpdrussoundpi-web@sha256:1" }, layout_change: "adds the read-only updater socket mount",
  interruption: "The web application restarts.", backup: "The backup is kept on this Pi. It is not disaster recovery.",
} };

function renderUpdates(view, updates, status, csrf = "t") {
  vm.runInContext(`adminCsrfToken = ${JSON.stringify(csrf)};`, view.context);
  view.context.latestUpdates = updates;
  vm.runInContext(`latestUpdates = this.latestUpdates; adminUpdateState = this.adminUpdateState;`,
    Object.assign(view.context, { adminUpdateState: status }));
  view.context.renderSoftwareUpdates();
  return view.adminUpdate;
}

function buttons(node) {
  const labels = [];
  node.find((child) => { if (child.tagName === "BUTTON") labels.push(child.textContent); return false; });
  return labels;
}

test("software updates: one block with check info for everyone and install for the administrator", () => {
  const view = renderAdminState({ configured: true, authenticated: false, locked_for_seconds: 0 });
  const anonymous = renderUpdates(view, availableUpdates, { configured: true, phase: "idle", busy: false }, null);
  assert.match(anonymous.allText(), /Installed dev-aaaaaaa/);
  assert.match(anonymous.allText(), /Newest published dev-ccccccc \(published just now\)/);
  assert.match(anonymous.allText(), /Log in as administrator to install it\./);
  assert.deepEqual(buttons(anonymous), ["Check now"]);
  const admin = renderUpdates(view, availableUpdates, { configured: true, phase: "idle", busy: false });
  assert.deepEqual(buttons(admin), ["Check now", "Update to dev-ccccccc"]);
  const upToDate = renderUpdates(view, { ...availableUpdates, state: "up_to_date" }, { configured: true, phase: "idle", busy: false });
  assert.deepEqual(buttons(upToDate), ["Check now"], "no install button without an available update");
  const notSetUp = renderUpdates(view, availableUpdates, { configured: false, phase: "idle" });
  assert.match(notSetUp.allText(), /not set up here/);
});

test("software updates: the review keeps typed text across refreshes and fails safely", () => {
  const view = renderAdminState({ configured: true, authenticated: true, csrf_token: "t", idle_expires_in_seconds: 900, expires_in_seconds: 28800 });
  const block = renderUpdates(view, availableUpdates, pendingReview);
  assert.match(block.allText(), /Review: update to dev-ccccccc/);
  assert.match(block.allText(), /not disaster recovery/);
  const input = block.find((node) => node.tagName === "INPUT");
  const confirm = block.find((node) => node.tagName === "BUTTON" && node.textContent === "Confirm update");
  input.value = "upd";
  input.fire("input");
  assert.equal(confirm.disabled, true);
  const refreshed = JSON.parse(JSON.stringify(pendingReview));
  refreshed.review.expires_in_seconds = 80;
  renderUpdates(view, availableUpdates, refreshed);
  const again = block.find((node) => node.tagName === "INPUT");
  assert.equal(again, input, "the same field survives the refresh");
  assert.equal(again.value, "upd");
  assert.match(block.allText(), /80 s/);
  again.value = "update";
  again.fire("input");
  assert.equal(confirm.disabled, false);
  const failed = renderUpdates(view, availableUpdates, { configured: true, phase: "failed", busy: false, restore_available: true,
    operation: "op-1", error: "<img src=x onerror=1>" });
  assert.deepEqual(buttons(failed), ["Check now", "Restore previous version", "Update to dev-ccccccc"]);
  assert.equal(failed.find((node) => node.tagName === "IMG"), null);
  const unknown = renderUpdates(view, availableUpdates, { configured: true, phase: "outcome_unknown", busy: false });
  assert.deepEqual(buttons(unknown), ["Check now"], "no new update while the outcome is unknown");
});

test("software updates: a failure is stated once, in the updater's words when it gives them", () => {
  const view = renderAdminState({ configured: true, authenticated: true, csrf_token: "t", idle_expires_in_seconds: 900, expires_in_seconds: 28800 });
  const message = (status) => {
    const block = renderUpdates(view, availableUpdates, { configured: true, busy: false, ...status });
    const paragraphs = [];
    block.find((node) => { if (node.tagName === "P") paragraphs.push(node.textContent); return false; });
    return paragraphs;
  };
  assert.deepEqual(message({ phase: "failed", kind: "update", restore_available: true, operation: "op-1",
    error: "The update failed after the services were stopped." }),
  ["The update failed after the services were stopped."]);
  assert.deepEqual(message({ phase: "failed", kind: "restore", error: "The restore failed; inspect the deployment state on the Pi." }),
    ["The restore failed; inspect the deployment state on the Pi."], "a failed restore is not reported as a failed update");
  assert.deepEqual(message({ phase: "failed_before_interruption",
    error: "The newest image could not be resolved on Docker Hub. Nothing was interrupted." }),
  ["The newest image could not be resolved on Docker Hub. Nothing was interrupted."]);
  assert.deepEqual(message({ phase: "failed" }), ["The last update failed after the services were stopped."],
    "without an updater message the phase text remains");
  assert.deepEqual(message({ phase: "cancelled", error: "The review expired." }), ["The last update was not started."]);
});

test("software updates: a successful update says how many old images it removed", () => {
  const view = renderAdminState({ configured: true, authenticated: true, csrf_token: "t", idle_expires_in_seconds: 900, expires_in_seconds: 28800 });
  const paragraphs = (status) => {
    const block = renderUpdates(view, availableUpdates, { configured: true, busy: false, ...status });
    const found = [];
    block.find((node) => { if (node.tagName === "P") found.push(node.textContent); return false; });
    return found;
  };
  assert.deepEqual(paragraphs({ phase: "succeeded", cleanup: { removed: 4, freed_bytes: 650_400_000 } }),
    ["The last update finished.", "Removed 4 old images (650 MB)."]);
  assert.deepEqual(paragraphs({ phase: "succeeded", cleanup: { removed: 1, freed_bytes: 0 } }),
    ["The last update finished.", "Removed 1 old image (0 MB)."]);
  for (const cleanup of [null, { removed: 0, freed_bytes: 0 }, { removed: "4" }]) {
    assert.deepEqual(paragraphs({ phase: "succeeded", cleanup }), ["The last update finished."]);
  }
  assert.deepEqual(paragraphs({ phase: "restored", cleanup: { removed: 4, freed_bytes: 1 } }),
    ["The previous version was restored."], "only after a successful update");
  const review = renderUpdates(view, availableUpdates, { ...pendingReview, review: { ...pendingReview.review,
    cleanup: "After a successful update, old images of this app are removed." } });
  assert.match(review.allText(), /Old images.*After a successful update, old images of this app are removed\./);
});
