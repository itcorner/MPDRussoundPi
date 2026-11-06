const DEFAULT_API_TIMEOUT_MS = 15000;

function apiFetch(path, options = {}) {
  const {
    signal: callerSignal,
    timeoutMs = DEFAULT_API_TIMEOUT_MS,
    ...requestOptions
  } = options;
  if (timeoutMs !== null && (!Number.isFinite(timeoutMs) || timeoutMs <= 0)) {
    return Promise.reject(new TypeError("timeoutMs must be a positive number or null."));
  }

  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(callerSignal.reason);
  if (callerSignal?.aborted) {
    abortFromCaller();
  } else {
    callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  }

  const timer = timeoutMs === null ? null : window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);

  return fetch(path, {
    ...requestOptions,
    credentials: "same-origin",
    signal: controller.signal,
  }).catch((error) => {
    if (timedOut) {
      const timeoutError = new Error(`Request timed out after ${timeoutMs} ms.`);
      timeoutError.name = "TimeoutError";
      throw timeoutError;
    }
    throw error;
  }).finally(() => {
    if (timer !== null) window.clearTimeout(timer);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  });
}
