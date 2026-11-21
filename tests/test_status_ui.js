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

function render(clients, events) {
  const elements = {
    clientCount: new FakeElement("span"),
    clientsList: new FakeElement("div"),
    clientsEmpty: new FakeElement("p"),
    eventsEmpty: new FakeElement("p"),
    eventsTableBody: new FakeElement("tbody"),
  };
  const context = {
    ...elements,
    expandedPayloadRows: new Set(),
    document: { createElement: (tagName) => new FakeElement(tagName) },
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

test("truncated payloads keep the expand toggle", () => {
  const elements = {
    clientCount: new FakeElement("span"),
    clientsList: new FakeElement("div"),
    clientsEmpty: new FakeElement("p"),
    eventsEmpty: new FakeElement("p"),
    eventsTableBody: new FakeElement("tbody"),
  };
  const context = {
    ...elements,
    expandedPayloadRows: new Set(),
    document: {
      createElement: (tagName) => {
        const element = new FakeElement(tagName);
        if (tagName === "code") element.scrollWidth = 500;
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
      rows: [
        { label: "Memory", value: "452 MiB used of 906 MiB", level: "ok" },
        { label: "Wi-Fi link", value: "-76 dBm (weak), 270 Mbit/s, 5 GHz", level: "warning" },
        { label: "Addresses", value: "192.168.1.105/24", level: "" },
      ],
    },
  },
};

function renderHostPayload(hosts, existing) {
  const elements = existing || {
    hostList: new FakeElement("div"), hostEmpty: new FakeElement("p"), hostCount: new FakeElement("span"),
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
  const { hostList, hostCount, hostEmpty } = renderHostPayload([reference]);
  assert.equal(hostCount.textContent, "1");
  assert.equal(hostEmpty.hidden, true);
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
  const rows = details.find((node) => node.tagName === "DL").children.map((node) => node.textContent);
  assert.deepEqual(rows.slice(0, 4), ["Memory", "452 MiB used of 906 MiB", "Wi-Fi link", "-76 dBm (weak), 270 Mbit/s, 5 GHz (Warning)"]);
  assert.equal(rows.at(-1), "192.168.1.105/24");
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
  hostile.host.view.rows = [{ label: markup, value: markup, level: "problem" }];
  const { hostList } = renderHostPayload([hostile]);
  assert.equal(hostList.find((node) => node.tagName === "IMG"), null);
  assert.match(hostList.allText(), /<img src=x onerror="alert\(1\)"> \(Problem\)/);
});

test("no configured agent shows the empty message", () => {
  const { hostEmpty, hostCount, hostList } = renderHostPayload([]);
  assert.equal(hostEmpty.hidden, false);
  assert.equal(hostCount.textContent, "0");
  assert.equal(hostList.children.length, 0);
});

function renderOverviewPayload(payload, existing) {
  const elements = existing || {
    overviewList: new FakeElement("div"), overviewEmpty: new FakeElement("p"), overviewLevel: new FakeElement("span"),
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

test("an empty overview shows the placeholder and an unknown level", () => {
  const view = renderOverviewPayload({ overall: "weird", sections: [] });
  assert.equal(view.overviewEmpty.hidden, false);
  assert.equal(view.overviewLevel.textContent, "Unknown");
});
