"""Load, validate and normalise the SDEC JSON config.

The JSON follows the ED workforce model's conventions (meta / areas / demand /
capacity / standards / future). This module keeps the JSON as the single source
of input truth and hands the engine a small, normalised in-memory model. No
calculation logic lives here.
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


class SchemaError(ValueError):
    """Raised when the config is structurally invalid."""


@dataclass
class Line:
    """A normalised shift-staffing line (baseline, lever, or custom addition).

    Mirrors the source tool's capacity-line shape so the engine's capacity
    functions read the same as the original JavaScript:
        n    -- WTE on this line
        rate -- patients seen per clinician per hour (medical) OR patients
                safely managed per nurse (nursing bay lines)
        s, e -- start/end as decimal hours (e.g. 8.5 == 08:30). e may be < s
                to denote an overnight wrap (e.g. 14.0 -> 0.0 == 14:00-00:00).
        days -- "all" | "wd" | "we"
        bay  -- nursing only: whether the line carries patients (adds nursing
                concurrency capacity). Always True for medical lines.
    """

    label: str
    profession: str  # "medical" | "nursing"
    n: float
    rate: float
    s: float
    e: float
    days: str
    bay: bool
    cost: float | None = None  # per-WTE annual cost override; None -> use default
    base_n: float = 0.0  # for custom future additions: the pulled-in baseline WTE


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
    levers: list[dict[str, Any]] = field(default_factory=list)
    custom_med: list[Line] = field(default_factory=list)
    custom_nur: list[Line] = field(default_factory=list)


@dataclass
class Model:
    meta: dict[str, Any]
    assumptions: Assumptions
    mean_los: float
    occ_weekday: list[float]
    occ_weekend: list[float]
    sources: dict[str, float]
    med_lines: list[Line]
    nur_lines: list[Line]
    future: Future
    raw: dict[str, Any] = field(default_factory=dict)

    def occ(self, day_type: str) -> list[float]:
        return self.occ_weekend if day_type == "weekend" else self.occ_weekday


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


def _line_from_capacity(entry: dict[str, Any]) -> Line:
    profession = entry.get("profession")
    _require(profession in ("medical", "nursing"), f"line {entry.get('id')!r}: profession must be medical or nursing")
    day_pattern = entry.get("dayPattern", "everyDay")
    _require(day_pattern in _DAY_PATTERN_TO_DAYS, f"line {entry.get('id')!r}: unknown dayPattern {day_pattern!r}")
    # For medical lines every line contributes seeing capacity; for nursing the
    # "counts towards profession capacity" flag is the source tool's bay flag.
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
        cost=None if cost is None else _num(cost),
    )


def _line_from_add(entry: dict[str, Any], profession: str) -> Line:
    day_pattern = entry.get("dayPattern", "everyDay")
    _require(day_pattern in _DAY_PATTERN_TO_DAYS, f"add line: unknown dayPattern {day_pattern!r}")
    counts = entry.get("countsTowardsProfessionCapacity", True)
    bay = True if profession == "medical" else bool(counts)
    cost = entry.get("costPerWtePerYear")
    return Line(
        label=str(entry.get("label", "addition")),
        profession=profession,
        n=_num(entry.get("wte", 0)),
        rate=_num(entry.get("patientsPerHour", 0)),
        s=_num(entry.get("startHour", 0)),
        e=_num(entry.get("endHour", 0)),
        days=_DAY_PATTERN_TO_DAYS[day_pattern],
        bay=bay,
        cost=None if cost is None else _num(cost),
    )


def _custom_line(entry: dict[str, Any], profession: str) -> Line:
    line = _line_from_add(entry, profession)
    line.base_n = _num(entry.get("baseWte", entry.get("baseN", 0)))
    return line


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

    # ---- demand ----------------------------------------------------------
    demand = data.get("demand", {})
    _require(isinstance(demand, dict), "demand must be an object")
    d_areas = demand.get("areas", [])
    _require(isinstance(d_areas, list) and d_areas, "demand.areas must be a non-empty list")
    d0 = d_areas[0]
    present = d0.get("patientsPresentOverride", {})
    _require(isinstance(present, dict), "patientsPresentOverride must be an object")
    occ_weekday = _profile(present.get("weekday"), "patientsPresentOverride.weekday")
    occ_weekend = _profile(present.get("weekend"), "patientsPresentOverride.weekend")

    los_profiles = demand.get("losProfiles", [])
    _require(isinstance(los_profiles, list) and los_profiles, "demand.losProfiles must be a non-empty list")
    mean_los = _num(los_profiles[0].get("averageLosHours", 5.0))
    _require(mean_los > 0, "averageLosHours must be positive")

    src = demand.get("sources", {}) or {}
    sources = {k: _num(src.get(k, 0)) for k in ("edPull", "gp", "followup", "oneoneone", "hotClinic")}

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
        line = _line_from_capacity(entry)
        (med_lines if line.profession == "medical" else nur_lines).append(line)

    # ---- future ----------------------------------------------------------
    fut = data.get("future", {}) or {}
    src_mult = fut.get("sourceMultipliers", {}) or {}
    future = Future(
        demand_mult=_num(fut.get("demandMultiplier", 1.0)),
        los_mult=_num(fut.get("losMultiplier", 1.0)),
        c4c_pct=_num(fut.get("c4cPercent", 0.0)),
        src_mult={k: _num(src_mult.get(k, 1)) for k in ("edPull", "gp", "followup", "oneoneone", "hotClinic")},
        levers=list(fut.get("schemes", []) or []),
    )
    custom = fut.get("customCapacityLines", {}) or {}
    future.custom_med = [_custom_line(e, "medical") for e in custom.get("medical", []) or []]
    future.custom_nur = [_custom_line(e, "nursing") for e in custom.get("nursing", []) or []]

    return Model(
        meta=meta,
        assumptions=assumptions,
        mean_los=mean_los,
        occ_weekday=occ_weekday,
        occ_weekend=occ_weekend,
        sources=sources,
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
