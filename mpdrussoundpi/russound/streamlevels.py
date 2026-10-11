"""Measure unmodified radio feeds and recommend attenuation-only stream gains."""

import argparse
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import TextIO, TypeAlias, Union
from urllib.parse import urlparse

from lib.streamgain import validate_gain_db


JSONValue: TypeAlias = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]


@dataclass(frozen=True)
class Measurement:
    name: str
    integrated_lufs: float
    loudness_range_lu: float
    true_peak_dbtp: float
    measured_seconds: float
    warnings: list[str]


def parse_measurement(name: str, stderr: str, progress: str, duration: float) -> Measurement:
    summary = stderr.rsplit("Summary:", 1)[-1]
    values: list[float] = []
    for pattern in (
        r"\bI:\s*([-\w.]+)\s+LUFS",
        r"\bLRA:\s*([-\w.]+)\s+LU",
        r"\bPeak:\s*([-\w.]+)\s+dBFS",
    ):
        match = re.search(pattern, summary)
        if not match:
            raise ValueError(f"{name}: missing FFmpeg loudness summary")
        values.append(float(match.group(1)))
    loudness, loudness_range, peak = values
    if not all(math.isfinite(value) for value in values) or loudness <= -70:
        raise ValueError(f"{name}: silent or unmeasurable feed; no gain recommendation")
    times = re.findall(r"^out_time_us=(\d+)$", progress, re.MULTILINE)
    if not times or "progress=end" not in progress:
        raise ValueError(f"{name}: missing completed measurement duration")
    seconds = int(times[-1]) / 1_000_000
    if seconds < duration - 0.1:
        raise ValueError(f"{name}: feed ended early ({seconds:g}s of {duration:g}s)")
    warnings = [line for line in stderr.splitlines() if "[warning]" in line]
    if any(level in stderr for level in ("[error]", "[fatal]")):
        raise ValueError(f"{name}: FFmpeg reported errors:\n{stderr[-4000:]}")
    return Measurement(name, loudness, loudness_range, peak, seconds, warnings)


def measure_streams(streams: list[dict[str, str]], duration: float, ffmpeg: str) -> list[Measurement]:
    measurements: list[Measurement] = []
    with ExitStack() as stack:
        jobs: list[tuple[str, subprocess.Popen[bytes], TextIO, TextIO, float]] = []
        try:
            for entry in streams:
                stdout = stack.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8"))
                stderr = stack.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8"))
                command = [
                    ffmpeg, "-nostdin", "-hide_banner", "-nostats", "-loglevel", "level+info",
                    "-stats_period", "10", "-progress", "pipe:1", "-xerror",
                    "-re", "-rw_timeout", "15000000", "-i", entry["stream"],
                    "-map", "0:a:0", "-t", str(duration), "-vn", "-sn", "-dn",
                    "-af", "aformat=channel_layouts=stereo,ebur128=peak=true:framelog=verbose",
                    "-f", "null", "-",
                ]
                print(f"Measuring {entry['name']} for {duration:g} seconds...", file=sys.stderr)
                process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
                jobs.append((entry["name"], process, stdout, stderr, time.monotonic() + duration + 60))
            for name, process, stdout, stderr, deadline in jobs:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
                stdout.seek(0)
                stderr.seek(0)
                progress, log = stdout.read(), stderr.read()
                if process.returncode:
                    raise ValueError(f"{name}: FFmpeg failed ({process.returncode}):\n{log[-4000:]}")
                measurements.append(parse_measurement(name, log, progress, duration))
        finally:
            for _, process, _, _, _ in jobs:
                if process.poll() is None:
                    process.terminate()
            for _, process, _, _, _ in jobs:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    return measurements


