"""Host-side driver: builds the images and runs every stage that executes third-party code in a container.

The host only reads git history and writes result files; subject installs, test suites and mutation tools run
inside ``pymutbench-*`` images, with the network disabled for every stage except image builds.

    python3 -m pymutbench.orchestrate build-base
    python3 -m pymutbench.orchestrate build-subject attrs
    python3 -m pymutbench.orchestrate validate attrs --jobs 3
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

BENCH = Path(__file__).resolve().parent.parent
BASE_IMAGE = "pymutbench-base:1"
WORK = Path(os.environ.get("PMB_WORK", str(Path.home() / ".cache" / "pymutbench")))
OUT = Path(os.environ.get("PMB_OUT_DIR", str(WORK / "out")))
# Optional CPU sets (``docker run --cpuset-cpus``, e.g. "0-3"): the native campaigns of the engine track run on
# their own cores while the other stages run beside them on theirs, so a measured time never shares a core.
CPUS = os.environ.get("PMB_CPUS", "")
ENGINE_CPUS = os.environ.get("PMB_ENGINE_CPUS", "")
# Optional memory limit of a native campaign container (``docker run --memory``, e.g. "3g"): a campaign that
# exceeds it fails inside its own container instead of the VM's out-of-memory killer choosing another process.
ENGINE_MEMORY = os.environ.get("PMB_ENGINE_MEMORY", "")
# The same for every other stage container (``PMB_MEMORY``): a mutant that exhausts memory is killed inside its own
# container (the oracle's external-kill rule then makes it unknown) instead of the VM killing another container.
MEMORY = os.environ.get("PMB_MEMORY", "")

sys.path.insert(0, str(BENCH))
from pymutbench.registry import REGISTRY  # noqa: E402
from pymutbench.subjects import SUBJECTS  # noqa: E402

_SNAPSHOT_LOCK = threading.Lock()


def _code_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in source.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        digest.update(path.relative_to(source).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def bench_snapshot() -> Path:
    """An immutable copy of the harness code, mounted as ``/bench`` by every container this process starts:
    editing the working tree while a stage runs cannot change the code the stage executes (and fingerprints).
    Named by the digest of its files and verified against it, so a reused snapshot is the code it names."""
    with _SNAPSHOT_LOCK:  # the drivers start containers from several threads
        return _snapshot()


@functools.cache
def _snapshot() -> Path:
    name = _code_digest(BENCH / "pymutbench")
    target = WORK / "snapshots" / name
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=f"{name}.", dir=target.parent))
        shutil.copytree(BENCH / "pymutbench", staging / "pymutbench", ignore=shutil.ignore_patterns("__pycache__"))
        try:
            staging.rename(target)
        except OSError:  # another process published the same snapshot first
            shutil.rmtree(staging)
    if _code_digest(target / "pymutbench") != name:
        raise SystemExit(f"{target} does not hold the code it names: remove it")
    return target


@functools.cache
def image_id(image: str) -> str:
    return sh("docker", "image", "inspect", "--format", "{{.Id}}", image).strip()


def docker_run(image: str, *mounts: str, engine: bool = False) -> list[str]:
    """``docker run`` of a stage container: removed on exit, no network, the harness snapshot at ``/bench``, the
    outputs at ``/out``, the image's identity in ``PMB_IMAGE``, on the stage's CPU set if one is set."""
    cpus = ENGINE_CPUS if engine else CPUS
    memory = ENGINE_MEMORY if engine else MEMORY
    volumes = [arg for mount in (f"{bench_snapshot()}:/bench:ro", f"{OUT}:/out", *mounts) for arg in ("-v", mount)]
    return ["docker", "run", "--rm", "--network", "none", *(["--cpuset-cpus", cpus] if cpus else []),
            *(["--memory", memory, "--memory-swap", memory] if memory else []), *volumes,
            "-e", f"PMB_IMAGE={image}@{image_id(image)}", image]


def sh(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def repo_dir(name: str) -> Path:
    return WORK / "repos" / name


def subject_image(name: str) -> str:
    head = sh("git", "-C", str(repo_dir(name)), "rev-parse", "--short=12", "HEAD").strip()
    return f"pymutbench-subject-{name}:{head}"


def build_base() -> None:
    subprocess.run(["docker", "build", "-t", BASE_IMAGE, str(BENCH)], check=True)


def pinned_head(name: str) -> str | None:
    """The commit of the subject's tree in the measured images (``bench/env/subjects.json``), if recorded."""
    path = ENV / "subjects.json"
    return json.loads(path.read_text(encoding="utf-8")).get(name, {}).get("head") if path.exists() else None


