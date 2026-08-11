"""Tests for the SDEC engine.

The centrepiece is a parity check against the original JavaScript: the
``tests/js_reference.js`` oracle runs the source tool's own maths over the
source seed for a battery of scenarios, and we assert the Python port
reproduces its per-hour arrays within floating-point tolerance. The remaining
tests cover the deconvolution round-trip, queue non-negativity, overnight shift
lines, the four-way hour classification and the cost sign convention.
"""

from __future__ import annotations

import copy
import json
import math
import subprocess
from pathlib import Path

import pytest

from sdec_model import compute_state, cost_summary, load_model
from sdec_model.engine import HOURS, covers, deconv, parse_hm
from sdec_model.schema import Assumptions, Line

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "sdec_model.json"
FIXTURE = ROOT / "tests" / "fixtures" / "js_reference.json"
REFERENCE_JS = ROOT / "tests" / "js_reference.js"

TOL = 1e-9

ARRAY_FIELDS = ["occ", "arr", "med_cap", "nur_cap", "queue", "queue_enter"]
JS_ARRAY_NAME = {
    "occ": "occ",
    "arr": "arr",
    "med_cap": "medCap",
    "nur_cap": "nurCap",
    "queue": "queue",
    "queue_enter": "queueEnter",
}
SCALAR_FIELDS = {
    "attend": "attend",
    "pth": "pth",
    "peak": "peak",
    "peak_h": "peakH",
    "nur_util": "nurUtil",
    "nur_over_hrs": "nurOverHrs",
    "med_util": "medUtil",
    "med_cap_day": "medCapDay",
    "peak_queue": "peakQueue",
    "h_constrained": "hConstrained",
}


# --------------------------------------------------------------------------
# JS-parity fixture
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def js_reference() -> dict:
    """The JS oracle output. Regenerated via node when available, else the
    committed fixture is used so the suite still runs without node."""
    if REFERENCE_JS.exists():
        try:
            proc = subprocess.run(
                ["node", str(REFERENCE_JS)],
                capture_output=True, text=True, timeout=30, check=True,
            )
            data = json.loads(proc.stdout)
            FIXTURE.parent.mkdir(parents=True, exist_ok=True)
            FIXTURE.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    assert FIXTURE.exists(), "no node available and no committed js_reference.json fixture"
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _apply_scenario(model, name: str):
    """Reproduce, on the Python model, the same scenario the JS oracle ran."""
    weekend = name.startswith("we_")
    day_type = "weekend" if weekend else "weekday"
    fut = model.future
    # reset future to neutral
    fut.demand_mult = 1.0
    fut.los_mult = 1.0
    fut.c4c_pct = 0.0
    fut.src_mult = {k: 1.0 for k in ("edPull", "gp", "followup", "oneoneone", "hotClinic")}
    fut.custom_med = []
    fut.custom_nur = []
    for lv in fut.levers:
        lv["enabled"] = False

    lever_map = {
        "wd_future_extcons": ["ext-cons"],
        "we_future_wknduplift": ["wknd-uplift"],
        "wd_future_twilight": ["twilight-dr"],
        "wd_future_extrabay": ["extra-bay"],
    }
    for lid in lever_map.get(name, []):
        for lv in fut.levers:
            if lv.get("id") == lid:
                lv["enabled"] = True

    if name == "wd_future_dials":
        fut.demand_mult = 1.2
        fut.los_mult = 1.1
        fut.c4c_pct = 10
        fut.src_mult["edPull"] = 1.5
        fut.src_mult["gp"] = 0.8

    if name == "wd_future_custom_removal":
        fut.custom_med = [
            Line(label="SHO reduced", profession="medical", n=1, rate=0.5,
                 s=8.5, e=17, days="wd", bay=True, cost=110000, base_n=3)
        ]
    return day_type


