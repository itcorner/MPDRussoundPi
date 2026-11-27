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

  fire(type, event = {}) {
    for (const handler of this.listeners[type] || []) handler(event);
  }

  set innerHTML(_value) {
    throw new Error("innerHTML must not be used for status rendering");
  }

  set outerHTML(_value) {
    throw new Error("outerHTML must not be used for status rendering");
  }

  append(...nodes) {
    for (const node of nodes) {
      if (typeof node === "string") {
        // Like the DOM: a string becomes a text node.
        const text = new FakeElement("#text");
        text.textContent = node;
        this.appendChild(text);
      } else {
        this.appendChild(node);
      }
    }
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


// The session and the login dialog belong to admin.js (tests/test_admin_mode.js); the
// Status page keeps the token for privileged requests and shows the update sections.
function renderStatusView(session, login = []) {
  const elements = {
    updatesState: new FakeElement("span"), adminMessage: new FakeElement("p"), adminUpdate: new FakeElement("section"),
    restorePanel: new FakeElement("details"), restoreState: new FakeElement("span"), restoreBox: new FakeElement("section"),
    systemPanel: new FakeElement("details"), systemState: new FakeElement("span"), systemBox: new FakeElement("section"),
    systemMessage: new FakeElement("p"),
  };
  const context = {
    ...elements,
    document: { createElement: (tagName) => new FakeElement(tagName), createTextNode: (text) => {
      const node = new FakeElement("#text");
      node.textContent = String(text);
      return node;
    } },
    window: { adminLogin: (options) => login.push(options) },
  };
  vm.createContext(context);
  vm.runInContext(rendering, context);
  context.renderAdmin(session);
  return { ...elements, context, login };
}

const guest = { configured: true, authenticated: false, locked_for_seconds: 0 };
const signedIn = { configured: true, authenticated: true, csrf_token: "t", idle_expires_in_seconds: 900,
  expires_in_seconds: 28800 };

test("the page keeps only the token of a signed-in session", () => {
  const view = renderStatusView(guest);
  assert.equal(vm.runInContext("adminCsrfToken", view.context), null);
  view.context.renderAdmin(signedIn);
  assert.equal(vm.runInContext("adminCsrfToken", view.context), "t");
  view.context.renderAdmin({ configured: false, authenticated: true, csrf_token: "x" });
  assert.equal(vm.runInContext("adminCsrfToken", view.context), null, "no token where no login is set up");
});

const availableUpdates = {
  state: "update_available", installed: { version: "dev-" + "a".repeat(40) }, stale: false, check_failed: false,
  checked_at: new Date().toISOString(), manual_check: { available: true, retry_after_seconds: 0 },
  result: { state: "update_available", available: { version: "dev-" + "c".repeat(40), published_at: new Date().toISOString(),
    link: "https://hub.docker.com/r/itcornernet/mpdrussoundpi-web/tags?name=dev-c" } },
};
const pendingReview = { configured: true, phase: "awaiting_approval", busy: true, review: {
  operation: "op-1", digest: "sha256:abc", action: "update", revision: "c".repeat(40), expires_in_seconds: 95,
  images: { web: "itcornernet/mpdrussoundpi-web@sha256:" + "2777aa50ce6f".padEnd(64, "0") },
  layout_change: "adds the read-only updater socket mount",
  interruption: "Web app and gateway restart.", backup: "Taken first, kept on this Pi only (not disaster recovery).",
} };
const failedUpdate = { configured: true, phase: "failed", kind: "update", busy: false, restore_available: true,
  operation: "op-1", candidate_revision: "c".repeat(40), error: "<img src=x onerror=1>",
  backups: [{ operation: "op-1", taken_at: new Date(Date.now() - 2 * 3600000).toISOString(), revision: "a".repeat(40),
    size_bytes: 108130296, reason: "before the update that failed" }] };

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

// Values created inside the page's sandbox have its own Object prototype; compare as data.
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function button(node, label) {
  return node.find((child) => child.tagName === "BUTTON" && child.textContent === label);
}

test("software updates: privileged buttons are amber for everyone and a guest signs in first", () => {
  const view = renderStatusView(guest);
  const ran = [];
  vm.runInContext("runUpdateAction = (action, body) => this.ran.push([action, body]);", Object.assign(view.context, { ran }));
  const anonymous = renderUpdates(view, availableUpdates, { configured: true, phase: "idle", busy: false }, null);
  assert.match(anonymous.allText(), /Installed dev-aaaaaaa/);
  assert.match(anonymous.allText(), /Newest published dev-ccccccc \(published just now\)/);
  assert.deepEqual(buttons(anonymous), ["Check now", "Update to dev-ccccccc"]);
  const install = button(anonymous, "Update to dev-ccccccc");
  assert.match(install.className, /admin-action/, "the amber shield style");
  install.fire("click");
  assert.equal(view.login.length, 1, "a guest's click opens the login dialog");
  assert.equal(view.login[0].reason, "Install the update to dev-ccccccc");
  assert.deepEqual(ran, []);
  view.login[0].onSuccess();
  assert.deepEqual(plain(ran), [["review", {}]], "and the action continues after signing in");
  const admin = renderUpdates(view, availableUpdates, { configured: true, phase: "idle", busy: false });
  button(admin, "Update to dev-ccccccc").fire("click");
  assert.deepEqual(plain(ran.at(-1)), ["review", {}]);
  assert.equal(view.login.length, 1, "signed in: no dialog");
  const upToDate = renderUpdates(view, { ...availableUpdates, state: "up_to_date" }, { configured: true, phase: "idle", busy: false });
  assert.deepEqual(buttons(upToDate), ["Check now"], "no install button without an available update");
  const notSetUp = renderUpdates(view, availableUpdates, { configured: false, phase: "idle" });
  assert.match(notSetUp.allText(), /not set up here/);
  assert.equal(view.updatesState.textContent, "Update available");
  assert.match(view.updatesState.className, /host-level-update/);
});

test("software updates: the channel is shown to everyone and changed by the administrator after a confirmation", () => {
  const view = renderStatusView(signedIn);
  const ran = [];
  vm.runInContext("runUpdateAction = (action, body) => this.ran.push([action, body]);", Object.assign(view.context, { ran }));
  const status = { configured: true, phase: "idle", busy: false, channel: "development" };
  const guestView = renderUpdates(view, availableUpdates, status, null);
  assert.match(guestView.allText(), /Channel Development builds/);
  assert.equal(guestView.find((node) => node.tagName === "SELECT"), null, "only the administrator chooses");

  const answers = [];
  view.context.window.confirm = (text) => { answers.push(text); return answers.length > 1; };
  const admin = renderUpdates(view, availableUpdates, status);
  const select = admin.find((node) => node.tagName === "SELECT");
  assert.match(select.className, /admin-action/);
  assert.equal(select.getAttribute("aria-label"), "Update channel");
  select.value = "stable";
  select.fire("change");
  assert.match(answers[0], /Follow stable releases\?/);
  assert.match(answers[0], /nothing goes back to an older version/);
  assert.equal(select.value, "development", "declined: the choice goes back");
  assert.deepEqual(ran, []);
  select.value = "stable";
  select.fire("change");
  assert.deepEqual(plain(ran), [["channel", { channel: "stable" }]]);
  assert.equal(select.disabled, true);

  const busy = renderUpdates(view, availableUpdates, { ...status, busy: true, phase: "running" });
  assert.equal(busy.find((node) => node.tagName === "SELECT"), null, "never during an update");

  const waiting = renderUpdates(view, { ...availableUpdates, state: "waiting_for_release",
    result: { state: "waiting_for_release", available: { version: "0.9.0", revision: "c".repeat(7), published_at: null } } },
  { ...status, channel: "stable" }, null);
  assert.match(waiting.allText(), /Channel Stable releases/);
  assert.match(waiting.allText(), /Status Waiting for a release/);
  assert.match(waiting.allText(), /development build that 0\.9\.0 does not include yet/);
  assert.deepEqual(buttons(waiting), ["Check now"], "nothing is offered");
  assert.equal(view.updatesState.textContent, "Waiting for a release");
});

test("software updates: an open channel list survives the refresh and is redrawn once it loses the focus", () => {
  const view = renderStatusView(signedIn);
  const status = { configured: true, phase: "idle", busy: false, channel: "development" };
  const select = renderUpdates(view, availableUpdates, status).find((node) => node.tagName === "SELECT");
  view.context.document.activeElement = select;
  const again = renderUpdates(view, { ...availableUpdates, checked_at: new Date().toISOString() }, status);
  assert.equal(again.find((node) => node.tagName === "SELECT"), select, "the same, still open list");
  view.context.document.activeElement = null;
  const later = renderUpdates(view, availableUpdates, status);
  assert.notEqual(later.find((node) => node.tagName === "SELECT"), select);
  assert.ok(later.find((node) => node.tagName === "SELECT"));
});

test("software updates: a running update shows its step, the time and a progress bar, also while the page is offline", () => {
  const view = renderStatusView(signedIn);
  const typical = [5, 60, 190, 20, 10];
  const running = (progress) => ({ configured: true, phase: "running", busy: true, kind: "update", progress });
  let block = renderUpdates(view, availableUpdates,
    running({ step: 1, steps: 5, elapsed_seconds: 3, step_elapsed_seconds: 3, typical_seconds: typical }));
  const text = block.allText();
  assert.match(text, /Step 1 of 5 · 0:03 elapsed · about 5 min left/);
  assert.match(text, /▸ Preparing/);
  assert.match(text, /○ Stopping the services and taking a backup/);
  assert.match(text, /loses its connection during steps 2 and 3/);
  assert.equal(block.find((node) => node.tagName === "PROGRESS").value, 1);

  // Offline 70 s after the page last saw step 2 (10 s into it): by last time's durations, step 3.
  vm.runInContext("adminUpdateReconnecting = true; adminUpdateReceivedAt = Date.now() - 70000;", view.context);
  block = renderUpdates(view, availableUpdates,
    running({ step: 2, steps: 5, elapsed_seconds: 15, step_elapsed_seconds: 10, typical_seconds: typical }));
  assert.match(block.allText(), /Step 3 of 5 · 1:25 elapsed · about 4 min left/);
  assert.match(block.allText(), /✓ Stopping the services and taking a backup/);
  assert.match(block.allText(), /▸ Loading and starting the new version/);
  assert.match(block.allText(), /estimated from the last update/);
  assert.equal(block.find((node) => node.tagName === "PROGRESS").value, 30);
  assert.doesNotMatch(block.allText(), /Waiting for the web application/);

  // The first update has no durations yet: the last step seen, and no time estimate.
  block = renderUpdates(view, availableUpdates,
    running({ step: 2, steps: 5, elapsed_seconds: 15, step_elapsed_seconds: 10, typical_seconds: null }));
  assert.match(block.allText(), /Step 2 of 5 · 1:25 elapsed/);
  assert.doesNotMatch(block.allText(), /left/);
  assert.match(block.allText(), /last step it saw/);
  vm.runInContext("adminUpdateReconnecting = false;", view.context);
});

test("software updates: Check now sits in one group with the label and the age of the last check", () => {
  const view = renderStatusView(signedIn);
  const block = renderUpdates(view, availableUpdates, { configured: true, phase: "idle", busy: false });
  const group = block.find((node) => node.className === "update-check-group");
  assert.ok(group, "label, age and button form one group");
  assert.match(group.allText(), /Last check/);
  assert.equal(group.find((node) => node.className === "update-check-age").textContent, "just now");
  assert.ok(button(group, "Check now"));
  const newest = block.find((node) => node.tagName === "DD" && /dev-ccccccc/.test(node.textContent));
  assert.ok(newest.find((node) => node.tagName === "A" && node.textContent === "Docker Hub"), "the link sits with the version");
  const failedCheck = renderUpdates(view, { ...availableUpdates, check_failed: true, error: "Docker Hub did not answer." },
    { configured: true, phase: "idle", busy: false });
  assert.match(failedCheck.allText(), /Check failed Docker Hub did not answer\./, "an error gets its own line");
});

test("software updates: the review keeps typed text across refreshes and hides the version overview", () => {
  const view = renderStatusView(signedIn);
  const block = renderUpdates(view, availableUpdates, pendingReview);
  assert.match(block.allText(), /Review: update to dev-ccccccc/);
  assert.match(block.allText(), /not disaster recovery/);
  assert.match(block.allText(), /Images\s+web\s+2777aa50ce6f/, "short image digests");
  assert.equal(block.infoBox.hidden, true, "while the review is open it is the only thing shown");
  const input = block.find((node) => node.tagName === "INPUT");
  const confirm = button(block, "Confirm update");
  assert.match(confirm.className, /admin-action/);
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
});

test("software updates: Enter in the confirmation field confirms only with the exact word", () => {
  const view = renderStatusView(signedIn);
  const ran = [];
  vm.runInContext("runUpdateAction = (action, body) => this.ran.push([action, body]);", Object.assign(view.context, { ran }));
  for (const [status, word] of [[pendingReview, "update"],
    [{ ...pendingReview, phase: "restore_awaiting_approval", kind: "restore",
      review: { ...pendingReview.review, action: "restore" } }, "restore"]]) {
    ran.length = 0;
    const block = renderUpdates(view, availableUpdates, status);
    const input = [view.adminUpdate, view.restoreBox].map((box) => box.find((node) => node.tagName === "INPUT"))
      .find(Boolean);
    let prevented = 0;
    const enter = (extra = {}) => input.fire("keydown", { key: "Enter", preventDefault: () => { prevented += 1; }, ...extra });
    input.value = "upd";
    input.fire("input");
    enter();
    assert.deepEqual(plain(ran), [], `an incomplete word does not confirm (${word})`);
    assert.equal(prevented, 1, "Enter never submits anything else");
    input.value = ` ${word.toUpperCase()} `;
    input.fire("input");
    enter({ repeat: true });
    enter({ isComposing: true });
    input.fire("keydown", { key: "a", preventDefault: () => { prevented += 1; } });
    assert.deepEqual(plain(ran), [], "held keys, composition and other keys do not confirm");
    enter();
    assert.deepEqual(plain(ran), [["approve", { operation: status.review.operation, digest: status.review.digest }]],
      `Enter confirms like the button (${word})`);
    assert.ok(block);
  }
});

test("a failed update offers a retry and opens the Restore backup section with the backup's facts", () => {
  const view = renderStatusView(signedIn);
  const ran = [];
  vm.runInContext("runUpdateAction = (action, body) => this.ran.push([action, body]);", Object.assign(view.context, { ran }));
  const failed = renderUpdates(view, availableUpdates, failedUpdate);
  assert.deepEqual(buttons(failed), ["Check now", "Retry update to dev-ccccccc"], "the same version is a retry");
  assert.equal(failed.find((node) => node.tagName === "IMG"), null, "the updater's text is never markup");
  assert.equal(view.restorePanel.hidden, false);
  assert.equal(view.restorePanel.open, true, "the section opens by itself after a failed update");
  assert.equal(view.restoreState.textContent, "Update failed");
  assert.match(view.restoreBox.allText(), /Backup from /);
  assert.match(view.restoreBox.allText(), /Taken 2 h ago, before the update that failed/);
  assert.match(view.restoreBox.allText(), /Version dev-aaaaaaa/);
  assert.match(view.restoreBox.allText(), /Size 108 MB/);
  button(view.restoreBox, "Restore this backup").fire("click");
  assert.deepEqual(plain(ran), [["restore", { operation: "op-1" }]]);
  view.restorePanel.open = false;  // the user closes it
  renderUpdates(view, availableUpdates, failedUpdate);
  assert.equal(view.restorePanel.open, false, "opened once per operation, the user's choice then stays");
  const newer = renderUpdates(view, availableUpdates, { ...failedUpdate, candidate_revision: "b".repeat(40) });
  assert.deepEqual(buttons(newer), ["Check now", "Update to dev-ccccccc"], "a newer version is a normal update");
  const unknown = renderUpdates(view, availableUpdates, { configured: true, phase: "outcome_unknown", kind: "update",
    busy: false, restore_available: true, operation: "op-1" });
  assert.deepEqual(buttons(unknown), ["Check now"], "no retry while the outcome is unknown");
  const idle = renderUpdates(view, availableUpdates, { configured: true, phase: "idle", busy: false });
  assert.equal(view.restorePanel.hidden, true, "nothing to restore, no section");
  assert.deepEqual(buttons(idle), ["Check now", "Update to dev-ccccccc"]);
});

test("a failed update whose candidate is still running is offered as a retry, not as up to date", () => {
  // On the Pi the failed candidate kept running: the check compared against it and said "up to date".
  const view = renderStatusView(signedIn);
  const candidate = { version: "dev-" + "c".repeat(40), revision: "ccccccc", published_at: new Date().toISOString(),
    link: "https://hub.docker.com/r/itcornernet/mpdrussoundpi-web/tags?name=dev-c" };
  const runningCandidate = { ...availableUpdates, state: "up_to_date",
    installed: { version: candidate.version, revision: "ccccccc" }, result: { state: "up_to_date", available: candidate } };
  const recorded = { ...failedUpdate, installed_revision: "a".repeat(40) };
  const failed = renderUpdates(view, runningCandidate, recorded);
  assert.deepEqual(buttons(failed), ["Check now", "Retry update to dev-ccccccc"]);
  assert.match(failed.allText(), /Status\s*Update failed/);
  assert.doesNotMatch(failed.allText(), /Up to date/);
  assert.match(failed.allText(), /Docker Hub/);
  assert.equal(view.updatesState.textContent, "Update failed");
  assert.match(view.updatesState.className, /host-level-problem/);
  assert.equal(view.restorePanel.open, true);
  for (const [label, status] of [
    ["after the restore", { ...recorded, kind: "restore", phase: "restored", restore_available: false }],
    ["when the candidate was recorded as installed", { ...recorded, installed_revision: "c".repeat(40) }],
    ["for an unknown outcome", { ...recorded, phase: "outcome_unknown" }],
    ["for another candidate", { ...recorded, candidate_revision: "b".repeat(40) }],
  ]) {
    const node = renderUpdates(view, runningCandidate, status);
    assert.deepEqual(buttons(node), ["Check now"], label);
    assert.equal(view.updatesState.textContent, "Up to date", label);
  }
});

test("a pending restore review opens its section and pauses the update button", () => {
  const view = renderStatusView(signedIn);
  const status = { ...failedUpdate, phase: "restore_awaiting_approval", kind: "restore", review: {
    operation: "op-1", digest: "sha256:r", action: "restore", revision: "a".repeat(40), expires_in_seconds: 100,
    images: {}, interruption: "Web app and gateway restart.", backup: "Taken first." } };
  const updates = renderUpdates(view, availableUpdates, status);
  assert.deepEqual(buttons(updates), ["Check now"]);
  assert.equal(view.restorePanel.open, true);
  assert.equal(view.restoreState.textContent, "Confirm restore");
  assert.match(view.restoreBox.allText(), /Review: restore backup/);
  assert.ok(button(view.restoreBox, "Confirm restore"));
});

test("software updates: a failure is stated once, in its own section, in the updater's words", () => {
  const view = renderStatusView(signedIn);
  const message = (status) => {
    renderUpdates(view, availableUpdates, { configured: true, busy: false, ...status });
    const paragraphs = [];
    for (const box of [view.adminUpdate, view.restoreBox]) {
      box.find((node) => { if (node.tagName === "P") paragraphs.push(node.textContent); return false; });
    }
    return paragraphs;
  };
  assert.deepEqual(message({ phase: "failed", kind: "update", restore_available: true, operation: "op-1",
    error: "The update failed after the services were stopped." }),
  ["The update failed after the services were stopped."]);
  assert.deepEqual(message({ phase: "failed", kind: "restore", error: "The restore failed; inspect the deployment state on the Pi." }),
    ["The restore failed; inspect the deployment state on the Pi."], "a failed restore is not reported as a failed update");
  assert.match(view.restoreBox.allText(), /The restore failed/, "and it appears in the restore section");
  assert.deepEqual(message({ phase: "failed_before_interruption",
    error: "The newest image could not be resolved on Docker Hub. Nothing was interrupted." }),
  ["The newest image could not be resolved on Docker Hub. Nothing was interrupted."]);
  assert.deepEqual(message({ phase: "failed" }), ["The last update failed after the services were stopped."],
    "without an updater message the phase text remains");
  assert.deepEqual(message({ phase: "cancelled", error: "The review expired." }), ["The last update was not started."]);
});

test("software updates: a successful update says how many old images it removed", () => {
  const view = renderStatusView(signedIn);
  const paragraphs = (status) => {
    renderUpdates(view, availableUpdates, { configured: true, busy: false, ...status });
    const found = [];
    for (const box of [view.adminUpdate, view.restoreBox]) {
      box.find((node) => { if (node.tagName === "P") found.push(node.textContent); return false; });
    }
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
    cleanup: "Removed after success; the backup keeps a copy." } });
  assert.match(review.allText(), /Old images.*Removed after success; the backup keeps a copy\./);
});

