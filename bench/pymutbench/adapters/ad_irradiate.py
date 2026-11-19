"""irradiate: the function variants ``irradiate run`` writes to ``mutants/`` before it executes anything.

Generation happens before the harness validation, so a run with a test directory that cannot validate the
harness still leaves the complete trampolined module. Each ``…__irradiate_N`` variant replaces the ``def`` it
was generated from (renamed, re-indented, decorators kept) in the original module; the materialization is
accepted only if the spliced function parses to the variant's own tree. A body that defines the name more than
once (``@overload`` stubs, a property's accessors) takes the variant in the definition closest to it.

The generated module holds a copy of a function per mutant, so it is never parsed whole: it is cut into its
top-level statements, and only a statement that holds a variant of a function in the bug's scope is parsed, on
its own. irradiate can write a statement that does not parse (a trampoline whose signature lost a ``lambda``
default's body); it holds no variant and is never read, and a variant that does not parse is recorded as
unmaterializable.
"""

from __future__ import annotations

import ast
import io
import re
import shutil
import sys
import tempfile
import tokenize
from difflib import SequenceMatcher
from pathlib import Path

from _common import emit, parse_args, run_tool  # type: ignore[import-not-found]

SEP = "ǁ"
_VARIANT = re.compile(r"^x(?:_(?P<func>.+)|ǁ(?P<cls>[^ǁ]+)ǁ(?P<meth>.+))__irradiate_(?P<n>\d+)$")


_VARIANT_DEF = re.compile(r"def\s+(x\S+?__irradiate_\d+)\s*\(")


def _statement_starts(text: str) -> list[int]:
    """The first lines (1-based) of the top-level statements, a decorator joined to the statement it decorates."""
    starts: list[int] = []
    depth, line_start, pending = 0, True, None
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.INDENT:
            depth += 1
        elif token.type == tokenize.DEDENT:
            depth -= 1
        elif token.type == tokenize.NEWLINE:
            line_start = True
        elif token.type in {tokenize.NL, tokenize.COMMENT, tokenize.ENCODING, tokenize.ENDMARKER}:
            continue
        elif line_start:
            line_start = False
            if depth == 0:
                pending = pending or token.start[0]
                if not (token.type == tokenize.OP and token.string == "@"):
                    starts.append(pending)
                    pending = None
    return starts


def _physical_lines(text: str) -> list[str]:
    """Lines ended by newline characters only, as the tokenizer and the parser number them (str.splitlines also
    breaks at U+2028 and other separators, which can occur inside string literals)."""
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _chunks(source: str) -> list[str]:
    """The top-level statements of a module as texts, each with the lines up to the next statement."""
    lines = _physical_lines(source)
    starts = _statement_starts(source)
    return ["".join(lines[a - 1 : b - 1]) for a, b in zip(starts, [*starts[1:], len(lines) + 1], strict=True)]


def _variants(tree: ast.Module) -> list[tuple[str | None, str, int, ast.FunctionDef | ast.AsyncFunctionDef]]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            match = _VARIANT.match(node.name)
            if match:
                if match.group("func") is not None:
                    found.append((None, match.group("func"), int(match.group("n")), node))
                else:
                    found.append((match.group("cls"), match.group("meth"), int(match.group("n")), node))
    return found


