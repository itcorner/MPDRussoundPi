const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../web/static/playback.js"), "utf8");

// Only the DOM surface used by playback.js; reject HTML parsing to catch unsafe rendering.
class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.value = "";
    this.className = "";
    this.textContent = "";
  }

  set innerHTML(_) {
    throw new Error("Playback must render untrusted data as text, not HTML.");
  }

  append(...items) {
    for (const item of items) {
      item.parent = this;
      this.children.push(item);
    }
  }

  appendChild(item) {
    this.append(item);
    return item;
  }

  replaceChildren(...items) {
    this.children = [];
    this.append(...items);
  }

  replaceWith(item) {
    const index = this.parent.children.indexOf(this);
    this.parent.children[index] = item;
    item.parent = this.parent;
  }

  setAttribute(key, value) {
    this.attributes[key] = value;
  }

  addEventListener(event, handler) {
    this.listeners[event] = handler;
  }

  showModal() {
    this.open = true;
    this.showCount = (this.showCount || 0) + 1;
  }

  close() {
    this.open = false;
    this.listeners.close?.();
  }

  focus() {
    this.focused = true;
  }

  querySelectorAll(selector) {
    return this.children.flatMap((child) => [
      ...(child.className.split(" ").includes(selector.slice(1)) ? [child] : []),
      ...child.querySelectorAll(selector),
    ]);
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0];
  }
}

function fixture() {
  return {
    configured: true,
    agents: [{
      agent_id: "agent/one",
      status: "ok",
      write_operations: ["play", "pause", "stop", "set_gain", "set_tunein_station"],
      snapshot: {
        config_revision: "config-1",
        outputs: [{
          id: "output/one",
          label: "Kitchen",
          control_revision: "control-1",
          selection: { source: "https://radio.example/..." },
          gain: { configured_db: -12 },
          playback: { age_ms: 0, value: { state: "stopped", current_source: "https://old.example/..." } },
        }],
      },
    }],
  };
}

function operation(status, action = "play", extra = {}) {
  return {
    api_version: 1,
    agent_id: "agent/one",
    operation: {
      id: "operation/one",
      output_id: "output/one",
      action,
      status,
      error: null,
      requested_intention_persisted: status === "applied" ? true : null,
      requested_source_persisted: status === "applied" ? true : null,
      applied: status === "applied" ? true : null,
      ...extra,
    },
  };
}

function response(status, body) {
  return { status, ok: status >= 200 && status < 300, json: async () => body };
}

const settle = async () => {
  for (let i = 0; i < 8; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
};

function harness(snapshot = fixture()) {
  const ids = Object.fromEntries(["playbackHealth", "playbackMessage", "playbackOutputs",
    "playbackConfigDialog", "playbackConfigTitle", "playbackConfigSubtitle", "playbackConfigControls",
    "playbackConfigClose"]
    .map((id) => [id, new Element("div")]));
  ids.playbackConfigDialog.append(ids.playbackConfigTitle, ids.playbackConfigControls, ids.playbackConfigClose);
  const requests = [];
  const plan = [];
  const timers = new Map();
  let timerId = 0;
  let clock = 100000;
  let snapshotFailure = false;
  let resizeTracks;
  class Clock extends Date {
    static now() { return clock; }
  }
  const context = vm.createContext({
    ResizeObserver: class {
      constructor(callback) { resizeTracks = callback; }
      observe() {}
      disconnect() {}
    },
    console, AbortController, Date: Clock, Math, JSON, Number, Set, Map, Promise, encodeURIComponent,
    crypto: { randomUUID: () => `test-key-${requests.length}` },
    document: {
      hidden: false,
      getElementById: (id) => ids[id],
      createElement: (tag) => new Element(tag),
      addEventListener() {},
    },
    window: {
      setTimeout(fn, delay) {
        timers.set(++timerId, { fn, delay });
        return timerId;
      },
      clearTimeout(id) { timers.delete(id); },
      setInterval() { return ++timerId; },
      clearInterval() {},
    },
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url === "/api/playback") {
        if (snapshotFailure) throw new Error("Snapshot connection lost");
        return response(200, JSON.parse(JSON.stringify(snapshot)));
      }
      if (url.startsWith("/api/playback/catalogs/tunein/search?")) {
        assert.ok(plan.length, "Unexpected TuneIn search request");
        const next = plan.shift();
        if (next instanceof Error) throw next;
        return typeof next === "function" ? next(options) : next;
      }
      assert.ok(plan.length, `Unexpected operation request: ${url}`);
      const next = plan.shift();
      if (next instanceof Error) throw next;
      return typeof next === "function" ? next(options) : next;
    },
  });
  vm.runInContext(source, context);
  const all = (node) => [node, ...node.children.flatMap(all)];
  const nodes = () => [...all(ids.playbackConfigDialog), ...all(ids.playbackOutputs)];
  const button = (label) => nodes().find((node) => node.tagName === "button" && node.textContent === label);
  const input = (type) => nodes().find((node) => node.tagName === "input" && node.type === type);
  return {
    snapshot, ids, requests, plan, button, input,
    resizeTracks: (entries) => resizeTracks(entries),
    open(index = 0) { ids.playbackOutputs.children[index].querySelector(".playback-config-button").listeners.click(); },
    close() { ids.playbackConfigClose.listeners.click(); },
    failSnapshot(value) { snapshotFailure = value; },
    evaluate: (code) => vm.runInContext(code, context),
    render() { vm.runInContext(`renderPlayback(${JSON.stringify(snapshot)})`, context); },
    posts: () => requests.filter((request) => request.options.method === "POST"),
    gets: () => requests.filter((request) => request.url.includes("/operations/")),
    status: () => all(ids.playbackConfigControls).find((node) => /^playback-command-(status|error)$/.test(node.className)),
    click(label) {
      const target = button(label);
      assert.ok(target, `Missing button: ${label}`);
      assert.equal(target.disabled, false, `Disabled button: ${label}`);
      target.listeners.click();
    },
    edit(type, value) {
      const target = input(type);
      assert.equal(target.disabled, false);
      target.value = value;
      target.listeners.input();
    },
    async tick(delay = 1000, advance = delay) {
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `Missing ${delay}ms timer`);
      timers.delete(entry[0]);
      clock += advance;
      entry[1].fn();
      await settle();
    },
  };
}