test("software updates: a successful update also says how many older backups it removed", () => {
  const view = renderStatusView(signedIn);
  const paragraphs = (status) => {
    renderUpdates(view, availableUpdates, { configured: true, busy: false, ...status });
    const found = [];
    view.adminUpdate.find((node) => { if (node.tagName === "P") found.push(node.textContent); return false; });
    return found;
  };
  assert.deepEqual(paragraphs({ phase: "succeeded", cleanup: { removed: 2, freed_bytes: 7_000_000 },
    retention: { operations: 2, freed_bytes: 580_400_000 } }),
  ["The last update finished.", "Removed 2 old images (7 MB).", "Removed the backups of 2 older updates (580 MB)."]);
  assert.deepEqual(paragraphs({ phase: "succeeded", retention: { operations: 1, freed_bytes: 290_000_000 } }),
    ["The last update finished.", "Removed the backups of 1 older update (290 MB)."]);
  assert.deepEqual(paragraphs({ phase: "succeeded", cleanup: { removed: 9, freed_bytes: 1_250_000_000 },
    retention: { operations: 46, freed_bytes: 9_977_176_064 } }),
  ["The last update finished.", "Removed 9 old images (1.3 GB).", "Removed the backups of 46 older updates (10.0 GB)."]);
  assert.deepEqual(paragraphs({ phase: "succeeded", retention: { operations: 1, freed_bytes: 999_400_000 } }),
    ["The last update finished.", "Removed the backups of 1 older update (999 MB)."]);
  for (const retention of [null, { operations: 0, freed_bytes: 0 }, { operations: "2" }]) {
    assert.deepEqual(paragraphs({ phase: "succeeded", retention }), ["The last update finished."]);
  }
  assert.deepEqual(paragraphs({ phase: "failed", retention: { operations: 2, freed_bytes: 1 } }).slice(1), [],
    "only after a successful update");
  const review = renderUpdates(view, availableUpdates, { ...pendingReview, review: { ...pendingReview.review,
    retention: "Kept for this and the previous update; older ones are removed after success." } });
  assert.match(review.allText(), /Old backups.*Kept for this and the previous update; older ones are removed after success\./);
});

