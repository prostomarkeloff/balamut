"""Static SVG charts for the report, one light and one dark file per figure (stdlib only).

The figures follow one system: the system sans, hairline recessive axes, bars at most 24px thick with a 4px
rounded data end and a square baseline, a 2px surface gap between touching segments, text in text tokens only
(never in a series color), values at the bar tip, and every mark with a ``<title>`` for hover in a browser.
Tool comparisons use the emphasis form: the benchmark's subject of interest in the accent hue, every other tool
in the de-emphasis gray, so no categorical hue is spent on identity that the row label already carries. The
three categorical slots (accent, second, third) were checked with the dataviz palette validator in both modes.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass

FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


@dataclass(frozen=True, slots=True)
class Theme:
    name: str
    surface: str
    ink: str
    ink2: str
    muted: str
    grid: str
    axis: str
    accent: str
    second: str
    third: str
    gray: str
    gray_light: str
    ramp: tuple[str, ...]


LIGHT = Theme("light", "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7",
              "#2a78d6", "#eb6834", "#1baf7a", "#a8a69e", "#d7d5cd",
              ("#f0efec", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"))
DARK = Theme("dark", "#1a1a19", "#f0efec", "#c3c2b7", "#898781", "#2c2c2a", "#383835",
             "#3987e5", "#d95926", "#199e70", "#6b6a64", "#45443f",
             ("#262624", "#0d366b", "#184f95", "#256abf", "#2a78d6", "#3987e5", "#6da7ec", "#9ec5f4"))
THEMES = (LIGHT, DARK)


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _luminance(color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` color: picks the readable ink for a filled cell."""
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _text(x: float, y: float, text: str, *, fill: str, size: int = 12, anchor: str = "start", weight: int = 400,
          baseline: str = "middle") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" font-weight="{weight}" '
            f'text-anchor="{anchor}" dominant-baseline="{baseline}">{_esc(text)}</text>')


def _bar(x: float, y: float, width: float, height: float, fill: str, title: str, radius: float = 4.0) -> str:
    """A horizontal bar growing from ``x``: square at the baseline, rounded at the data end."""
    if width <= 0:
        return ""
    r = min(radius, width, height / 2)
    path = (f"M{x:.2f},{y:.2f} H{x + width - r:.2f} Q{x + width:.2f},{y:.2f} {x + width:.2f},{y + r:.2f} "
            f"V{y + height - r:.2f} Q{x + width:.2f},{y + height:.2f} {x + width - r:.2f},{y + height:.2f} "
            f"H{x:.2f} Z")
    return f'<path d="{path}" fill="{fill}"><title>{_esc(title)}</title></path>'


CHAR_PX = 6.4  # 12px system sans: about 6.4px per character, an overestimate that keeps text inside the frame


def _pack(pieces: list[str], joiner: str, limit: int) -> list[str]:
    """Pieces packed greedily into lines of at most ``limit`` characters (a longer piece keeps its own line)."""
    lines: list[str] = []
    for piece in pieces:
        if lines and len(lines[-1]) + len(joiner) + len(piece) <= limit:
            lines[-1] += joiner + piece
        else:
            lines.append(piece)
    return lines


def _wrap(text: str, width: float) -> list[str]:
    """Lines of a 12px text at most ``width`` wide (by ``CHAR_PX``): broken at its `` · `` separators when that
    takes no more lines than breaking between words."""
    limit = max(1, int(width / CHAR_PX))
    by_word = _pack(text.split(" "), " ", limit)
    by_part = _pack(text.split(" · "), " · ", limit)
    fits = all(len(line) <= limit for line in by_part)
    return by_part if fits and len(by_part) <= len(by_word) else by_word


def _frame(width: int, height: int, theme: Theme, body: list[str], title: str, subtitle: str) -> str:
    lines = _wrap(subtitle, width - 48) if subtitle else []
    shift = 16 * max(0, len(lines) - 1)  # each further subtitle line moves the chart down
    height += shift
    head = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'font-family=\'{FONT}\' role="img" aria-label="{_esc(title)}">',
        f"<title>{_esc(title)}</title>",
        f'<rect width="{width}" height="{height}" fill="{theme.surface}" rx="8"/>',
        _text(24, 28, title, fill=theme.ink, size=15, weight=600),
        *(_text(24, 48 + 16 * index, line, fill=theme.ink2, size=12) for index, line in enumerate(lines)),
    ]
    content = [f'<g transform="translate(0,{shift})">', *body, "</g>"] if shift else body
    return "\n".join([*head, *content, "</svg>"]) + "\n"


