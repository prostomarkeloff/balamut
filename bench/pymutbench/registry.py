"""The registry of benchmarked tools (``tools.toml``): the one list every stage reads.

The generation driver takes each tool's interpreter and adapter from it, the image builds its install
requirements, the campaigns its environment name, the measures its default configuration. A tool outside the
registry is evaluated from its own mutants (``submit``).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    version: str
    url: str
    install: tuple[str, ...]
    venv: str
    adapter: str
    default_drop: str = ""
    engine: str = ""

    @property
    def python(self) -> str:
        """The interpreter of the tool's generation environment in the tools image."""
        return f"/tools/{self.venv}/bin/python"


REGISTRY_FILE = Path(__file__).resolve().parent / "tools.toml"


def load(path: Path = REGISTRY_FILE) -> dict[str, Tool]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        name: Tool(
            name=name,
            version=entry["version"],
            url=entry["url"],
            install=tuple(entry["install"]),
            venv=entry["venv"],
            adapter=entry["adapter"],
            default_drop=entry.get("default_drop", ""),
            engine=entry.get("engine", ""),
        )
        for name, entry in data["tools"].items()
    }


REGISTRY = load()
TOOLS: tuple[str, ...] = tuple(REGISTRY)
