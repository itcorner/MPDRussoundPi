const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../web/static/app.js"), "utf8");

// Minimal DOM for app.js. Every mutation is counted so tests can assert that live updates leave the DOM alone.
let mutations = 0;

class TextNode {
  constructor(text) {
    this.textContent = String(text);
    this.parentNode = null;
  }
}

function matches(element, selector) {
  if (!(element instanceof Element)) return false;
  if (selector.startsWith(".")) return element.classList.contains(selector.slice(1));
  const parsed = /^([a-z]*)((?:\[[^\]]+\])*)$/.exec(selector);
  if (!parsed) throw new Error(`Unsupported selector ${selector}`);
  if (parsed[1] && element.tagName !== parsed[1]) return false;
  for (const [, name, value] of parsed[2].matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
    if (!(name in element.attributes)) return false;
    if (value !== undefined && element.attributes[name] !== value) return false;
  }
  return true;
}

class ClassList {
  constructor(element) {
    this.element = element;
  }

  get values() {
    return this.element.className.split(" ").filter(Boolean);
  }

  contains(name) {
    return this.values.includes(name);
  }

  toggle(name, force) {
    const present = this.contains(name);
    const wanted = force === undefined ? !present : Boolean(force);
    if (wanted !== present) {
      this.element.className = wanted ? [...this.values, name].join(" ") : this.values.filter((value) => value !== name).join(" ");
    }
    return wanted;
  }
}

class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.parentNode = null;
    this.attributes = {};
    this.listeners = {};
    this._className = "";
    this._text = "";
    this._value = "";
    this._disabled = false;
    this.hidden = false;
    this.title = "";
    this.classList = new ClassList(this);
  }

  set innerHTML(_) {
    throw new Error("Rooms page must build DOM nodes instead of parsing HTML.");
  }

  get className() {
    return this._className;
  }

  set className(value) {
    mutations += 1;
    this._className = value;
  }

  get disabled() {
    return this._disabled;
  }

  set disabled(value) {
    mutations += 1;
    this._disabled = Boolean(value);
  }

  get id() {
    return this.attributes.id;
  }

  get dataset() {
    const element = this;
    return new Proxy({}, {
      get: (_, key) => element.attributes[`data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`],
      set: (_, key, value) => {
        element.setAttribute(`data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`, String(value));
        return true;
      },
    });
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  set textContent(value) {
    mutations += 1;
    this.children = [];
    this._text = String(value);
  }

  get firstChild() {
    return this.children[0] || null;
  }

  get lastChild() {
    return this.children[this.children.length - 1] || null;
  }

  get nextSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.children;
    return siblings[siblings.indexOf(this) + 1] || null;
  }

  setAttribute(name, value) {
    mutations += 1;
    this.attributes[name] = String(value);
    if (name === "class") this._className = String(value);
    if (name === "value") this._value = String(value);
  }

  append(...items) {
    for (const item of items) this.insertBefore(typeof item === "string" ? new TextNode(item) : item, null);
  }

  appendChild(item) {
    this.insertBefore(item, null);
    return item;
  }

  insertBefore(item, reference) {
    mutations += 1;
    if (item.parentNode) item.parentNode.children.splice(item.parentNode.children.indexOf(item), 1);
    const index = reference ? this.children.indexOf(reference) : this.children.length;
    this.children.splice(index < 0 ? this.children.length : index, 0, item);
    item.parentNode = this;
    return item;
  }

  replaceChildren(...items) {
    mutations += 1;
    for (const child of this.children) child.parentNode = null;
    this.children = [];
    this.append(...items);
  }

  remove() {
    if (!this.parentNode) return;
    mutations += 1;
    this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
    this.parentNode = null;
  }

  querySelectorAll(selector) {
    return this.children.flatMap((child) => (child instanceof Element
      ? [...(matches(child, selector) ? [child] : []), ...child.querySelectorAll(selector)]
      : []));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  closest(selector) {
    for (let node = this; node instanceof Element; node = node.parentNode) {
      if (matches(node, selector)) return node;
    }
    return null;
  }

  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }

  dispatch(type) {
    const event = { type, target: this };
    for (let node = this; node; node = node.parentNode) {
      for (const handler of node.listeners?.[type] || []) handler(event);
    }
  }

  // Form control behaviour for <select> and <input>.
  get options() {
    return this.children.filter((child) => child.tagName === "option");
  }

  get selectedIndex() {
    return this.options.findIndex((option) => option.selected);
  }

  set selectedIndex(index) {
    mutations += 1;
    this.options.forEach((option, optionIndex) => {
      option.selected = optionIndex === index;
    });
  }

  get value() {
    if (this.tagName === "select") return this.options.find((option) => option.selected)?.value ?? "";
    return this._value;
  }

  set value(value) {
    mutations += 1;
    if (this.tagName === "select") {
      this.options.forEach((option) => {
        option.selected = option.value === String(value);
      });
      return;
    }
    this._value = String(value);
  }
}