test("the page reloads once when the web application comes back as a new build", () => {
  const view = renderStatusView({ configured: true, authenticated: false, locked_for_seconds: 0 });
  const reloads = [];
  const stored = new Map();
  view.context.window = {
    location: { reload: () => reloads.push("reload") },
    sessionStorage: { getItem: (key) => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value) },
  };
  const check = (build) => view.context.reloadForNewBuild(build);
  const before = { revision: "a".repeat(40), built_at: "2026-11-22T16:14:17Z" };
  assert.equal(check(before), false, "the first build seen is the page's own");
  assert.equal(check(before), false);
  assert.equal(check({ revision: null, built_at: null }), false, "no identity never reloads");
  assert.deepEqual(reloads, []);
  const after = { revision: "b".repeat(40), built_at: "2026-11-22T18:00:00Z" };
  assert.equal(check(after), true);
  assert.equal(check({ revision: "c".repeat(40), built_at: null }), true, "a later build may reload again");
  assert.equal(reloads.length, 2);
  assert.equal(stored.get("russoundReloadedForBuild"), "c".repeat(40) + "@");
  assert.equal(check(after), true, "a restore back to an earlier build reloads too");
  assert.equal(check(after), false, "but only once for the same build");
  assert.equal(reloads.length, 3);
});

