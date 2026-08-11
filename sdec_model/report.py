"""Render a self-contained HTML report from a SDEC model.

The output is a single static ``.html`` file - inline CSS and inline SVG only,
no external requests and no build step - that opens directly in a browser with
a double-click. It mirrors the structure (not the pixel styling) of the source
tool's Current State / Future State sections: headline stats, an hour-by-hour
status view, demand-vs-capacity charts, a capacity-additions / cost summary and
a plain-language interpretation.
"""

from __future__ import annotations

import datetime as _dt
import html
from dataclasses import dataclass

from .engine import HOURS, StateResult, compute_state, cost_summary
from .schema import Model

# --------------------------------------------------------------------------
# number formatting (mirrors the source's f1 / f0)
# --------------------------------------------------------------------------
def f1(n: float) -> str:
    return f"{round(n * 10) / 10:,.1f}"


def f0(n: float) -> str:
    return f"{round(n):,}"


def gbp(n: float) -> str:
    return "£" + f"{round(n):,}"


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


STATUS_META = {
    "within": ("Within capacity", "OK", "within"),
    "medical": ("Medical constraint", "Med", "medical"),
    "nursing": ("Nursing constraint", "Nurs", "nursing"),
    "both": ("Medical and nursing both constrained", "Both", "both"),
    "closed": ("Closed", "Closed", "closed"),
}


# --------------------------------------------------------------------------
# plain-language interpretation (ported from interpretationStatements)
# --------------------------------------------------------------------------
def interpretation_statements(st: StateResult) -> list[str]:
    c = st.hour_counts
    if st.h_constrained == 0:
        return ["No hours are constrained - medical and nursing capacity both keep pace with demand all day."]
    operating_hours = c["within"] + c["medical"] + c["nursing"] + c["both"]
    med_hrs = c["medical"] + c["both"]
    nur_hrs = c["nursing"] + c["both"]
    main = other = None
    main_hrs = med_hrs
    if med_hrs > nur_hrs:
        main, other, main_hrs = "medical", "nursing", med_hrs
    elif nur_hrs > med_hrs:
        main, other, main_hrs = "nursing", "medical", nur_hrs

    plural = "" if operating_hours == 1 else "s"
    if main:
        out = [f"The main constraint is {main} capacity, constrained for {main_hrs} of {operating_hours} operating hour{plural}."]
    else:
        out = [f"Medical and nursing capacity are equally constrained, each for {main_hrs} of {operating_hours} operating hour{plural}."]

    match_hours = []
    for h in range(HOURS):
        cls = st.hour_class[h]
        if main == "medical":
            matches = cls in ("medical", "both")
        elif main == "nursing":
            matches = cls in ("nursing", "both")
        else:
            matches = cls in ("medical", "nursing", "both")
        if matches:
            match_hours.append(h)
    if match_hours:
        lo, hi = min(match_hours), max(match_hours)
        out.append(f"It occurs mainly between {lo:02d}:00 and {(hi + 1) % 24:02d}:00.")

    if other:
        if c["both"] > 0:
            out.append(f"{other.capitalize()} is also constrained at the same time, for {c['both']} of those hours.")
        elif (c["medical"] if other == "medical" else c["nursing"]) > 0:
            out.append(f"{other.capitalize()} is constrained separately, at a different time.")
        else:
            out.append(f"{other.capitalize()} capacity is not constrained at any point.")
    elif c["both"] > 0:
        out.append(f"Medical and nursing constraints overlap for {c['both']} of those hours.")
    return out[:3]


def future_readout(model: Model, day_label: str, cur: StateResult, fut: StateResult) -> str:
    dq = fut.peak_queue - cur.peak_queue
    dn = (fut.nur_util - cur.nur_util) * 100
    head = (
        f"Future {day_label}: <strong>{f1(fut.attend)}</strong> attendances/day "
        f"({f1(cur.attend)} today) &middot; peak nursing use <strong>{f0(fut.nur_util * 100)}%</strong> "
        f"&middot; peak queue <strong>{f1(fut.peak_queue)}</strong>. "
    )
    if abs(dq) < 0.5 and abs(dn) < 2:
        return head + "Broadly unchanged from today."
    queue_word = "grows" if dq > 0.5 else "shrinks" if dq < -0.5 else "holds"
    nur_word = "rises" if dn > 2 else "eases" if dn < -2 else "holds"
    return head + f"The queue {queue_word} and nursing pressure {nur_word} against today."


