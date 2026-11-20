import logging
import re
from typing import TextIO


class SafeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        message = re.sub(r"https?://[^\s<>\"']+", "[redacted-url]", message, flags=re.IGNORECASE)
        message = re.sub(r"\bBearer\s+\S+", "Bearer [redacted]", message, flags=re.IGNORECASE)
        message = re.sub(
            r"""(["'])(token|password|secret|session_id|token_file|tls_ca_file)\1\s*:\s*(["']).*?\3""",
            r'"\2": "[redacted]"', message, flags=re.IGNORECASE,
        )
        message = re.sub(
            r"\b(token|password|secret|session_id|token_file|tls_ca_file)\s*[=:]\s*[^,\s]+",
            r"\1=[redacted]", message, flags=re.IGNORECASE,
        )
        return message


def configure_console(debug: bool, *, stream: TextIO | None = None) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        stream=stream,
        format="%(asctime)s %(levelname)s %(message)s",
        force=True,
    )
    formatter = SafeFormatter("%(asctime)s %(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    logging.getLogger("russound.health").setLevel(logging.INFO)
