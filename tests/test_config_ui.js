const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../web/static/config.js"), "utf8");
const backendRendering = source.slice(
  source.indexOf("function updateConnectionFields("),
  source.indexOf("function formatSoundValue("),
);
const backendCollection = source.slice(
  source.indexOf("function collectBackend("),
  source.indexOf("function collectControllers("),
);
const refresh = source.slice(source.indexOf("async function refreshBackendStatus("), source.indexOf("function setBackendConnectionStatus("));
const setBackendStatus = source.slice(
  source.indexOf("function setBackendConnectionStatus("),
  source.indexOf("function setMessage("),
);
const save = source.slice(source.indexOf("async function saveConfig("), source.indexOf("function hasRelevantTarget("));
const rebuild = source.slice(source.indexOf("function rebuildZoneSlots("), source.indexOf("function collectZoneSlots("));

function renderBackend(payload) {
  const ids = [
    "backendMode", "backendHost", "backendPort", "backendDevice", "backendBaud",
    "backendPollInterval", "backendModeSource", "backendHostSource", "backendPortSource",
    "backendHostField", "backendPortField", "backendDeviceField", "backendBaudField",
    "backendSourceDetail",
  ];
  const elements = Object.fromEntries(ids.map((id) => [id, {
    value: "", hidden: false, disabled: false, readOnly: false, dataset: {},
    textContent: "",
  }]));
  const context = {
    document: { getElementById: (id) => elements[id] },
    backendMode: elements.backendMode,
    backendHost: elements.backendHost,
    backendPort: elements.backendPort,
    backendDevice: elements.backendDevice,
    backendBaud: elements.backendBaud,
    backendPollInterval: elements.backendPollInterval,
  };
  vm.createContext(context);
  vm.runInContext(backendRendering, context);
  context.renderBackendFields(payload);
  return { context, elements };
}

test("effective backend fields show source and lock only environment-controlled values", () => {
  const { elements } = renderBackend({
    config: { backend: { host: "saved.gateway", port: 6666, poll_interval_seconds: 2 } },
    effective_backend: {
      mode: { value: "tcp", source: "environment" },
      host: { value: "runtime.gateway", source: "environment" },
      port: { value: 7000, source: "saved" },
    },
  });
  assert.equal(elements.backendMode.value, "tcp");
  assert.equal(elements.backendMode.disabled, true);
  assert.equal(elements.backendModeSource.textContent, "Environment");
  assert.equal(elements.backendHost.value, "runtime.gateway");
  assert.equal(elements.backendHost.readOnly, true);
  assert.equal(elements.backendHostSource.textContent, "Environment");
  assert.doesNotMatch(elements.backendSourceDetail.textContent, /Active environment override|RUSSOUND_BACKEND_HOST|saved config-file value is retained/);
  assert.equal(elements.backendPort.value, 6666);
  assert.equal(elements.backendPort.readOnly, false);
  assert.equal(elements.backendPortSource.textContent, "Config file");
  assert.equal(elements.backendPortSource.hidden, true);
});

test("environment TCP override preserves a saved serial endpoint and avoids editing hidden TCP fallback", () => {
  const payload = {
    config: { backend: { device: "/dev/ttyUSB0", baud: 19200, poll_interval_seconds: 2 } },
    effective_backend: {
      mode: { value: "tcp", source: "environment" },
      host: { value: "gateway.local", source: "environment" },
      port: { value: 6666, source: "default" },
    },
  };
  const { elements } = renderBackend(payload);
  assert.equal(elements.backendMode.disabled, true);
  assert.equal(elements.backendHostField.hidden, false);
  assert.equal(elements.backendDeviceField.hidden, true);
  assert.equal(elements.backendHost.readOnly, true);
  assert.equal(elements.backendPort.readOnly, true);
  assert.match(elements.backendSourceDetail.textContent, /saved endpoint is serial/);

  const collectionContext = {
    configPayload: payload,
    backendMode: elements.backendMode,
    backendHost: elements.backendHost,
    backendPort: elements.backendPort,
    backendDevice: elements.backendDevice,
    backendBaud: elements.backendBaud,
    backendPollInterval: elements.backendPollInterval,
  };
  vm.createContext(collectionContext);
  vm.runInContext(backendCollection, collectionContext);
  assert.deepEqual(JSON.parse(JSON.stringify(collectionContext.collectBackend())), {
    device: "/dev/ttyUSB0",
    baud: 19200,
    poll_interval_seconds: 2,
  });
});

