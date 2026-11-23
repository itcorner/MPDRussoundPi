// Administrator mode (web/static/admin.js): the pure view model and the page wiring.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const source = fs.readFileSync(path.join(root, "web/static/admin.js"), "utf8");
const context = {};
vm.createContext(context);
vm.runInContext(source, context);  // no document here, so nothing starts

test("countdown text", () => {
  assert.equal(context.adminCountdown(872), "14:32");
  assert.equal(context.adminCountdown(59.9), "00:59");
  assert.equal(context.adminCountdown(-5), "00:00");
  assert.equal(context.adminCountdown("junk"), "00:00");
});

test("view model for guest, elevated and the last minute", () => {
  const guest = context.adminView({ configured: true, authenticated: false }, 0);
  assert.equal(guest.elevated, false);
  assert.equal(guest.configured, true);
  assert.equal(guest.menuLabel, "Sign in as administrator");
  const elevated = context.adminView({ configured: true, authenticated: true }, 872);
  assert.equal(elevated.elevated, true);
  assert.equal(elevated.low, false);
  assert.equal(elevated.countdown, "14:32");
  assert.equal(elevated.menuLabel, "Exit admin mode");
  const low = context.adminView({ configured: true, authenticated: true }, 42);
  assert.equal(low.low, true, "the last minute turns the strip orange and offers Extend");
  const unconfigured = context.adminView({ configured: false, authenticated: false }, 0);
  assert.equal(unconfigured.configured, false, "no button where no administrator login exists");
  assert.equal(context.adminView(null, 0).elevated, false);
  assert.equal(context.adminView({ configured: false, authenticated: true }, 900).elevated, false);
});

test("every page loads the administrator mode after the API helper", () => {
  for (const page of ["index", "playback", "config", "status"]) {
    const html = fs.readFileSync(path.join(root, "web/static", `${page}.html`), "utf8");
    const api = html.indexOf('src="/static/api.js"');
    const admin = html.indexOf('src="/static/admin.js"');
    assert.ok(admin > api && api >= 0, `${page}.html loads admin.js`);
    assert.match(html, /class="system-panel[^"]*"|class="panel system-panel"/, `${page}.html has the header admin.js extends`);
    assert.match(html, /class="page-navigation"/);
  }
});

test("session data never reaches innerHTML", () => {
  const writes = [...source.matchAll(/\.innerHTML\s*=\s*([^;]+);/g)].map((match) => match[1].trim());
  assert.deepEqual(writes, ["svg"], "only the fixed icons from this file");
  assert.match(source, /function adminIcon\(element, svg\)/);
  for (const call of source.matchAll(/adminIcon\([^,]+,\s*([^)]+)\)/g)) {
    assert.match(call[1], /SHIELD_SVG|LOCK_SVG|svg/, call[0]);
  }
});