def test_js_parity(js_reference):
    """Python reproduces the original JavaScript per-hour arrays and headline
    scalars for every scenario, within floating-point tolerance."""
    for name, entry in js_reference.items():
        model = load_model(CONFIG)
        day_type = _apply_scenario(model, name)
        is_future = entry["meta"]["future"]
        res = compute_state(model, day_type, future=is_future)
        js = entry["result"]

        for pyf in ARRAY_FIELDS:
            py_arr = getattr(res, pyf)
            js_arr = js[JS_ARRAY_NAME[pyf]]
            assert len(py_arr) == len(js_arr) == HOURS
            for h in range(HOURS):
                assert py_arr[h] == pytest.approx(js_arr[h], abs=TOL), (
                    f"{name}: {pyf}[{h}] python={py_arr[h]} js={js_arr[h]}"
                )

        for pyf, jsf in SCALAR_FIELDS.items():
            py_v = getattr(res, pyf)
            js_v = js[jsf]
            assert py_v == pytest.approx(js_v, abs=TOL), f"{name}: {pyf} python={py_v} js={js_v}"

        assert res.hour_class == js["hourClass"], f"{name}: hour_class differs"
        assert res.hour_counts == js["hourCounts"], f"{name}: hour_counts differs"

        # finite medical/nursing pressures match; no-cover masks match
        for h in range(HOURS):
            assert res.med_no_cover[h] == js["medNoCover"][h], f"{name}: medNoCover[{h}]"
            assert res.nur_no_cover[h] == js["nurNoCover"][h], f"{name}: nurNoCover[{h}]"
            if js["medPressure"][h] is not None and math.isfinite(res.med_pressure[h]):
                assert res.med_pressure[h] == pytest.approx(js["medPressure"][h], abs=TOL)
            if js["nurPressure"][h] is not None and math.isfinite(res.nur_pressure[h]):
                assert res.nur_pressure[h] == pytest.approx(js["nurPressure"][h], abs=TOL)


# --------------------------------------------------------------------------
# deconvolution
# --------------------------------------------------------------------------
def _boxcar_occupancy(arr: list[float], los: float) -> list[float]:
    """Forward box-car convolution: occupancy is the last ``los`` hours of
    arrivals, the oldest weighted by the fractional remainder. This is the
    inverse of :func:`deconv`."""
    occ = [0.0] * HOURS
    for h in range(HOURS):
        total = 0.0
        k = 0
        while los - k > 0:
            i = h - k
            if i >= 0:
                total += arr[i] * min(1.0, los - k)
            k += 1
        occ[h] = total
    return occ


@pytest.mark.parametrize("los", [5.0, 4.0, 3.5, 2.4, 6.25])
def test_deconv_round_trip(los):
    """Convolving arrivals into occupancy and deconvolving back recovers the
    original arrivals (for a strictly increasing/decaying non-negative curve)."""
    arr = [0, 0, 0, 0, 0, 0, 0, 1.0, 2.0, 3.5, 4.0, 4.2, 3.9, 3.0, 2.5,
           2.0, 1.5, 1.0, 0.8, 0.5, 0.3, 0.2, 0.1, 0.0]
    occ = _boxcar_occupancy(arr, los)
    recovered = deconv(occ, los)
    for h in range(HOURS):
        assert recovered[h] == pytest.approx(arr[h], abs=1e-9)


def test_deconv_non_negative():
    """Deconvolution never emits a negative arrival count."""
    model = load_model(CONFIG)
    arr = deconv(model.occ_weekday, model.mean_los)
    assert all(v >= 0 for v in arr)


# --------------------------------------------------------------------------
# queue
# --------------------------------------------------------------------------
def test_queue_never_negative():
    model = load_model(CONFIG)
    for day_type in ("weekday", "weekend"):
        for fut in (False, True):
            res = compute_state(model, day_type, future=fut)
            assert all(q >= 0 for q in res.queue), (day_type, fut)
            assert all(q >= 0 for q in res.queue_enter), (day_type, fut)


# --------------------------------------------------------------------------
# overnight (wraparound) shift lines
# --------------------------------------------------------------------------
def test_parse_hm():
    assert parse_hm("08:30") == 8.5
    assert parse_hm("00:00") == 0.0
    assert parse_hm("20:30") == 20.5
    assert parse_hm("24:00") == 24.0
    assert parse_hm(8.5) == 8.5


def test_overnight_covers():
    """A line that starts at 22:00 and ends at 06:00 covers late-night and
    early-morning hours but not the middle of the day."""
    a = Assumptions(open_hour=0, close_hour=24)
    night = Line(label="night", profession="nursing", n=1, rate=4,
                 s=22.0, e=6.0, days="all", bay=True)
    assert covers(night, 22, a.open_hour, a.close_hour)   # 22:30 -> covered
    assert covers(night, 23, a.open_hour, a.close_hour)   # 23:30 -> covered
    assert covers(night, 2, a.open_hour, a.close_hour)    # 02:30 -> covered
    assert covers(night, 5, a.open_hour, a.close_hour)    # 05:30 -> covered
    assert not covers(night, 6, a.open_hour, a.close_hour)  # 06:30 -> past end
    assert not covers(night, 12, a.open_hour, a.close_hour)  # midday -> not covered
    assert not covers(night, 21, a.open_hour, a.close_hour)  # 21:30 -> before start