test("write controls require each capability and string revisions; unavailable/stale agents stay read-only", async () => {
  const h = harness();
  await settle();
  h.open();
  const agent = h.snapshot.agents[0];
  const output = agent.snapshot.outputs[0];
  for (const capabilities of [undefined, [], ["play"], ["pause", "set_gain"]]) {
    agent.write_operations = capabilities;
    h.render();
    for (const [action, label] of [["play", "Play"], ["pause", "Pause"], ["stop", "Stop"], ["set_gain", "Apply gain"]]) {
      assert.equal(h.button(label).disabled, !capabilities?.includes(action));
    }
    assert.equal(h.input("range").disabled, !capabilities?.includes("set_gain"));
  }
  agent.write_operations = ["play", "pause", "stop", "set_gain", "set_tunein_station"];
  h.render();
  for (const target of [agent.snapshot, output]) {
    const field = target === output ? "control_revision" : "config_revision";
    for (const value of [undefined, null, 123]) {
      target[field] = value;
      h.render();
      for (const label of ["Play", "Pause", "Stop", "Apply gain"]) assert.equal(h.button(label).disabled, true);
    }
    target[field] = "revision";
  }
  agent.status = "unavailable";
  h.render();
  assert.equal(h.button("Play").disabled, true);
  agent.status = "ok";
  agent.snapshot.stale = true;
  h.render();
  assert.equal(h.button("Play").disabled, true);
  assert.equal(h.posts().length, 0);
});

test("track overflow uses measured distance and stops scrolling when text fits", async () => {
  const h = harness();
  await settle();
  const properties = {};
  const line = {
    className: "playback-track", clientWidth: 200, dataset: {},
    style: { setProperty: (name, value) => { properties[name] = value; } },
    firstElementChild: { scrollWidth: 350 },
  };
  h.resizeTracks([{ target: line }]);
  assert.equal(line.dataset.overflow, "true");
  assert.equal(properties["--track-offset"], "-150px");
  assert.equal(properties["--track-duration"], "10s");
  line.clientWidth = 400;
  h.resizeTracks([{ target: line }]);
  assert.equal(line.dataset.overflow, "false");
  assert.equal(properties["--track-offset"], "0px");
  line.firstElementChild.scrollWidth = 500;
  h.resizeTracks([{ target: { parentElement: line } }]);
  assert.equal(line.dataset.overflow, "true");
  assert.equal(properties["--track-offset"], "-100px");
});

