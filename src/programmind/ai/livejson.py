"""One field of a half-written JSON answer (spec 4.1, 13 September 2026).

Every prompt in Program Mind asks for a single JSON object, so what the
model streams while it writes is half-finished JSON. The page shows the
field that carries the prose - ``answer`` for Ask the vault, ``view`` for
a board member, ``overall_recommendation`` for the consolidation - read
out of whatever has arrived so far.

For the eye only. What Python parses, checks and stores is the finished
answer of the call.
"""

from __future__ import annotations

import re

_ESCAPES = {"n": "\n", "t": "\t", "r": "", '"': '"', "\\": "\\", "/": "/", "b": "", "f": ""}


def field(partial: str, key: str = "answer") -> str:
    """The value of ``key`` as far as it is written: unescaped, stopping at
    the closing quote, empty until the key appears. A broken escape at the
    end is not shown yet rather than shown wrongly."""
    match = re.search(r'"' + re.escape(key) + r'"\s*:\s*"', partial)
    if match is None:
        return ""
    out: list[str] = []
    index = match.end()
    while index < len(partial):
        char = partial[index]
        if char == "\\":
            if index + 1 >= len(partial):
                break                                  # an escape the model has not finished writing
            following = partial[index + 1]
            if following == "u":
                if index + 6 > len(partial):
                    break
                try:
                    out.append(chr(int(partial[index + 2:index + 6], 16)))
                except ValueError:
                    pass
                index += 6
                continue
            out.append(_ESCAPES.get(following, following))
            index += 2
            continue
        if char == '"':
            break                                      # the end of the field; the rest is the other keys
        out.append(char)
        index += 1
    return "".join(out)
