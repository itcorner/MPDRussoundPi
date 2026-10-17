const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function load(script) {
  const elements = new Map();
  const element = () => ({
    dataset: {}, options: [], value: "", textContent: "", listeners: {},
    addEventListener(name, handler) { this.listeners[name] = handler; },
    querySelectorAll() { return []; },
    add(option) { this.options.push(option); },
  });
  const document = {
    hidden: true,
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, element());
      return elements.get(id);
    },
    querySelector() { return element(); },
    querySelectorAll() { return []; },
    addEventListener() {},
  };
  const context = vm.createContext({
    document, console,
    window: { localStorage: { getItem() { return null; } }, addEventListener() {}, setInterval() {} },
    fetch: () => new Promise(() => {}),
    Option: function(text, value, defaultSelected, selected) {
      Object.assign(this, { textContent: text, value, selected });
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, `../web/static/${script}.js`), "utf8"), context);
  return { context, elements, element };
}

test("dashboard resolves names and updates only option text on playback events", async () => {
  const { context, elements } = load("app");
  const option = { value: "1", selected: true };
  const select = { options: [option], value: "1" };
  const zoneCard = {};
  const shortcut = { dataset: { shortcutId: "morning" } };
  elements.get("zones").querySelectorAll = () => [select];
  elements.get("zones").zoneCard = zoneCard;
  elements.get("shortcuts").querySelectorAll = () => [shortcut];
  vm.runInContext('state.shortcuts = [{id: "morning", source: 1}]', context);
  context.fetch = async () => ({
    ok: true,
    json: async () => ({
      resolved_inputs: [{ id: 1, name: "Radio", display_name: "<Station & name>", playback_status: "ok" }],
    }),
  });
  await vm.runInContext("refreshInputNames()", context);
  assert.equal(option.textContent, "<Station & name>");
  assert.equal(option.selected, true);
  assert.equal(select.value, "1");
  assert.equal(elements.get("zones").zoneCard, zoneCard);
  assert.equal(shortcut.title, "<Station & name>");
  assert.equal(vm.runInContext('inputDisplayName({name:"Radio", display_name:"OE3", playback_status:"unavailable"})', context), "OE3 (unavailable)");
  assert.equal(vm.runInContext('inputDisplayName({name:"TV"})', context), "TV");
});

test("editor includes mapping changes in dirty tracking", () => {
  const { context } = load("config");
  const original = vm.runInContext('hashSourceSlots([{id:1,name:"Radio",enabled:true}])', context);
  const changed = vm.runInContext('hashSourceSlots([{id:1,name:"Radio",enabled:true,playback_output:{agent_id:"musicpi",output_id:"stereo_front"}}])', context);
  assert.notEqual(original, changed);
  assert.equal(original, vm.runInContext('hashSourceSlots([{id:1,name:"Radio",enabled:true,playback_output:null}])', context));
});

test("editor retains unknown offline mappings and collects explicit removal", () => {
  const { context, element } = load("config");
  const binding = { agent_id: "musicpi", output_id: "stereo_front" };
  const select = element();
  const preview = element();
  const fields = {
    "playback-output": select, "playback-preview": preview,
    "source-name": { value: "Radio" }, "source-enabled": { checked: true },
  };
  const row = {
    dataset: { sourceId: "1" },
    querySelector(selector) { return fields[selector.match(/data-field="([^"]+)"/)[1]]; },
  };
  context.row = row;
  context.binding = binding;
  vm.runInContext('configPayload = {playback_outputs:[]}; populatePlaybackOutputs(row, binding)', context);
  assert.deepEqual(JSON.parse(select.value), binding);
  assert.match(select.options[1].textContent, /Unavailable \[musicpi \/ stereo_front\]/);
  assert.equal(preview.textContent, "Unavailable");
  assert.match(select.title, /fallback/);
  context.document.querySelectorAll = () => [row];
  assert.deepEqual(JSON.parse(vm.runInContext("JSON.stringify(collectSourceSlots())", context))[0].playback_output, binding);
  select.value = "null";
  assert.equal(JSON.parse(vm.runInContext("JSON.stringify(collectSourceSlots())", context))[0].playback_output, null);
});

test("editor previews current saved station without replacing fallback or draft binding", () => {
  const { context, element } = load("config");
  const binding = { agent_id: "musicpi", output_id: "stereo_front" };
  const select = { value: JSON.stringify(binding), options: [] };
  const preview = element();
  context.row = {
    dataset: { sourceId: "1" },
    querySelector(selector) { return selector.includes("playback-output") ? select : preview; },
  };
  context.resolved = { id: 1, playback_output: binding, display_name: "New station", playback_message: "" };
  vm.runInContext("configPayload = {playback_outputs: [], resolved_inputs:[resolved]}; updatePlaybackPreview(row)", context);
  assert.equal(preview.textContent, "New station");
  select.value = JSON.stringify({ ...binding, output_id: "other_output" });
  vm.runInContext("updatePlaybackPreview(row)", context);
  assert.equal(preview.textContent, "Unavailable");
});

test("stream mapping menu shows station and output but closed value shows only station", () => {
  const { context, element } = load("config");
  const select = element();
  const preview = element();
  context.row = {
    dataset: { sourceId: "1" },
    querySelector(selector) { return selector.includes("playback-output") ? select : preview; },
  };
  vm.runInContext(`configPayload = {playback_outputs: [{
    agent_id:"musicpi",output_id:"stereo_front",station_name:"Radio <88.6>",available:true
  }]}; populatePlaybackOutputs(row, {agent_id:"musicpi",output_id:"stereo_front"})`, context);
  assert.equal(select.options[1].textContent, "Radio <88.6> [musicpi / stereo_front]");
  assert.equal(preview.textContent, "Radio <88.6>");
  assert.equal(select.title, "");
});