# --------------------------------------------------------------------------
# SVG charts
# --------------------------------------------------------------------------
@dataclass
class Series:
    name: str
    vals: list[float]
    color: str
    dash: bool = False


def _line_chart(series: list[Series], open_h: int, close_h: int, aria: str) -> str:
    W, H, L, R, T, B = 720, 210, 38, 16, 14, 26
    open_ = max(0, min(23, round(open_h)))
    close = max(open_ + 1, min(24, round(close_h)))
    rng = close - open_
    window_vals = [v for s in series for v in s.vals[open_:close]]
    max_v = max(window_vals + [1.0]) * 1.15

    def x(h: float) -> float:
        return L + (W - L - R) * (h - open_) / max(1, rng - 1)

    def y(v: float) -> float:
        return T + (H - T - B) * (1 - v / max_v)

    parts = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{_esc(aria)}" class="chart-svg">']
    ticks = 3
    for i in range(ticks + 1):
        v = max_v * i / ticks
        parts.append(f'<line x1="{L}" y1="{y(v):.1f}" x2="{W - R}" y2="{y(v):.1f}" class="grid"/>')
        parts.append(f'<text x="{L - 6}" y="{y(v) + 4:.1f}" text-anchor="end" class="tick">{round(v)}</text>')
    tick_step = max(1, round(rng / 5))
    for h in range(open_, close, tick_step):
        parts.append(f'<text x="{x(h):.1f}" y="{H - 8}" text-anchor="middle" class="tick">{h:02d}:00</text>')
    parts.append(f'<line x1="{L}" y1="{y(0):.1f}" x2="{W - R}" y2="{y(0):.1f}" class="axis"/>')
    for s in series:
        pts = " ".join(f"{x(open_ + i):.1f},{y(v):.1f}" for i, v in enumerate(s.vals[open_:close]))
        dash = ' stroke-dasharray="5 4" opacity="0.75"' if s.dash else ""
        parts.append(f'<polyline points="{pts}" fill="none" stroke="{s.color}" stroke-width="2.2" stroke-linejoin="round"{dash}/>')
    parts.append("</svg>")
    legend = '<div class="legend">' + "".join(
        f'<span class="key"><span class="swatch{" dash" if s.dash else ""}" style="background:{s.color}"></span>{_esc(s.name)}</span>'
        for s in series
    ) + "</div>"
    return f'<div class="chart-wrap">{"".join(parts)}</div>{legend}'


def _status_strip(st: StateResult) -> str:
    segs = []
    for h in range(HOURS):
        cls = st.hour_class[h]
        label, tag, css = STATUS_META[cls]
        closed = cls == "closed"
        no_cover = (not closed) and (st.med_no_cover[h] or st.nur_no_cover[h])
        tag_txt = "No cover" if no_cover else tag

        def ptext(p: float, nc: bool) -> str:
            if nc or p == float("inf"):
                return "No cover"
            return f"{f0(p * 100)}%"

        med_txt = "n/a - closed" if closed else ptext(st.med_pressure[h], st.med_no_cover[h])
        nur_txt = "n/a - closed" if closed else ptext(st.nur_pressure[h], st.nur_no_cover[h])
        title = f"{h:02d}:00 - {label}. Medical {med_txt}, nursing {nur_txt}."
        segs.append(
            f'<div class="seg seg-{css}" title="{_esc(title)}" aria-label="{_esc(title)}">'
            f'<span class="seg-hour">{h:02d}</span><span class="seg-tag">{_esc(tag_txt)}</span></div>'
        )
    legend = (
        '<div class="legend strip-legend">'
        '<span class="key"><span class="swatch box seg-within"></span>Within capacity</span>'
        '<span class="key"><span class="swatch box seg-medical"></span>Medical constraint</span>'
        '<span class="key"><span class="swatch box seg-nursing"></span>Nursing constraint</span>'
        '<span class="key"><span class="swatch box seg-both"></span>Both constrained</span>'
        '<span class="key"><span class="swatch box seg-closed"></span>Closed</span>'
        "</div>"
    )
    return f'<div class="status-strip">{"".join(segs)}</div>{legend}'