def clone(name: str) -> None:
    """The subject's clone, checked out at the recorded commit when there is one (a new clone otherwise stays at the
    default branch's head, and ``snapshot-env`` records it)."""
    repo = repo_dir(name)
    if not repo.exists():
        repo.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{SUBJECTS[name].repo}.git"
        subprocess.run(["git", "clone", "--quiet", url, str(repo)], check=True)
    head = pinned_head(name)
    if head:
        subprocess.run(["git", "-C", str(repo), "checkout", "--quiet", "--detach", head], check=True)


def build_subject(name: str) -> str:
    clone(name)
    subject = SUBJECTS[name]
    image = subject_image(name)
    install = " ".join(f"'{arg}'" for arg in subject.install)
    # The measured versions (``bench/env/subjects/<name>.txt``) constrain the install when they are recorded.
    pins = ENV / "subjects" / f"{name}.txt"
    constrain = "-c /tmp/pins.txt " if pins.exists() else ""
    dockerfile = (
        f"FROM {BASE_IMAGE}\n"
        "COPY . /work/repo\n"
        + ("COPY --from=pins pins.txt /tmp/pins.txt\n" if pins.exists() else "")
        + "RUN git -C /work/repo reset --hard --quiet && git -C /work/repo clean -fdxq\n"
        f"RUN uv venv -q /work/venv && cd /work/repo && uv pip install -q --python /work/venv/bin/python "
        f"{constrain}{install}\n"
        "RUN /work/venv/bin/python -m pip --version >/dev/null 2>&1 || true\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        context = Path(tmp)
        (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
        if pins.exists():
            (context / "pins").mkdir()
            shutil.copyfile(pins, context / "pins" / "pins.txt")
        extra = ["--build-context", f"pins={context / 'pins'}"] if pins.exists() else []
        subprocess.run(["docker", "build", "-q", "-t", image, "-f", str(context / "Dockerfile"), *extra,
                        str(repo_dir(name))], check=True)
    return image


# Every registered tool's environment: its directory name and its install requirements (``tools.toml``).
NATIVE_TOOLS: dict[str, tuple[str, ...]] = {tool.venv: tool.install for tool in REGISTRY.values()}


# Tools installed in the generation image for the census only (they do not run on CPython 3.14: docs/benchmark.md).
CENSUS_TOOLS: dict[str, tuple[str, ...]] = {"mutatest": ("mutatest==3.1.0", "setuptools"), "mutpy": ("mutpy==0.6.1",)}
TOOLS_IMAGE = "pymutbench-tools:1"


def build_tools() -> str:
    """The generation image: one environment per registered tool (and per census tool) under /tools, each with
    pytest, installed with the measured versions as constraints (``bench/env/tools``) and its freeze recorded in
    ``/tools/<venv>/freeze.txt``. Build the balamut wheel into ``bench/.wheels`` first (``uv build --wheel``)."""
    lines = [f"FROM {BASE_IMAGE}", "COPY .wheels /wheels", "COPY env/tools /pins"]
    for venv, specs in [*NATIVE_TOOLS.items(), *CENSUS_TOOLS.items()]:
        pins = ENV / "tools" / f"{venv}.txt"
        constrain = f"-c /pins/{venv}.txt " if pins.exists() else ""
        install = " ".join(f"'{spec}'" for spec in specs)
        lines.append(f"RUN uv venv -q /tools/{venv} && uv pip install -q --python /tools/{venv}/bin/python {constrain}"
                     f"{install} pytest && uv pip freeze --python /tools/{venv}/bin/python > /tools/{venv}/freeze.txt")
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as fh:
        fh.write("\n".join(lines) + "\n")
    subprocess.run(["docker", "build", "-q", "-t", TOOLS_IMAGE, "-f", fh.name, str(BENCH)], check=True)
    Path(fh.name).unlink()
    return TOOLS_IMAGE


def native_image(name: str) -> str:
    return subject_image(name).replace("pymutbench-subject-", "pymutbench-native-")


# Test dependencies a project's suite needs at HEAD beyond the subject's judging environment, installed into every
# tool environment and the reference one of the engine track. humanize's benchmark tests need a `benchmark`
# fixture: pytest-codspeed alone provides one that calls the function once (without pytest-benchmark, whose fixture
# calibrates many rounds, and without `--codspeed`), so they run as ordinary tests.
NATIVE_TEST_EXTRAS: dict[str, tuple[str, ...]] = {"humanize": ("pytest-codspeed",)}


def build_native(name: str) -> str:
    """The subject at HEAD in /work/run, installed editable into one environment per tool."""
    subject = SUBJECTS[name]
    install = " ".join("'-e' '.'" if arg == "." else f"'{arg}'"
                       for arg in (*subject.install, *NATIVE_TEST_EXTRAS.get(name, ())))
    pins = ENV / "native" / name  # the measured versions, one constraints file per environment, when recorded
    lines = [f"FROM {subject_image(name)}", "COPY .wheels /wheels",
             *([f"COPY env/native/{name} /pins"] if pins.exists() else []),
             "RUN cp -a /work/repo /work/run && git -C /work/run reset --hard -q && git -C /work/run clean -fdxq"]
    for venv, specs in NATIVE_TOOLS.items():
        tool = " ".join(f"'{spec}'" for spec in specs)
        constrain = f"-c /pins/{venv}.txt " if (pins / f"{venv}.txt").exists() else ""
        lines.append(f"RUN cd /work/run && uv venv -q /native/{venv} && "
                     # --no-config: a project's own uv settings (attrs: a one-week cooldown on new releases)
                     # must not keep a tool's pinned release out of its environment.
                     f"uv pip install --no-config -q --python /native/{venv}/bin/python {constrain}{install} {tool} "
                     "pytest pytest-cov")
    # The reference environment runs the project's own pytest configuration too, which may need pytest-cov
    # (python-semver's addopts pass --cov); every tool environment above has it, and the test extras.
    extras = " ".join(f"'{spec}'" for spec in NATIVE_TEST_EXTRAS.get(name, ()))
    constrain = "-c /pins/reference.txt " if (pins / "reference.txt").exists() else ""
    lines.append(f"RUN uv pip install --no-config -q --python /work/venv/bin/python {constrain}pytest-cov "
                 f"{extras}".rstrip())
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as fh:
        fh.write("\n".join(lines) + "\n")
    image = native_image(name)
    subprocess.run(["docker", "build", "-q", "-t", image, "-f", fh.name, str(BENCH)], check=True)
    Path(fh.name).unlink()
    return image


CONFORMANCE_IMAGE = "pymutbench-conformance:1"


def fixtures() -> list[str]:
    return sorted(p.name for p in (BENCH / "conformance").iterdir() if p.is_dir() and p.name.startswith("cf_"))


def trap_module(name: str) -> Path:
    """The file a trap fixture's campaigns mutate: its package's one module, or ``__init__.py`` when the package
    has no other."""
    package = BENCH / "conformance" / name / "src" / name
    return next(iter(sorted(package.glob("[!_]*.py"))), package / "__init__.py")


def build_conformance() -> str:
    """Every fixture as a git project under /work/cf, installed editable into one environment per tool; the
    reference interpreter in /work/venv has pytest only (the oracle imports each judged copy via PYTHONPATH)."""
    names = fixtures()
    editable = " ".join(f"'-e' '/work/cf/{name}'" for name in names)
    pins = ENV / "conformance"  # the measured versions, one constraints file per environment, when recorded

    def constrain(venv: str) -> str:
        return f"-c /pins/{venv}.txt " if (pins / f"{venv}.txt").exists() else ""

    lines = [f"FROM {BASE_IMAGE}", "COPY .wheels /wheels", "COPY conformance /work/cf",
             *(["COPY env/conformance /pins"] if pins.exists() else []),
             "RUN for d in /work/cf/cf_*; do git -C $d init -q -b main && git -C $d add -A && "
             "git -C $d commit -q -m fixture; done",
             "RUN uv venv -q /work/venv && uv pip install -q --python /work/venv/bin/python "
             f"{constrain('reference')}pytest"]
    for venv, specs in NATIVE_TOOLS.items():
        tool = " ".join(f"'{spec}'" for spec in specs)
        lines.append(f"RUN uv venv -q /native/{venv} && uv pip install -q --python /native/{venv}/bin/python "
                     f"{constrain(venv)}{editable} {tool} pytest pytest-cov")
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as fh:
        fh.write("\n".join(lines) + "\n")
    subprocess.run(["docker", "build", "-q", "-t", CONFORMANCE_IMAGE, "-f", fh.name, str(BENCH)], check=True)
    Path(fh.name).unlink()
    return CONFORMANCE_IMAGE


def add_fixtures(measured: str) -> list[str]:
    """Add the trap fixtures the trap image does not hold yet, without touching its environments: the image is kept
    under the tag ``measured`` (once) and rebuilt from itself with each new fixture as a git project under /work/cf,
    installed editable (without dependencies) into every tool environment and the reference one, which also gets
    pytest-cov for fixtures whose pytest configuration runs it. The measured fixtures' campaigns and verdicts stay
    as they are."""
    source = f"{CONFORMANCE_IMAGE.partition(':')[0]}:{measured}"
    if subprocess.run(["docker", "image", "inspect", source], capture_output=True, check=False).returncode:
        sh("docker", "tag", CONFORMANCE_IMAGE, source)
    probe = ["docker", "run", "--rm", "--network", "none", *(["--cpuset-cpus", CPUS] if CPUS else [])]
    present = set(sh(*probe, CONFORMANCE_IMAGE, "ls", "/work/cf").split())
    new = [name for name in fixtures() if name not in present]
    if not new:
        return []
    pins = ENV / "conformance" / "reference.txt"
    lines = [f"FROM {CONFORMANCE_IMAGE}", *(f"COPY conformance/{name} /work/cf/{name}" for name in new),
             *(["COPY env/conformance/reference.txt /tmp/reference.txt"] if pins.exists() else []),
             "RUN for d in " + " ".join(f"/work/cf/{name}" for name in new) + "; do git -C $d init -q -b main && "
             "git -C $d add -A && git -C $d commit -q -m fixture; done",
             "RUN uv pip install -q --python /work/venv/bin/python "
             + ("-c /tmp/reference.txt " if pins.exists() else "") + "pytest-cov"]
    editable = " ".join(f"'-e' '/work/cf/{name}'" for name in new)
    lines += [f"RUN uv pip install -q --no-deps --python /native/{venv}/bin/python {editable}" for venv in NATIVE_TOOLS]
    with tempfile.NamedTemporaryFile("w", suffix=".Dockerfile", delete=False) as fh:
        fh.write("\n".join(lines) + "\n")
    subprocess.run(["docker", "build", "-q", "-t", CONFORMANCE_IMAGE, "-f", fh.name, str(BENCH)], check=True)
    Path(fh.name).unlink()
    return new


def _distribution(requirement: str) -> str:
    """The normalized distribution name of a ``uv pip freeze`` line (``name==1.0``, ``name @ url``)."""
    return re.sub(r"[-_.]+", "-", re.split(r"[\s=@]", requirement, maxsplit=1)[0]).lower()


def refresh_tool(tool: str, measured: str, unpin: list[str]) -> None:
    """Install the release of ``tool`` in ``NATIVE_TOOLS`` into every image that holds an environment of it, with
    the rest of each image as measured. Each image is kept under the tag ``measured`` and rebuilt FROM it: the
    release goes into the tool's existing environment, every other installed distribution pinned to its version
    except ``unpin`` (the tool's own and the dependencies its release moves). The other environments, the reference
    one and the project trees stay as they were, so the release runs on the pytest the other tools ran on and the
    oracle judges in the environment it judged them in. A full rebuild would reinstall every environment with
    unpinned pytest."""
    from pymutbench.pipeline import ENGINE_SUBJECTS

    released = {_distribution(name) for name in unpin}
    specs = " ".join(f"'{spec}'" for spec in NATIVE_TOOLS[tool])
    targets = [(TOOLS_IMAGE, f"/tools/{tool}"), (CONFORMANCE_IMAGE, f"/native/{tool}"),
               *((native_image(name), f"/native/{tool}") for name in ENGINE_SUBJECTS)]
    probe = ["docker", "run", "--rm", "--network", "none", *(["--cpuset-cpus", CPUS] if CPUS else [])]
    for image, venv in targets:
        source = f"{image.partition(':')[0]}:{measured}"
        if subprocess.run(["docker", "image", "inspect", source], capture_output=True, check=False).returncode:
            sh("docker", "tag", image, source)
        before = sh(*probe, source, "uv", "pip", "freeze", "--python", f"{venv}/bin/python").splitlines()
        pins = [line for line in before if line and not line.startswith("-e ") and _distribution(line) not in released]
        with tempfile.TemporaryDirectory() as tmp:
            context = Path(tmp)
            shutil.copytree(BENCH / ".wheels", context / ".wheels")
            (context / "pins.txt").write_text("".join(f"{pin}\n" for pin in pins), encoding="utf-8")
            # build-tools records each generation environment in freeze.txt: it describes the new one.
            freeze = f"uv pip freeze --python {venv}/bin/python > {venv}/freeze.txt"
            (context / "Dockerfile").write_text(
                f"FROM {source}\nCOPY .wheels /wheels\nCOPY pins.txt /tmp/refresh-pins.txt\n"
                f"RUN uv pip install --no-config -q --python {venv}/bin/python -c /tmp/refresh-pins.txt {specs} "
                f"&& if [ -f {venv}/freeze.txt ]; then {freeze}; fi\n",
                encoding="utf-8")
            subprocess.run(["docker", "build", "-q", "-t", image, str(context)], check=True)
        after = sh(*probe, image, "uv", "pip", "freeze", "--python", f"{venv}/bin/python").splitlines()
        removed, added = sorted(set(before) - set(after)), sorted(set(after) - set(before))
        print(f"{image} {venv}: -{removed} +{added}", flush=True)


ENV = BENCH / "env"


def _frozen(lines: str) -> str:
    """A ``uv pip freeze`` output as a constraints file: every pinned distribution, without the editable and local
    installs (the project itself, a tool's wheel), which a constraints file cannot name."""
    kept = [line for line in lines.splitlines() if line and not line.startswith("-e ") and "@ file://" not in line]
    return "".join(f"{line}\n" for line in sorted(kept, key=str.lower))


def snapshot_env() -> None:
    """Record the versions of every environment of the measured images in ``bench/env``: per subject image its
    judging environment and the commit of its tree (``subjects.json``), per tool its generation environment, and per
    engine-track and trap image every tool environment and the reference one. A rebuild installs with these files
    as constraints (``build-subject``, ``build-native``, ``build-conformance``)."""
    from pymutbench.pipeline import ENGINE_SUBJECTS

    probe = ["docker", "run", "--rm", "--network", "none", *(["--cpuset-cpus", CPUS] if CPUS else [])]
    heads: dict[str, dict[str, str]] = {}
    for name in sorted(SUBJECTS):
        image = subject_image(name)
        out = sh(*probe, image, "sh", "-c", "git -C /work/repo rev-parse HEAD; "
                 "uv pip freeze --python /work/venv/bin/python").splitlines()
        heads[name] = {"head": out[0].strip(), "image": f"{image}@{image_id(image)}"}
        (ENV / "subjects").mkdir(parents=True, exist_ok=True)
        (ENV / "subjects" / f"{name}.txt").write_text(_frozen("\n".join(out[1:])), encoding="utf-8")
    (ENV / "subjects.json").write_text(json.dumps(heads, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    venvs = sorted(NATIVE_TOOLS)
    (ENV / "tools").mkdir(parents=True, exist_ok=True)
    listing = sh(*probe, TOOLS_IMAGE, "sh", "-c", 'for d in /tools/*/; do n=$(basename $d); echo "### $n"; '
                 "cat $d/freeze.txt; done")
    for block in listing.split("### ")[1:]:
        venv, _, body = block.partition("\n")
        (ENV / "tools" / f"{venv.strip()}.txt").write_text(_frozen(body), encoding="utf-8")
    for image, folder in [*((native_image(name), ENV / "native" / name) for name in ENGINE_SUBJECTS),
                          (CONFORMANCE_IMAGE, ENV / "conformance")]:
        folder.mkdir(parents=True, exist_ok=True)
        script = "; ".join([*(f"echo '### {venv}'; uv pip freeze --python /native/{venv}/bin/python" for venv in venvs),
                            "echo '### reference'; uv pip freeze --python /work/venv/bin/python"])
        for block in sh(*probe, image, "sh", "-c", script).split("### ")[1:]:
            venv, _, body = block.partition("\n")
            (folder / f"{venv.strip()}.txt").write_text(_frozen(body), encoding="utf-8")
    print(f"wrote {ENV}")


def run_in_subject(name: str, argv: list[str], *, network: bool = False, cpus: float = 0) -> int:
    image = subject_image(name)
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "--rm", "-v", f"{bench_snapshot()}:/bench:ro", "-v", f"{OUT}:/out"]
    if not network:
        cmd += ["--network", "none"]
    if cpus:
        cmd += ["--cpus", str(cpus)]
    cmd += [image, *argv]
    return subprocess.run(cmd, check=False).returncode


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build-base")
    p = sub.add_parser("build-subject")
    p.add_argument("names", nargs="+")
    sub.add_parser("build-conformance")
    sub.add_parser("build-tools", help="the generation image, from tools.toml")
    p = sub.add_parser("add-fixtures", help="add new trap fixtures to the trap image, keeping its environments")
    p.add_argument("--measured", required=True, help="the tag that keeps the trap image as measured")
    sub.add_parser("snapshot-env", help="record the versions of every environment of the measured images")
    p = sub.add_parser("build-native")
    p.add_argument("names", nargs="+")
    p = sub.add_parser("refresh-tool", help="install a tool's release from NATIVE_TOOLS into the measured images")
    p.add_argument("tool", choices=sorted(NATIVE_TOOLS))
    p.add_argument("--measured", required=True, help="the tag that keeps the images as measured (balamut-0.1.2)")
    p.add_argument("--unpin", nargs="+", required=True,
                   help="distributions free to change: the tool's own and the dependencies its release moves")
    p = sub.add_parser("validate-all", help="validate many subjects, several containers at a time")
    p.add_argument("names", nargs="*")
    p.add_argument("--containers", type=int, default=4)
    p.add_argument("--jobs", type=int, default=3)
    p.add_argument("--stage", default="validate", help="output directory under $PMB_OUT_DIR")
    p.add_argument("--recheck", default="", help="re-validate only the candidates admitted in this stage's output")
    p.add_argument("--only", default="", help="comma-separated commit prefixes (one subject)")
    p.add_argument("--list", action="store_true", help="only write each subject's candidate list")
    p = sub.add_parser("validate")
    p.add_argument("name")
    p.add_argument("--jobs", type=int, default=3)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--only", default="")
    p.add_argument("--max-src-lines", type=int, default=40)
    args = parser.parse_args()
    match args.cmd:
        case "build-base":
            build_base()
        case "build-subject":
            for name in args.names:
                print(build_subject(name), flush=True)
        case "build-conformance":
            print(build_conformance(), flush=True)
        case "build-tools":
            print(build_tools(), flush=True)
        case "add-fixtures":
            print("added", add_fixtures(args.measured), flush=True)
        case "snapshot-env":
            snapshot_env()
        case "build-native":
            for name in args.names:
                print(build_native(name), flush=True)
        case "refresh-tool":
            refresh_tool(args.tool, args.measured, args.unpin)
        case "validate-all":
            names = args.names or sorted(SUBJECTS)
            (OUT / args.stage).mkdir(parents=True, exist_ok=True)

            def one(name: str) -> tuple[str, int]:
                argv = ["python3", "/bench/pymutbench/validate.py", "--subject", name, "--jobs", str(args.jobs),
                        "--out", f"/out/{args.stage}/{name}.jsonl"]
                only = [prefix for prefix in args.only.split(",") if prefix]
                if args.recheck:
                    source = OUT / args.recheck / f"{name}.jsonl"
                    text = source.read_text(encoding="utf-8")
                    rows = [json.loads(line) for line in text.splitlines() if line.strip()]
                    admitted = [row["commit"][:12] for row in rows if row.get("admitted")]
                    if only:  # re-validate the named admissions only
                        admitted = [commit for commit in admitted if commit.startswith(tuple(only))]
                    if not admitted:
                        return name, 0
                    only = admitted
                if only:
                    argv += ["--only", ",".join(only)]
                if args.list:
                    argv.append("--list")
                log = (OUT / args.stage / f"{name}.log").open("a", encoding="utf-8")
                cmd = [*docker_run(subject_image(name)), *argv]
                return name, subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, check=False).returncode

            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(args.containers) as pool:
                for name, code in pool.map(one, names):
                    print(f"{name}: exit {code}", flush=True)
        case "validate":
            argv = ["python3", "/bench/pymutbench/validate.py", "--subject", args.name, "--jobs", str(args.jobs),
                    "--out", f"/out/validate/{args.name}.jsonl", "--max-src-lines", str(args.max_src_lines)]
            if args.limit:
                argv += ["--limit", str(args.limit)]
            if args.only:
                argv += ["--only", args.only]
            (OUT / "validate").mkdir(parents=True, exist_ok=True)
            raise SystemExit(run_in_subject(args.name, argv))
        case _:
            raise AssertionError(args.cmd)


if __name__ == "__main__":
    main()