test("saving a non-overridden TCP field never copies the effective environment value", () => {
  const payload = {
    config: { backend: { host: "saved.gateway", port: 6666, poll_interval_seconds: 2 } },
    effective_backend: {
      mode: { value: "tcp", source: "environment" },
      host: { value: "runtime.gateway", source: "environment" },
      port: { value: 7000, source: "saved" },
    },
  };
  const { elements } = renderBackend(payload);
  elements.backendPort.value = "7001";
  const context = {
    configPayload: payload,
    backendMode: elements.backendMode,
    backendHost: elements.backendHost,
    backendPort: elements.backendPort,
    backendDevice: elements.backendDevice,
    backendBaud: elements.backendBaud,
    backendPollInterval: elements.backendPollInterval,
  };
  vm.createContext(context);
  vm.runInContext(backendCollection, context);
  assert.deepEqual(JSON.parse(JSON.stringify(context.collectBackend())), {
    host: "saved.gateway",
    port: 7001,
    poll_interval_seconds: 2,
  });
});

test("backend provenance labels distinguish saved values and defaults", () => {
  const { elements } = renderBackend({
    config: { backend: { host: "saved.gateway", port: 6666 } },
    effective_backend: {
      mode: { value: "tcp", source: "saved" },
      host: { value: "saved.gateway", source: "saved" },
      port: { value: 6666, source: "saved" },
    },
  });
  assert.equal(elements.backendModeSource.textContent, "Config file");
  assert.equal(elements.backendModeSource.hidden, true);
  assert.equal(elements.backendHostSource.textContent, "Config file");
  assert.equal(elements.backendHostSource.hidden, true);
  assert.equal(elements.backendPortSource.dataset.source, "saved");
  assert.equal(elements.backendPortSource.hidden, true);

  const defaults = renderBackend({
    config: {},
    effective_backend: {
      mode: { value: "tcp", source: "default" },
      host: { value: "127.0.0.1", source: "default" },
      port: { value: 6666, source: "default" },
    },
  });
  assert.equal(defaults.elements.backendHostSource.textContent, "Default");
  assert.equal(defaults.elements.backendHostSource.hidden, false);
  assert.match(
    fs.readFileSync(path.join(__dirname, "../web/static/config.html"), "utf8"),
    /Config file and default values can be edited here and saved/,
  );
});

test("provenance explanations appear in a small paragraph below backend fields", () => {
  const html = fs.readFileSync(path.join(__dirname, "../web/static/config.html"), "utf8");
  assert.match(html, /id="backendSourceExplanation" class="config-source-explanation"/);
  assert.match(html, /id="backendSourceDetail"/);
  for (const id of ["backendMode", "backendHost", "backendPort"]) {
    assert.match(html, new RegExp(`id="${id}"[^>]*aria-describedby="backendSourceExplanation"`));
  }
  assert.doesNotMatch(html, /role="tooltip"|backend(?:Mode|Host|Port)Help/);
  assert.ok(html.indexOf('id="backendSourceExplanation"') > html.indexOf('<div class="setup-fields">'));
  const styles = fs.readFileSync(path.join(__dirname, "../web/static/styles.css"), "utf8");
  assert.match(styles, /\.config-source-explanation\s*\{[^}]*font-size:\s*0\.78rem/s);
  assert.doesNotMatch(styles, /config-source-help|config-source-trigger/);
});

