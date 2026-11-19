"""The fix of a bug as the report reads it (``fix.json`` in the bug's directory).

Per source file the fix changed: its status, the fixed and the buggy text and the ``-U0`` diff between them
(``git diff --no-renames -U0 <parent> <commit> -- <path>``); for the fix commit: its author and committer dates and
an explicit AI-tool marker, if any. ``ensure`` writes the record once from the subject's clone; ``restore`` writes
it from the published data, so a report needs no clone.

An AI-tool marker is explicit only: a ``Co-authored-by`` or ``Assisted-by`` trailer naming an AI tool or its
vendor, a "Generated with/by" line naming one, or a merge of a branch an AI agent names (``codex/…``,
``copilot/…``).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

_TOOLS = (
    r"\b(?:claude(?:code)?|anthropic|copilot|codex|openai|chatgpt|gpt-\d+|cursor|devin|gemini|aider|jules|openhands)\b"
)
_MARKERS = (
    re.compile(rf"^(?:co-authored-by|assisted-by):.*{_TOOLS}.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(rf"^.*generated (?:with|by)\b.*{_TOOLS}.*$", re.IGNORECASE | re.MULTILINE),
    re.compile(
        r"^merge (?:pull request #\d+ from|branch|remote-tracking branch) .*?(?:[\s/'-])"
        r"(?:codex|copilot|claude|cursor|devin)/\S+.*$",
        re.IGNORECASE | re.MULTILINE,
    ),
)


def ai_marker(message: str) -> str | None:
    """The first line of a commit message that marks an AI tool as a co-author or the source of the change."""
    for pattern in _MARKERS:
        match = pattern.search(message)
        if match:
            return match.group(0).strip()[:200]
    return None


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _show(repo: Path, rev: str, path: str) -> str | None:
    proc = subprocess.run(["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, check=False)
    return proc.stdout.decode("utf-8") if proc.returncode == 0 else None


def build(repo: Path, bug: dict[str, Any]) -> dict[str, Any]:
    commit, parent = bug["commit"], bug["parent"]
    author, committer, message = _git(repo, "log", "-1", "--format=%aI%x00%cI%x00%B", commit).split("\0", 2)
    files: dict[str, dict[str, Any]] = {}
    for change in bug["src_files"]:
        path = change["path"]
        files[path] = {
            "status": change["status"],
            "fixed": _show(repo, commit, path) if change["status"] != "D" else None,
            "buggy": _show(repo, parent, path) if change["status"] != "A" else None,
            "diff_u0": _git(repo, "diff", "--no-renames", "-U0", parent, commit, "--", path),
        }
    return {
        "commit": commit,
        "parent": parent,
        "author_date": author,
        "committer_date": committer,
        "ai_marker": ai_marker(message),
        "files": files,
    }


def ensure(directory: Path, repo: Path, bug: dict[str, Any]) -> dict[str, Any]:
    path = directory / "fix.json"
    if not path.exists():
        path.write_text(json.dumps(build(repo, bug)), encoding="utf-8")
    return load(directory)


def load(directory: Path) -> dict[str, Any]:
    return json.loads((directory / "fix.json").read_text(encoding="utf-8"))


def fixed_bytes(fix: dict[str, Any], path: str) -> bytes | None:
    text = fix["files"].get(path, {}).get("fixed")
    return None if text is None else text.encode("utf-8")


def buggy_bytes(fix: dict[str, Any], path: str) -> bytes | None:
    text = fix["files"].get(path, {}).get("buggy")
    return None if text is None else text.encode("utf-8")