test("gain movement is draft-only, latest draft survives semantic rerenders, and values below −60 are not clamped", async () => {
  const h = harness();
  await settle();
  h.open();
  assert.equal(h.input("range").min, "-60");
  assert.equal(h.input("range").max, "0");
  assert.equal(h.input("range").step, "0.1");
  assert.equal(h.input("number"), undefined);
  assert.equal(h.input("text"), undefined);
  h.edit("range", "-18.4");
  h.edit("range", "-19.2");
  assert.equal(h.input("range").value, "-19.2");
  assert.equal(h.posts().length, 0);
  const slider = h.input("range");
  const controls = h.ids.playbackConfigControls.children[0];
  h.snapshot.agents[0].snapshot.outputs[0].label = "Changed label";
  h.snapshot.agents[0].snapshot.config_revision = "config-2";
  h.render();
  assert.equal(h.input("range"), slider);
  assert.equal(h.ids.playbackConfigControls.children[0], controls);
  assert.equal(slider.value, "-19.2");
  assert.equal(h.ids.playbackConfigDialog.open, true);
  assert.equal(h.ids.playbackConfigDialog.showCount, 1);
  h.plan.push(response(202, operation("applied", "set_gain")));
  h.click("Apply gain");
  await settle();
  const body = JSON.parse(h.posts()[0].options.body);
  assert.equal(body.gain_db, -19.2);
  assert.equal(body.expected_config_revision, "config-2");
});

test("each explicit action submits the contract and locks only its output until confirmed terminal status", async () => {
  for (const action of ["play", "pause", "stop", "set_gain"]) {
    const snapshot = fixture();
    snapshot.agents[0].snapshot.outputs.push({
      ...snapshot.agents[0].snapshot.outputs[0], id: "other", label: "Other",
    });

    const h = harness(snapshot);
    await settle();
    h.open();
    h.plan.push(response(202, operation("queued", action)), response(200, operation("running", action)),
      response(200, operation("applied", action)));
    h.click(action === "set_gain" ? "Apply gain" : action[0].toUpperCase() + action.slice(1));
    assert.equal(h.button("Play").disabled, true);
    assert.equal(h.input("range").disabled, true);
    assert.equal(h.ids.playbackOutputs.children[1].querySelector(".playback-config-button").disabled, false);
    await settle();
    assert.equal(h.posts().length, 1);
    const request = h.posts()[0];
    const body = JSON.parse(request.options.body);
    assert.equal(request.url, "/api/playback/agents/agent%2Fone/outputs/output%2Fone/operations");
    assert.equal(request.options.credentials, "same-origin");
    assert.equal(request.options.headers["Content-Type"], "application/json");
    assert.deepEqual(Object.keys(body).sort(), [
      "action", "expected_config_revision", "expected_control_revision", "idempotency_key",
      ...(action === "set_gain" ? ["gain_db"] : []),
    ].sort());
    assert.equal(body.action, action);
    assert.equal(body.expected_config_revision, "config-1");
    assert.equal(body.expected_control_revision, "control-1");
    assert.equal(typeof body.idempotency_key, "string");
    assert.ok(body.idempotency_key.length);
    assert.match(h.status().textContent, /running/);
    assert.doesNotMatch(h.status().textContent, /Command confirmed/);
    h.open(1);
    assert.equal(h.button("Play").disabled, false);
    assert.equal(h.input("range").disabled, false);
    h.open(0);
    assert.equal(h.button("Play").disabled, true);
    assert.match(h.status().textContent, /running/);
    const snapshotsBefore = h.requests.filter((item) => item.url === "/api/playback").length;
    await h.tick();
    assert.match(h.status().textContent, /Command confirmed.*applied/);
    assert.equal(h.button("Play").disabled, false);
    assert.equal(h.requests.filter((item) => item.url === "/api/playback").length, snapshotsBefore + 1);
    assert.equal(h.gets()[0].url, "/api/playback/agents/agent%2Fone/operations/operation%2Fone");
  }
});

