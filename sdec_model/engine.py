"""The SDEC calculation engine.

Descended from the calculation logic in the "Medical SDEC Demand and Capacity
Tool", rearchitected around **areas**. Function and variable names still mirror
the browser tool's JavaScript so the two can be checked line-for-line:
``parse_hm``/``parseHM``, ``in_window``/``inWindow``, ``line_active``/
``lineActive``, ``covers``, ``med_capacity``/``medCapacity``, ``nur_capacity``/
``nurCapacity``, ``deconv``, ``compute_state``/``computeState``.

The model in one paragraph: each area has its own hourly patients-present
curve, its own physical spaces and its own roster. For each area and hour we
compare (a) medical demand - arrivals plus the queue entering the hour -
against that area's **seeing capacity** (a flow measure), (b) patients present
against that area's **nursing holding capacity** (a concurrency measure, only
where nurses hold the patients), and (c) patients present against the area's
**physical spaces** (a hard ceiling). The three are never added together; each
hour of each area is classified by whichever of them, if any, is exceeded.
Staff with no area (the floating pool) are shared out across areas each hour in
proportion to that area's demand, so a floor whose clinicians all circulate
reads exactly as the old floor-wide model did. Floor-wide demand, capacity and
spaces are the sum across areas.

The engine holds no input data of its own - it operates on a normalised
:class:`~sdec_model.schema.Model`.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

from .schema import SOURCE_KEYS, Area, Assumptions, Future, Line, Model

HOURS = 24
PRESSURE_EPS = 1e-6

_FAM_OF_PROFESSION = {"medical": "med", "nursing": "nur"}

#: Every value ``hour_class`` can take. A constrained hour names each exceeded
#: dimension, joined with "+" in a fixed order, so "medical+space" means the
#: clinicians could not keep pace *and* the area ran out of physical spaces.
HOUR_CLASSES = (
    "within",
    "medical",
    "nursing",
    "space",
    "medical+nursing",
    "medical+space",
    "nursing+space",
    "medical+nursing+space",
    "closed",
)


def classify_hour(med_bad: bool, nur_bad: bool, space_bad: bool) -> str:
    """Name the constraint(s) biting in one hour (see :data:`HOUR_CLASSES`)."""
    parts = [
        name
        for name, bad in (("medical", med_bad), ("nursing", nur_bad), ("space", space_bad))
        if bad
    ]
    return "+".join(parts) if parts else "within"


def empty_hour_counts() -> dict[str, int]:
    return {k: 0 for k in HOUR_CLASSES}


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
    """Per-hour nursing holding capacity (patients safely held at once).

    Only lines that hold patients contribute. A line uses its own rate as the
    patients-per-nurse figure, falling back to the global default ratio when it
    has none.
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


def lines_for_area(lines: list[Line], area_id: str | None) -> list[Line]:
    """The subset of ``lines`` rostered to ``area_id`` (``None`` = floating)."""
    return [l for l in lines if l.area_id == area_id]


