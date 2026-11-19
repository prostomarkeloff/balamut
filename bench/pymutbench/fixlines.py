"""Where a fix changed a file, where a mutant of the fixed file lies relative to that change, and the same mutant
written into the buggy file.

``git diff --no-renames -U0 <parent> <commit> -- <path>`` splits a fix into hunks: a hunk that removes no line
inserts new code, any other rewrites the lines it adds. A mutant **touches** the lines of the fixed file whose
stripped text it changes or deletes (a multi-line edit is compared line by line, so a function variant that only
re-indents a docstring touches the lines it really changes); an insertion touches the line it lands on. A mutant is

* ``rewritten`` when it touches a rewritten line,
* else ``inserted`` when it touches an inserted line (code that did not exist before the fix),
* else ``unchanged``.

An ``unchanged`` mutant exists in the buggy file too: ``transplant`` writes its changed lines into the buggy file
at the place the diff maps them to.

A scope region holds no change of the program (``noise``) when it is an import statement, an assignment of module
metadata (``__version__``, ``__credits__``, …) or a definition whose code, its syntax tree without docstrings, is
the same before and after the fix (the fix edited only comments, docstrings or formatting there).
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
CLASSES = ("unchanged", "rewritten", "inserted")  # best first: a bug's class for a tool is its best witness's


@dataclass(frozen=True, slots=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def new_lines(self) -> range:
        return range(self.new_start, self.new_start + self.new_count)


def hunks(diff_u0: str) -> list[Hunk]:
    """The hunks of a ``-U0`` diff of one file, in order (a count of 0 puts the start before the gap)."""
    return [
        Hunk(int(m.group(1)), int(m.group(2) or "1"), int(m.group(3)), int(m.group(4) or "1"))
        for m in _HUNK.finditer(diff_u0)
    ]


def fix_lines(diff_u0: str) -> tuple[set[int], set[int]]:
    """The lines of the fixed file that the fix inserted as new code and the lines it rewrote."""
    inserted: set[int] = set()
    rewritten: set[int] = set()
    for hunk in hunks(diff_u0):
        (inserted if hunk.old_count == 0 else rewritten).update(hunk.new_lines)
    return inserted, rewritten


def buggy_lines(diff_u0: str) -> set[int]:
    """The lines of the buggy file the fix removed or rewrote; an insertion marks the buggy lines around it."""
    lines: set[int] = set()
    for hunk in hunks(diff_u0):
        lines.update(
            range(hunk.old_start, hunk.old_start + hunk.old_count)
            if hunk.old_count
            else {max(1, hunk.old_start), hunk.old_start + 1}
        )
    return lines


def composition(diffs: list[str]) -> str:
    """What a fix's hunks do over all its files: ``insertion`` (every hunk only adds lines), ``rewrite`` (every hunk
    removes lines, deletions included) or ``mixed``."""
    found = {"insertion" if h.old_count == 0 else "rewrite" for diff in diffs for h in hunks(diff)}
    return found.pop() if len(found) == 1 else "mixed" if found else "none"


def touched_lines(original: bytes, start: int, end: int, replacement: str) -> set[int]:
    """Lines of ``original`` whose stripped text the edit changes or deletes; an insertion marks the line it lands
    on."""
    first = original.count(b"\n", 0, start) + 1
    last = original.count(b"\n", 0, max(start, end - 1)) + 1
    if last <= first:
        return {first}
    mutated = original[:start] + replacement.encode("utf-8") + original[end:]
    before, after = original.split(b"\n"), mutated.split(b"\n")
    delta = len(after) - len(before)
    old = [line.strip() for line in before[first - 1 : last]]
    new = [line.strip() for line in after[first - 1 : last + delta]]
    touched: set[int] = set()
    for tag, i1, i2, _j1, _j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        if i2 > i1:
            touched.update(range(first + i1, first + i2))
        else:
            touched.add(min(last, first + i1))
    return touched or {first}


def classify(touched: set[int], inserted: set[int], rewritten: set[int]) -> str:
    if touched & rewritten:
        return "rewritten"
    if touched & inserted:
        return "inserted"
    return "unchanged"


def best(classes: set[str]) -> str | None:
    return next((cls for cls in CLASSES if cls in classes), None)


def to_parent_line(line: int, file_hunks: list[Hunk]) -> int | None:
    """The buggy file's line that an unchanged line of the fixed file was, or None for a line a hunk added."""
    shift = 0
    for hunk in file_hunks:
        if hunk.new_count and hunk.new_start <= line < hunk.new_start + hunk.new_count:
            return None
        # A hunk that ends before the line moves it by what the hunk removed minus what it added.
        end = hunk.new_start + hunk.new_count if hunk.new_count else hunk.new_start + 1
        if end <= line:
            shift += hunk.old_count - hunk.new_count
    return line + shift