test("TuneIn search returns selectable stations and submits only station identity", async () => {
  const h = harness();
  await settle();
  h.open();
  const agent = h.snapshot.agents[0];
  h.edit("search", "BBC");
  assert.equal(h.posts().length, 0);
  h.plan.push(response(200, {
    api_version: 1,
    provider: "tunein",
    stations: [{
      id: "s231065",
      name: "Radio BBC",
      description: "News",
      formats: "aac",
      bitrate_kbps: 128,
    }],
  }));
  let prevented = false;
  h.input("search").listeners.keydown({ key: "Enter", isComposing: false, preventDefault() { prevented = true; } });
  await settle();
  assert.equal(prevented, true);
  assert.equal(h.requests.some((request) => request.url === "/api/playback/catalogs/tunein/search?q=BBC"), true);
  agent.write_operations = ["play", "pause", "stop", "set_gain"];
  h.render();
  assert.equal(h.button("Select and play").disabled, true);
  agent.write_operations.push("set_tunein_station");
  h.render();
  assert.equal(h.button("Select and play").disabled, false);
  h.plan.push(response(202, operation("queued", "set_tunein_station")),
    response(200, operation("applied", "set_tunein_station")));
  h.click("Select and play");
  await settle();
  assert.equal(h.posts().length, 1);
  const body = JSON.parse(h.posts()[0].options.body);
  assert.equal(body.action, "set_tunein_station");
  assert.equal(body.station_id, "s231065");
  assert.equal(body.station_name, "Radio BBC");
  assert.equal("source_url" in body, false);
  assert.match(h.status().textContent, /Desired station persisted: yes/);
  const cardText = h.ids.playbackOutputs.children[0].children.map((child) => child.textContent).join(" ");
  assert.doesNotMatch(cardText, /secret-value|radio\.example|old\.example|MPD source/);
  assert.ok(!h.ids.playbackOutputs.children[0].querySelector(".playback-logo"));
  agent.snapshot.outputs[0].selection = {
    provider: "tunein", station_id: "s231065", station_name: "Radio BBC", source: "https://radio.example/...",
  };
  agent.snapshot.outputs[0].playback.value.now_playing = { station: "icy-name", title: "Song" };
  h.render();
  const card = h.ids.playbackOutputs.children[0];
  assert.equal(card.querySelector(".playback-state").textContent, "stopped · Radio BBC");
  assert.equal(card.querySelector(".playback-logo").src, "https://cdn-profiles.tunein.com/s231065/images/logoq.png");
  agent.snapshot.outputs[0].selection = { source: "https://radio.example/..." };
  h.render();
  assert.equal(h.ids.playbackOutputs.children[0].querySelector(".playback-state").textContent, "stopped · icy-name");
});

test("lost submission exposes unknown outcome and manual retry reuses the identical body and key", async () => {
  const h = harness();
  await settle();
  h.open();
  h.plan.push(new Error("Connection lost"));
  h.click("Play");
  await settle();
  assert.match(h.status().textContent, /outcome unknown.*Connection lost/);
  assert.equal(h.status().attributes.role, "alert");
  assert.equal(h.button("Pause").disabled, true);
  const first = h.posts()[0];
  h.render();
  assert.equal(h.posts().length, 1);
  h.plan.push(response(202, operation("applied")));
  h.click("Retry identical submission");
  await settle();
  assert.equal(h.posts()[1].options.body, first.options.body);
  assert.equal(h.posts()[1].url, first.url);
  assert.equal(h.button("Pause").disabled, false);
});

test("stopped and paused gain success says saved for next play, not live mixer application", async () => {
  for (const state of ["stopped", "paused"]) {
    const snapshot = fixture();
    snapshot.agents[0].snapshot.outputs[0].playback.value.state = state;
    snapshot.agents[0].snapshot.outputs[0].gain.configured_db = -75;
    const h = harness(snapshot);
    await settle();
    h.open();
    h.edit("range", "-72.5");
    h.plan.push(response(202, operation("applied", "set_gain", { gain_application: "on_next_play" })));
    h.click("Apply gain");
    await settle();
    assert.match(h.status().textContent, /Calibration saved; applies on next play/);
    assert.match(h.status().textContent, /not applied to the live mixer/);
    assert.doesNotMatch(h.status().textContent, /Applied: yes/);
    assert.equal(h.posts().length, 1);
    assert.equal(JSON.parse(h.posts()[0].options.body).gain_db, -72.5);
    assert.equal(snapshot.agents[0].snapshot.outputs[0].playback.value.state, state);
  }
});