def deconv(occ: list[float], los: float) -> list[float]:
    """Derive arrivals per hour from occupancy by de-convolving a box-car stay.

    A patient occupies a space for ``los`` hours, so occupancy at hour ``h`` is
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
    """Normalised add-lines for every enabled staffing lever of family ``fam``.

    Each add line inherits its scheme's ``areaId`` unless it names its own.
    """
    out: list[Line] = []
    for lv in future.levers:
        if not lv.get("enabled"):
            continue
        if _FAM_OF_PROFESSION.get(lv.get("profession")) != fam:
            continue
        profession = "medical" if fam == "med" else "nursing"
        scheme_area = lv.get("areaId") or None
        for add in lv.get("add", []) or []:
            out.append(_normalise_add(add, profession, scheme_area))
    return out


def active_space_additions(future: Future) -> dict[str, float]:
    """Extra physical spaces per area from enabled space levers, plus the
    ad-hoc ``areaSpaceDeltas``."""
    out: dict[str, float] = {k: float(v) for k, v in future.area_space_deltas.items()}
    for lv in future.levers:
        if not lv.get("enabled") or lv.get("type") != "physicalSpaceAddition":
            continue
        area_id = lv.get("areaId") or None
        if area_id is None:
            continue
        out[area_id] = out.get(area_id, 0.0) + float(lv.get("addSpaces", 0) or 0)
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


def _normalise_add(add: dict, profession: str, area_id: str | None = None) -> Line:
    from .schema import _DAY_PATTERN_TO_DAYS  # local import to avoid cycle at module top

    day_pattern = add.get("dayPattern", "everyDay")
    counts = add.get("countsTowardsProfessionCapacity", True)
    bay = True if profession == "medical" else bool(counts)
    cost = add.get("costPerWtePerYear")
    own_area = add.get("areaId") or None
    return Line(
        label=str(add.get("label", "addition")),
        profession=profession,
        n=float(add.get("wte", 0) or 0),
        rate=float(add.get("patientsPerHour", 0) or 0),
        s=float(add.get("startHour", 0) or 0),
        e=float(add.get("endHour", 0) or 0),
        days=_DAY_PATTERN_TO_DAYS.get(day_pattern, "all"),
        bay=bay,
        area_id=own_area if own_area is not None else area_id,
        cost=None if cost is None else float(cost),
    )


# --------------------------------------------------------------------------
# the core per-hour model (computeState)
# --------------------------------------------------------------------------
@dataclass
class AreaResult:
    """One area's hour-by-hour demand, capacity and constraint classification."""

    area_id: str
    name: str
    nursing_role: bool
    demand_is_placeholder: bool
    occ: list[float]
    arr: list[float]
    own_med_cap: list[float]
    alloc_med_cap: list[float]
    med_cap: list[float]
    own_nur_cap: list[float]
    alloc_nur_cap: list[float]
    nur_cap: list[float]
    space_cap: list[float]
    hold_cap: list[float]
    queue: list[float]
    queue_enter: list[float]
    med_pressure: list[float]
    nur_pressure: list[float]
    space_pressure: list[float]
    overall_pressure: list[float]
    med_no_cover: list[bool]
    nur_no_cover: list[bool]
    hour_class: list[str]
    hour_counts: dict[str, int]
    h_constrained: int
    physical_spaces: float
    attend: float
    pth: float
    peak: float
    peak_h: int
    med_util: float
    med_cap_day: float
    nur_util: float
    space_util: float
    peak_queue: float
    queue_h: int

    @property
    def has_demand(self) -> bool:
        return self.pth > PRESSURE_EPS


@dataclass
class StateResult:
    """The floor-wide roll-up, plus the per-area detail that drives it."""

    day_type: str
    future: bool
    occ: list[float]
    arr: list[float]
    med_cap: list[float]
    nur_cap: list[float]
    space_cap: list[float]
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
    space_util: float
    peak_queue: float
    queue_h: int
    med_pressure: list[float]
    nur_pressure: list[float]
    space_pressure: list[float]
    overall_pressure: list[float]
    med_no_cover: list[bool]
    nur_no_cover: list[bool]
    hour_class: list[str]
    hour_counts: dict[str, int]
    h_constrained: int
    areas: list[AreaResult] = field(default_factory=list)

    def area(self, area_id: str) -> AreaResult | None:
        for a in self.areas:
            if a.area_id == area_id:
                return a
        return None

    @property
    def areas_constrained(self) -> list[AreaResult]:
        """Areas with at least one constrained hour - the "who is actually in
        trouble" list that a floor-wide average can hide."""
        return [a for a in self.areas if a.h_constrained > 0]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _pressure(demand: float, cap: float) -> tuple[float, bool]:
    """Demand over capacity, with the source tool's no-cover convention:
    demand with no capacity at all is infinite pressure, not a divide by zero."""
    if cap > PRESSURE_EPS:
        return demand / cap, False
    if demand > PRESSURE_EPS:
        return math.inf, True
    return 0.0, False


def area_demand_scale(area: Area, future: Future) -> float:
    """How much this area's demand moves in the future state.

    The per-referral-source multipliers reach an area through its own source
    mix, so scaling hot clinics up scales the hot clinic rooms and nothing
    else. An area with no source mix moves on the overall dial alone.
    """
    base = sum(area.sources.get(k, 0.0) for k in SOURCE_KEYS)
    if base <= 0:
        return future.demand_mult
    fut = sum(area.sources.get(k, 0.0) * future.src_mult.get(k, 1.0) for k in SOURCE_KEYS)
    return (fut / base) * future.demand_mult


