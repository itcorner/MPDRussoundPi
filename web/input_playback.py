from __future__ import annotations

from typing import Any


def resolve_input_names(inputs: list[dict[str, Any]], playback: dict[str, Any]) -> list[dict[str, Any]]:
    agents: dict[str, dict[str, Any]] = {agent["agent_id"]: agent for agent in playback["agents"]}
    result: list[dict[str, Any]] = []
    for source in inputs:
        fallback = source.get("name") or f"Source {source['id']}"
        item: dict[str, Any] = {**source, "display_name": fallback}
        binding = source.get("playback_output")
        if binding:
            agent = agents.get(binding["agent_id"])
            snapshot: dict[str, Any] = (agent.get("snapshot") or {}) if agent is not None else {}
            output: dict[str, Any] | None = next(
                (output for output in snapshot.get("outputs", []) if output["id"] == binding["output_id"]),
                None,
            )
            if agent is None:
                status, message = "unmapped", "Configured playback agent is not available."
            elif output is None:
                status = "unmapped" if agent["status"] == "ok" and snapshot else "unavailable"
                message = "Configured playback output is not available."
            else:
                selection: dict[str, Any] = output.get("selection") or {}
                item["display_name"] = selection.get("label") or fallback
                if agent["status"] != "ok" or snapshot.get("stale"):
                    status, message = "unavailable", "Playback status unavailable; showing the last known station."
                elif not selection.get("key"):
                    status, message = "no_station", "No station selected."
                else:
                    status, message = "ok", ""
            item["playback_status"] = status
            item["playback_message"] = message
        result.append(item)
    return result
