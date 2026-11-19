"""Release version comparison for custom filters (release:<2.0.0 etc.).

Rules from docs.sentry.io (Custom Filters > Release):
  - a value is one release with a version: ``1.2.0`` or ``myapp@1.2.0``
  - one to four numeric parts, missing parts count as zero (1.2 == 1.2.0)
  - a pre-release sorts below its final release (1.0.0-rc.1 < 1.0.0)
  - build metadata (+build.2) is ignored
  - a value with a package only matches releases of that package; a value
    without a package compares with every package
  - a release without a version (e.g. a commit hash) never matches
"""

from __future__ import annotations

import re
from typing import Optional, Tuple

_VER = re.compile(
    r"^v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]*)?$"
)


class Version:
    __slots__ = ("package", "nums", "pre")

    def __init__(self, package: Optional[str], nums: Tuple[int, int, int, int], pre: Optional[str]):
        self.package = package
        self.nums = nums
        self.pre = pre

    def _key(self):
        # final release sorts above any pre-release of the same numbers
        if self.pre is None:
            pre_key = (1,)
        else:
            parts = []
            for p in self.pre.split("."):
                parts.append((0, int(p), "") if p.isdigit() else (1, 0, p))
            pre_key = (0, tuple(parts))
        return (self.nums, pre_key)

    def cmp(self, other: "Version") -> int:
        a, b = self._key(), other._key()
        return (a > b) - (a < b)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Version({self.package!r}, {self.nums!r}, {self.pre!r})"


def parse_release(release: str) -> Optional[Version]:
    """``pkg@1.2.3-rc.1+b`` -> Version; ``None`` if there is no version."""
    if release is None:
        return None
    release = release.strip()
    package = None
    version = release
    if "@" in release:
        package, version = release.rsplit("@", 1)
        package = package or None
    m = _VER.match(version)
    if not m:
        return None
    nums = tuple(int(g) if g is not None else 0 for g in m.groups()[:4])
    return Version(package.lower() if package else None, nums, m.group(5))  # type: ignore[arg-type]


def compare(op: str, release: Optional[str], value: str) -> bool:
    """Does ``release`` satisfy ``op value``? op in gt/gte/lt/lte."""
    want = parse_release(value)
    if want is None:
        raise ValueError(f"release comparison value has no version: {value!r}")
    have = parse_release(release) if release else None
    if have is None:
        return False
    if want.package is not None and have.package != want.package:
        return False
    c = have.cmp(want)
    return {"gt": c > 0, "gte": c >= 0, "lt": c < 0, "lte": c <= 0}[op]