test("a reload loop is impossible even when the reloaded page is still the old one", () => {
  const view = renderStatusView({ configured: true, authenticated: false, locked_for_seconds: 0 });
  const reloads = [];
  const stored = new Map([["russoundReloadedForBuild", "b".repeat(40) + "@x"]]);
  view.context.window = {
    location: { reload: () => reloads.push("reload") },
    sessionStorage: { getItem: (key) => stored.get(key) ?? null, setItem: (key, value) => stored.set(key, value) },
  };
  view.context.reloadForNewBuild({ revision: "a".repeat(40), built_at: "old" });
  assert.equal(view.context.reloadForNewBuild({ revision: "b".repeat(40), built_at: "x" }), false,
    "already reloaded for this build in this tab");
  const blocked = renderStatusView({ configured: true, authenticated: false, locked_for_seconds: 0 });
  blocked.context.window = { location: { reload: () => reloads.push("blocked") },
    sessionStorage: { getItem: () => { throw new Error("denied"); } } };
  blocked.context.reloadForNewBuild({ revision: "a".repeat(40), built_at: "1" });
  assert.equal(blocked.context.reloadForNewBuild({ revision: "b".repeat(40), built_at: "2" }), true);
  assert.equal(blocked.context.reloadForNewBuild({ revision: "c".repeat(40), built_at: "3" }), true);
  assert.equal(blocked.context.reloadForNewBuild({ revision: "c".repeat(40), built_at: "3" }), false);
  assert.deepEqual(reloads, ["blocked", "blocked"]);
});

