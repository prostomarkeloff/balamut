"""Resetting a persistent worker to its baseline (balamut.runtime.worker_reset).

The reset keeps the import graph intact and clears ``@lru_cache`` wrappers. Baselines are built by hand here
(only the cache clears a test needs) so the process-wide caches stay untouched.
"""

from __future__ import annotations

import functools
import gc
import importlib.util
import sys
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from balamut.runtime.worker_reset import (
    WorkerBaseline,
    capture_baseline,
    namespace_cache_wrappers,
    plain_modules,
    reset_to_baseline,
)


def _load_py_module(tmp_path: Path, name: str) -> None:
    path = tmp_path / f"{name}.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)


def test_capture_baseline_snapshots_modules_without_mutating() -> None:
    baseline = capture_baseline()
    assert "sys" in baseline.modules
    assert isinstance(baseline.cache_clears, tuple)
    assert "sys" in sys.modules


def test_reset_keeps_pure_python_module_imported_after_baseline(tmp_path: Path) -> None:
    baseline = WorkerBaseline(modules=frozenset(sys.modules), cache_clears=())
    name = "balamut_pure_py_reset_mod"
    _load_py_module(tmp_path, name)
    try:
        assert name in sys.modules
        reset_to_baseline(baseline)
        assert name in sys.modules
    finally:
        sys.modules.pop(name, None)


def test_reset_keeps_submodule_and_parent_attr(tmp_path: Path) -> None:
    (tmp_path / "bm_pkg").mkdir()
    (tmp_path / "bm_pkg" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "bm_pkg" / "sub.py").write_text("MARK = 1\n", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    try:
        importlib.import_module("bm_pkg")
        baseline = WorkerBaseline(modules=frozenset(sys.modules), cache_clears=())
        importlib.import_module("bm_pkg.sub")
        pkg = sys.modules["bm_pkg"]
        assert "sub" in pkg.__dict__
        reset_to_baseline(baseline)
        assert "bm_pkg.sub" in sys.modules
        assert "sub" in pkg.__dict__
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("bm_pkg.sub", None)
        sys.modules.pop("bm_pkg", None)


def test_reset_does_not_reimport_partial_python_graph(tmp_path: Path) -> None:
    # Re-importing part of a graph re-runs module bodies that attach singletons: identity must survive.
    package = tmp_path / "bm_identity_graph"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "router.py").write_text("parent = None\n", encoding="utf-8")
    (package / "app.py").write_text(
        "from . import router\n"
        "if router.parent is not None:\n"
        "    raise RuntimeError('router already attached')\n"
        "router.parent = object()\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        importlib.import_module("bm_identity_graph.router")
        baseline = WorkerBaseline(modules=frozenset(sys.modules), cache_clears=())
        app = importlib.import_module("bm_identity_graph.app")
        reset_to_baseline(baseline)
        assert importlib.import_module("bm_identity_graph.app") is app
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("bm_identity_graph.app", None)
        sys.modules.pop("bm_identity_graph.router", None)
        sys.modules.pop("bm_identity_graph", None)


def test_reset_clears_lru_cache() -> None:
    calls: list[int] = []

    @functools.lru_cache
    def f(x: int) -> int:
        calls.append(x)
        return x * 2

    assert f(3) == 6
    assert f(3) == 6
    assert calls == [3]
    baseline = WorkerBaseline(modules=frozenset(sys.modules), cache_clears=(f.cache_clear,))
    reset_to_baseline(baseline)
    assert f(3) == 6
    assert calls == [3, 3]


_CACHED_SOURCE = """\
from functools import cache, lru_cache


@lru_cache
def rate(x):
    return x


class Tariff:
    @staticmethod
    @cache
    def base():
        return 1

    @classmethod
    @cache
    def scale(cls):
        return 2

    @property
    @cache
    def fee(self):
        return 3

    class Inner:
        @cache
        def nested(self):
            return 4
"""


def _register_module(monkeypatch: pytest.MonkeyPatch, path: Path, source: str) -> types.ModuleType:
    path.write_text(source, encoding="utf-8")
    name = f"bm_reset_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _clears(module: types.ModuleType) -> tuple[Callable[[], None], ...]:
    tariff: Any = module.Tariff
    fee = cast("property", vars(tariff)["fee"])
    wrappers: tuple[Any, ...] = (module.rate, tariff.base, tariff.scale, fee.fget, tariff.Inner.nested)
    return tuple(wrapper.cache_clear for wrapper in wrappers)


def test_capture_baseline_reaches_wrappers_frozen_before_the_fork(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # pytest-fast freezes the collected heap before forking its workers; gc.get_objects() then lists none of it.
    module = _register_module(monkeypatch, tmp_path / "frozen.py", _CACHED_SOURCE)
    gc.freeze()
    try:
        assert not any(obj is module.rate for obj in gc.get_objects())
        baseline = capture_baseline()
    finally:
        gc.unfreeze()

    assert all(clear in baseline.cache_clears for clear in _clears(module))


def test_namespace_cache_wrappers_never_evaluate_a_lazy_proxy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # A lazy proxy (Django's settings, say) evaluates its target on `__class__`, which isinstance() reads.
    module = _register_module(
        monkeypatch,
        tmp_path / "proxied.py",
        "from functools import cache\n\n\n"
        "class Proxy:\n    @property\n    def __class__(self):\n        raise AssertionError('evaluated')\n\n\n"
        "settings = Proxy()\n\n\n@cache\ndef rate():\n    return 1\n",
    )

    wrappers = namespace_cache_wrappers([module])

    assert [wrapper.cache_clear for wrapper in wrappers] == [module.rate.cache_clear]


def test_plain_modules_skip_a_lazy_module_subclass(monkeypatch: pytest.MonkeyPatch) -> None:
    class LazyModule(types.ModuleType):
        pass

    lazy = LazyModule("bm_reset_lazy")
    monkeypatch.setitem(sys.modules, "bm_reset_lazy", lazy)

    modules = plain_modules()

    assert lazy not in modules
    assert sys.modules["sys"] in modules