def test_midnight_wrap_line_matches_normal_line_over_evening():
    """An add-line ending at 0.0 (midnight, overnight wrap) covers the evening
    hours up to close, exactly as the twilight lever intends."""
    a = Assumptions(open_hour=7, close_hour=24)
    twilight = Line(label="twilight", profession="medical", n=1, rate=0.6,
                    s=14.0, e=0.0, days="all", bay=True)
    for h in range(14, 24):
        assert covers(twilight, h, a.open_hour, a.close_hour), h
    assert not covers(twilight, 13, a.open_hour, a.close_hour)


def test_zero_length_line_never_covers():
    a = Assumptions(open_hour=0, close_hour=24)
    zero = Line(label="zero", profession="medical", n=1, rate=1, s=9.0, e=9.0, days="all", bay=True)
    assert not any(covers(zero, h, a.open_hour, a.close_hour) for h in range(HOURS))


# --------------------------------------------------------------------------
# four-way hour classification
# --------------------------------------------------------------------------
def test_classification_values_are_valid():
    model = load_model(CONFIG)
    res = compute_state(model, "weekday", future=False)
    assert set(res.hour_class) <= {"within", "medical", "nursing", "both", "closed"}


def test_classification_consistent_with_pressures():
    """Each hour's class agrees with whether medical and/or nursing pressure
    exceeds 1."""
    model = load_model(CONFIG)
    for day_type in ("weekday", "weekend"):
        res = compute_state(model, day_type, future=False)
        for h in range(HOURS):
            cls = res.hour_class[h]
            if cls == "closed":
                continue
            med_bad = res.med_pressure[h] > 1 + 1e-6
            nur_bad = res.nur_pressure[h] > 1 + 1e-6
            expected = "both" if (med_bad and nur_bad) else "medical" if med_bad else "nursing" if nur_bad else "within"
            assert cls == expected, (day_type, h, cls, expected)


def test_all_four_operating_classes_present_in_seed_weekday():
    """The seed weekday exercises every operating classification."""
    model = load_model(CONFIG)
    res = compute_state(model, "weekday", future=False)
    for cls in ("within", "medical", "nursing", "both"):
        assert res.hour_counts[cls] > 0, cls


def test_hconstrained_matches_counts():
    model = load_model(CONFIG)
    res = compute_state(model, "weekday", future=False)
    c = res.hour_counts
    assert res.h_constrained == c["medical"] + c["nursing"] + c["both"]


# --------------------------------------------------------------------------
# cost / saving sign convention
# --------------------------------------------------------------------------
def test_cost_of_addition_is_positive():
    """Enabling a lever that adds 1 WTE registers a positive (cost) change."""
    model = load_model(CONFIG)
    for lv in model.future.levers:
        if lv.get("id") == "ext-cons":
            lv["enabled"] = True
    summary = cost_summary(model)
    assert summary.med_wte == pytest.approx(1.0)
    assert summary.med_cost == pytest.approx(110000.0)
    assert summary.total_cost > 0


def test_cost_of_removal_is_a_saving():
    """A net WTE reduction registers a negative cost (a saving)."""
    model = load_model(CONFIG)
    model.future.custom_med = [
        Line(label="SHO reduced", profession="medical", n=1, rate=0.5,
             s=8.5, e=17, days="wd", bay=True, cost=110000, base_n=3)
    ]
    summary = cost_summary(model)
    assert summary.med_wte == pytest.approx(-2.0)
    assert summary.total_cost == pytest.approx(-220000.0)
    assert summary.total_cost < 0


def test_no_changes_means_no_cost_rows():
    model = load_model(CONFIG)
    summary = cost_summary(model)
    assert summary.rows == []
    assert summary.total_cost == 0.0


def test_per_line_cost_override_used_over_default():
    """A custom line's own cost overrides the profession default."""
    model = load_model(CONFIG)
    model.future.custom_nur = [
        Line(label="pricey nurse", profession="nursing", n=2, rate=4,
             s=8.0, e=20.5, days="all", bay=True, cost=50000, base_n=0)
    ]
    summary = cost_summary(model)
    # 2 WTE * 50000 override (not the 42000 default)
    assert summary.nur_cost == pytest.approx(100000.0)
