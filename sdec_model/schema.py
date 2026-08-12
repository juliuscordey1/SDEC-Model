"""Load, validate and normalise the SDEC JSON config.

The JSON follows the ED workforce model's conventions (meta / areas / demand /
capacity / standards / future). This module keeps the JSON as the single source
of input truth and hands the engine a small, normalised in-memory model. No
calculation logic lives here.

Schema v2 is **per-area**: every physical area carries its own spaces, its own
hourly demand curve and its own referral-source mix, and every capacity line is
tagged with the area it belongs to (or ``None`` for the floating pool of staff
who circulate across the whole floor). Floor-wide figures are the roll-up.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# dayPattern (JSON, matching the ED model) <-> days (internal, matching the
# source tool's "all"/"wd"/"we"). Kept as a translation so the JSON reads like
# the ED schema while the engine reads like the source JavaScript.
_DAY_PATTERN_TO_DAYS = {"everyDay": "all", "weekday": "wd", "weekend": "we"}
_HOURS = 24
SOURCE_KEYS = ("edPull", "gp", "followup", "oneoneone", "hotClinic")


class SchemaError(ValueError):
    """Raised when the config is structurally invalid."""


@dataclass
class Line:
    """A normalised shift-staffing line (baseline, lever, or custom addition).

    Mirrors the source tool's capacity-line shape so the engine's capacity
    functions read the same as the original JavaScript:
        n    -- WTE on this line
        rate -- patients seen per clinician per hour (medical) OR patients
                safely held per nurse (nursing lines that hold patients)
        s, e -- start/end as decimal hours (e.g. 8.5 == 08:30). e may be < s
                to denote an overnight wrap (e.g. 14.0 -> 0.0 == 14:00-00:00).
        days -- "all" | "wd" | "we"
        bay  -- nursing only: whether the line holds patients (adds holding
                capacity to its area). Always True for medical lines.
        area_id -- the area this line is rostered to, or None for the floating
                pool (staff who circulate rather than being fixed to one area).
    """

    label: str
    profession: str  # "medical" | "nursing"
    n: float
    rate: float
    s: float
    e: float
    days: str
    bay: bool
    area_id: str | None = None
    cost: float | None = None  # per-WTE annual cost override; None -> use default
    base_n: float = 0.0  # for custom future additions: the pulled-in baseline WTE


@dataclass
class Area:
    """One physical area of the SDEC floor: spaces, demand and its own roster.

    ``nursing_role`` marks an area whose patients are held by nurses (the
    bays): a patients-per-nurse holding capacity applies there, and is then
    capped at ``physical_spaces``. Areas without a nursing role are constrained
    by their physical spaces alone.
    """

    id: str
    name: str
    order: int = 0
    trolleys: float = 0.0
    chairs: float = 0.0
    rooms: float = 0.0
    nursing_role: bool = False
    occ_weekday: list[float] = field(default_factory=lambda: [0.0] * _HOURS)
    occ_weekend: list[float] = field(default_factory=lambda: [0.0] * _HOURS)
    sources: dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in SOURCE_KEYS})
    demand_is_placeholder: bool = True
    notes: str = ""

    @property
    def physical_spaces(self) -> float:
        return self.trolleys + self.chairs + self.rooms

    def occ(self, day_type: str) -> list[float]:
        return self.occ_weekend if day_type == "weekend" else self.occ_weekday


@dataclass
class Assumptions:
    open_hour: int = 7
    close_hour: int = 24
    nurse_ratio: float = 4.0
    cost_med_wte: float = 110000.0
    cost_nur_wte: float = 42000.0


@dataclass
class Future:
    demand_mult: float = 1.0
    los_mult: float = 1.0
    c4c_pct: float = 0.0
    src_mult: dict[str, float] = field(default_factory=dict)
    area_space_deltas: dict[str, float] = field(default_factory=dict)
    levers: list[dict[str, Any]] = field(default_factory=list)
    custom_med: list[Line] = field(default_factory=list)
    custom_nur: list[Line] = field(default_factory=list)


@dataclass
class Model:
    meta: dict[str, Any]
    assumptions: Assumptions
    mean_los: float
    areas: list[Area]
    med_lines: list[Line]
    nur_lines: list[Line]
    future: Future
    raw: dict[str, Any] = field(default_factory=dict)

    def area(self, area_id: str) -> Area | None:
        for a in self.areas:
            if a.id == area_id:
                return a
        return None

    @property
    def area_ids(self) -> list[str]:
        return [a.id for a in self.areas]

    def occ(self, day_type: str) -> list[float]:
        """Floor-wide patients present - the sum across areas."""
        out = [0.0] * _HOURS
        for a in self.areas:
            for h, v in enumerate(a.occ(day_type)):
                out[h] += v
        return out

    @property
    def sources(self) -> dict[str, float]:
        """Floor-wide referral-source volumes - the sum across areas."""
        return {k: sum(a.sources.get(k, 0.0) for a in self.areas) for k in SOURCE_KEYS}

    @property
    def physical_spaces(self) -> float:
        """Floor-wide physical footprint - the sum across areas."""
        return sum(a.physical_spaces for a in self.areas)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise SchemaError(msg)


def _num(v: Any, default: float | None = None) -> float:
    if v is None and default is not None:
        return float(default)
    try:
        return float(v)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise SchemaError(f"expected a number, got {v!r}") from exc


def _profile(arr: Any, name: str) -> list[float]:
    _require(isinstance(arr, list), f"{name} must be a list")
    _require(len(arr) == _HOURS, f"{name} must have {_HOURS} hourly entries, got {len(arr)}")
    return [_num(v) for v in arr]


def _area_id(entry: dict[str, Any], key: str = "areaId") -> str | None:
    """Read an area tag. Missing, null or empty all mean "floating"."""
    v = entry.get(key)
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _line_from_capacity(entry: dict[str, Any], known_areas: set[str]) -> Line:
    profession = entry.get("profession")
    _require(profession in ("medical", "nursing"), f"line {entry.get('id')!r}: profession must be medical or nursing")
    day_pattern = entry.get("dayPattern", "everyDay")
    _require(day_pattern in _DAY_PATTERN_TO_DAYS, f"line {entry.get('id')!r}: unknown dayPattern {day_pattern!r}")
    area_id = _area_id(entry)
    _require(area_id is None or area_id in known_areas, f"line {entry.get('id')!r}: unknown areaId {area_id!r}")
    # For medical lines every line contributes seeing capacity; for nursing the
    # "counts towards profession capacity" flag is the source tool's bay flag -
    # whether the line holds patients and so adds holding capacity to its area.
    counts = entry.get("countsTowardsProfessionCapacity", True)
    bay = True if profession == "medical" else bool(counts)
    cost = entry.get("costPerWtePerYear")
    return Line(
        label=str(entry.get("name", entry.get("id", "line"))),
        profession=profession,
        n=_num(entry.get("wte", 0)),
        rate=_num(entry.get("patientsPerHour", 0)),
        s=_num(entry.get("startHour", 0)),
        e=_num(entry.get("endHour", 0)),
        days=_DAY_PATTERN_TO_DAYS[day_pattern],
        bay=bay,
        area_id=area_id,
        cost=None if cost is None else _num(cost),
    )


def _line_from_add(entry: dict[str, Any], profession: str, area_id: str | None = None) -> Line:
    day_pattern = entry.get("dayPattern", "everyDay")
    _require(day_pattern in _DAY_PATTERN_TO_DAYS, f"add line: unknown dayPattern {day_pattern!r}")
    counts = entry.get("countsTowardsProfessionCapacity", True)
    bay = True if profession == "medical" else bool(counts)
    cost = entry.get("costPerWtePerYear")
    # An add line may carry its own areaId; otherwise it inherits the scheme's.
    own_area = _area_id(entry)
    return Line(
        label=str(entry.get("label", "addition")),
        profession=profession,
        n=_num(entry.get("wte", 0)),
        rate=_num(entry.get("patientsPerHour", 0)),
        s=_num(entry.get("startHour", 0)),
        e=_num(entry.get("endHour", 0)),
        days=_DAY_PATTERN_TO_DAYS[day_pattern],
        bay=bay,
        area_id=own_area if own_area is not None else area_id,
        cost=None if cost is None else _num(cost),
    )


def _custom_line(entry: dict[str, Any], profession: str) -> Line:
    line = _line_from_add(entry, profession)
    line.base_n = _num(entry.get("baseWte", entry.get("baseN", 0)))
    return line


def _parse_area(entry: dict[str, Any], index: int) -> Area:
    _require(isinstance(entry, dict), "areas[] entries must be objects")
    area_id = str(entry.get("id", "")).strip()
    _require(bool(area_id), f"areas[{index}]: id is required")
    spaces = entry.get("spaces", {}) or {}
    _require(isinstance(spaces, dict), f"area {area_id!r}: spaces must be an object")
    return Area(
        id=area_id,
        name=str(entry.get("name", area_id)),
        order=int(_num(entry.get("order", index))),
        trolleys=_num(spaces.get("trolleys", 0)),
        chairs=_num(spaces.get("chairs", 0)),
        rooms=_num(spaces.get("rooms", 0)),
        nursing_role=bool(entry.get("nursingConcurrency", False)),
        demand_is_placeholder=bool(entry.get("demandIsPlaceholder", False)),
        notes=str(entry.get("notes", "")),
    )


def parse_model(data: dict[str, Any]) -> Model:
    """Validate a parsed JSON dict and return a normalised :class:`Model`."""
    _require(isinstance(data, dict), "config root must be an object")

    meta = data.get("meta", {})
    _require(isinstance(meta, dict), "meta must be an object")

    # ---- assumptions -----------------------------------------------------
    a = data.get("assumptions", {}) or {}
    win = a.get("operatingWindow", {}) or {}
    assumptions = Assumptions(
        open_hour=int(_num(win.get("openHour", 7))),
        close_hour=int(_num(win.get("closeHour", 24))),
        nurse_ratio=_num(a.get("defaultNurseRatio", 4)),
        cost_med_wte=_num(a.get("defaultCostMedWtePerYear", 110000)),
        cost_nur_wte=_num(a.get("defaultCostNurWtePerYear", 42000)),
    )

    # ---- areas (physical layout) -----------------------------------------
    areas_raw = data.get("areas", [])
    _require(isinstance(areas_raw, list) and areas_raw, "areas must be a non-empty list")
    areas = [_parse_area(e, i) for i, e in enumerate(areas_raw) if e.get("active", True)]
    _require(bool(areas), "at least one area must be active")
    seen: set[str] = set()
    for ar in areas:
        _require(ar.id not in seen, f"duplicate area id {ar.id!r}")
        seen.add(ar.id)
    areas.sort(key=lambda x: x.order)
    by_id = {ar.id: ar for ar in areas}

    # ---- demand (one hourly curve + source mix per area) -------------------
    demand = data.get("demand", {})
    _require(isinstance(demand, dict), "demand must be an object")
    d_areas = demand.get("areas", [])
    _require(isinstance(d_areas, list) and d_areas, "demand.areas must be a non-empty list")
    for d in d_areas:
        area_id = _area_id(d)
        _require(area_id in by_id, f"demand.areas: unknown areaId {area_id!r}")
        area = by_id[area_id]
        present = d.get("patientsPresentOverride", {})
        _require(isinstance(present, dict), f"demand for {area_id!r}: patientsPresentOverride must be an object")
        area.occ_weekday = _profile(present.get("weekday"), f"{area_id}.patientsPresentOverride.weekday")
        area.occ_weekend = _profile(present.get("weekend"), f"{area_id}.patientsPresentOverride.weekend")
        src = d.get("sources", {}) or {}
        area.sources = {k: _num(src.get(k, 0)) for k in SOURCE_KEYS}
        if "isPlaceholder" in d:
            area.demand_is_placeholder = bool(d.get("isPlaceholder"))

    los_profiles = demand.get("losProfiles", [])
    _require(isinstance(los_profiles, list) and los_profiles, "demand.losProfiles must be a non-empty list")
    mean_los = _num(los_profiles[0].get("averageLosHours", 5.0))
    _require(mean_los > 0, "averageLosHours must be positive")

    # ---- capacity --------------------------------------------------------
    capacity = data.get("capacity", {})
    _require(isinstance(capacity, dict), "capacity must be an object")
    lines_raw = capacity.get("lines", [])
    _require(isinstance(lines_raw, list), "capacity.lines must be a list")
    med_lines: list[Line] = []
    nur_lines: list[Line] = []
    for entry in lines_raw:
        if not entry.get("active", True):
            continue
        line = _line_from_capacity(entry, seen)
        (med_lines if line.profession == "medical" else nur_lines).append(line)

    # ---- future ----------------------------------------------------------
    fut = data.get("future", {}) or {}
    src_mult = fut.get("sourceMultipliers", {}) or {}
    deltas = fut.get("areaSpaceDeltas", {}) or {}
    future = Future(
        demand_mult=_num(fut.get("demandMultiplier", 1.0)),
        los_mult=_num(fut.get("losMultiplier", 1.0)),
        c4c_pct=_num(fut.get("c4cPercent", 0.0)),
        src_mult={k: _num(src_mult.get(k, 1)) for k in SOURCE_KEYS},
        area_space_deltas={k: _num(v, 0) for k, v in deltas.items() if k in by_id},
        levers=list(fut.get("schemes", []) or []),
    )
    custom = fut.get("customCapacityLines", {}) or {}
    future.custom_med = [_custom_line(e, "medical") for e in custom.get("medical", []) or []]
    future.custom_nur = [_custom_line(e, "nursing") for e in custom.get("nursing", []) or []]

    return Model(
        meta=meta,
        assumptions=assumptions,
        mean_los=mean_los,
        areas=areas,
        med_lines=med_lines,
        nur_lines=nur_lines,
        future=future,
        raw=data,
    )


def load_model(path: str | Path) -> Model:
    """Load and validate a SDEC config from ``path``."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SchemaError(f"{path}: invalid JSON - {exc}") from exc
    return parse_model(data)
