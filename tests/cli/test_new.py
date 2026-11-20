"""``balamut new``: scaffolding the project config in place."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from balamut.cli import new
from balamut.config import load_kind_overrides, load_profile
from bm_support import write_file


@pytest.fixture(autouse=True)
def _in_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_new_src_layout_picks_main_and_wires_the_pyproject(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    write_file(tmp_path / "pyproject.toml", '[project]\nname = "demo"\nversion = "0.1.0"\n')
    _git(tmp_path, "init", "-q", "-b", "main")

    assert new.main([]) == 0

    conf = tmp_path / "balamut_conf.py"
    assert conf.is_file()
    assert 'source=("src",)' in conf.read_text(encoding="utf-8")
    assert 'base="main"' in conf.read_text(encoding="utf-8")
    profile = load_profile(tmp_path)
    assert profile.layout.source == ("src",)
    assert profile.base == "main"


def test_new_flat_layout_picks_master(tmp_path: Path) -> None:
    write_file(tmp_path / "mod.py", "x = 1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "branch", "-m", "master")

    assert new.main([]) == 0

    conf = (tmp_path / "balamut_conf.py").read_text(encoding="utf-8")
    assert 'source=(".",)' in conf
    assert 'import_roots=(".",)' in conf
    assert 'base="master"' in conf
    assert load_profile(tmp_path).base == "master"


def test_new_name_dir_layout_imports_the_package_from_the_project_root(tmp_path: Path) -> None:
    write_file(tmp_path / "demo/__init__.py", "")
    write_file(tmp_path / "pyproject.toml", '[project]\nname = "demo"\nversion = "0.1.0"\n')
    _git(tmp_path, "init", "-q", "-b", "main")

    assert new.main([]) == 0

    conf = (tmp_path / "balamut_conf.py").read_text(encoding="utf-8")
    assert 'source=("demo",)' in conf
    assert 'import_roots=(".",)' in conf
    layout = load_profile(tmp_path).layout
    assert layout.module_name("demo/utils.py") == "demo.utils"
    assert layout.module_name("demo/filters/others.py") == "demo.filters.others"
    assert layout.module_name("demo/__init__.py") == "demo"


def test_new_creates_a_missing_pyproject(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    _git(tmp_path, "init", "-q", "-b", "main")

    assert new.main([]) == 0

    pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert f'name = "{tmp_path.name}"' in pyproject
    assert '[tool.balamut]\nconfig = "balamut_conf.py"' in pyproject


def test_new_without_git_defaults_the_diff_base(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()

    assert new.main([]) == 0

    conf = (tmp_path / "balamut_conf.py").read_text(encoding="utf-8")
    assert "base=" not in conf  # the default origin/main is not written
    assert load_profile(tmp_path).base == "origin/main"


def test_new_refuses_to_overwrite_an_existing_config_file(tmp_path: Path) -> None:
    write_file(tmp_path / "balamut_conf.py", "from balamut.config import init\n\ninit()\n")
    _git(tmp_path, "init", "-q", "-b", "main")

    assert new.main([]) == 1
    assert "init()" in (tmp_path / "balamut_conf.py").read_text(encoding="utf-8")
    assert new.main(["--force"]) == 0


def test_new_refuses_an_already_wired_pyproject(tmp_path: Path) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "balamut_conf.py"\n')
    _git(tmp_path, "init", "-q", "-b", "main")

    assert new.main([]) == 1
    assert not (tmp_path / "balamut_conf.py").exists()


def test_new_config_loads_and_registers(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    _git(tmp_path, "init", "-q", "-b", "main")

    assert new.main([]) == 0

    overrides = load_kind_overrides(tmp_path)
    assert overrides.kinds == {}
    assert load_profile(tmp_path).kinds == ("src", "concurrency", "fault", "oracle", "extreme")


def test_new_rejects_unknown_arguments_with_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert new.main(["bogus"]) == 1
    assert "unknown argument(s): bogus" in capsys.readouterr().err


def test_new_success_writes_an_acknowledgement(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "src").mkdir()
    _git(tmp_path, "init", "-q", "-b", "main")

    assert new.main([]) == 0
    assert "wrote" in capsys.readouterr().err


def test_write_pyproject_refuses_an_already_wired_project(tmp_path: Path) -> None:
    write_file(tmp_path / "pyproject.toml", '[tool.balamut]\nconfig = "balamut_conf.py"\n')

    assert new._write_pyproject(tmp_path) == 1


def test_project_name_ignores_a_non_string_name(tmp_path: Path) -> None:
    write_file(tmp_path / "pyproject.toml", "[project]\nname = 123\n")

    assert new._project_name(tmp_path) == tmp_path.name


def test_write_pyproject_strips_trailing_whitespace(tmp_path: Path) -> None:
    write_file(tmp_path / "pyproject.toml", "[project]\nname = 'x'\n\n\n")

    assert new._write_pyproject(tmp_path) == 0
    text = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    assert text == "[project]\nname = 'x'\n\n[tool.balamut]\nconfig = \"balamut_conf.py\"\n"


def test_live_base_returns_the_main_branch(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "--allow-empty", "-qm", "init")

    assert new._live_base(tmp_path) == "main"


def test_live_base_is_main_while_another_branch_is_checked_out(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "--allow-empty", "-qm", "init")
    _git(tmp_path, "checkout", "-q", "-b", "feature")

    assert new._live_base(tmp_path) == "main"


def test_live_base_outside_a_repository_keeps_git_quiet(tmp_path: Path, capfd: pytest.CaptureFixture[str]) -> None:
    """The probes capture git's own output: its "not a git repository" complaint never reaches the terminal."""
    assert new._live_base(tmp_path) == "origin/main"

    assert capfd.readouterr() == ("", "")


def test_project_name_falls_back_to_the_directory_for_invalid_toml(tmp_path: Path) -> None:
    write_file(tmp_path / "pyproject.toml", "[project\n")

    assert new._project_name(tmp_path) == tmp_path.name