// Pi system updates: counts for everyone, packages and the actions for the administrator.
const systemCheck = { state: "ready", checked_at: new Date().toISOString(), index_updated_at: new Date().toISOString(),
  index_stale: false, upgrades: 4, new: 1, removals: 1, security: 1, restart_needed: false, error: null };
const systemDetails = { ...systemCheck, retry_after_seconds: 0, removed: ["pcmanfm"], restart_reasons: [],
  automatic: { at: new Date(Date.now() - 2 * 3600000).toISOString(), result: "installed", packages: ["openssh-server"] },
  packages: [{ name: "libc6", from: "1", to: "2", security: false },
    { name: '<img src=x onerror="1">', from: "1", to: "2", security: true }] };
const systemReview = { configured: true, phase: "awaiting_approval", kind: "install", busy: true, check: systemDetails,
  review: { operation: "sys-1", digest: "sha256:def", action: "install", expires_in_seconds: 100, upgrades: 4, new: 1,
    removals: 1, security: 1, removed: ["pcmanfm"], reasons: [], index_updated_at: new Date().toISOString(),
    index_stale: false, docker: true, restart_after: "A restart is needed afterwards (libc6).",
    interruption: "Takes several minutes.", undo: "There is no undo.", config: "Configuration files you changed are kept." } };