@dataclass(frozen=True, slots=True)
class Row:
    label: str
    value: float
    low: float | None = None
    high: float | None = None
    emphasis: bool = False
    note: str = ""  # a short text after the value (e.g. "n/a: does not run")


def nice_scale(peak: float, *, share: bool = True) -> tuple[float, tuple[float, ...]]:
    """A zero-based axis that ends at the first round step (1, 2, 2.5 or 5 times a power of ten) at or above
    ``peak``, with at most six ticks; a share axis never ends above 100%."""
    peak = max(peak, 1e-9)
    magnitude = 10 ** math.floor(math.log10(peak / 5))
    for step in (magnitude * m for m in (1, 2, 2.5, 5, 10, 20, 25, 50)):
        top = max(step, math.ceil(peak / step - 1e-9) * step)
        if top / step <= 5:
            if share and top >= 1.0:
                return 1.0, (0, 0.25, 0.5, 0.75, 1.0)
            return top, tuple(round(step * i, 10) for i in range(round(top / step) + 1))
    return peak, (0, peak)


def ranked_bars(title: str, subtitle: str, rows: list[Row], theme: Theme, *, scale_max: float | None = None,
                percent: bool = True, ticks: tuple[float, ...] | None = None, unit: str = "") -> str:
    """Ranked horizontal bars with optional interval whiskers; the axis starts at zero and, unless given, ends
    at the first round step above the largest value or interval end. Without ``percent`` the values are plain
    numbers with ``unit`` after them."""
    if scale_max is None or ticks is None:
        peak = max((max(r.value, r.high or 0.0) for r in rows if not math.isnan(r.value)), default=1.0)
        scale_max, ticks = nice_scale(peak, share=percent)
    width, left, right, top, row_h, bar_h = 760, 168, 120, 72, 30, 14
    plot_w = width - left - right
    height = top + row_h * len(rows) + 40
    body: list[str] = []
    for tick in ticks:
        x = left + plot_w * tick / scale_max
        body.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{top + row_h * len(rows)}" '
                    f'stroke="{theme.grid}" stroke-width="1"/>')
        label = f"{tick * 100:.0f}%" if percent else _number(tick, unit)
        body.append(_text(x, top + row_h * len(rows) + 16, label, fill=theme.muted, size=11, anchor="middle"))
    body.append(f'<line x1="{left}" y1="{top - 8}" x2="{left}" y2="{top + row_h * len(rows)}" '
                f'stroke="{theme.axis}" stroke-width="1"/>')
    for index, row in enumerate(rows):
        y = top + index * row_h
        cy = y + row_h / 2
        color = theme.accent if row.emphasis else theme.gray
        weight = 600 if row.emphasis else 400
        body.append(_text(left - 12, cy, row.label, fill=theme.ink if row.emphasis else theme.ink2, anchor="end",
                          weight=weight))
        value_text = (_percent(row.value) if percent else _number(row.value, unit)) if not math.isnan(row.value) else ""
        if math.isnan(row.value):
            body.append(_text(left + 8, cy, row.note or "n/a", fill=theme.muted, size=12))
            continue
        bar_w = plot_w * max(0.0, row.value) / scale_max
        tip = f"{row.label}: {value_text}"
        if row.low is not None and row.high is not None:
            tip += (f" (95% CI {row.low * 100:.0f}–{row.high * 100:.0f}%)" if percent
                    else f" ({row.low:.2f}–{row.high:.2f})")
        body.append(_bar(left, cy - bar_h / 2, bar_w, bar_h, color, tip))
        end = bar_w
        if row.low is not None and row.high is not None:
            x1, x2 = left + plot_w * row.low / scale_max, left + plot_w * row.high / scale_max
            body.append(f'<line x1="{x1:.1f}" y1="{cy:.1f}" x2="{x2:.1f}" y2="{cy:.1f}" stroke="{theme.ink2}" '
                        f'stroke-width="1"><title>{_esc(tip)}</title></line>')
            body += [f'<line x1="{x:.1f}" y1="{cy - 4:.1f}" x2="{x:.1f}" y2="{cy + 4:.1f}" '
                     f'stroke="{theme.ink2}" stroke-width="1"/>' for x in (x1, x2)]
            end = max(end, x2 - left)
        body.append(_text(left + end + 8, cy, value_text + (f"  {row.note}" if row.note else ""),
                          fill=theme.ink if row.emphasis else theme.ink2, weight=weight))
    return _frame(width, height, theme, body, title, subtitle)