function Option(text, value) {
  const option = new Element("option");
  option._text = text;
  option.value = value;
  option.selected = false;
  return option;
}

function zone(controller, number, extra = {}) {
  return { controller, zone: number, name: `Zone ${number}`, power: true, source: 1, volume: 20, bass: 0, ...extra };
}

function payload({ zones = [zone(1, 1), zone(1, 2)], revision, epoch = "epoch-1", inputs, backend, shortcuts } = {}) {
  return {
    ...(revision === undefined ? {} : { revision, epoch }),
    config_required: false,
    backend_status: backend || { connected: true, message: "" },
    config: {
      shortcuts: shortcuts || [{ id: "radio", name: "Radio", source: 2 }],
    },
    resolved_inputs: inputs || [
      { id: 1, name: "Tuner", display_name: "Tuner", enabled: true },
      { id: 2, name: "Pi", display_name: "Kitchen radio", enabled: true, playback_status: "ok" },
    ],
    state: { system_power: true, zones },
  };
}

const settle = async () => {
  for (let i = 0; i < 8; i += 1) await new Promise((resolve) => setImmediate(resolve));
};

async function harness(initial = payload({ revision: 1 })) {
  mutations = 0;
  const appShell = new Element("main");
  appShell.className = "app-shell";
  const elements = {};
  for (const [id, tagName] of [["zones", "section"], ["systemPower", "button"], ["shortcuts", "div"],
    ["configHint", "p"], ["shortcutsHeading", "h2"]]) {
    elements[id] = new Element(tagName);
    elements[id].setAttribute("id", id);
  }
  elements.shortcuts.append(elements.systemPower);
  appShell.append(elements.shortcuts, elements.zones);
  const requests = [];
  const responses = [];
  let stateResponse = initial;
  const document = {
    hidden: false,
    activeElement: null,
    getElementById: (id) => elements[id],
    querySelector: (selector) => (selector === "main.app-shell" ? appShell : null),
    createElement: (tagName) => new Element(tagName),
    addEventListener() {},
  };
  const streams = [];
  class EventSource {
    static CLOSED = 2;

    constructor(url) {
      this.url = url;
      this.listeners = {};
      streams.push(this);
    }

    addEventListener(type, handler) {
      this.listeners[type] = handler;
    }

    emit(type, data) {
      this.listeners[type]({ data: data === undefined ? undefined : JSON.stringify(data) });
    }

    close() {}
  }
  const context = {
    console,
    document,
    EventSource,
    Option,
    JSON,
    Number,
    String,
    Boolean,
    Array,
    Object,
    Map,
    Set,
    window: {
      EventSource,
      location: { replace(url) { context.redirect = url; } },
      setTimeout: (handler) => setImmediate(handler),
      clearTimeout() {},
      setInterval: () => 1,
      clearInterval() {},
    },
    fetch: async (url, options = {}) => {
      requests.push({ url, method: options.method || "GET", body: options.body && JSON.parse(options.body) });
      const body = responses.length ? responses.shift() : stateResponse;
      return { ok: true, status: 200, json: async () => body };
    },
  };
  vm.createContext(context);
  vm.runInContext(source, context);
  await settle();
  const cards = () => elements.zones.children;
  const card = (index) => {
    const element = cards()[index];
    return {
      element,
      name: element.querySelector(".zone-name"),
      power: element.querySelector('button[data-action="power"]'),
      select: element.querySelector('select[data-action="source"]'),
      volume: element.querySelector('input[data-action="volume"]'),
      volumeLabel: element.querySelector("[data-value-output]"),
    };
  };
  return {
    context,
    elements,
    appShell,
    requests,
    stream: () => streams[0],
    cards,
    card,
    apply: (body) => context.applyPayload(body),
    queueResponse: (body) => responses.push(body),
    setStateResponse: (body) => { stateResponse = body; },
    mutationCount: () => mutations,
  };
}

test("rendering the same state again keeps every element and does not touch the DOM", async () => {
  const ui = await harness();
  assert.equal(ui.cards().length, 2);
  const before = ui.card(0);
  assert.equal(before.select.value, "1");
  assert.equal(before.volume.value, "20");
  assert.equal(before.volumeLabel.textContent, "20%");
  assert.deepEqual(before.select.options.map((option) => option.textContent), ["Tuner", "Kitchen radio"]);
  const options = before.select.options;
  const shortcut = ui.elements.shortcuts.children[0];

  const count = ui.mutationCount();
  ui.apply(payload({ revision: 2 }));
  assert.equal(ui.mutationCount(), count);

  const after = ui.card(0);
  assert.equal(after.element, before.element);
  assert.equal(after.select, before.select);
  assert.equal(after.volume, before.volume);
  assert.equal(after.power, before.power);
  assert.deepEqual(after.select.options, options);
  assert.equal(ui.elements.shortcuts.children[0], shortcut);
  assert.equal(ui.elements.shortcuts.lastChild, ui.elements.systemPower);
});