test("all terminal states refresh snapshots; failures are explicit safe text, unknown stays locked", async () => {
  for (const status of ["failed", "expired", "superseded", "outcome_unknown"]) {
    const h = harness();
    await settle();
    h.open();
    const malicious = '<script>alert("unsafe")</script>';
    h.plan.push(response(202, operation(status, "play", {
      error: { code: "command_failed", message: malicious, retryable: false },
      applied: false, requested_intention_persisted: false,
    })));
    h.click("Play");
    await settle();
    assert.equal(h.status().className, "playback-command-error");
    assert.equal(h.status().attributes.role, "alert");
    assert.ok(h.status().textContent.includes(malicious));
    assert.ok(h.status().textContent.includes(status));
    assert.match(h.status().textContent, /command_failed.*retryable: no/);
    assert.equal(h.button("Play").disabled, status === "outcome_unknown");
    assert.equal(h.requests.filter((item) => item.url === "/api/playback").length, 2);
    if (status === "outcome_unknown") {
      h.plan.push(response(200, operation("applied")));
      h.click("Check operation status");
      await settle();
      assert.equal(h.button("Play").disabled, false);
      assert.equal(h.posts().length, 1);
    }
  }
});

test("rejected submissions show errors; server errors, malformed responses and timeouts are unknown, never auto-resubmitted", async () => {
  for (const result of [
    response(409, { error: { code: "revision_conflict", message: "<b>Conflict</b>" } }),
    response(503, { error: { message: "Agent unavailable" } }),
    response(202, { api_version: 1, operation: { id: "bad" } }),
    (options) => new Promise((_, reject) => options.signal.addEventListener("abort", () => reject(new Error("Request timed out")))),
  ]) {
    const h = harness();
    await settle();
    h.open();
    h.plan.push(result);
    h.click("Play");
    await settle();
    if (typeof result === "function") await h.tick(10000);
    assert.equal(h.status().className, "playback-command-error");
    assert.equal(h.posts().length, 1);
    if (result.status === 409) {
      assert.match(h.status().textContent, /Command rejected.*409.*<b>Conflict<\/b>.*revision_conflict/);
      assert.equal(h.button("Play").disabled, false);
    } else {
      assert.match(h.status().textContent, /outcome unknown/);
      assert.equal(h.button("Play").disabled, true);
      assert.ok(h.button("Retry identical submission"));
    }
  }
});

test("polling failures are visible and bounded by attempts, with manual status-only recovery", async () => {
  const h = harness();
  await settle();
  h.open();
  h.plan.push(response(202, operation("queued")),
    ...Array.from({ length: 60 }, () => response(503, {})));
  h.click("Play");
  await settle();
  assert.match(h.status().textContent, /status check failed.*503/);
  assert.equal(h.button("Play").disabled, true);
  for (let i = 0; i < 60; i += 1) await h.tick();
  assert.equal(h.gets().length, 60);
  assert.equal(h.plan.length, 0);
  assert.match(h.status().textContent, /outcome unknown.*polling reached/);
  assert.equal(h.posts().length, 1);
  delete h.snapshot.agents[0].write_operations;
  h.render();
  assert.equal(h.button("Check operation status").disabled, false);
  h.plan.push(response(200, operation("applied")));
  h.click("Check operation status");
  await settle();
  assert.match(h.status().textContent, /Command confirmed/);
  assert.equal(h.posts().length, 1);
});

test("queued operation polling also stops at its time deadline without another command", async () => {
  const h = harness();
  await settle();
  h.open();
  h.plan.push(response(202, operation("queued")), response(200, operation("queued")));
  h.click("Play");
  await settle();
  await h.tick(1000, 90000);
  assert.equal(h.gets().length, 1);
  assert.match(h.status().textContent, /outcome unknown.*time or attempt limit/);
  assert.equal(h.button("Play").disabled, true);
  assert.equal(h.posts().length, 1);
});

test("freshness changes at exactly 4000ms without a semantic rerender or lost drafts", async () => {
  const h = harness();
  await settle();
  h.open();
  h.edit("range", "-51");
  const card = h.ids.playbackOutputs.children[0];
  const agent = h.snapshot.agents[0];
  for (const [age, expected] of [[3999, "Updated now"], [4000, "Updated 4s ago"]]) {
    agent.snapshot.outputs[0].playback.age_ms = age;
    await h.evaluate("fetchPlayback()");
    assert.equal(h.ids.playbackOutputs.children[0], card);
    assert.equal(card.querySelector(".playback-freshness").textContent, expected);
    assert.equal(h.input("range").value, "-51");
  }
  agent.status = "unavailable";
  h.render();
  assert.match(h.ids.playbackOutputs.children[0].querySelector(".playback-freshness").textContent, /Stale.*4s old/);
  agent.error = { message: "<img src=x onerror=alert(1)>" };
  h.render();
  assert.equal(h.ids.playbackMessage.children[0].children[1].textContent,
    "agent/one: <img src=x onerror=alert(1)>");
});