function renderSystem(view, status, csrf = "t") {
  vm.runInContext(`adminCsrfToken = ${JSON.stringify(csrf)}; systemUpdateState = this.systemStatus;`,
    Object.assign(view.context, { systemStatus: status }));
  view.context.renderSystemUpdates();
  return view.systemBox;
}

test("pi system: everyone sees the counts, the administrator the packages and the automatic runs", () => {
  const view = renderStatusView(guest);
  const ran = [];
  vm.runInContext("runSystemAction = (action, body) => this.ran.push([action, body]);", Object.assign(view.context, { ran }));
  const anonymous = renderSystem(view, { configured: true, phase: "idle", busy: false, check: systemCheck }, null);
  assert.equal(view.systemPanel.hidden, false);
  assert.match(anonymous.allText(), /Pending 4 updates \(1 security\), 1 new package, 1 removed/);
  assert.equal(view.systemState.textContent, "5 updates");
  assert.deepEqual(buttons(anonymous), ["Check now", "Install system updates"]);
  assert.doesNotMatch(anonymous.allText(), /Show 2 packages|Security updates/, "details need the login");
  button(anonymous, "Install system updates").fire("click");
  assert.equal(view.login.at(-1).reason, "Install the system updates", "a guest signs in first");

  const admin = renderSystem(view, { configured: true, phase: "idle", busy: false, check: systemDetails });
  assert.match(admin.allText(), /Security updates Installed automatically every day\. Last run 2 h ago: installed openssh-server/);
  assert.match(admin.allText(), /Show 2 packages/);
  const first = admin.find((node) => node.tagName === "LI");
  assert.equal(first.textContent, '<img src=x onerror="1"> 1 → 2 (security)', "security first, always as text");
  assert.equal(admin.find((node) => node.tagName === "IMG"), null);
  button(admin, "Install system updates").fire("click");
  assert.deepEqual(plain(ran.at(-1)), ["review", { kind: "install" }]);
});