def _targets(tree: ast.Module, cls: str | None, name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every ``def`` of ``name`` in the module's or the class's body, in source order."""
    body = tree.body
    if cls is not None:
        owner = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls), None)
        if owner is None:
            return []
        body = owner.body
    return [n for n in body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and n.name == name]


def _target(tree: ast.Module, cls: str | None, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """The ``def`` the name is bound to: the last of its definitions (an ``@overload`` stub comes before it)."""
    found = _targets(tree, cls, name)
    return found[-1] if found else None


def _origin(candidates: list[ast.FunctionDef | ast.AsyncFunctionDef], variant: ast.FunctionDef | ast.AsyncFunctionDef,
            name: str) -> int:
    """Which of a body's definitions of one name a variant was generated from: the one whose tree is closest to the
    variant's (a mutant differs from its original by one edit, and from an ``@overload`` stub or a property's other
    accessor by much more); a tie goes to the later definition, the one a call reaches."""
    if len(candidates) == 1:
        return 0
    shape = _renamed_dump(variant, name, indent=0).splitlines()
    scores = [SequenceMatcher(None, _renamed_dump(c, name, indent=0).splitlines(), shape, autojunk=False).ratio()
              for c in candidates]
    return max(range(len(candidates)), key=lambda i: (scores[i], i))


def splice(lines: list[str], target: ast.FunctionDef | ast.AsyncFunctionDef, base: int, text: str) -> str:
    """The module ``lines`` with the ``def`` of ``target`` (decorators kept) replaced by a variant's ``text``,
    renamed already: the variant's lines after the first lose the variant's own indentation ``base`` and take the
    indentation of the original ``def`` line, characters included, so a tab-indented class keeps its tabs."""
    indent = lines[target.lineno - 1][: target.col_offset]
    body = text.split("\n")
    body = [body[0].lstrip(), *(indent + line[base:] if line.strip() else "" for line in body[1:])]
    start, end = target.lineno - 1, target.end_lineno or target.lineno
    return "".join(lines[:start]) + indent + "\n".join(body) + "\n" + "".join(lines[end:])


def _renamed_dump(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str, indent: int | None = None) -> str:
    clone = ast.parse(ast.unparse(node)).body[0]
    clone.name = name  # type: ignore[attr-defined]
    clone.decorator_list = []  # type: ignore[attr-defined]
    return ast.dump(clone, include_attributes=False, indent=indent)


def main() -> None:
    root, files, out, scope = parse_args()
    irradiate = str(Path(sys.executable).parent / "irradiate")
    with out.open("w", encoding="utf-8") as fh:
        for rel in files:
            source = (root / rel).read_text(encoding="utf-8")
            original = ast.parse(source)
            lines = _physical_lines(source)
            with tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                (project / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / rel, project / rel)
                # irradiate names modules by package: every directory on the way needs its __init__.py.
                parts = Path(rel).parts[:-1]
                for depth in range(1, len(parts) + 1):
                    if parts[:depth] == ("src",):
                        continue
                    init = project.joinpath(*parts[:depth], "__init__.py")
                    if not init.exists():
                        init.write_text("", encoding="utf-8")
                top = rel.split("/")[0]
                tests = project / "pmb_no_tests"
                tests.mkdir()
                (tests / "test_nothing.py").write_text("def test_nothing():\n    pass\n", encoding="utf-8")
                # The run itself fails by design (the harness cannot validate); only its generation matters.
                proc = run_tool([irradiate, "run", top, "--tests-dir", tests.name, "--python", sys.executable],
                                cwd=project, text=True, timeout=1800)
                tail = rel.removeprefix("src/")
                generated_path = project / "mutants" / tail
                if not generated_path.exists():  # the tool generated nothing for the file: the adapter fails
                    raise RuntimeError(f"irradiate wrote no mutants/{tail}: rc={proc.returncode} {proc.stderr[-500:]}")
                generated_source = generated_path.read_text(encoding="utf-8")
            for chunk in _chunks(generated_source):
                held = [(raw, _VARIANT.match(raw)) for raw in _VARIANT_DEF.findall(chunk)]
                wanted = {}
                for raw, match in held:
                    cls, name = (match.group("cls"), match.group("meth") or match.group("func")) if match else (None, raw)
                    candidates = _targets(original, cls, name)
                    if not candidates or any(scope.overlaps(rel, c.lineno, c.end_lineno or c.lineno) for c in candidates):
                        wanted[raw] = candidates
                if not wanted:
                    continue
                try:
                    tree = ast.parse(chunk)
                except SyntaxError:
                    for raw, candidates in wanted.items():
                        match = _VARIANT.match(raw)
                        native = (f"{match.group('cls') + '.' if match and match.group('cls') else ''}"
                                  f"{(match.group('meth') or match.group('func')) if match else raw}"
                                  f"__irradiate_{match.group('n') if match else '?'}")
                        emit(fh, rel, native, "", None, status="generated_variant_does_not_parse",
                             line=candidates[-1].lineno if candidates else None)
                    continue
                for cls, name, number, variant in _variants(tree):
                    if variant.name not in wanted:
                        continue
                    native = f"{cls + '.' if cls else ''}{name}__irradiate_{number}"
                    candidates = wanted[variant.name]
                    if not candidates:
                        emit(fh, rel, native, "", None, status="original_function_not_found")
                        continue
                    index = _origin(candidates, variant, name)
                    target = candidates[index]
                    if not scope.overlaps(rel, target.lineno, target.end_lineno or target.lineno):
                        continue
                    text = ast.get_source_segment(chunk, variant) or ""
                    text = re.sub(r"^((?:async\s+)?def\s+)" + re.escape(variant.name), r"\g<1>" + name, text, count=1)
                    spliced = splice(lines, target, variant.col_offset, text)
                    try:
                        after = _targets(ast.parse(spliced), cls, name)
                    except SyntaxError:
                        after = []
                    check = after[index] if len(after) == len(candidates) else None
                    if check is None or _renamed_dump(check, name) != _renamed_dump(variant, name):
                        # An emission of the tool all the same: its line charges it to the in-scope budget.
                        emit(fh, rel, native, "", None, status="no_faithful_text", line=target.lineno)
                        continue
                    emit(fh, rel, native, "", spliced, line=target.lineno)


if __name__ == "__main__":
    main()