# --------------------------------------------------------------------------
# stat cards
# --------------------------------------------------------------------------
def _card(label: str, value: str, sub: str, value_cls: str = "") -> str:
    return (
        f'<div class="card"><div class="stat-label">{_esc(label)}</div>'
        f'<div class="stat-value {value_cls}">{value}</div>'
        f'<div class="stat-sub">{sub}</div></div>'
    )


def _delta(v: float, unit: str, good_down: bool) -> str:
    if abs(v) < 0.05:
        return "no change vs today"
    up = v > 0
    cls = "pos" if (not up if good_down else up) else "neg"
    sign = "+" if up else "−"
    return f'<span class="{cls}">{sign}{f1(abs(v))}{unit} vs today</span>'


def _constraint_sub(st: StateResult) -> str:
    c = st.hour_counts
    if st.h_constrained == 0:
        return "none - within capacity all day"
    return f"{c['medical']} medical &middot; {c['nursing']} nursing &middot; {c['both']} both"


def _current_cards(cur: StateResult) -> str:
    nur_sub = (
        f"{f1(cur.nur_peak_over)} patients over capacity at {cur.nur_peak_over_h:02d}:00"
        if cur.nur_peak_over > 0.05 else "within nursing capacity all day"
    )
    queue_sub = f"peak at {cur.queue_h:02d}:00" if cur.peak_queue > 0.5 else "capacity keeps pace"
    return (
        '<div class="cards">'
        + _card("Attendances / day", f1(cur.attend), f"{cur.day_type} demand")
        + _card("Peak nursing use", f"{f0(cur.nur_util * 100)}%", nur_sub, "neg" if cur.nur_util > 1.001 else "")
        + _card("Medical capacity used", f"{f0(cur.med_util * 100)}%",
                f"{f1(cur.med_cap_day)} seen/day capacity vs {f1(cur.attend)} arrivals")
        + _card("Peak queue to be seen", f1(cur.peak_queue), queue_sub)
        + _card("Hours constrained", str(cur.h_constrained), _constraint_sub(cur))
        + "</div>"
    )


def _future_cards(cur: StateResult, fut: StateResult) -> str:
    return (
        '<div class="cards">'
        + _card("Attendances / day", f1(fut.attend), _delta(fut.attend - cur.attend, "", False))
        + _card("Peak nursing use", f"{f0(fut.nur_util * 100)}%",
                _delta((fut.nur_util - cur.nur_util) * 100, "pp", True), "neg" if fut.nur_util > 1.001 else "")
        + _card("Medical capacity used", f"{f0(fut.med_util * 100)}%",
                _delta((fut.med_util - cur.med_util) * 100, "pp", True))
        + _card("Peak queue to be seen", f1(fut.peak_queue), _delta(fut.peak_queue - cur.peak_queue, "", True))
        + _card("Hours constrained", str(fut.h_constrained), _constraint_sub(fut))
        + "</div>"
    )


def _interpretation_box(st: StateResult) -> str:
    items = "".join(f"<li>{_esc(s)}</li>" for s in interpretation_statements(st))
    return f'<div class="interpretation"><ul>{items}</ul></div>'


# --------------------------------------------------------------------------
# cost / capacity-additions summary
# --------------------------------------------------------------------------
def _cost_cell(cost: float) -> str:
    if abs(cost) < 0.5:
        return "<span>£0/yr</span>"
    if cost > 0:
        return f'<span class="neg">{gbp(cost)}/yr</span>'
    return f'<span class="pos">{gbp(abs(cost))}/yr saving</span>'


def _wte_cell(wte: float) -> str:
    if abs(wte) < 0.001:
        return "0.0 WTE"
    cls = "pos" if wte > 0 else "neg"
    sign = "+" if wte >= 0 else "−"
    return f'<span class="{cls}">{sign}{f1(abs(wte))} WTE</span>'