def compute_state(model: Model, day_type: str = "weekday", future: bool = False) -> StateResult:
    """Run the per-hour, per-area demand-vs-capacity model for one day type.

    ``day_type`` is "weekday" or "weekend" (the source's ``weekendView``).
    ``future`` toggles the future-state scaling and capacity additions.
    """
    weekend = day_type == "weekend"
    a = model.assumptions
    areas = model.areas

    med_lines = list(model.med_lines)
    nur_lines = list(model.nur_lines)
    space_extra: dict[str, float] = {}
    if future:
        med_lines = med_lines + active_levers(model.future, "med") + custom_addition_lines(model.future, "med")
        nur_lines = nur_lines + active_levers(model.future, "nur") + custom_addition_lines(model.future, "nur")
        space_extra = active_space_additions(model.future)

    # ---- per-area demand -------------------------------------------------
    # Arrivals are deconvolved from the *unscaled* curve and then scaled, and
    # the C4C diversion reduces patients present but NOT arrivals: both are
    # deliberate asymmetries carried over from the source tool.
    c4c = 1 - _clamp(model.future.c4c_pct or 0, 0, 100) / 100 if future else 1.0
    occ_by_area: dict[str, list[float]] = {}
    arr_by_area: dict[str, list[float]] = {}
    for area in areas:
        base_occ = list(area.occ(day_type))
        base_arr = deconv(base_occ, model.mean_los)
        if future:
            dscale = area_demand_scale(area, model.future)
            occ_by_area[area.id] = [v * dscale * model.future.los_mult * c4c for v in base_occ]
            arr_by_area[area.id] = [v * dscale for v in base_arr]
        else:
            occ_by_area[area.id] = base_occ
            arr_by_area[area.id] = base_arr
        for h in range(HOURS):
            if in_window(h, a.open_hour, a.close_hour):
                continue
            occ_by_area[area.id][h] = 0.0
            arr_by_area[area.id][h] = 0.0

    # ---- per-area and floating capacity ----------------------------------
    own_med: dict[str, list[float]] = {}
    own_nur: dict[str, list[float]] = {}
    space: dict[str, list[float]] = {}
    spaces_total: dict[str, float] = {}
    for area in areas:
        own_med[area.id] = med_capacity(lines_for_area(med_lines, area.id), weekend, a)
        own_nur[area.id] = nur_capacity(lines_for_area(nur_lines, area.id), weekend, a)
        total_spaces = max(0.0, area.physical_spaces + space_extra.get(area.id, 0.0))
        spaces_total[area.id] = total_spaces
        space[area.id] = [
            total_spaces if in_window(h, a.open_hour, a.close_hour) else 0.0 for h in range(HOURS)
        ]
    float_med = med_capacity(lines_for_area(med_lines, None), weekend, a)
    float_nur = nur_capacity(lines_for_area(nur_lines, None), weekend, a)

    # ---- hour by hour, all areas at once ---------------------------------
    # The floating pool has to be shared out before each area's queue can be
    # rolled forward, so the hours are walked jointly rather than area by area.
    alloc_med = {area.id: [0.0] * HOURS for area in areas}
    alloc_nur = {area.id: [0.0] * HOURS for area in areas}
    med_cap = {area.id: [0.0] * HOURS for area in areas}
    nur_cap = {area.id: [0.0] * HOURS for area in areas}
    queue = {area.id: [0.0] * HOURS for area in areas}
    queue_enter = {area.id: [0.0] * HOURS for area in areas}
    carried = {area.id: 0.0 for area in areas}

    for h in range(HOURS):
        demands = {area.id: arr_by_area[area.id][h] + carried[area.id] for area in areas}
        total_demand = sum(demands.values())
        total_occ = sum(occ_by_area[area.id][h] for area in areas)
        for area in areas:
            share_med = demands[area.id] / total_demand if total_demand > PRESSURE_EPS else 0.0
            share_nur = occ_by_area[area.id][h] / total_occ if total_occ > PRESSURE_EPS else 0.0
            alloc_med[area.id][h] = float_med[h] * share_med
            alloc_nur[area.id][h] = float_nur[h] * share_nur
            med_cap[area.id][h] = own_med[area.id][h] + alloc_med[area.id][h]
            nur_cap[area.id][h] = own_nur[area.id][h] + alloc_nur[area.id][h]
            queue_enter[area.id][h] = carried[area.id]
            carried[area.id] = max(0.0, demands[area.id] - med_cap[area.id][h])
            queue[area.id][h] = carried[area.id]

    # ---- per-area results -------------------------------------------------
    area_results: list[AreaResult] = []
    for area in areas:
        area_results.append(
            _area_result(area, a, occ_by_area[area.id], arr_by_area[area.id], own_med[area.id],
                         alloc_med[area.id], med_cap[area.id], own_nur[area.id], alloc_nur[area.id],
                         nur_cap[area.id], space[area.id], queue[area.id], queue_enter[area.id],
                         spaces_total[area.id])
        )

    # ---- floor-wide roll-up ----------------------------------------------
    def _sum(get) -> list[float]:
        return [sum(get(r)[h] for r in area_results) for h in range(HOURS)]

    occ = _sum(lambda r: r.occ)
    arr = _sum(lambda r: r.arr)
    floor_med_cap = [own + float_med[h] for h, own in enumerate(_sum(lambda r: r.own_med_cap))]
    floor_nur_cap = [own + float_nur[h] for h, own in enumerate(_sum(lambda r: r.own_nur_cap))]
    floor_space_cap = _sum(lambda r: r.space_cap)
    floor_queue = _sum(lambda r: r.queue)
    floor_queue_enter = _sum(lambda r: r.queue_enter)

    med_pressure = [0.0] * HOURS
    nur_pressure = [0.0] * HOURS
    space_pressure = [0.0] * HOURS
    overall_pressure = [0.0] * HOURS
    med_no_cover = [False] * HOURS
    nur_no_cover = [False] * HOURS
    hour_class = ["closed"] * HOURS
    hour_counts = empty_hour_counts()
    for h in range(HOURS):
        if not in_window(h, a.open_hour, a.close_hour):
            hour_class[h] = "closed"
            hour_counts["closed"] += 1
            continue
        med_pressure[h], med_no_cover[h] = _pressure(arr[h] + floor_queue_enter[h], floor_med_cap[h])
        nur_pressure[h], nur_no_cover[h] = _pressure(occ[h], floor_nur_cap[h])
        space_pressure[h], _ = _pressure(occ[h], floor_space_cap[h])
        overall_pressure[h] = max(med_pressure[h], nur_pressure[h], space_pressure[h])
        cls = classify_hour(
            med_pressure[h] > 1 + PRESSURE_EPS,
            nur_pressure[h] > 1 + PRESSURE_EPS,
            space_pressure[h] > 1 + PRESSURE_EPS,
        )
        hour_class[h] = cls
        hour_counts[cls] += 1
    h_constrained = sum(v for k, v in hour_counts.items() if k not in ("within", "closed"))

    attend = sum(arr)
    pth = sum(occ)
    peak, peak_h = _peak(occ)
    nur_util = 0.0
    nur_over_hrs = 0
    nur_peak_over = 0.0
    nur_peak_over_h = 0
    space_util = 0.0
    for h in range(HOURS):
        if not in_window(h, a.open_hour, a.close_hour):
            continue
        if floor_nur_cap[h] > 0:
            nur_util = max(nur_util, occ[h] / floor_nur_cap[h])
        if floor_space_cap[h] > 0:
            space_util = max(space_util, occ[h] / floor_space_cap[h])
        if occ[h] > floor_nur_cap[h] + 1e-6:
            nur_over_hrs += 1
        over = occ[h] - floor_nur_cap[h]
        if over > nur_peak_over:
            nur_peak_over = over
            nur_peak_over_h = h
    med_cap_day = sum(floor_med_cap)
    med_util = attend / med_cap_day if med_cap_day > 0 else 0.0
    peak_queue = max(floor_queue)
    queue_h = 0
    for h, v in enumerate(floor_queue):
        if v == peak_queue:
            queue_h = h

    return StateResult(
        day_type=day_type,
        future=future,
        occ=occ,
        arr=arr,
        med_cap=floor_med_cap,
        nur_cap=floor_nur_cap,
        space_cap=floor_space_cap,
        queue=floor_queue,
        queue_enter=floor_queue_enter,
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
        space_util=space_util,
        peak_queue=peak_queue,
        queue_h=queue_h,
        med_pressure=med_pressure,
        nur_pressure=nur_pressure,
        space_pressure=space_pressure,
        overall_pressure=overall_pressure,
        med_no_cover=med_no_cover,
        nur_no_cover=nur_no_cover,
        hour_class=hour_class,
        hour_counts=hour_counts,
        h_constrained=h_constrained,
        areas=area_results,
    )


