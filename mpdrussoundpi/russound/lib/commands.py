"""Paths of the package's console commands for generated systemd units."""

from pathlib import Path
import sys


def installed_command(name: str) -> str:
    """The console command ``name`` installed next to the running interpreter.

    Generated units must run the same installed environment as the command that
    writes them. Refuse to write a unit that would point at a missing command.
    """
    # Keep the virtual environment's bin directory: resolving the interpreter
    # symlink would leave the environment.
    command = Path(sys.executable).parent / name
    if not command.is_file():
        raise RuntimeError(
            f"{name} is not installed in {command.parent}; install the mpdrussoundpi package into this environment"
        )
    return str(command)
