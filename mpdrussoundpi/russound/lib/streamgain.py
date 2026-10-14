import math
import re
import subprocess
import time


def validate_gain_db(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("gain_db must be a number")
    gain = float(value)
    if not math.isfinite(gain) or gain > 0:
        raise ValueError("gain_db must be finite and <= 0 (attenuation only)")
    return gain


def read_sink_gain(sink_name: str) -> tuple[int, int]:
    result = subprocess.run(
        ["pactl", "get-sink-volume", f"sink.{sink_name}"],
        check=True, capture_output=True, text=True, timeout=2,
    )
    if result.stderr.strip():
        raise RuntimeError("Mixer inspection reported a diagnostic")
    observed = [int(value) for value in re.findall(r":\s*(\d+)\s*/", result.stdout)]
    if len(observed) != 2:
        raise ValueError("Mixer inspection requires exactly two channels")
    return observed[0], observed[1]


def set_sink_gain(sink_name: str, gain_db: float) -> None:
    gain = validate_gain_db(gain_db)
    # PulseAudio volume is cubic; signed dB arguments are relative, not absolute.
    volume = round(65536 * 10 ** (gain / 60))
    sink = f"sink.{sink_name}"
    deadline = time.monotonic() + 5
    try:
        while True:
            subprocess.run(
                ["pactl", "set-sink-volume", sink, str(volume)],
                check=True, capture_output=True, text=True, timeout=2,
            )
            result = subprocess.run(
                ["pactl", "get-sink-volume", sink],
                check=True, capture_output=True, text=True, timeout=2,
            )
            observed = [int(value) for value in re.findall(r":\s*(\d+)\s*/", result.stdout)]
            if observed and all(value == volume for value in observed):
                return
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Gain {gain:g} dB did not take effect on {sink}; "
                    "ensure playback is active and the sink supports volume control"
                )
            time.sleep(0.1)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"Cannot set gain for {sink}: {error.stderr.strip()}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Timed out setting gain for {sink}") from error
