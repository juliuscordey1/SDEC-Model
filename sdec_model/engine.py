"""The SDEC calculation engine.

A faithful port of the calculation logic in the "Medical SDEC Demand and
Capacity Tool" (the ``<script>`` block of the source HTML). Function and
variable names deliberately mirror the original JavaScript so the two can be
checked line-for-line: ``parse_hm``/``parseHM``, ``in_window``/``inWindow``,
``line_active``/``lineActive``, ``covers``, ``med_capacity``/``medCapacity``,
``nur_capacity``/``nurCapacity``, ``deconv``, ``compute_state``/``computeState``.

The engine holds no input data of its own - it operates on a normalised
:class:`~sdec_model.schema.Model`.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

from .schema import Assumptions, Future, Line, Model

HOURS = 24
PRESSURE_EPS = 1e-6

_FAM_OF_PROFESSION = {"medical": "med", "nursing": "nur"}


# --------------------------------------------------------------------------
# shift-line time-window logic (parseHM / inWindow / lineActive / covers)
# --------------------------------------------------------------------------
def parse_hm(t: str | float) -> float:
    """Parse an "HH:MM" string to decimal hours (e.g. "08:30" -> 8.5).

    Mirrors the source ``parseHM``; also accepts a number, returned as-is.
    """
    if isinstance(t, (int, float)):
        return float(t)
    parts = str(t).split(":")
    h = float(parts[0]) if parts and parts[0] else 0.0
    m = float(parts[1]) if len(parts) > 1 and parts[1] else 0.0
    return h + m / 60.0


def in_window(h: int, open_hour: int, close_hour: int) -> bool:
    """Whether clock hour ``h`` is inside the operating window."""
    open_ = max(0, min(23, open_hour or 0))
    close = max(open_ + 1, min(24, close_hour or 24))
    return open_ <= h < close


def line_active(line: Line, weekend: bool) -> bool:
    """Whether a line runs on the day type currently in view."""
    if line.days == "we":
        return weekend
    if line.days == "wd":
        return not weekend
    return True


def covers(line: Line, h: int, open_hour: int, close_hour: int) -> bool:
    """Whether ``line`` is on shift during clock hour ``h``.

    The hour is sampled at its midpoint (``h + 0.5``). A start greater than the
    end denotes an overnight wrap.
    """
    if not in_window(h, open_hour, close_hour):
        return False
    t = h + 0.5
    s, e = line.s, line.e
    if abs(s - e) < 0.01:
        return False
    if s < e:
        return s <= t < e
    return t >= s or t < e


def med_capacity(lines: list[Line], weekend: bool, a: Assumptions) -> list[float]:
    """Per-hour medical seeing capacity (patients assessed per hour)."""
    out = [0.0] * HOURS
    for h in range(HOURS):
        total = 0.0
        for l in lines:
            if line_active(l, weekend) and covers(l, h, a.open_hour, a.close_hour):
                total += l.n * (l.rate or 0.0)
        out[h] = total
    return out


def nur_capacity(lines: list[Line], weekend: bool, a: Assumptions) -> list[float]:
    """Per-hour nursing concurrency capacity (patients safely managed at once).

    Only bay lines contribute. A line uses its own rate as the patients-per-nurse
    figure, falling back to the global default ratio when it has none.
    """
    out = [0.0] * HOURS
    for h in range(HOURS):
        total = 0.0
        for l in lines:
            if not (l.bay and line_active(l, weekend) and covers(l, h, a.open_hour, a.close_hour)):
                continue
            per_nurse = l.rate if (isinstance(l.rate, (int, float)) and math.isfinite(l.rate) and l.rate > 0) else a.nurse_ratio
            total += l.n * per_nurse
        out[h] = total
    return out


def deconv(occ: list[float], los: float) -> list[float]:
    """Derive arrivals per hour from occupancy by de-convolving a box-car stay.

    A patient occupies a chair for ``los`` hours, so occupancy at hour ``h`` is
    the sum of the last ``los`` hours of arrivals (the oldest weighted by the
    fractional remainder). This inverts that relationship with a forward
    recursion. Ported verbatim from the source ``deconv``.
    """
    arr = [0.0] * HOURS
    for h in range(HOURS):
        s = 0.0
        k = 1
        while los - k > 0:
            i = h - k
            if i >= 0:
                s += arr[i] * min(1.0, los - k)
            k += 1
        arr[h] = max(0.0, occ[h] - s)
    return arr


# --------------------------------------------------------------------------
# future-state capacity additions (levers + custom builder)
# --------------------------------------------------------------------------
def active_levers(future: Future, fam: str) -> list[Line]:
    """Normalised add-lines for every enabled preset lever of family ``fam``."""
    out: list[Line] = []
    for lv in future.levers:
        if not lv.get("enabled"):
            continue
        if _FAM_OF_PROFESSION.get(lv.get("profession")) != fam:
            continue
        profession = "medical" if fam == "med" else "nursing"
        for add in lv.get("add", []):
            out.append(_normalise_add(add, profession))
    return out


def custom_addition_lines(future: Future, fam: str) -> list[Line]:
    """Pulled-in custom lines, counting only the net change vs the baseline."""
    rows = future.custom_med if fam == "med" else future.custom_nur
    out: list[Line] = []
    for r in rows:
        net = (r.n or 0.0) - (r.base_n or 0.0)
        if abs(net) > 1e-9:
            line = copy.copy(r)
            line.n = net
            out.append(line)
    return out


def _normalise_add(add: dict, profession: str) -> Line:
    from .schema import _DAY_PATTERN_TO_DAYS  # local import to avoid cycle at module top

    day_pattern = add.get("dayPattern", "everyDay")
    counts = add.get("countsTowardsProfessionCapacity", True)
    bay = True if profession == "medical" else bool(counts)
    cost = add.get("costPerWtePerYear")
    return Line(
        label=str(add.get("label", "addition")),
        profession=profession,
        n=float(add.get("wte", 0) or 0),
        rate=float(add.get("patientsPerHour", 0) or 0),
        s=float(add.get("startHour", 0) or 0),
        e=float(add.get("endHour", 0) or 0),
        days=_DAY_PATTERN_TO_DAYS.get(day_pattern, "all"),
        bay=bay,
        cost=None if cost is None else float(cost),
    )


# --------------------------------------------------------------------------
# the core per-hour model (computeState)
# --------------------------------------------------------------------------
@dataclass
class StateResult:
    """Everything the source ``computeState`` returns, plus the day type."""

    day_type: str
    future: bool
    occ: list[float]
    arr: list[float]
    med_cap: list[float]
    nur_cap: list[float]
    queue: list[float]
    queue_enter: list[float]
    attend: float
    pth: float
    peak: float
    peak_h: int
    nur_util: float
    nur_over_hrs: int
    nur_peak_over: float
    nur_peak_over_h: int
    med_util: float
    med_cap_day: float
    peak_queue: float
    queue_h: int
    med_pressure: list[float]
    nur_pressure: list[float]
    overall_pressure: list[float]
    med_no_cover: list[bool]
    nur_no_cover: list[bool]
    hour_class: list[str]
    hour_counts: dict[str, int]
    h_constrained: int


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def compute_state(model: Model, day_type: str = "weekday", future: bool = False) -> StateResult:
    """Run the per-hour demand-vs-capacity model for one day type.

    ``day_type`` is "weekday" or "weekend" (the source's ``weekendView``).
    ``future`` toggles the future-state scaling and capacity additions.
    """
    weekend = day_type == "weekend"
    a = model.assumptions

    occ_base = list(model.occ(day_type))
    arr_base = deconv(occ_base, model.mean_los)
    occ = list(occ_base)
    arr = list(arr_base)
    med_lines = list(model.med_lines)
    nur_lines = list(model.nur_lines)

    if future:
        s = model.sources
        m = model.future.src_mult
        base = (s["edPull"] + s["gp"] + s["followup"] + s["oneoneone"] + s["hotClinic"]) or 1.0
        fut = (
            s["edPull"] * m["edPull"]
            + s["gp"] * m["gp"]
            + s["followup"] * m["followup"]
            + s["oneoneone"] * m["oneoneone"]
            + s["hotClinic"] * m["hotClinic"]
        )
        dscale = (fut / base) * model.future.demand_mult
        # C4C diverts a share of demand before the physical footprint - it
        # reduces patients present only, NOT arrivals. This asymmetry is
        # deliberate in the source and preserved here.
        c4c = 1 - _clamp(model.future.c4c_pct or 0, 0, 100) / 100
        occ = [v * dscale * model.future.los_mult * c4c for v in occ_base]
        arr = [v * dscale for v in arr_base]
        med_lines = med_lines + active_levers(model.future, "med") + custom_addition_lines(model.future, "med")
        nur_lines = nur_lines + active_levers(model.future, "nur") + custom_addition_lines(model.future, "nur")

    med_cap = med_capacity(med_lines, weekend, a)
    nur_cap = nur_capacity(nur_lines, weekend, a)

    # zero demand and capacity outside the operating window
    for h in range(HOURS):
        if in_window(h, a.open_hour, a.close_hour):
            continue
        occ[h] = 0.0
        arr[h] = 0.0
        med_cap[h] = 0.0
        nur_cap[h] = 0.0

    # medical queue - single daily pass (SDEC resets each day)
    queue_enter = [0.0] * HOURS
    queue = [0.0] * HOURS
    q = 0.0
    for h in range(HOURS):
        queue_enter[h] = q
        q = max(0.0, q + arr[h] - med_cap[h])
        queue[h] = q

    # Medical capacity is a flow measure; nursing capacity is a concurrency
    # measure. They are never added together - each hour is scored independently.
    med_pressure = [0.0] * HOURS
    nur_pressure = [0.0] * HOURS
    overall_pressure = [0.0] * HOURS
    med_no_cover = [False] * HOURS
    nur_no_cover = [False] * HOURS
    hour_class = ["closed"] * HOURS
    hour_counts = {"within": 0, "medical": 0, "nursing": 0, "both": 0, "closed": 0}
    for h in range(HOURS):
        if not in_window(h, a.open_hour, a.close_hour):
            hour_class[h] = "closed"
            hour_counts["closed"] += 1
            continue
        med_demand = arr[h] + queue_enter[h]
        if med_cap[h] > PRESSURE_EPS:
            med_pressure[h] = med_demand / med_cap[h]
        elif med_demand > PRESSURE_EPS:
            med_pressure[h] = math.inf
            med_no_cover[h] = True
        else:
            med_pressure[h] = 0.0
        if nur_cap[h] > PRESSURE_EPS:
            nur_pressure[h] = occ[h] / nur_cap[h]
        elif occ[h] > PRESSURE_EPS:
            nur_pressure[h] = math.inf
            nur_no_cover[h] = True
        else:
            nur_pressure[h] = 0.0
        overall_pressure[h] = max(med_pressure[h], nur_pressure[h])
        med_bad = med_pressure[h] > 1 + PRESSURE_EPS
        nur_bad = nur_pressure[h] > 1 + PRESSURE_EPS
        cls = "both" if (med_bad and nur_bad) else "medical" if med_bad else "nursing" if nur_bad else "within"
        hour_class[h] = cls
        hour_counts[cls] += 1
    h_constrained = hour_counts["medical"] + hour_counts["nursing"] + hour_counts["both"]

    attend = sum(arr)
    pth = sum(occ)
    peak = 0.0
    peak_h = 0
    for h, v in enumerate(occ):
        if v > peak:
            peak = v
            peak_h = h
    nur_util = 0.0
    nur_over_hrs = 0
    nur_peak_over = 0.0
    nur_peak_over_h = 0
    for h in range(HOURS):
        if not in_window(h, a.open_hour, a.close_hour):
            continue
        if nur_cap[h] > 0:
            nur_util = max(nur_util, occ[h] / nur_cap[h])
        if occ[h] > nur_cap[h] + 1e-6:
            nur_over_hrs += 1
        over = occ[h] - nur_cap[h]
        if over > nur_peak_over:
            nur_peak_over = over
            nur_peak_over_h = h
    med_cap_day = sum(med_cap)
    med_util = attend / med_cap_day if med_cap_day > 0 else 0.0
    peak_queue = max(queue)
    queue_h = 0
    for h, v in enumerate(queue):
        if v == peak_queue:
            queue_h = h

    return StateResult(
        day_type=day_type,
        future=future,
        occ=occ,
        arr=arr,
        med_cap=med_cap,
        nur_cap=nur_cap,
        queue=queue,
        queue_enter=queue_enter,
        attend=attend,
        pth=pth,
        peak=peak,
        peak_h=peak_h,
        nur_util=nur_util,
        nur_over_hrs=nur_over_hrs,
        nur_peak_over=nur_peak_over,
        nur_peak_over_h=nur_peak_over_h,
        med_util=med_util,
        med_cap_day=med_cap_day,
        peak_queue=peak_queue,
        queue_h=queue_h,
        med_pressure=med_pressure,
        nur_pressure=nur_pressure,
        overall_pressure=overall_pressure,
        med_no_cover=med_no_cover,
        nur_no_cover=nur_no_cover,
        hour_class=hour_class,
        hour_counts=hour_counts,
        h_constrained=h_constrained,
    )


# --------------------------------------------------------------------------
# cost of explicit future-state staffing additions or removals
# --------------------------------------------------------------------------
@dataclass
class CapAdditionRow:
    label: str
    fam: str  # "med" | "nur"
    wte: float
    cost: float | None = None  # per-WTE override


@dataclass
class CostSummary:
    rows: list[CapAdditionRow] = field(default_factory=list)
    med_wte: float = 0.0
    med_cost: float = 0.0
    nur_wte: float = 0.0
    nur_cost: float = 0.0

    @property
    def total_wte(self) -> float:
        return self.med_wte + self.nur_wte

    @property
    def total_cost(self) -> float:
        return self.med_cost + self.nur_cost


def capacity_addition_rows(model: Model) -> list[CapAdditionRow]:
    """Every active preset lever and custom edit, each with its net WTE change.

    Sign convention: a WTE increase is a positive (cost) change; a WTE decrease
    is negative (a saving). Ported from the source ``capacityAdditionRows``.
    """
    rows: list[CapAdditionRow] = []
    for lv in model.future.levers:
        if not lv.get("enabled"):
            continue
        wte = sum(float(add.get("wte", 0) or 0) for add in lv.get("add", []))
        fam = _FAM_OF_PROFESSION.get(lv.get("profession"), "med")
        rows.append(CapAdditionRow(label=f"{lv.get('name', 'Lever')} (preset)", fam=fam, wte=wte))
    for fam, rows_src in (("med", model.future.custom_med), ("nur", model.future.custom_nur)):
        for r in rows_src:
            added = (r.n or 0.0) - (r.base_n or 0.0)
            if abs(added) > 1e-9:
                rows.append(CapAdditionRow(label=r.label, fam=fam, wte=added, cost=r.cost))
    return rows


def cost_summary(model: Model) -> CostSummary:
    """Annualised cost/saving of the explicit future-state staffing changes."""
    a = model.assumptions
    summary = CostSummary(rows=capacity_addition_rows(model))
    for r in summary.rows:
        cost_per_wte = r.cost if (r.cost is not None and math.isfinite(r.cost)) else (a.cost_med_wte if r.fam == "med" else a.cost_nur_wte)
        cost = r.wte * cost_per_wte
        if r.fam == "med":
            summary.med_wte += r.wte
            summary.med_cost += cost
        else:
            summary.nur_wte += r.wte
            summary.nur_cost += cost
    return summary