def _cost_summary_html(model: Model) -> str:
    summary = cost_summary(model)
    if not summary.rows:
        return (
            '<p class="muted">No capacity additions or removals are configured for the future state, '
            "so there is no cost or saving to report. Enable a scheme in the JSON "
            "(<code>future.schemes[].enabled</code>) or add a custom capacity line to model a change.</p>"
        )
    body = ""
    for r in summary.rows:
        cost_per_wte = r.cost if (r.cost is not None) else (model.assumptions.cost_med_wte if r.fam == "med" else model.assumptions.cost_nur_wte)
        cost = r.wte * cost_per_wte
        team = "Medical" if r.fam == "med" else "Nursing"
        body += f"<tr><td>{_esc(r.label)}</td><td>{team}</td><td>{_wte_cell(r.wte)}</td><td>{_cost_cell(cost)}</td></tr>"
    total = (
        f'<tr class="total"><td>Total</td><td></td>'
        f"<td>{_wte_cell(summary.total_wte)}</td><td>{_cost_cell(summary.total_cost)}</td></tr>"
    )
    return (
        '<div class="table-wrap"><table class="cost-table">'
        "<thead><tr><th>Addition</th><th>Team</th><th>Net WTE</th><th>Annual cost / saving</th></tr></thead>"
        f"<tbody>{body}{total}</tbody></table></div>"
        '<p class="muted">Indicative only - confirm costs and establishment with finance. '
        "A WTE increase is a cost; a WTE decrease is a saving.</p>"
    )


# --------------------------------------------------------------------------
# per-day-type section
# --------------------------------------------------------------------------
def _charts(prefix: str, st: StateResult, model: Model, cmp: StateResult | None) -> str:
    a = model.assumptions
    accent = "var(--accent)"
    muted = "var(--muted)"
    bad = "var(--bad)"
    aqua = "var(--aqua)"
    ink2 = "var(--ink-2)"

    med_series = [
        Series("Arrivals /hr", st.arr, muted),
        Series("Seeing capacity /hr", st.med_cap, accent),
        Series("Queue waiting to be seen", st.queue, bad, dash=True),
    ]
    if cmp:
        med_series.append(Series("Arrivals today", cmp.arr, muted, dash=True))
    nur_series = [
        Series("Patients present", st.occ, aqua),
        Series("Nursing capacity", st.nur_cap, ink2),
    ]
    if cmp:
        nur_series.append(Series("Present today", cmp.occ, muted, dash=True))

    return (
        '<div class="chart-panel"><div class="panel-title">Medical - arrivals vs seeing capacity</div>'
        '<div class="panel-sub">Can the clinicians on shift keep pace with arrivals, or does a queue build?</div>'
        + _line_chart(med_series, a.open_hour, a.close_hour, f"{prefix} medical arrivals versus seeing capacity by hour")
        + "</div>"
        '<div class="chart-panel"><div class="panel-title">Nursing - patients present vs nursing capacity</div>'
        '<div class="panel-sub">Are there enough bay nurses for the patients present?</div>'
        + _line_chart(nur_series, a.open_hour, a.close_hour, f"{prefix} patients present versus nursing capacity by hour")
        + "</div>"
    )


def _day_section(model: Model, day_type: str) -> str:
    day_label = "Weekend" if day_type == "weekend" else "Weekday"
    cur = compute_state(model, day_type, future=False)
    fut = compute_state(model, day_type, future=True)

    return f"""
<section class="day-section">
  <h2>{day_label}</h2>

  <div class="state-block current">
    <h3>Current state</h3>
    {_current_cards(cur)}
    <div class="panel-title">Overall service - hourly status</div>
    <div class="panel-sub">Medical capacity (patients assessed per hour) and nursing capacity (patients safely managed at once) are never added together. Each hour is classified by whichever, if either, is under pressure.</div>
    {_status_strip(cur)}
    {_interpretation_box(cur)}
    <div class="charts">{_charts("current", cur, model, None)}</div>
  </div>

  <div class="state-block future">
    <h3>Future state</h3>
    {_future_cards(cur, fut)}
    <div class="panel-title">Overall service - hourly status</div>
    <div class="panel-sub">Future-state demand and capacity, scored the same way as current state.</div>
    {_status_strip(fut)}
    {_interpretation_box(fut)}
    <p class="readout">{future_readout(model, day_label.lower(), cur, fut)}</p>
    <div class="charts">{_charts("future", fut, model, cur)}</div>
    <div class="panel-title">Cost of future-state staffing changes</div>
    {_cost_summary_html(model)}
  </div>
</section>
"""


