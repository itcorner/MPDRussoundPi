const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const test = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../web/static/config.js"), "utf8");
const description = source.slice(source.indexOf("function describeEffectiveBackend("), source.indexOf("function renderEditor("));
const refresh = source.slice(source.indexOf("async function refreshBackendStatus("), source.indexOf("function setBackendConnectionStatus("));

test("runtime description keeps saved and environment sources distinct", () => {
  const context = {};
  vm.createContext(context);
  vm.runInContext(description, context);
  const effective = { mode: { value: "tcp" }, host: { value: "gateway", source: "environment" }, port: { value: 6666, source: "saved" } };
  assert.match(context.describeEffectiveBackend(effective), /gateway \(environment\):6666 \(saved\)/);
  assert.match(context.describeEffectiveBackend(effective), /Fields below edit saved settings/);
  assert.match(context.describeEffectiveBackend({ mode: { value: "serial" } }), /serial \(saved device\)/);
  assert.equal(context.describeEffectiveBackend(null), "");
});

test("connectivity consumes authoritative metadata, never raw override precedence", async () => {
  for (const [configured, connected, expected] of [[true, true, "connected"], [true, false, "disconnected"], [false, true, "incomplete"]]) {
    let result;
    const context = {
      statusInFlight: false,
      document: { hidden: false },
      configPayload: null,
      renderSoundControls() {},
      setBackendConnectionStatus(state) { result = state; },
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
    vm.runInContext(refresh, context);
    await context.refreshBackendStatus();
    assert.equal(result, expected);
    assert.equal(context.statusInFlight, false);
  }
});