test("TCP provenance and editability follow each independent override", () => {
  const saved = { host: "saved.gateway", port: 6666 };
  for (const [hostSource, portSource, host, port] of [
    ["saved", "saved", "saved.gateway", 6666],
    ["environment", "saved", "saved.gateway", 6666],
    ["saved", "environment", "saved.gateway", 7000],
    ["environment", "environment", "runtime.gateway", 7000],
  ]) {
    const overridden = hostSource === "environment" || portSource === "environment";
    const { elements } = renderBackend({
      config: { backend: saved },
      effective_backend: {
        mode: { value: "tcp", source: overridden ? "environment" : "saved" },
        host: { value: host, source: hostSource },
        port: { value: port, source: portSource },
      },
    });
    assert.equal(elements.backendHost.value, host);
    assert.equal(elements.backendPort.value, port);
    assert.equal(elements.backendHost.readOnly, hostSource === "environment");
    assert.equal(elements.backendPort.readOnly, portSource === "environment");
    assert.equal(elements.backendMode.disabled, overridden);
    assert.equal(elements.backendHostSource.hidden, hostSource === "saved");
    assert.equal(elements.backendPortSource.hidden, portSource === "saved");
  }
});

test("first setup with an environment override explains the missing saved endpoint", () => {
  const { elements } = renderBackend({
    config: { backend: { host: "", port: 6666 } },
    effective_backend: {
      mode: { value: "tcp", source: "environment" },
      host: { value: "gateway.local", source: "environment" },
      port: { value: 6666, source: "default" },
    },
  });
  assert.match(elements.backendSourceDetail.textContent, /No saved endpoint exists yet/);
  assert.equal(elements.backendHost.readOnly, true);
});

test("an inactive partial saved TCP endpoint shows runtime defaults without activating it", () => {
  const payload = {
    config_required: false,
    config: { backend: { host: "inactive.gateway", poll_interval_seconds: 2 } },
    effective_backend: {
      mode: { value: "tcp", source: "default" },
      host: { value: "127.0.0.1", source: "default" },
      port: { value: 6666, source: "default" },
    },
  };
  const { elements } = renderBackend(payload);
  assert.equal(elements.backendHost.value, "127.0.0.1");
  assert.equal(elements.backendPort.value, 6666);
  const context = {
    configPayload: payload,
    backendMode: elements.backendMode,
    backendHost: elements.backendHost,
    backendPort: elements.backendPort,
    backendDevice: elements.backendDevice,
    backendBaud: elements.backendBaud,
    backendPollInterval: elements.backendPollInterval,
  };
  vm.createContext(context);
  vm.runInContext(backendCollection, context);
  assert.deepEqual(JSON.parse(JSON.stringify(context.collectBackend())), payload.config.backend);
  elements.backendHost.value = "new.gateway";
  assert.deepEqual(JSON.parse(JSON.stringify(context.collectBackend())), {
    host: "new.gateway", port: 6666, poll_interval_seconds: 2,
  });
});

test("saving unrelated changes omits an unchanged incomplete backend", async () => {
  const savedBackend = { host: "inactive.gateway", poll_interval_seconds: 2 };
  let request;
  const messages = [];
  const context = {
    saveInFlight: false,
    hasUnsavedChanges: true,
    configPayload: {
      config_required: false,
      config: { backend: savedBackend },
      effective_backend: { mode: { source: "default" } },
    },
    collectBackend: () => ({ ...savedBackend }),
    backendMode: { value: "tcp" },
    collectControllers: () => [{ id: 1, zone_count: 6 }],
    collectZoneSlots: () => [],
    collectSourceSlots: () => [],
    collectShortcuts: () => [],
    updateSaveButtonState() {},
    setMessage: (...args) => messages.push(args),
    renderEditor() {},
    apiFetch: async (url, options) => {
      request = JSON.parse(options.body);
      return { ok: true, json: async () => ({}) };
    },
  };
  vm.createContext(context);
  vm.runInContext(save, context);
  await context.saveConfig();
  assert.equal(Object.hasOwn(request, "backend"), false);
  assert.equal(messages.at(-1)[0], "Configuration saved.");

  request = undefined;
  context.collectBackend = () => ({ ...savedBackend, poll_interval_seconds: 3 });
  await context.saveConfig();
  assert.equal(request, undefined);
  assert.match(messages.at(-1)[0], /Complete the saved backend endpoint/);

  context.collectBackend = () => ({ host: "", poll_interval_seconds: 2 });
  await context.saveConfig();
  assert.equal(request, undefined);
  assert.match(messages.at(-1)[0], /Enter a gateway host/);

  context.configPayload = {
    config_required: true,
    config: { backend: { host: "", port: 6666 } },
    effective_backend: { mode: { source: "environment" } },
  };
  context.collectBackend = () => ({ host: "", port: 6666, poll_interval_seconds: 2 });
  await context.saveConfig();
  assert.equal(request, undefined);
  assert.match(messages.at(-1)[0], /Remove or change the environment override/);
});

