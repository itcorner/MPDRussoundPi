import re


def output_id(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None:
        raise ValueError("Output ID must match [a-z][a-z0-9_-]{0,63}")
    return value


def output_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or not all(c.isprintable() for c in value):
        raise ValueError("Output label must be nonempty and printable")
    return value