def _peak(vals: list[float]) -> tuple[float, int]:
    peak = 0.0
    peak_h = 0
    for h, v in enumerate(vals):
        if v > peak:
            peak = v
            peak_h = h
    return peak, peak_h


def _area_result(area: Area, a: Assumptions, occ: list[float], arr: list[float],
                 own_med_cap: list[float], alloc_med_cap: list[float], med_cap: list[float],
                 own_nur_cap: list[float], alloc_nur_cap: list[float], nur_cap: list[float],
                 space_cap: list[float], queue: list[float], queue_enter: list[float],
                 physical_spaces: float) -> AreaResult:
    """Score one area's hours once its capacity and queue are known."""
    med_pressure = [0.0] * HOURS
    nur_pressure = [0.0] * HOURS
    space_pressure = [0.0] * HOURS
    overall_pressure = [0.0] * HOURS
    med_no_cover = [False] * HOURS
    nur_no_cover = [False] * HOURS
    hold_cap = [0.0] * HOURS
    hour_class = ["closed"] * HOURS
    hour_counts = empty_hour_counts()

    for h in range(HOURS):
        # An area is subject to a nursing holding constraint if it is declared
        # a nursing/concurrency area, or if nurses who hold patients are
        # actually rostered there this hour.
        nurse_managed = area.nursing_role or nur_cap[h] > PRESSURE_EPS
        hold_cap[h] = min(nur_cap[h], space_cap[h]) if nurse_managed else space_cap[h]
        if not in_window(h, a.open_hour, a.close_hour):
            hour_class[h] = "closed"
            hour_counts["closed"] += 1
            continue
        med_pressure[h], med_no_cover[h] = _pressure(arr[h] + queue_enter[h], med_cap[h])
        if nurse_managed:
            nur_pressure[h], nur_no_cover[h] = _pressure(occ[h], nur_cap[h])
        space_pressure[h], _ = _pressure(occ[h], space_cap[h])
        overall_pressure[h] = max(med_pressure[h], nur_pressure[h], space_pressure[h])
        cls = classify_hour(
            med_pressure[h] > 1 + PRESSURE_EPS,
            nur_pressure[h] > 1 + PRESSURE_EPS,
            space_pressure[h] > 1 + PRESSURE_EPS,
        )
        hour_class[h] = cls
        hour_counts[cls] += 1

    attend = sum(arr)
    med_cap_day = sum(med_cap)
    peak, peak_h = _peak(occ)
    nur_util = 0.0
    space_util = 0.0
    for h in range(HOURS):
        if not in_window(h, a.open_hour, a.close_hour):
            continue
        if nur_cap[h] > 0:
            nur_util = max(nur_util, occ[h] / nur_cap[h])
        if space_cap[h] > 0:
            space_util = max(space_util, occ[h] / space_cap[h])
    peak_queue = max(queue)
    queue_h = 0
    for h, v in enumerate(queue):
        if v == peak_queue:
            queue_h = h

    return AreaResult(
        area_id=area.id,
        name=area.name,
        nursing_role=area.nursing_role,
        demand_is_placeholder=area.demand_is_placeholder,
        occ=occ,
        arr=arr,
        own_med_cap=own_med_cap,
        alloc_med_cap=alloc_med_cap,
        med_cap=med_cap,
        own_nur_cap=own_nur_cap,
        alloc_nur_cap=alloc_nur_cap,
        nur_cap=nur_cap,
        space_cap=space_cap,
        hold_cap=hold_cap,
        queue=queue,
        queue_enter=queue_enter,
        med_pressure=med_pressure,
        nur_pressure=nur_pressure,
        space_pressure=space_pressure,
        overall_pressure=overall_pressure,
        med_no_cover=med_no_cover,
        nur_no_cover=nur_no_cover,
        hour_class=hour_class,
        hour_counts=hour_counts,
        h_constrained=sum(v for k, v in hour_counts.items() if k not in ("within", "closed")),
        physical_spaces=physical_spaces,
        attend=attend,
        pth=sum(occ),
        peak=peak,
        peak_h=peak_h,
        med_util=attend / med_cap_day if med_cap_day > 0 else 0.0,
        med_cap_day=med_cap_day,
        nur_util=nur_util,
        space_util=space_util,
        peak_queue=peak_queue,
        queue_h=queue_h,
    )