def _percent(value: float) -> str:
    """A share label in whole percent; a nonzero share that rounds to 0% keeps one decimal place, so a visible bar
    never reads 0%."""
    if 0 < value < 0.0005:
        return "<0.1%"
    return f"{value * 100:.1f}%" if 0 < value < 0.005 else f"{value * 100:.0f}%"


def _number(value: float, unit: str) -> str:
    """A value label: at most three significant digits, thousands grouped."""
    text = f"{value:,.0f}" if abs(value) >= 100 else f"{value:.3g}"
    return f"{text} {unit}" if unit else text


@dataclass(frozen=True, slots=True)
class Part:
    name: str
    slot: str  # accent, second, third, gray or gray_light


def stacked_bars(title: str, subtitle: str, rows: list[tuple[str, list[float], bool]], parts: list[Part],
                 theme: Theme, *, total_label: str = "") -> str:
    """100%-stacked horizontal bars (one per row), the parts in a fixed order with a 2px surface gap."""
    width, left, right, top, row_h, bar_h = 760, 168, 70, 96, 30, 16
    plot_w = width - left - right
    height = top + row_h * len(rows) + 24
    body: list[str] = []
    x = 24.0
    for part in parts:  # the legend, in part order, above the plot
        body.append(f'<rect x="{x:.1f}" y="62" width="10" height="10" rx="2" fill="{getattr(theme, part.slot)}"/>')
        body.append(_text(x + 16, 67, part.name, fill=theme.ink2, size=12))
        x += 16 + CHAR_PX * len(part.name) + 18
    for index, (label, values, emphasis) in enumerate(rows):
        y = top + index * row_h
        cy = y + row_h / 2
        body.append(_text(left - 12, cy, label, fill=theme.ink if emphasis else theme.ink2, anchor="end",
                          weight=600 if emphasis else 400))
        total = sum(values)
        cursor = float(left)
        for part, value in zip(parts, values, strict=True):
            if value <= 0 or total <= 0:
                continue
            seg = plot_w * value / total
            share = value / total
            fill = getattr(theme, part.slot)
            gap = 2 if cursor > left else 0
            body.append(f'<rect x="{cursor + gap:.2f}" y="{cy - bar_h / 2:.2f}" width="{max(0.5, seg - gap):.2f}" '
                        f'height="{bar_h}" rx="3" fill="{fill}"><title>{_esc(label)} — {_esc(part.name)}: '
                        f'{value:g} ({share * 100:.1f}%)</title></rect>')
            cursor += seg
        body.append(_text(left + plot_w + 8, cy, f"{total:,.0f}{total_label}", fill=theme.muted, size=11))
    return _frame(width, height, theme, body, title, subtitle)