# --------------------------------------------------------------------------
# page assembly
# --------------------------------------------------------------------------
_CSS = """
:root {
  color-scheme: light dark;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#101010; --ink-2:#52514e; --muted:#8a8880;
  --border:rgba(16,16,16,0.12); --grid:#e6e5de; --axis:#c3c2b7;
  --accent:#2f6df6; --aqua:#0e9aa7; --bad:#d64545; --bad-2:#e08a1e; --good:#3aa76d;
  --card:#ffffff; --shadow:0 1px 2px rgba(0,0,0,0.05),0 2px 8px rgba(0,0,0,0.04);
  --pos:#2f8f57; --neg:#c23b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --page:#121317; --surface:#1a1c22; --ink:#f2f2f0; --ink-2:#b8b7b2; --muted:#8b8a84;
    --border:rgba(255,255,255,0.12); --grid:#2a2d35; --axis:#3a3e48;
    --accent:#5b8dff; --aqua:#2bc0cf; --bad:#f16d6d; --bad-2:#f0a94a; --good:#4fbf85;
    --card:#20232b; --shadow:0 1px 2px rgba(0,0,0,0.3);
    --pos:#5fce8f; --neg:#f37272;
  }
}
* { box-sizing:border-box; }
body { margin:0; background:var(--page); color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  line-height:1.5; -webkit-font-smoothing:antialiased; }
.wrap { max-width:1000px; margin:0 auto; padding:32px 20px 80px; }
header.page-head { margin-bottom:28px; }
h1 { font-size:26px; margin:0 0 6px; letter-spacing:-0.01em; }
h2 { font-size:21px; margin:0 0 16px; letter-spacing:-0.01em; }
h3 { font-size:16px; text-transform:uppercase; letter-spacing:0.06em; color:var(--ink-2); margin:0 0 14px; }
.lede { color:var(--ink-2); max-width:70ch; margin:0 0 10px; }
.meta-line { color:var(--muted); font-size:13px; }
.banner { display:inline-block; margin-top:12px; padding:6px 12px; border-radius:999px;
  background:color-mix(in srgb, var(--bad-2) 16%, transparent); color:var(--bad-2);
  font-size:12.5px; font-weight:600; border:1px solid color-mix(in srgb, var(--bad-2) 30%, transparent); }
.day-section { margin-top:40px; padding-top:8px; border-top:2px solid var(--border); }
.state-block { margin:20px 0 34px; padding:20px; background:var(--surface);
  border:1px solid var(--border); border-radius:14px; }
.state-block.future { background:color-mix(in srgb, var(--accent) 5%, var(--surface)); }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; margin-bottom:22px; }
.card { background:var(--card); border:1px solid var(--border); border-radius:12px; padding:14px 16px; box-shadow:var(--shadow); }
.stat-label { font-size:12px; text-transform:uppercase; letter-spacing:0.05em; color:var(--muted); }
.stat-value { font-size:26px; font-weight:650; margin:4px 0 2px; letter-spacing:-0.01em; }
.stat-value.neg { color:var(--neg); }
.stat-sub { font-size:12.5px; color:var(--ink-2); }
.panel-title { font-weight:650; margin:20px 0 2px; }
.panel-sub { color:var(--ink-2); font-size:13px; margin-bottom:10px; max-width:78ch; }
.charts { display:grid; grid-template-columns:1fr; gap:8px; }
.chart-panel { margin-top:8px; }
.chart-wrap { width:100%; overflow-x:auto; }
.chart-svg { width:100%; height:auto; display:block; min-width:520px; }
.grid { stroke:var(--grid); stroke-width:1; }
.axis { stroke:var(--axis); stroke-width:1; }
.tick { font-size:10.5px; fill:var(--muted); }
.legend { display:flex; flex-wrap:wrap; gap:14px; margin:8px 0 4px; font-size:12.5px; color:var(--ink-2); }
.key { display:inline-flex; align-items:center; gap:6px; }
.swatch { width:14px; height:3px; border-radius:2px; display:inline-block; }
.swatch.dash { height:0; border-top:2px dashed currentColor; background:none !important; width:16px; }
.swatch.box { width:13px; height:13px; border-radius:3px; }
.status-strip { display:grid; grid-template-columns:repeat(24,1fr); gap:2px; margin:6px 0; }
.seg { min-height:44px; border-radius:4px; padding:4px 0 3px; text-align:center; display:flex;
  flex-direction:column; justify-content:space-between; color:#fff; font-size:10px; }
.seg-hour { font-weight:600; opacity:0.9; }
.seg-tag { font-size:9px; opacity:0.95; }
.seg-within  { background:var(--good); }
.seg-medical { background:var(--accent); }
.seg-nursing { background:var(--bad-2); }
.seg-both    { background:var(--bad); }
.seg-closed  { background:color-mix(in srgb, var(--muted) 45%, transparent); color:var(--ink-2); }
.swatch.box.seg-closed { background:color-mix(in srgb, var(--muted) 45%, transparent); }
.strip-legend { margin-top:8px; }
.interpretation { margin:12px 0 4px; padding:12px 16px; background:var(--card);
  border:1px solid var(--border); border-radius:10px; }
.interpretation ul { margin:0; padding-left:20px; }
.interpretation li { margin:2px 0; color:var(--ink-2); }
.readout { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:12px 16px; }
.pos { color:var(--pos); font-weight:600; }
.neg { color:var(--neg); font-weight:600; }
.table-wrap { overflow-x:auto; }
table.cost-table { width:100%; border-collapse:collapse; font-size:13.5px; margin-top:6px; min-width:460px; }
.cost-table th, .cost-table td { text-align:left; padding:8px 10px; border-bottom:1px solid var(--border); }
.cost-table th { font-size:12px; text-transform:uppercase; letter-spacing:0.04em; color:var(--muted); }
.cost-table tr.total td { font-weight:650; border-top:2px solid var(--border); border-bottom:none; }
.muted { color:var(--muted); font-size:12.5px; }
code { background:color-mix(in srgb, var(--muted) 18%, transparent); padding:1px 5px; border-radius:4px; font-size:12px; }
footer { margin-top:48px; padding-top:16px; border-top:1px solid var(--border); color:var(--muted); font-size:12.5px; }
"""


