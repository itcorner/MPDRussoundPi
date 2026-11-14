"""Disposable Linux target fixture; no production target paths or Pi access."""

import json
import sys
from pathlib import Path

from tool.deployment_channel import Channel
from tool.deployment_compose import paths
from tool.deployment_operator import serve


root = Path(sys.argv[1])
try:
    serve(Channel(sys.stdin.buffer, sys.stdout.buffer), paths(root))
except Exception as error:
    print(type(error).__name__ + ": " + str(error), file=sys.stderr)
    raise