def stacked_shares(title: str, subtitle: str, rows: list[tuple[str, list[float], bool]], parts: list[Part],
                   theme: Theme) -> str:
    """Horizontal bars of shares on one zero-based axis, each split into the parts in a fixed order with a 2px
    surface gap; the label at the tip is the bar's total."""
    peak = max((sum(values) for _, values, _ in rows), default=1.0)
    scale_max, ticks = nice_scale(peak)
    width, left, right, top, row_h, bar_h = 760, 168, 70, 96, 30, 16
    plot_w = width - left - right
    height = top + row_h * len(rows) + 40
    body: list[str] = []
    x = 24.0
    for part in parts:
        body.append(f'<rect x="{x:.1f}" y="62" width="10" height="10" rx="2" fill="{getattr(theme, part.slot)}"/>')
        body.append(_text(x + 16, 67, part.name, fill=theme.ink2, size=12))
        x += 16 + CHAR_PX * len(part.name) + 18
    for tick in ticks:
        tx = left + plot_w * tick / scale_max
        body.append(f'<line x1="{tx:.1f}" y1="{top - 8}" x2="{tx:.1f}" y2="{top + row_h * len(rows)}" '
                    f'stroke="{theme.grid}" stroke-width="1"/>')
        body.append(_text(tx, top + row_h * len(rows) + 16, f"{tick * 100:.0f}%", fill=theme.muted, size=11,
                          anchor="middle"))
    body.append(f'<line x1="{left}" y1="{top - 8}" x2="{left}" y2="{top + row_h * len(rows)}" '
                f'stroke="{theme.axis}" stroke-width="1"/>')
    for index, (label, values, emphasis) in enumerate(rows):
        cy = top + index * row_h + row_h / 2
        body.append(_text(left - 12, cy, label, fill=theme.ink if emphasis else theme.ink2, anchor="end",
                          weight=600 if emphasis else 400))
        cursor = float(left)
        for part, value in zip(parts, values, strict=True):
            if value <= 0:
                continue
            seg = plot_w * value / scale_max
            gap = 2 if cursor > left else 0
            body.append(f'<rect x="{cursor + gap:.2f}" y="{cy - bar_h / 2:.2f}" width="{max(0.5, seg - gap):.2f}" '
                        f'height="{bar_h}" rx="3" fill="{getattr(theme, part.slot)}"><title>{_esc(label)} — '
                        f'{_esc(part.name)}: {_percent(value)}</title></rect>')
            cursor += seg
        body.append(_text(cursor + 8, cy, _percent(sum(values)), fill=theme.ink if emphasis else theme.ink2,
                          weight=600 if emphasis else 400))
    return _frame(width, height, theme, body, title, subtitle)


def scatter(title: str, subtitle: str, points: list[tuple[str, float, float, bool]], theme: Theme, *,
            x_label: str, y_label: str) -> str:
    """Labelled points: x a count on a log scale, y a share on a zero-based axis."""
    width, height, left, right, top, bottom = 760, 460, 72, 150, 80, 56
    plot_w, plot_h = width - left - right, height - top - bottom
    xs = [p[1] for p in points if p[1] > 0]
    lo, hi = math.floor(math.log10(min(xs, default=1))), math.ceil(math.log10(max(xs, default=10)))
    hi = max(hi, lo + 1)
    y_max, y_ticks = nice_scale(max((p[2] for p in points), default=1.0))

    def px(value: float) -> float:
        return left + plot_w * (math.log10(max(value, 10**lo)) - lo) / (hi - lo)

    def py(value: float) -> float:
        return top + plot_h * (1 - value / y_max)

    body: list[str] = []
    for tick in y_ticks:
        body.append(f'<line x1="{left}" y1="{py(tick):.1f}" x2="{left + plot_w}" y2="{py(tick):.1f}" '
                    f'stroke="{theme.grid}" stroke-width="1"/>')
        body.append(_text(left - 8, py(tick), f"{tick * 100:.0f}%", fill=theme.muted, size=11, anchor="end"))
    for power in range(lo, hi + 1):
        x = px(10**power)
        body.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" stroke="{theme.grid}" '
                    'stroke-width="1"/>')
        body.append(_text(x, top + plot_h + 16, f"{10**power:,}", fill=theme.muted, size=11, anchor="middle"))
    body.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" '
                f'stroke="{theme.axis}" stroke-width="1"/>')
    body.append(_text(left + plot_w / 2, top + plot_h + 38, x_label, fill=theme.ink2, size=12, anchor="middle"))
    body.append(_text(24, top - 18, y_label, fill=theme.ink2, size=12))
    for label, x, y, emphasis in sorted(points, key=lambda p: p[3]):
        color = theme.accent if emphasis else theme.ink2
        tip = f"{label}: {_percent(y)} at {x:g}"
        body.append(f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="5" fill="{color}"><title>{_esc(tip)}</title>'
                    "</circle>")
        body.append(_text(px(x) + 9, py(y), label, fill=theme.ink if emphasis else theme.ink2, size=11,
                          weight=600 if emphasis else 400))
    return _frame(width, height, theme, body, title, subtitle)