test("fields the Rooms page does not show cause no DOM work", async () => {
  const ui = await harness();
  const count = ui.mutationCount();
  const changed = payload({ revision: 2, zones: [zone(1, 1, { bass: 7, treble: 3, loudness: true }), zone(1, 2)] });
  changed.config.notes = ["documentation only"];
  ui.apply(changed);
  assert.equal(ui.mutationCount(), count);
});

test("an update for another zone keeps this zone's controls and updates only the changed one", async () => {
  const ui = await harness();
  const first = ui.card(0);
  const second = ui.card(1);
  ui.apply(payload({ revision: 2, zones: [zone(1, 1), zone(1, 2, { volume: 64, source: 2, power: false })] }));
  assert.equal(ui.card(0).select, first.select);
  assert.equal(ui.card(1).select, second.select);
  assert.equal(second.volume.value, "64");
  assert.equal(second.volumeLabel.textContent, "64%");
  assert.equal(second.select.value, "2");
  assert.equal(second.volume.disabled, true);
  assert.equal(second.power.textContent, "Off");
  assert.ok(second.power.classList.contains("is-off"));
});

test("an open source dropdown is not rebuilt or overwritten until the user is done", async () => {
  const ui = await harness();
  const { select } = ui.card(0);
  const options = select.options;
  select.dispatch("pointerdown");

  const renamed = [
    { id: 1, name: "Tuner", display_name: "FM", enabled: true },
    { id: 2, name: "Pi", display_name: "Kitchen radio", enabled: true },
  ];
  ui.apply(payload({ revision: 2, inputs: renamed, zones: [zone(1, 1, { source: 2, volume: 30 }), zone(1, 2)] }));
  assert.deepEqual(select.options, options);
  assert.equal(select.value, "1");
  assert.equal(select.options[0].textContent, "Tuner");
  assert.equal(ui.card(0).volume.value, "30");

  select.dispatch("focusout");
  assert.equal(select.value, "2");
  assert.equal(select.options[0].textContent, "FM");
  assert.equal(ui.card(0).select, select);
});

test("choosing a source sends it and keeps the choice while the request is pending", async () => {
  const ui = await harness();
  const { select } = ui.card(0);
  select.dispatch("pointerdown");
  select.value = "2";
  select.dispatch("change");
  assert.deepEqual(ui.requests.at(-1), { url: "/api/controller/1/zone/1/source", method: "POST", body: { source: 2 } });
  assert.equal(select.value, "2");
  await settle();
});

test("a slider drag is not reset by live updates and its value is sent on release", async () => {
  const ui = await harness();
  const { volume, volumeLabel } = ui.card(0);
  volume.dispatch("pointerdown");
  volume.value = "70";
  volume.dispatch("input");
  assert.equal(volumeLabel.textContent, "70%");

  ui.apply(payload({ revision: 2, zones: [zone(1, 1, { volume: 30 }), zone(1, 2)] }));
  assert.equal(volume.value, "70");
  assert.equal(volumeLabel.textContent, "70%");
  assert.equal(ui.card(0).volume, volume);

  ui.queueResponse(payload({ revision: 3, zones: [zone(1, 1, { volume: 70 }), zone(1, 2)] }));
  volume.dispatch("change");
  assert.deepEqual(ui.requests.at(-1), { url: "/api/controller/1/zone/1/volume", method: "POST", body: { volume: 70 } });
  assert.equal(volume.value, "70");
  await settle();
  assert.equal(volume.value, "70");
  assert.equal(volumeLabel.textContent, "70%");
});

test("a slider click without a value change syncs again after pointerup", async () => {
  const ui = await harness();
  const { volume } = ui.card(0);
  volume.dispatch("pointerdown");
  ui.apply(payload({ revision: 2, zones: [zone(1, 1, { volume: 44 }), zone(1, 2)] }));
  assert.equal(volume.value, "20");
  volume.dispatch("pointerup");
  await settle();
  assert.equal(volume.value, "44");
});

test("power toggles keep the button element and show the new state immediately", async () => {
  const ui = await harness();
  const { power, volume } = ui.card(0);
  ui.queueResponse(payload({ revision: 2, zones: [zone(1, 1, { power: false }), zone(1, 2)] }));
  power.dispatch("click");
  assert.equal(power.textContent, "Off");
  assert.equal(volume.disabled, true);
  assert.deepEqual(ui.requests.at(-1).body, { power: false });
  await settle();
  assert.equal(ui.card(0).power, power);
  assert.equal(power.textContent, "Off");
});

