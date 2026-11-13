"""Non-secret completion events for the controlled deployment runner."""

import json
import os
from typing import Any

from ansible.plugins.callback import CallbackBase  # pyright: ignore[reportMissingTypeStubs]


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "aggregate"
    CALLBACK_NAME = "deployment_completion"
    CALLBACK_NEEDS_ENABLED = True

    def __init__(self) -> None:
        super().__init__()
        self.failed = False
        self.sequence = 0
        self.operation = os.environ.get("RUSSOUND_COMPLETION_OPERATION", "")
        self.host = os.environ.get("RUSSOUND_COMPLETION_HOST", "")
        self.stream = None
        try:
            path = os.environ["RUSSOUND_COMPLETION_STREAM"]
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            self.stream = os.fdopen(fd, "w")
            directory = os.open(os.path.dirname(path), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except (KeyError, OSError):
            self.failed = True
            self._display.warning("Deployment completion stream could not be created; finalization is blocked")
        self.emit("start")

    def emit(self, event: str, **fields: Any) -> None:
        value: dict[str, Any] = {
            "schema_version": 1, "sequence": self.sequence, "operation": self.operation,
            "host": self.host, "event": event, **fields,
        }
        self.sequence += 1
        if self.stream is None:
            self.failed = True
            return
        try:
            self.stream.write(json.dumps(value) + "\n")
            self.stream.flush()
            os.fsync(self.stream.fileno())
        except (OSError, ValueError, TypeError):
            self.failed = True
            self._display.warning("Deployment completion recording failed; finalization is blocked")

    def task_event(self, event: str, host: Any, task: Any) -> None:
        tags: list[str] = [tag.removeprefix("deployment_required_") for tag in task.tags
                           if isinstance(tag, str) and tag.startswith("deployment_required_")]
        required = tags[0] if len(tags) == 1 else None
        # Required retry/item/async/conditional semantics must be implemented
        # and tested before the runner is allowed to accept those patterns.
        unsupported = len(tags) > 1 or bool(tags and (
            task.loop is not None or task.until or task.async_val or task.when or task.ignore_errors
        ))
        self.emit(event, actual_host=host.get_name(), task=required, unsupported=unsupported)

    def v2_runner_on_start(self, host: Any, task: Any) -> None:
        self.task_event("task-start", host, task)

    def v2_runner_on_ok(self, result: Any) -> None:
        self.task_event("passed", result._host, result._task)

    def v2_runner_on_failed(self, result: Any, ignore_errors: bool = False) -> None:
        self.task_event("failed", result._host, result._task)

    def v2_runner_on_unreachable(self, result: Any) -> None:
        self.task_event("unreachable", result._host, result._task)

    def v2_runner_on_skipped(self, result: Any) -> None:
        self.task_event("skipped", result._host, result._task)

    def v2_runner_retry(self, result: Any) -> None:
        self.task_event("unsupported-retry", result._host, result._task)

    def v2_runner_item_on_ok(self, result: Any) -> None:
        self.task_event("unsupported-item", result._host, result._task)

    def v2_runner_item_on_failed(self, result: Any) -> None:
        self.task_event("failed", result._host, result._task)

    def v2_runner_item_on_skipped(self, result: Any) -> None:
        self.task_event("unsupported-item", result._host, result._task)

    def v2_playbook_on_stats(self, stats: Any) -> None:
        totals = {key: 0 for key in ("failures", "unreachable", "rescued", "ignored")}
        hosts = sorted(stats.processed)
        for host in hosts:
            summary = stats.summarize(host)
            for key in totals:
                totals[key] += summary.get(key, 0)
        self.emit("end", recording_failed=self.failed, hosts=hosts, totals=totals)
        try:
            fd = int(os.environ["RUSSOUND_COMPLETION_FD"])
            os.write(fd, b"0" if self.failed else b"1")
        except (KeyError, ValueError, OSError):
            self._display.warning("Deployment completion acknowledgement failed; finalization is blocked")