def _indent(line: bytes) -> bytes:
    return line[: len(line) - len(line.lstrip())]


def transplant(
    fixed: bytes, start: int, end: int, replacement: str, parent: bytes, file_hunks: list[Hunk]
) -> bytes | None:
    """The buggy file with the mutant's changed lines written at the lines the diff maps them to, or None when a
    changed line is one the fix added or rewrote, or when the mapped lines differ.

    Lines are compared stripped, as ``touched_lines`` compares them: a line whose text the mutant does not change
    stays as the buggy file has it (a function variant that re-indents the whole body changes only its touched
    lines here). Every run of lines the mutant changes must be lines of the fixed file outside every hunk,
    consecutive in the buggy file and equal there; the new lines take the buggy line's indentation plus their own
    indentation relative to the run's first new line. A run that only inserts lines lands between two such lines,
    which are the same bytes in both files: its lines keep their own indentation, less the indentation the mutant
    adds to every line it keeps (a re-indented variant)."""
    first = fixed.count(b"\n", 0, start) + 1
    last = max(first, fixed.count(b"\n", 0, max(start, end - 1)) + 1)
    mutated = fixed[:start] + replacement.encode("utf-8") + fixed[end:]
    before, after = fixed.split(b"\n"), mutated.split(b"\n")
    old = before[first - 1 : last]
    new = after[first - 1 : last + len(after) - len(before)]
    base = parent.split(b"\n")
    replacements: list[tuple[int, int, list[bytes]]] = []  # (buggy line index, lines replaced, new lines)
    stripped_old, stripped_new = [line.strip() for line in old], [line.strip() for line in new]
    opcodes = difflib.SequenceMatcher(None, stripped_old, stripped_new, autojunk=False).get_opcodes()
    # The indentation the mutant adds to a kept line (old index, new index): a re-indented body shifts every line.
    kept = [
        (i1 + offset, j1 + offset)
        for tag, i1, i2, j1, _j2 in opcodes
        if tag == "equal"
        for offset in range(i2 - i1)
        if stripped_old[i1 + offset]
    ]
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        lines = list(range(first + i1, first + i2))  # fixed lines replaced; none for a pure insertion
        if lines:
            mapped = [to_parent_line(line, file_hunks) for line in lines]
            if any(m is None for m in mapped):
                return None
            targets = [m for m in mapped if m is not None]
            if targets != list(range(targets[0], targets[0] + len(targets))) or targets[-1] > len(base):
                return None  # a hunk lies inside the run, or the run leaves the buggy file
            if [base[m - 1].strip() for m in targets] != stripped_old[i1:i2]:
                return None
            anchor = base[targets[0] - 1]
            index, count = targets[0] - 1, len(targets)
        else:
            # A pure insertion before fixed line ``first + i1``: that line and the one above must map adjacently.
            below = to_parent_line(first + i1, file_hunks)
            above = to_parent_line(first + i1 - 1, file_hunks) if first + i1 > 1 else 0
            if below is None or above is None or below != above + 1 or below > len(base) + 1:
                return None
            inserted = new[j1:j2]
            # The nearest kept line above the insertion, else below it, gives the shift of its block.
            near = [pair for pair in kept if pair[1] < j1][-1:] or [pair for pair in kept if pair[1] >= j2][:1]
            shift = len(_indent(new[near[0][1]])) - len(_indent(old[near[0][0]])) if near else 0
            if shift < 0 or any(line.strip() and len(_indent(line)) < shift for line in inserted):
                return None
            replacements.append((below - 1, 0, [line[shift:] if line.strip() else line for line in inserted]))
            continue
        own = _indent(new[j1]) if j2 > j1 else b""
        lines_new = [
            _indent(anchor) + line[len(own) :] if line.strip() and line.startswith(own) else line for line in new[j1:j2]
        ]
        replacements.append((index, count, lines_new))
    if not replacements:
        return None
    out = list(base)
    for index, count, lines_new in sorted(replacements, reverse=True):
        out[index : index + count] = lines_new
    result = b"\n".join(out)
    return result if result != parent else None