def render_html(model: Model, generated_at: str | None = None) -> str:
    """Return the complete self-contained HTML report as a string."""
    meta = model.meta
    name = _esc(meta.get("name", "Medical SDEC demand and capacity model"))
    org = _esc(meta.get("organisation", ""))
    generated = generated_at or _dt.datetime.now().strftime("%d %b %Y, %H:%M")
    synthetic = bool(meta.get("isSyntheticDemoData"))
    banner = (
        '<div class="banner">Synthetic demonstration data - not confirmed local figures</div>'
        if synthetic else ""
    )

    sections = _day_section(model, "weekday") + _day_section(model, "weekend")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
<header class="page-head">
  <h1>{name}</h1>
  <p class="lede">A bottom-up model of Same Day Emergency Care demand and capacity. Current state is built from the hourly patients-present profile and shift-level staffing; future state applies the configured demand and capacity assumptions and reads the change against today.</p>
  <div class="meta-line">{org}{" &middot; " if org else ""}Generated {generated} &middot; medical capacity is a flow measure, nursing capacity a concurrency measure - the two are never added together.</div>
  {banner}
</header>
{sections}
<footer>
  Generated by the SDEC Python model from <code>data/sdec_model.json</code>. Calculation logic is a faithful port of the Medical SDEC Demand and Capacity Tool. Figures are indicative; confirm local data before formal use.
</footer>
</div>
</body>
</html>
"""


def write_report(model: Model, out_path: str, generated_at: str | None = None) -> str:
    """Render and write the report to ``out_path``. Returns the path."""
    from pathlib import Path

    html_text = render_html(model, generated_at=generated_at)
    p = Path(out_path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html_text, encoding="utf-8")
    return out_path