def recommend_gains(measurements: list[Measurement], peak_ceiling: float) -> tuple[float, dict[str, float]]:
    if not measurements:
        raise ValueError("No measurements")
    if not math.isfinite(peak_ceiling) or peak_ceiling > 0:
        raise ValueError("Peak ceiling must be finite and <= 0 dBTP")
    # Lower the common target further if a feed needs additional peak headroom.
    target = min(
        item.integrated_lufs - max(0, item.true_peak_dbtp - peak_ceiling)
        for item in measurements
    )
    gains = {
        item.name: validate_gain_db(math.floor((target - item.integrated_lufs) * 10 + 1e-9) / 10)
        for item in measurements
    }
    return target, gains


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json", type=Path, help="Stream configuration")
    parser.add_argument("--duration", type=float, default=900, help="Seconds of decoded audio per feed (default: 900)")
    parser.add_argument("--peak-ceiling", type=float, default=-1, help="Maximum predicted true peak in dBTP (default: -1)")
    parser.add_argument("--output-config", type=Path, help="Write a NEW configuration with recommended gains")
    args = parser.parse_args(argv)
    if not math.isfinite(args.duration) or args.duration < 10:
        parser.error("duration must be finite and >= 10 seconds")
    if not math.isfinite(args.peak_ceiling) or args.peak_ceiling > 0:
        parser.error("peak-ceiling must be finite and <= 0")
    if args.output_config and (args.output_config.resolve() == args.json.resolve() or args.output_config.exists()):
        parser.error("output-config must be a new file, not the input configuration")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        parser.exit(1, "FFmpeg is required with the ebur128 filter (on Debian: sudo apt install ffmpeg).\n")
    try:
        with args.json.open(encoding="utf-8") as source:
            config: JSONValue = json.load(source)
        if not isinstance(config, dict):
            raise ValueError("Configuration must contain a nonempty streams list")
        streams = config.get("streams")
        if not isinstance(streams, list) or not streams:
            raise ValueError("Configuration must contain a nonempty streams list")
        sources: list[dict[str, str]] = []
        configured_streams: list[dict[str, JSONValue]] = []
        names: set[str] = set()
        for entry in streams:
            if not isinstance(entry, dict):
                raise ValueError("Each stream must be an object")
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("Each stream requires a nonempty name")
            if name in names:
                raise ValueError(f"Duplicate stream name: {name!r}")
            names.add(name)
            url = entry.get("stream")
            if not isinstance(url, str) or urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
                raise ValueError(f"{name}: stream must be an HTTP(S) URL")
            validate_gain_db(entry.get("gain_db", 0))
            sources.append({"name": name, "stream": url})
            configured_streams.append(entry)
        started = datetime.now(timezone.utc).isoformat()
        measurements = measure_streams(sources, args.duration, ffmpeg)
        target, gains = recommend_gains(measurements, args.peak_ceiling)
        report: dict[str, object] = {
            "started_utc": started,
            "completed_utc": datetime.now(timezone.utc).isoformat(),
            "requested_seconds_per_stream": args.duration,
            "method": "FFmpeg ebur128; direct feeds decoded as stereo; existing gains bypassed",
            "target_lufs": target,
            "peak_ceiling_dbtp": args.peak_ceiling,
            "note": "Snapshot recommendations, not continuous normalization. Re-measure across programmes.",
            "streams": [
                {**asdict(item), "gain_db": gains[item.name],
                 "predicted_lufs": item.integrated_lufs + gains[item.name],
                 "predicted_true_peak_dbtp": item.true_peak_dbtp + gains[item.name]}
                for item in measurements
            ],
        }
        if args.output_config:
            for entry, source_entry in zip(configured_streams, sources):
                entry["gain_db"] = gains[source_entry["name"]]
            with args.output_config.open("x", encoding="utf-8") as destination:
                json.dump(config, destination, indent=2, allow_nan=False)
                destination.write("\n")
        print(json.dumps(report, indent=2, allow_nan=False))
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"Measurement failed: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Measurement interrupted; FFmpeg processes stopped.\n")


if __name__ == "__main__":
    main()
