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