test("pi system: the package list stays open and scrolled across refreshes", () => {
  const view = renderStatusView(signedIn);
  const status = { configured: true, phase: "idle", busy: false, check: systemDetails };
  const first = renderSystem(view, status).find((node) => node.tagName === "DETAILS");
  first.open = true;
  first.list.scrollTop = 240;
  const again = renderSystem(view, status).find((node) => node.tagName === "DETAILS");
  assert.notEqual(again, first, "rebuilt on refresh");
  assert.equal(again.open, true);
  assert.equal(again.list.scrollTop, 240);
  renderSystem(view, systemReview);
  const after = renderSystem(view, status).find((node) => node.tagName === "DETAILS");
  assert.equal(after.open, false, "a review in between starts the list closed again");
});

test("pi system: restart needed, up to date and installations without the updater", () => {
  const view = renderStatusView(signedIn);
  const ran = [];
  vm.runInContext("runSystemAction = (action, body) => this.ran.push([action, body]);", Object.assign(view.context, { ran }));
  const restart = renderSystem(view, { configured: true, phase: "installed", busy: false, result: { kept_configs: ["/etc/a.conf"] },
    check: { ...systemDetails, upgrades: 0, new: 0, removals: 0, packages: [], restart_needed: true,
      restart_reasons: ["updated since the last start: linux-image-rpi-v8"] } });
  assert.equal(view.systemState.textContent, "Restart needed");
  assert.match(restart.allText(), /Restart Needed: updated since the last start: linux-image-rpi-v8/);
  assert.match(restart.allText(), /Kept your configuration files: \/etc\/a\.conf\./);
  assert.deepEqual(buttons(restart), ["Check now", "Restart the Pi"]);
  button(restart, "Restart the Pi").fire("click");
  assert.deepEqual(plain(ran.at(-1)), ["review", { kind: "restart" }]);
  renderSystem(view, { configured: true, phase: "idle", busy: false, check: { ...systemDetails, upgrades: 0, new: 0, removals: 0 } });
  assert.equal(view.systemState.textContent, "Up to date");
  renderSystem(view, { configured: false, phase: "idle" });
  assert.equal(view.systemPanel.hidden, true);
});