test("cards show only Config; native dialog opens, closes and switches output identity without sending drafts", async () => {
  const snapshot = fixture();
  snapshot.agents[0].snapshot.outputs.push({
    ...snapshot.agents[0].snapshot.outputs[0], id: "other", label: "Bedroom", gain: { configured_db: -20 },
  });
  const h = harness(snapshot);
  await settle();
  for (const card of h.ids.playbackOutputs.children) {
    assert.equal(card.querySelector(".playback-controls"), undefined);
    assert.equal(card.querySelector(".playback-transport"), undefined);
    assert.equal(card.querySelector(".playback-config-button").textContent, "Config");
    assert.equal(card.querySelector(".playback-config-button").attributes["aria-haspopup"], "dialog");
  }
  h.open();
  assert.equal(h.ids.playbackConfigDialog.open, true);
  assert.equal(h.ids.playbackConfigTitle.textContent, "Kitchen");
  assert.equal(h.ids.playbackConfigSubtitle.textContent, "agent/one · output/one");
  assert.equal(h.input("number"), undefined);
  assert.equal(h.input("text"), undefined);
  h.edit("range", "-27.3");
  h.open(1);
  assert.match(h.ids.playbackConfigTitle.textContent, /Bedroom/);
  assert.match(h.ids.playbackConfigSubtitle.textContent, /other/);
  assert.equal(h.input("range").value, "-20");
  h.edit("range", "-31.1");
  h.open(0);
  assert.equal(h.input("range").value, "-27.3");
  h.close();
  assert.equal(h.ids.playbackConfigDialog.open, false);
  assert.equal(h.ids.playbackOutputs.children[0].querySelector(".playback-config-button").focused, true);
  h.open(1);
  assert.equal(h.input("range").value, "-31.1");
  // The browser handles Escape; no cancel handler prevents native dismissal.
  assert.equal(h.ids.playbackConfigDialog.listeners.cancel, undefined);
  h.ids.playbackConfigDialog.close();
  assert.equal(h.ids.playbackConfigDialog.open, false);
  assert.equal(h.posts().length, 0);
});

test("existing low gain extends slider minimum and preserves its exact value until moved or applied", async () => {
  for (const gain of [-65.1, -65.12345]) {
    const snapshot = fixture();
    snapshot.agents[0].snapshot.outputs[0].gain.configured_db = gain;
    const h = harness(snapshot);
    await settle();
    h.open();
    const slider = h.input("range");
    assert.equal(slider.min, String(gain));
    assert.equal(slider.value, String(gain));
    assert.equal(slider.max, "0");
    assert.equal(slider.step, "0.1");
    h.snapshot.agents[0].snapshot.outputs[0].label = "Renamed";
    h.render();
    assert.equal(h.input("range"), slider);
    assert.equal(slider.value, String(gain));
    const gainDisplay = h.ids.playbackConfigControls.querySelector(".playback-gain-value");
    assert.equal(gainDisplay.tagName, "output");
    assert.equal(gainDisplay.textContent, `${gain} dB`);
    h.plan.push(response(202, operation("applied", "set_gain")));
    h.click("Apply gain");
    await settle();
    assert.equal(JSON.parse(h.posts()[0].options.body).gain_db, gain);
    h.edit("range", "-60.1");
    h.render();
    assert.equal(slider.min, String(gain));
    assert.equal(slider.value, "-60.1");
    assert.equal(gainDisplay.textContent, "-60.1 dB");
  }
});

test("missing or invalid configured gain cannot be applied until a valid slider draft is chosen", async () => {
  for (const gain of [undefined, null, 0.1, "invalid"]) {
    const snapshot = fixture();
    snapshot.agents[0].snapshot.outputs[0].gain.configured_db = gain;
    const h = harness(snapshot);
    await settle();
    h.open();
    assert.equal(h.button("Apply gain").disabled, true);
    assert.equal(h.input("range").disabled, false);
    h.edit("range", "-20.1");
    assert.equal(h.button("Apply gain").disabled, false);
    assert.equal(h.posts().length, 0);
  }
});

