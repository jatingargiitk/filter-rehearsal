"""Glob matching as documented for Sentry inbound filters.

docs.sentry.io/concepts/data-management/filtering/#glob-matching:
  - case insensitive
  - ``?`` one character, ``*`` zero or more characters
  - ``\\`` escapes the next character (a backslash before a non-meta
    character is ignored)
  - ``[...]`` character classes, ``[!...]`` negated classes
  - ``{`` and ``}`` are reserved; we treat ``{a,b}`` as alternation, as bash
    does, and say so in the README.

The whole string must match (``*Error*`` is how you say "contains").
"""

from __future__ import annotations

import re
from functools import lru_cache

__all__ = ["GlobError", "compile_glob", "glob_match"]


class GlobError(ValueError):
    pass


def _translate(pat: str) -> str:
    out = []
    i, n = 0, len(pat)
    depth = 0  # inside {...}
    while i < n:
        c = pat[i]
        if c == "\\":
            if i + 1 < n:
                out.append(re.escape(pat[i + 1]))
                i += 2
            else:
                out.append(re.escape("\\"))
                i += 1
            continue
        if c == "*":
            out.append(".*")
        elif c == "?":
            out.append(".")
        elif c == "[":
            j = i + 1
            negate = False
            if j < n and pat[j] == "!":
                negate = True
                j += 1
            start = j
            # a ']' right after '[' or '[!' is literal
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                raise GlobError(f"unclosed '[' in pattern {pat!r}")
            body = pat[start:j]
            body = body.replace("\\", "\\\\").replace("^", "\\^")
            out.append("[" + ("^" if negate else "") + body + "]")
            i = j + 1
            continue
        elif c == "{":
            depth += 1
            out.append("(?:")
        elif c == "}":
            if depth == 0:
                raise GlobError(f"unmatched '}}' in pattern {pat!r}")
            depth -= 1
            out.append(")")
        elif c == "," and depth > 0:
            out.append("|")
        else:
            out.append(re.escape(c))
        i += 1
    if depth:
        raise GlobError(f"unclosed '{{' in pattern {pat!r}")
    return "".join(out)


@lru_cache(maxsize=4096)
def compile_glob(pat: str) -> "re.Pattern[str]":
    return re.compile(r"\A(?:" + _translate(pat) + r")\Z", re.IGNORECASE | re.DOTALL)


def glob_match(pat: str, text: str) -> bool:
    return compile_glob(pat).match(text) is not None