test("pi system: the review keeps typed text, confirms with the exact word and Enter", async () => {
  const view = renderStatusView(signedIn);
  const ran = [];
  vm.runInContext("runSystemAction = (action, body) => this.ran.push([action, body]);", Object.assign(view.context, { ran }));
  const box = renderSystem(view, systemReview);
  assert.match(box.allText(), /Review: install 5 system updates/);
  assert.match(box.allText(), /Docker Docker is updated: the web app and gateway restart once\./);
  assert.match(box.allText(), /Restart A restart is needed afterwards \(libc6\)\./);
  assert.match(box.allText(), /Undo There is no undo\./);
  assert.match(box.allText(), /Removed pcmanfm/);
  const input = box.find((node) => node.tagName === "INPUT");
  input.value = "install";
  input.fire("input");
  renderSystem(view, { ...systemReview, review: { ...systemReview.review, expires_in_seconds: 80 } });
  assert.equal(view.systemBox.find((node) => node.tagName === "INPUT"), input, "same card, typed text kept");
  assert.match(view.systemBox.allText(), /80 s/);
  input.fire("keydown", { key: "Enter", preventDefault: () => {} });
  assert.deepEqual(plain(ran.at(-1)), ["approve", { operation: "sys-1", digest: "sha256:def" }]);
  input.fire("keydown", { key: "Enter", preventDefault: () => {} });
  button(view.systemBox, "Confirm install").fire("click");
  assert.equal(ran.filter((entry) => entry[0] === "approve").length, 1, "one approval per review, also on a second Enter or click");
  assert.equal(input.disabled, true);
  assert.equal(button(view.systemBox, "Cancel").disabled, true);

  const restartBox = renderSystem(view, { ...systemReview, kind: "restart",
    review: { ...systemReview.review, operation: "sys-2", action: "restart", reasons: ["updated since the last start: libc6"],
      interruption: "Playback stops for about two minutes." } });
  assert.match(restartBox.allText(), /Review: restart the Pi/);
  assert.match(restartBox.allText(), /Reason updated since the last start: libc6/);
  const word = restartBox.find((node) => node.tagName === "INPUT");
  assert.equal(word.placeholder, 'Type "restart" to confirm');
  word.value = "install";
  word.fire("input");
  assert.equal(button(restartBox, "Confirm restart").disabled, true);

  // A refused approval enables the row again, so the administrator can retry or cancel.
  vm.runInContext("runSystemAction = async (action, body) => { this.ran.push([action, body]); return false; };",
    Object.assign(view.context, { ran }));
  word.value = "restart";
  word.fire("input");
  button(restartBox, "Confirm restart").fire("click");
  assert.equal(word.disabled, true, "disabled while the request runs");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(word.disabled, false);
  assert.equal(button(restartBox, "Confirm restart").disabled, false);
});

