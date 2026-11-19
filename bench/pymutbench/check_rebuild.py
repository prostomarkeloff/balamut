"""Compare a rebuilt real-bug report with the published one, file by file and byte by byte.

    python3 -m pymutbench.check_rebuild PUBLISHED_RESULTS REBUILT_RESULTS PUBLISHED_IMAGES REBUILT_IMAGES

Every file ``report bugs`` writes must be identical: the report is deterministic (sorted keys, seeded bootstraps,
gzip without a time stamp), so any difference is a change of the code or of the data behind the numbers. Exits 1
with the differing files listed.
"""

from __future__ import annotations

import filecmp
import sys
from pathlib import Path


def differences(published: Path, rebuilt: Path, images_published: Path, images_rebuilt: Path) -> list[str]:
    found = []
    for path in sorted(p for p in rebuilt.rglob("*") if p.is_file()):
        twin = published / path.relative_to(rebuilt)
        if not twin.exists() or not filecmp.cmp(path, twin, shallow=False):
            found.append(str(twin))
    for path in sorted(images_rebuilt.glob("*.svg")):
        twin = images_published / path.name
        if not twin.exists() or not filecmp.cmp(path, twin, shallow=False):
            found.append(str(twin))
    return found


def main() -> None:
    published, rebuilt, images_published, images_rebuilt = (Path(arg) for arg in sys.argv[1:5])
    found = differences(published, rebuilt, images_published, images_rebuilt)
    for path in found:
        print(f"differs from the rebuilt report: {path}")
    if found:
        raise SystemExit(1)
    print("the rebuilt report equals the published one")


if __name__ == "__main__":
    main()
