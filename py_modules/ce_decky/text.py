from __future__ import annotations

import unicodedata

# Bidirectional formatting characters that reorder what follows them. A label
# carrying one can render as a different label entirely, which is the whole
# reason untrusted table text is normalized before the panel sees it.
UNSAFE_BIDI_CLASSES = frozenset({"RLE", "LRE", "RLO", "LRO", "PDF", "RLI", "LRI", "FSI", "PDI"})


def is_unsafe_display_character(ch: str) -> bool:
    """A character that is invisible or reorders its neighbours when rendered.

    One rule, in one place, because two readers now normalize untrusted table
    text: the inspector, which renders a label on a row, and the source view,
    which renders the table's own code. A view of the code that let through
    what the label rule removes would be the more dangerous of the two, since
    the whole reason to read a script is to decide whether to run it.
    """
    return (
        unicodedata.category(ch) in {"Cc", "Cf"}
        or unicodedata.bidirectional(ch) in UNSAFE_BIDI_CLASSES
    )


def utf8_bytes(value: str, field: str = "text") -> bytes:
    """Encode text strictly, normalizing codec failures into validation errors."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} is not valid Unicode text") from exc


def utf8_len(value: str, field: str = "text") -> int:
    return len(utf8_bytes(value, field))


def fit_utf8(value: str, field: str, max_bytes: int) -> str:
    """The longest leading part of `value` that fits `max_bytes` when encoded.

    One rule in one place, and it exists because the obvious way to write it is
    quadratic in a length a stranger's file chooses. Dropping one character at a
    time and re-encoding to ask how long the rest is now took 66 seconds for a
    single 2 MB value on this device, against a 32 MB table ceiling, and every
    description, label and dropdown value of every imported table goes through
    this - before any consent, on the ordinary import route.

    Slicing the encoded bytes can land inside a character; `ignore` drops that
    partial one, which is the same leading text either way.
    """
    encoded = utf8_bytes(value, field)
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", "ignore")
