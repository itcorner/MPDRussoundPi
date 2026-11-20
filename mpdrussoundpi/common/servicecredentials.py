from pathlib import Path


MAX_TOKEN_BYTES = 4096


def validate_service_token(token: str) -> str:
    if not 32 <= len(token) <= MAX_TOKEN_BYTES or any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("Service token must contain 32-4096 printable ASCII characters without whitespace")
    return token


def read_service_token(path: str | Path) -> str:
    try:
        with open(path, "rb") as source:
            raw = source.read(MAX_TOKEN_BYTES + 3)
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith(b"\n"):
            raw = raw[:-1]
        return validate_service_token(raw.decode("ascii"))
    except (OSError, UnicodeError, ValueError):
        raise ValueError("Cannot read a valid service token; check the protected token file") from None