def matrix(title: str, subtitle: str, labels: list[str], values: list[list[float]], theme: Theme, *,
           emphasis: str = "", fmt: str = "{:.0f}") -> str:
    """A square matrix on the sequential ramp; the diagonal is blank."""
    n = len(labels)
    cell, left, top = 46, 150, 160
    width = left + cell * n + 24
    height = top + cell * n + 24
    peak = max((v for i, r in enumerate(values) for j, v in enumerate(r) if i != j), default=1) or 1
    body: list[str] = []
    for j, label in enumerate(labels):
        x = left + j * cell + cell / 2
        body.append(f'<text x="{x:.1f}" y="{top - 10}" fill="{theme.ink if label == emphasis else theme.ink2}" '
                    f'font-size="11" text-anchor="start" transform="rotate(-40 {x:.1f} {top - 10})">'
                    f'{_esc(label)}</text>')
    for i, label in enumerate(labels):
        y = top + i * cell
        body.append(_text(left - 10, y + cell / 2, label, fill=theme.ink if label == emphasis else theme.ink2,
                          anchor="end", size=11, weight=600 if label == emphasis else 400))
        for j in range(n):
            x = left + j * cell
            if i == j:
                continue
            value = values[i][j]
            step = 0 if value <= 0 else 1 + min(len(theme.ramp) - 2, int((len(theme.ramp) - 2) * value / peak))
            fill = theme.ramp[step]
            ink = theme.muted if value <= 0 else ("#ffffff" if _luminance(fill) < 0.18 else "#0b0b0b")
            body.append(f'<rect x="{x + 1}" y="{y + 1}" width="{cell - 2}" height="{cell - 2}" rx="3" fill="{fill}">'
                        f"<title>{_esc(label)} detects, {_esc(labels[j])} does not: {fmt.format(value)}"
                        "</title></rect>")
            body.append(_text(x + cell / 2, y + cell / 2, fmt.format(value), fill=ink, size=11, anchor="middle"))
    return _frame(width, height, theme, body, title, subtitle)


@dataclass(frozen=True, slots=True)
class Status:
    key: str
    label: str  # the legend text
    mark: str  # the text in the cell
    slot: str  # theme fill: second, third, gray, gray_light or surface


def status_matrix(title: str, subtitle: str, rows: list[str], columns: list[str], cells: list[list[str]],
                  statuses: list[Status], theme: Theme, *, emphasis: str = "",
                  notes: dict[tuple[int, int], str] | None = None) -> str:
    """A rows × columns grid of categorical outcomes: each cell filled by its status and marked, a legend above."""
    by_key = {status.key: status for status in statuses}
    left, top, cell_h, width = 168, 150, 30, 760  # every figure is 760 wide: the cells share what is left
    cell_w = min(64, (width - left - 24) // max(1, len(columns)))
    height = top + cell_h * len(rows) + 24
    body: list[str] = []
    x = 24.0
    for status in statuses:  # the legend, above the grid
        body.append(f'<rect x="{x:.1f}" y="62" width="10" height="10" rx="2" fill="{getattr(theme, status.slot)}" '
                    f'stroke="{theme.grid}"/>')
        body.append(_text(x + 16, 67, f"{status.mark} {status.label}", fill=theme.ink2, size=12))
        x += 16 + CHAR_PX * (len(status.label) + 2) + 18
    for j, column in enumerate(columns):
        cx = left + j * cell_w + cell_w / 2
        body.append(f'<text x="{cx:.1f}" y="{top - 10}" fill="{theme.ink2}" font-size="11" text-anchor="start" '
                    f'transform="rotate(-40 {cx:.1f} {top - 10})">{_esc(column)}</text>')
    for i, row in enumerate(rows):
        y = top + i * cell_h
        strong = row == emphasis
        body.append(_text(left - 12, y + cell_h / 2, row, fill=theme.ink if strong else theme.ink2, anchor="end",
                          weight=600 if strong else 400))
        for j, key in enumerate(cells[i]):
            status = by_key[key]
            fill = getattr(theme, status.slot)
            ink = theme.muted if status.slot == "surface" else ("#ffffff" if _luminance(fill) < 0.18 else "#0b0b0b")
            tip = f"{row} — {columns[j]}: {status.label}" + (f" ({notes[(i, j)]})" if notes and (i, j) in notes else "")
            body.append(f'<rect x="{left + j * cell_w + 1}" y="{y + 1}" width="{cell_w - 2}" '
                        f'height="{cell_h - 2}" rx="3" '
                        f'fill="{fill}" stroke="{theme.grid}"><title>{_esc(tip)}</title></rect>')
            body.append(_text(left + j * cell_w + cell_w / 2, y + cell_h / 2, status.mark, fill=ink, size=12,
                              anchor="middle"))
    return _frame(width, height, theme, body, title, subtitle)