test("transport exposes only the observed-state Play or Pause action alongside Stop", async () => {
  const h = harness();
  await settle();
  h.open();
  for (const state of ["stop", "pause", "play", "unknown"]) {
    h.snapshot.agents[0].snapshot.outputs[0].playback.value.state = state;
    h.render();
    assert.equal(h.button("Play").hidden, state === "play");
    assert.equal(h.button("Pause").hidden, state !== "play");
    assert.equal(h.button("Stop").hidden, undefined);
    assert.equal(h.button("Stop").disabled, state === "stop");
    if (state === "stop") {
      h.button("Stop").listeners.click();
      assert.equal(h.posts().length, 0);
    }
    for (const action of ["play", "pause", "stop"]) {
      assert.equal(h.button(action[0].toUpperCase() + action.slice(1)).dataset.action, action);
    }
  }
});

test("command feedback stays in the dialog, including after closing and reopening", async () => {
  const h = harness();
  await settle();
  h.open();
  const feedback = h.ids.playbackConfigControls.querySelector(".playback-command-feedback");
  assert.equal(feedback.hidden, true);
  assert.equal(feedback.attributes["aria-label"], "Command feedback");
  h.plan.push(response(202, operation("queued")), response(200, operation("running")),
    response(200, operation("failed", "play", { error: { code: "failed", message: "<b>Failure</b>", retryable: false } })));
  h.click("Play");
  await settle();
  assert.equal(feedback.hidden, false);
  h.close();
  assert.equal(h.ids.playbackOutputs.querySelector(".playback-command-status"), undefined);
  h.open();
  assert.equal(h.button("Play").disabled, true);
  assert.match(h.status().textContent, /running/);
  h.snapshot.agents[0].snapshot.outputs[0].label = "Renamed while pending";
  const slider = h.input("range");
  h.render();
  assert.equal(h.input("range"), slider);
  assert.match(h.status().textContent, /running/);
  h.close();
  await h.tick();
  assert.equal(h.ids.playbackOutputs.querySelector(".playback-command-error"), undefined);
  h.open();
  const status = h.status();
  assert.match(status.textContent, /failed.*<b>Failure<\/b>/);
  assert.equal(status.attributes.role, "alert");
  assert.equal(status.hidden, false);
  assert.equal(h.posts().length, 1);
});

test("consecutive gain changes wait for fresh agent-wide revisions and preserve the saved draft", async () => {
  const h = harness();
  h.snapshot.agents[0].snapshot.outputs.push({
    ...h.snapshot.agents[0].snapshot.outputs[0], id: "other", label: "Other",
  });
  await settle();
  h.open();
  h.edit("range", "-20");
  h.plan.push(response(202, operation("applied", "set_gain")));
  h.click("Apply gain");
  await settle();
  assert.equal(h.button("Apply gain").disabled, true);
  assert.equal(h.input("range").value, "-20");
  assert.match(h.ids.playbackConfigControls.querySelector(".playback-meta").textContent, /Waiting for updated configuration/);
  h.button("Apply gain").listeners.click();
  assert.equal(h.posts().length, 1);
  h.open(1);
  assert.equal(h.button("Play").disabled, true);
  h.open(0);
  h.snapshot.agents[0].snapshot.outputs[0].gain.configured_db = -20;
  h.render();
  assert.equal(h.button("Apply gain").disabled, true);
  h.snapshot.agents[0].snapshot.config_revision = "config-2";
  h.failSnapshot(true);
  await h.evaluate("fetchPlayback()");
  assert.equal(h.button("Apply gain").disabled, true);
  assert.equal(h.posts().length, 1);
  h.failSnapshot(false);
  await h.evaluate("fetchPlayback()");
  assert.equal(h.button("Apply gain").disabled, false);
  assert.equal(h.input("range").value, "-20");
  h.edit("range", "-21");
  h.plan.push(response(202, operation("applied", "set_gain")));
  h.click("Apply gain");
  await settle();
  assert.equal(JSON.parse(h.posts()[1].options.body).expected_config_revision, "config-2");
  assert.equal(JSON.parse(h.posts()[1].options.body).gain_db, -21);
  h.snapshot.agents[0].snapshot.config_revision = "config-3";
  h.snapshot.agents[0].snapshot.outputs[0].gain.configured_db = -21;
  await h.evaluate("fetchPlayback()");
  assert.equal(h.button("Apply gain").disabled, false);
  assert.equal(h.input("range").value, "-21");
});