METADATA = frozenset(
    {
        "__version__",
        "__version_info__",
        "__credits__",
        "__author__",
        "__authors__",
        "__copyright__",
        "__license__",
        "__email__",
        "__maintainer__",
        "__status__",
        "__date__",
    }
)


def _parse_region(text: str) -> ast.stmt | None:
    """The one statement of a region's text; an indented region is parsed inside ``if True:``, so continuation and
    string lines keep their columns."""
    try:
        tree = ast.parse(text if not text[:1].isspace() else "if True:\n" + text)
    except SyntaxError:
        return None
    body = tree.body
    if body and text[:1].isspace() and isinstance(body[0], ast.If):
        body = body[0].body
    return body[0] if len(body) == 1 else None


def _code(node: ast.stmt) -> str:
    """The statement's syntax tree without positions and without docstrings."""
    clone = ast.parse(ast.unparse(node)).body[0]
    for item in ast.walk(clone):
        body = getattr(item, "body", None)
        if (
            isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            item.body = body[1:] or [ast.Pass()]
    return ast.dump(clone, include_attributes=False)


def buggy_region(fixed: list[str], buggy: list[str], file_hunks: list[Hunk], start: int, end: int) -> list[str] | None:
    """The buggy file's text of the fixed region ``start``..``end``: its lines with every hunk inside it replaced by
    the hunk's old side, or None when a hunk crosses the region's edge."""
    out: list[str] = []
    line = start
    while line <= end:
        hunk = next((h for h in file_hunks if h.new_count and h.new_start == line), None)
        if hunk is not None:
            if hunk.new_start + hunk.new_count - 1 > end:
                return None
            out += buggy[hunk.old_start - 1 : hunk.old_start - 1 + hunk.old_count]
            line += hunk.new_count
            continue
        if any(h.new_count and h.new_start < line < h.new_start + h.new_count for h in file_hunks):
            return None
        out.append(fixed[line - 1])
        deletion = next((h for h in file_hunks if not h.new_count and h.new_start == line), None)
        if deletion is not None and line < end:
            out += buggy[deletion.old_start - 1 : deletion.old_start - 1 + deletion.old_count]
        line += 1
    return out


def noise(fixed: str, buggy: str | None, diff_u0: str, start: int, end: int) -> str | None:
    """Why the fixed region ``start``..``end`` holds no change of the program (``import``, ``metadata``,
    ``no code change``), or None."""
    fixed_lines = fixed.split("\n")
    node = _parse_region("\n".join(fixed_lines[start - 1 : end]))
    if node is None:
        return None
    if isinstance(node, ast.Import | ast.ImportFrom):
        return "import"
    targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
    if targets and all(isinstance(t, ast.Name) and t.id in METADATA for t in targets):
        return "metadata"
    if buggy is None:
        return None
    old = buggy_region(fixed_lines, buggy.split("\n"), hunks(diff_u0), start, end)
    old_node = _parse_region("\n".join(old)) if old is not None else None
    if old_node is not None and _code(old_node) == _code(node):
        return "no code change"
    return None
