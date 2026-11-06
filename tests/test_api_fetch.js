const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../web/static/api.js"), "utf8");

function harness(fetch) {
  const timers = new Map();
  const scheduled = [];
  let timerId = 0;
  const context = vm.createContext({
    AbortController,
    Error,
    Number,
    Promise,
    TypeError,
    fetch,
    window: {
      setTimeout(handler, delay) {
        const id = ++timerId;
        timers.set(id, { handler, delay });
        scheduled.push(delay);
        return id;
      },
      clearTimeout(id) {
        timers.delete(id);
      },
    },
  });
  vm.runInContext(source, context);
  return { context, timers, scheduled };
}

function stalledFetch(requests) {
  return (url, options) => {
    requests.push({ url, options });
    return new Promise((resolve, reject) => {
      options.signal.addEventListener("abort", () => reject(options.signal.reason), { once: true });
    });
  };
}

test("a stalled request aborts at the default timeout and reports a timeout", async () => {
  const requests = [];
  const { context, timers } = harness(stalledFetch(requests));
  const request = context.apiFetch("/api/rooms");
  assert.equal(timers.size, 1);
  const [id, timer] = timers.entries().next().value;
  assert.equal(timer.delay, 15000);

  timer.handler();

  await assert.rejects(request, { name: "TimeoutError" });
  assert.equal(requests[0].options.signal.aborted, true);
  assert.equal(timers.has(id), false);
});

test("caller cancellation is preserved and clears the shared timeout", async () => {
  const requests = [];
  const { context, timers } = harness(stalledFetch(requests));
  const caller = new AbortController();
  const request = context.apiFetch("/api/rooms", { signal: caller.signal });

  caller.abort();

  await assert.rejects(request, { name: "AbortError" });
  assert.equal(requests[0].options.signal.aborted, true);
  assert.equal(timers.size, 0);
});

test("a caller can set a longer timeout or disable it", async () => {
  const requests = [];
  const { context, timers, scheduled } = harness(async (url, options) => {
    requests.push({ url, options });
    return { ok: true };
  });

  await context.apiFetch("/api/slow", { timeoutMs: 90000 });
  assert.equal(scheduled[0], 90000);
  assert.equal("timeoutMs" in requests[0].options, false);
  assert.equal(requests[0].options.credentials, "same-origin");

  await context.apiFetch("/api/unbounded", { timeoutMs: null });
  assert.equal(scheduled.length, 1);
  assert.equal(timers.size, 0);
});