test("payloads older than the last applied revision are ignored until the server restarts", async () => {
  const ui = await harness(payload({ revision: 5 }));
  const { volume } = ui.card(0);
  ui.apply(payload({ revision: 4, zones: [zone(1, 1, { volume: 90 }), zone(1, 2)] }));
  assert.equal(volume.value, "20");
  ui.apply(payload({ revision: 5, zones: [zone(1, 1, { volume: 22 }), zone(1, 2)] }));
  assert.equal(volume.value, "22");
  ui.apply(payload({ revision: 1, epoch: "epoch-2", zones: [zone(1, 1, { volume: 10 }), zone(1, 2)] }));
  assert.equal(volume.value, "10");
  ui.apply(payload({ zones: [zone(1, 1, { volume: 12 }), zone(1, 2)] }));
  assert.equal(volume.value, "12");
});

test("zones are added, removed and reordered without recreating the remaining cards", async () => {
  const ui = await harness();
  const first = ui.card(0).element;
  const second = ui.card(1).element;
  ui.apply(payload({ revision: 2, zones: [zone(1, 2), zone(2, 1), zone(1, 1)] }));
  assert.deepEqual(ui.cards().map((card) => card.querySelector(".zone-name").textContent), ["Zone 2", "Zone 1", "Zone 1"]);
  assert.equal(ui.cards()[0], second);
  assert.equal(ui.cards()[2], first);
  ui.apply(payload({ revision: 3, zones: [zone(1, 1)] }));
  assert.deepEqual(ui.cards(), [first]);
});

test("shortcut buttons are kept and only rebuilt when the shortcuts change", async () => {
  const ui = await harness();
  const [radio] = ui.elements.shortcuts.children;
  assert.equal(radio.textContent, "Radio");
  assert.equal(radio.title, "Kitchen radio");
  ui.apply(payload({ revision: 2, inputs: [{ id: 2, name: "Pi", display_name: "Pi", playback_status: "unavailable" }] }));
  assert.equal(ui.elements.shortcuts.children[0], radio);
  assert.equal(radio.title, "Pi (unavailable)");
  ui.apply(payload({ revision: 3, shortcuts: [{ id: "tv", name: "TV", source: 1 }] }));
  assert.deepEqual(ui.elements.shortcuts.children.map((button) => button.textContent), ["TV", "System off"]);
});

test("the backend warning renders the message as text and is removed when connected", async () => {
  const ui = await harness();
  const message = "<img src=x onerror=alert(1)>";
  ui.apply(payload({ revision: 2, backend: { connected: false, message } }));
  const banner = ui.appShell.firstChild;
  assert.ok(banner.classList.contains("backend-warning-banner"));
  assert.equal(banner.lastChild.textContent, message);
  ui.apply(payload({ revision: 3, backend: { connected: false, message: "Still down" } }));
  assert.equal(ui.appShell.firstChild, banner);
  assert.equal(banner.lastChild.textContent, "Still down");
  ui.apply(payload({ revision: 4 }));
  assert.equal(ui.appShell.querySelector(".backend-warning-banner"), null);
});

test("state-change events apply their payload in place", async () => {
  const ui = await harness();
  const { volume } = ui.card(0);
  ui.stream().emit("state-change", { revision: 2, payload: payload({ revision: 2, zones: [zone(1, 1, { volume: 58 }), zone(1, 2)] }) });
  assert.equal(ui.card(0).volume, volume);
  assert.equal(volume.value, "58");
});

test("playback events refresh source names as text without rebuilding cards or losing the selection", async () => {
  const ui = await harness(payload({ revision: 1, zones: [zone(1, 1, { source: 2 }), zone(1, 2)] }));
  const { element, select } = ui.card(0);
  const [shortcut] = ui.elements.shortcuts.children;
  const name = "<Station & name>";
  ui.setStateResponse(payload({
    revision: 1,
    zones: [zone(1, 1, { source: 2 }), zone(1, 2)],
    inputs: [
      { id: 1, name: "Tuner", display_name: "Tuner", enabled: true },
      { id: 2, name: "Pi", display_name: name, enabled: true, playback_status: "ok", playback_message: "Streaming" },
    ],
  }));
  ui.stream().emit("playback-change", { event: "playback-change" });
  await settle();
  assert.equal(ui.requests.at(-1).url, "/api/state");
  assert.equal(ui.card(0).element, element);
  assert.equal(ui.card(0).select, select);
  assert.equal(select.value, "2");
  assert.equal(select.options[1].textContent, name);
  assert.equal(select.options[1].title, "Streaming");
  assert.equal(shortcut.title, name);
});

test("config-required payloads redirect to the configuration page", async () => {
  const ui = await harness();
  ui.apply({ config_required: true });
  assert.equal(ui.context.redirect, "/config");
});