# --------------------------------------------------------------------------
# cost of explicit future-state staffing additions or removals
# --------------------------------------------------------------------------
@dataclass
class CapAdditionRow:
    label: str
    fam: str  # "med" | "nur"
    wte: float
    area_id: str | None = None
    cost: float | None = None  # per-WTE override


@dataclass
class SpaceAdditionRow:
    label: str
    area_id: str
    spaces: float


@dataclass
class CostSummary:
    rows: list[CapAdditionRow] = field(default_factory=list)
    space_rows: list[SpaceAdditionRow] = field(default_factory=list)
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

    @property
    def total_spaces(self) -> float:
        return sum(r.spaces for r in self.space_rows)


def capacity_addition_rows(model: Model) -> list[CapAdditionRow]:
    """Every active staffing lever and custom edit, each with its net WTE change.

    Sign convention: a WTE increase is a positive (cost) change; a WTE decrease
    is negative (a saving). Ported from the source ``capacityAdditionRows``.
    """
    rows: list[CapAdditionRow] = []
    for lv in model.future.levers:
        if not lv.get("enabled") or lv.get("type") == "physicalSpaceAddition":
            continue
        adds = lv.get("add", []) or []
        wte = sum(float(add.get("wte", 0) or 0) for add in adds)
        fam = _FAM_OF_PROFESSION.get(lv.get("profession"), "med")
        rows.append(CapAdditionRow(label=f"{lv.get('name', 'Lever')} (preset)", fam=fam, wte=wte,
                                   area_id=lv.get("areaId") or None))
    for fam, rows_src in (("med", model.future.custom_med), ("nur", model.future.custom_nur)):
        for r in rows_src:
            added = (r.n or 0.0) - (r.base_n or 0.0)
            if abs(added) > 1e-9:
                rows.append(CapAdditionRow(label=r.label, fam=fam, wte=added, area_id=r.area_id, cost=r.cost))
    return rows


def space_addition_rows(model: Model) -> list[SpaceAdditionRow]:
    """Every future-state change to an area's physical footprint.

    Physical space carries no staffing cost here - capital and estates cost is
    out of scope for this model, so these rows are reported but not priced.
    """
    rows: list[SpaceAdditionRow] = []
    for lv in model.future.levers:
        if not lv.get("enabled") or lv.get("type") != "physicalSpaceAddition":
            continue
        area_id = lv.get("areaId") or None
        spaces = float(lv.get("addSpaces", 0) or 0)
        if area_id and abs(spaces) > 1e-9:
            rows.append(SpaceAdditionRow(label=f"{lv.get('name', 'Lever')} (preset)", area_id=area_id, spaces=spaces))
    for area_id, delta in model.future.area_space_deltas.items():
        if abs(delta) > 1e-9:
            area = model.area(area_id)
            rows.append(SpaceAdditionRow(label=f"{area.name if area else area_id} - space change",
                                         area_id=area_id, spaces=float(delta)))
    return rows


def cost_summary(model: Model) -> CostSummary:
    """Annualised cost/saving of the explicit future-state staffing changes."""
    a = model.assumptions
    summary = CostSummary(rows=capacity_addition_rows(model), space_rows=space_addition_rows(model))
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
