"""Merge the tools' normalized artifacts of one bug into the union the oracle judges once.

Identical artifacts (same file, same mutated bytes) from several tools become one artifact with several
origins; each origin keeps the tool, its native id and operator, so every tool's own set can be rebuilt.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymutbench.artifacts import Artifact, merge, write_jsonl
from pymutbench.metrics import TOOLS


def merge_bug(directory: Path, tools: tuple[str, ...] = TOOLS) -> int:
    """The union of the tools' artifacts (``<tool>.jsonl``; the registered tools by default); no other file of the
    bug is read."""
    artifacts: list[Artifact] = []
    for tool in tools:
        path = directory / f"{tool}.jsonl"
        if path.exists():
            artifacts += [Artifact.from_json(json.loads(line))
                          for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
    union = merge(artifacts)
    write_jsonl(directory / "union.jsonl", union)
    return len(union)


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(arg, merge_bug(Path(arg)))