test("partial saved endpoint stays inactive under an independent environment override", () => {
  const payload = {
    config_required: false,
    config: { backend: { host: "inactive.gateway", poll_interval_seconds: 2 } },
    effective_backend: {
      mode: { value: "tcp", source: "environment" },
      host: { value: "127.0.0.1", source: "default" },
      port: { value: 7000, source: "environment" },
    },
  };
  const { elements } = renderBackend(payload);
  assert.equal(elements.backendHost.value, "127.0.0.1");
  assert.equal(elements.backendPort.value, 7000);
  const context = {
    configPayload: payload,
    backendMode: elements.backendMode,
    backendHost: elements.backendHost,
    backendPort: elements.backendPort,
    backendDevice: elements.backendDevice,
    backendBaud: elements.backendBaud,
    backendPollInterval: elements.backendPollInterval,
  };
  vm.createContext(context);
  vm.runInContext(backendCollection, context);
  assert.deepEqual(JSON.parse(JSON.stringify(context.collectBackend())), payload.config.backend);
});

test("rebuilding controller slots preserves backend drafts and the original saved endpoint", () => {
  const savedBackend = { host: "inactive.gateway" };
  const fields = ["backendMode", "backendHost", "backendPort", "backendDevice", "backendBaud", "backendPollInterval"];
  const context = {
    draftZones: new Map(),
    baselineZoneSlotsHash: "zones",
    baselineSourceSlotsHash: "sources",
    baselineConnectionHash: "connection",
    baselineShortcutsHash: "shortcuts",
    configPayload: { config: { backend: savedBackend } },
    collectZoneSlots: () => [],
    collectControllers: () => [{ id: 1, zone_count: 1 }],
    collectSourceSlots: () => [],
    collectShortcuts: () => [],
    renderEditor(payload) {
      assert.equal(payload.config.backend, savedBackend);
      for (const name of fields) context[name].value = "rendered";
    },
    updateConnectionFields() {},
    recomputeUnsavedChanges() {},
  };
  fields.forEach((name) => { context[name] = { value: `${name}-draft` }; });
  vm.createContext(context);
  vm.runInContext(rebuild, context);
  context.rebuildZoneSlots();
  for (const name of fields) assert.equal(context[name].value, `${name}-draft`);
});

test("connectivity consumes authoritative metadata, never raw override precedence", async () => {
  for (const [configured, connected, expected] of [[true, true, "connected"], [true, false, "disconnected"], [false, true, "incomplete"]]) {
    const elements = {
      backendSummaryStatus: { dataset: {}, textContent: "", title: "" },
      backendConnectionStatus: { textContent: "", hidden: true },
    };
    const context = {
      statusInFlight: false,
      configPayload: null,
      renderSoundControls() {},
      document: { hidden: false, getElementById: (id) => elements[id] },
      backendSummaryStatus: elements.backendSummaryStatus,
      backendConnectionStatus: elements.backendConnectionStatus,
      apiFetch: async () => ({
        ok: true,
        json: async () => ({
          effective_backend: { configured },
          backend_status: { connected },
          backend: { host: "ignored", port: "bad" },
          backend_overrides: { RUSSOUND_BACKEND_PORT: "also-ignored" },
        }),
      }),
    };
    vm.createContext(context);
    vm.runInContext(`${setBackendStatus}\n${refresh}`, context);
    await context.refreshBackendStatus();
    assert.equal(elements.backendSummaryStatus.dataset.state, expected);
    assert.equal(elements.backendConnectionStatus.hidden, expected === "connected");
    assert.equal(context.statusInFlight, false);
  }
});
