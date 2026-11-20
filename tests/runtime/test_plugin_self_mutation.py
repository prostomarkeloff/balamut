"""balamut mutating its own plugin: no mutant of the plugin outlives its lease in a persistent worker.

The plugin applies and observes every mutant, so a mutant of the plugin that stayed live after its lease would
decide how the next mutants are applied: its own code would run at the next boundary, or a revert that it broke
would leave it in the worker for good. The plugin is loaded a second time under another name and its mutants are
applied to that copy, never to the plugin of the process that runs this test. One fake session plays one persistent
worker: each mutant gets a lease of one item, an item of an original lease follows, and after that the copy must be
exactly what it was.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import multiprocessing
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from balamut.engine.identify import identify, mutant_spec
from balamut.operators.src import enumerate_mutants
from balamut.runtime import plugin
from balamut.runtime.reloads import reload_log
from bm_support.plugin_fakes import FakeItem, FakeSession

_COPY = "bm_plugin_copy"
_PATH = "src/balamut/runtime/plugin.py"

type _Definition = tuple[types.CodeType, tuple[object, ...] | None, dict[str, object] | None]


@pytest.fixture
def copy(monkeypatch: pytest.MonkeyPatch) -> Iterator[types.ModuleType]:
    spec = importlib.util.spec_from_file_location(_COPY, plugin.__file__)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_COPY] = module
    try:
        spec.loader.exec_module(module)
        # The copy's setup scrubs these from the real environment and resets the process's daemon flag.
        for key in plugin._OWNED_TRANSPORT_ENV:
            monkeypatch.delenv(key, raising=False)
        process = multiprocessing.current_process()
        monkeypatch.setattr(process, "daemon", process.daemon)
        yield module
    finally:
        sys.modules.pop(_COPY, None)


def _definitions(module: types.ModuleType) -> dict[str, _Definition]:
    """Every function the module defines, at module level or in its classes, with what a mutant can edit."""
    found: dict[str, _Definition] = {}

    def walk(namespace: dict[str, object], prefix: str) -> None:
        for name, value in namespace.items():
            if isinstance(value, types.FunctionType) and value.__module__ == _COPY:
                found[prefix + name] = (value.__code__, value.__defaults__, value.__kwdefaults__)
            elif isinstance(value, type) and value.__module__ == _COPY:
                walk(dict(vars(value)), f"{prefix}{name}.")

    walk(dict(vars(module)), "")
    return found


def _same(left: dict[str, _Definition], right: dict[str, _Definition]) -> bool:
    return left.keys() == right.keys() and all(
        all(a is b for a, b in zip(left[name], right[name], strict=True)) for name in left
    )


def _run_item(copy: types.ModuleType, session: FakeSession) -> None:
    """One test item as a worker runs it: the protocol wrapper starts first, the setup applies the lease."""
    item = cast("pytest.Item", FakeItem(session))
    wrapper = copy.pytest_runtest_protocol(item)
    next(wrapper)
    copy.pytest_runtest_setup(item)
    with pytest.raises(StopIteration):
        wrapper.send(True)


# A mutant that removes the yield of a generator hook swaps a generator body for a plain one: CPython warns.
@pytest.mark.filterwarnings("ignore:Assigning a code object of non-matching type:DeprecationWarning")
def test_no_mutant_of_the_plugin_outlives_its_lease(
    copy: types.ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(plugin.__file__).read_text(encoding="utf-8")
    spec_file = tmp_path / "mutant.json"
    monkeypatch.setenv(plugin.MUTANT_FILE_ENV, str(spec_file))
    session = FakeSession()
    session.config.stash[copy._RELOADS] = reload_log(importlib.reload)
    namespace = dict(vars(copy))
    reference = _definitions(copy)
    tools = {tool: sys.monitoring.get_tool(tool) for tool in plugin._MONITORING_TOOL_IDS}
    applied = 0
    outlived: list[str] = []
    for index, mutant in enumerate(enumerate_mutants(_PATH, source)):
        identity = identify(mutant, source, module=_COPY)
        if identity is None:
            continue  # module or class level: a worker never applies it
        spec = mutant_spec(identity["spec"], source, mutant, fuzz_target=identity["fuzz_target"])
        lease = {**spec, "request_token": f"lease-{index}", "touch_path": str(tmp_path / "touched")}
        spec_file.write_text(json.dumps(lease), encoding="utf-8")
        _run_item(copy, session)
        unsupported = Path(f"{spec_file}{plugin.UNSUPPORTED_MARKER_SUFFIX}")
        applied += not unsupported.exists()
        unsupported.unlink(missing_ok=True)
        spec_file.write_text("", encoding="utf-8")
        _run_item(copy, session)
        if not _same(_definitions(copy), reference) or {tool: sys.monitoring.get_tool(tool) for tool in tools} != tools:
            outlived.append(f"{mutant.lineno}: {mutant.original!r} -> {mutant.mutated!r}")
            break  # the copy is no longer the reference: later mutants would prove nothing
    assert outlived == []
    # Names are only added (the helpers a domain mutant references): every binding of the copy is still the same.
    assert all(vars(copy)[name] is value for name, value in namespace.items())
    assert applied > 100  # the leases did put the mutants into the copy