test("removed output closes and invalidates its dialog and stale handlers cannot send commands", async () => {
  const h = harness();
  await settle();
  h.open();
  const play = h.button("Play");
  h.snapshot.agents[0].snapshot.outputs = [];
  h.render();
  assert.equal(h.ids.playbackConfigDialog.open, false);
  play.listeners.click();
  assert.equal(h.posts().length, 0);
  const state = h.evaluate('outputControls.get(JSON.stringify(["agent/one", "output/one"]))');
  assert.equal(state.present, false);
  h.evaluate('openOutputConfig(outputControls.get(JSON.stringify(["agent/one", "output/one"])))');
  assert.equal(h.ids.playbackConfigDialog.open, false);
});

test("snapshot request failure disables open dialog in place until a fresh snapshot succeeds", async () => {
  const h = harness();
  await settle();
  h.open();
  h.edit("range", "-22.3");
  const slider = h.input("range");
  h.failSnapshot(true);
  await h.evaluate("fetchPlayback()");
  assert.equal(h.ids.playbackConfigDialog.open, true);
  assert.equal(h.input("range"), slider);
  assert.equal(slider.value, "-22.3");
  assert.equal(slider.disabled, true);
  for (const label of ["Play", "Pause", "Stop", "Apply gain"]) assert.equal(h.button(label).disabled, true);
  h.button("Play").listeners.click();
  assert.equal(h.posts().length, 0);
  assert.equal(h.ids.playbackMessage.hidden, false);
  h.failSnapshot(false);
  await h.evaluate("fetchPlayback()");
  assert.equal(h.input("range"), slider);
  assert.equal(slider.value, "-22.3");
  assert.equal(slider.disabled, false);
  assert.equal(h.button("Play").disabled, false);
});

test("dialog groups controls into sections and shows the gain hint only for drafts", async () => {
  const h = harness();
  await settle();
  h.open();
  const titles = h.ids.playbackConfigControls.querySelectorAll(".playback-section-title")
    .map((title) => title.textContent);
  assert.deepEqual(titles, ["Playback", "Station", "Calibration gain"]);
  assert.equal(h.ids.playbackConfigControls.querySelector(".playback-section-detail").textContent, "stopped");
  const hint = h.ids.playbackConfigControls.querySelector(".playback-gain-actions")
    .querySelector(".playback-gain-hint");
  assert.equal(hint.hidden, true);
  h.edit("range", "-20");
  assert.equal(hint.hidden, false);
  assert.match(hint.textContent, /Unsaved draft/);
  h.edit("range", "-12");
  assert.equal(hint.hidden, true);
});

test("station logos fall back across TuneIn CDN paths and survive re-renders without flicker", async () => {
  const h = harness();
  await settle();
  const output = h.snapshot.agents[0].snapshot.outputs[0];
  output.selection = { provider: "tunein", station_id: "s296247", station_name: "METAL Radio" };
  h.render();
  const logo = h.ids.playbackOutputs.children[0].querySelector(".playback-logo");
  assert.equal(logo.src, "https://cdn-profiles.tunein.com/s296247/images/logoq.png");
  assert.equal(logo.hidden, true);
  logo.listeners.error();
  assert.equal(logo.src, "https://cdn-profiles.tunein.com/s296247/images/logoq.jpg");
  logo.listeners.load();
  assert.equal(logo.hidden, false);
  h.render();
  const rerendered = h.ids.playbackOutputs.children[0].querySelector(".playback-logo");
  assert.equal(rerendered, logo);
  assert.equal(rerendered.src, "https://cdn-profiles.tunein.com/s296247/images/logoq.jpg");
  assert.equal(rerendered.hidden, false);

  output.selection = { provider: "tunein", station_id: "s1", station_name: "No logo" };
  h.render();
  const missing = h.ids.playbackOutputs.children[0].querySelector(".playback-logo");
  missing.listeners.error();
  missing.listeners.error();
  assert.equal(missing.src, "https://cdn-radiotime-logos.tunein.com/s1q.png");
  missing.listeners.error();
  h.render();
  assert.equal(h.ids.playbackOutputs.children[0].querySelector(".playback-logo"), undefined);
});

test("empty TuneIn search results explain that nothing playable was found", async () => {
  const h = harness();
  await settle();
  h.open();
  h.edit("search", "s999999999");
  h.plan.push(response(200, { api_version: 1, provider: "tunein", stations: [] }));
  h.click("Search");
  await settle();
  const status = h.ids.playbackConfigControls.querySelectorAll(".playback-gain-hint")
    .find((node) => node.textContent === "No playable stations found.");
  assert.ok(status);
  assert.equal(status.hidden, false);
  assert.equal(h.button("Select and play"), undefined);
});
