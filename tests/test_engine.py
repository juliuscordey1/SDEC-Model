"""Tests for the SDEC engine.

Two oracles pin the maths from both sides:

* ``tests/js_engine_check.js`` extracts the MODEL CORE region of the live
  browser tool and runs it over the same config, floor-wide and per area. The
  Python engine must reproduce it exactly - that is what keeps the interactive
  editor and this package numerically identical.
* ``tests/js_reference.js`` is the *pre-rearchitecture* floor-wide tool, kept
  verbatim. The per-area model's floor-wide roll-up must still reproduce it on
  the seeded placeholder split - the migration-fidelity check that says the
  rearchitecture moved the model apart without moving the totals.

The remaining tests cover the deconvolution round-trip, queue non-negativity,
overnight shift lines, the floating-pool allocation, physical-space capping,
the nine-way hour classification and the cost/saving sign convention.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path

import pytest

from sdec_model import compute_state, cost_summary, load_model
from sdec_model.engine import (
    HOUR_CLASSES,
    HOURS,
    classify_hour,
    covers,
    deconv,
    lines_for_area,
    parse_hm,
    space_addition_rows,
)
from sdec_model.schema import Assumptions, Line

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "sdec_model.json"
LEGACY_FIXTURE = ROOT / "tests" / "fixtures" / "js_reference.json"
LEGACY_JS = ROOT / "tests" / "js_reference.js"
ENGINE_FIXTURE = ROOT / "tests" / "fixtures" / "js_engine.json"
ENGINE_JS = ROOT / "tests" / "js_engine_check.js"

TOL = 1e-9
#: the placeholder area split is rounded to 2dp, so a scenario that scales
#: referral sources *differently* per source reaches the floor total by a very
#: slightly different route than the single floor-wide multiplier did
ROUNDING_TOL = 5e-3

SOURCE_KEYS = ("edPull", "gp", "followup", "oneoneone", "hotClinic")

#: old four-way class name -> new canonical name
LEGACY_CLASS = {"both": "medical+nursing"}


def _run_node(script: Path, fixture: Path) -> dict:
    """Regenerate an oracle via node when available, else use the committed
    fixture so the suite still runs without node."""
    if script.exists():
        try:
            proc = subprocess.run(
                ["node", str(script)],
                capture_output=True, text=True, timeout=60, check=True,
            )
            data = json.loads(proc.stdout)
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(json.dumps(data, indent=2), encoding="utf-8")
            return data
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            pass
    assert fixture.exists(), f"no node available and no committed fixture at {fixture}"
    return json.loads(fixture.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def js_reference() -> dict:
    """The legacy floor-wide oracle (the tool as it was before areas)."""
    return _run_node(LEGACY_JS, LEGACY_FIXTURE)


@pytest.fixture(scope="session")
def js_engine() -> dict:
    """The current browser engine, extracted from interactive/base_tool.html."""
    return _run_node(ENGINE_JS, ENGINE_FIXTURE)


def _reset_future(model) -> None:
    fut = model.future
    fut.demand_mult = 1.0
    fut.los_mult = 1.0
    fut.c4c_pct = 0.0
    fut.src_mult = {k: 1.0 for k in SOURCE_KEYS}
    fut.custom_med = []
    fut.custom_nur = []
    fut.area_space_deltas = {}
    for lv in fut.levers:
        lv["enabled"] = False


_LEVER_MAP = {
    "wd_future_extcons": ["ext-cons"],
    "we_future_wknduplift": ["wknd-uplift"],
    "wd_future_twilight": ["twilight-dr"],
    "wd_future_extrabay": ["extra-bay"],
    "wd_future_hotclinicroom": ["hot-clinic-room"],
}


def _apply_scenario(model, name: str) -> str:
    """Reproduce, on the Python model, the same scenario a JS oracle ran."""
    _reset_future(model)
    for lid in _LEVER_MAP.get(name, []):
        for lv in model.future.levers:
            if lv.get("id") == lid:
                lv["enabled"] = True

    if name == "wd_future_dials":
        model.future.demand_mult = 1.2
        model.future.los_mult = 1.1
        model.future.c4c_pct = 10
        model.future.src_mult["edPull"] = 1.5
        model.future.src_mult["gp"] = 0.8
    if name == "wd_future_hotclinic_only":
        model.future.src_mult["hotClinic"] = 2.0
    if name == "wd_future_custom_removal":
        model.future.custom_med = [
            Line(label="SHO reduced", profession="medical", n=1, rate=0.5,
                 s=8.5, e=17, days="wd", bay=True, area_id=None, cost=110000, base_n=3)
        ]
    if name == "wd_future_space_delta":
        model.future.area_space_deltas = {"hot-clinic": 4, "ambulatory-chairs": -30}
    if name == "wd_retag_gp_to_hotclinic":
        for line in model.med_lines:
            if line.label == "Long Day GP":
                line.area_id = "hot-clinic"
    if name == "wd_demand_in_bays":
        bay = model.area("bay-1")
        amb = model.area("ambulatory-chairs")
        for h in range(HOURS):
            bay.occ_weekday[h] = amb.occ_weekday[h] * 0.5
            amb.occ_weekday[h] *= 0.5
        bay.sources["edPull"] = 7.1
        amb.sources["edPull"] = 7.1
    return "weekend" if name.startswith("we_") else "weekday"


# --------------------------------------------------------------------------
# parity with the live browser engine (floor-wide AND per area)
# --------------------------------------------------------------------------
def test_browser_engine_parity(js_engine):
    """Python reproduces the interactive tool's own maths, area by area."""
    for name, entry in js_engine.items():
        model = load_model(CONFIG)
        day_type = _apply_scenario(model, name)
        res = compute_state(model, day_type, future=entry["meta"]["future"])
        js = entry["result"]

        for jsf, py in (("occ", res.occ), ("arr", res.arr), ("medCap", res.med_cap),
                        ("nurCap", res.nur_cap), ("spaceCap", res.space_cap),
                        ("queue", res.queue), ("queueEnter", res.queue_enter)):
            for h in range(HOURS):
                assert py[h] == pytest.approx(js["arrays"][jsf][h], abs=TOL), f"{name}: floor {jsf}[{h}]"
        for jsf, py in (("attend", res.attend), ("pth", res.pth), ("peak", res.peak),
                        ("peakH", res.peak_h), ("nurUtil", res.nur_util),
                        ("nurOverHrs", res.nur_over_hrs), ("medUtil", res.med_util),
                        ("medCapDay", res.med_cap_day), ("spaceUtil", res.space_util),
                        ("peakQueue", res.peak_queue), ("hConstrained", res.h_constrained)):
            assert py == pytest.approx(js["scalars"][jsf], abs=TOL), f"{name}: floor {jsf}"
        assert res.hour_class == js["hourClass"], f"{name}: floor hour_class"
        assert res.hour_counts == js["hourCounts"], f"{name}: floor hour_counts"

        assert [a.area_id for a in res.areas] == [a["areaId"] for a in js["areas"]], f"{name}: area order"
        for pa, ja in zip(res.areas, js["areas"]):
            where = f"{name}/{pa.area_id}"
            for jsf, py in (("occ", pa.occ), ("arr", pa.arr), ("ownMedCap", pa.own_med_cap),
                            ("allocMedCap", pa.alloc_med_cap), ("medCap", pa.med_cap),
                            ("ownNurCap", pa.own_nur_cap), ("allocNurCap", pa.alloc_nur_cap),
                            ("nurCap", pa.nur_cap), ("spaceCap", pa.space_cap),
                            ("holdCap", pa.hold_cap), ("queue", pa.queue),
                            ("queueEnter", pa.queue_enter)):
                for h in range(HOURS):
                    assert py[h] == pytest.approx(ja["arrays"][jsf][h], abs=TOL), f"{where}: {jsf}[{h}]"
            for jsf, py in (("attend", pa.attend), ("pth", pa.pth), ("peak", pa.peak),
                            ("peakH", pa.peak_h), ("medUtil", pa.med_util),
                            ("medCapDay", pa.med_cap_day), ("nurUtil", pa.nur_util),
                            ("spaceUtil", pa.space_util), ("peakQueue", pa.peak_queue),
                            ("hConstrained", pa.h_constrained),
                            ("physicalSpaces", pa.physical_spaces)):
                assert py == pytest.approx(ja["scalars"][jsf], abs=TOL), f"{where}: {jsf}"
            assert pa.hour_class == ja["hourClass"], f"{where}: hour_class"
            assert pa.hour_counts == ja["hourCounts"], f"{where}: hour_counts"
            assert pa.has_demand == ja["hasDemand"], f"{where}: has_demand"
            for h in range(HOURS):
                assert pa.med_no_cover[h] == ja["medNoCover"][h], f"{where}: medNoCover[{h}]"
                assert pa.nur_no_cover[h] == ja["nurNoCover"][h], f"{where}: nurNoCover[{h}]"


# --------------------------------------------------------------------------
# migration fidelity: the floor roll-up still reproduces the old floor model
# --------------------------------------------------------------------------
def test_floor_rollup_matches_legacy_floor_model(js_reference):
    """With the seeded placeholder split, floor-wide demand, medical capacity,
    nursing capacity and the queue are unchanged from the pre-area model.

    This is the whole claim of the migration: the model was taken apart, not
    re-scaled. The one scenario with *unequal* per-source multipliers differs in
    the third decimal only, because the placeholder area split is stored to 2dp.
    """
    for name, entry in js_reference.items():
        model = load_model(CONFIG)
        day_type = _apply_scenario(model, name)
        res = compute_state(model, day_type, future=entry["meta"]["future"])
        js = entry["result"]
        tol = ROUNDING_TOL if name == "wd_future_dials" else TOL

        for jsf, py in (("occ", res.occ), ("arr", res.arr), ("medCap", res.med_cap),
                        ("nurCap", res.nur_cap), ("queue", res.queue),
                        ("queueEnter", res.queue_enter)):
            for h in range(HOURS):
                assert py[h] == pytest.approx(js[jsf][h], abs=tol), (
                    f"{name}: {jsf}[{h}] python={py[h]} legacy={js[jsf][h]}"
                )
        for jsf, py in (("attend", res.attend), ("peak", res.peak), ("nurUtil", res.nur_util),
                        ("medUtil", res.med_util), ("medCapDay", res.med_cap_day),
                        ("peakQueue", res.peak_queue)):
            assert py == pytest.approx(js[jsf], abs=tol), f"{name}: {jsf}"

        # the classification also survives: physical space never binds floor-wide
        # in the seed (57 spaces against a ~21 peak), so the old four-way call stands
        expected = [LEGACY_CLASS.get(c, c) for c in js["hourClass"]]
        assert res.hour_class == expected, f"{name}: hour_class drifted from the legacy model"


def test_floor_totals_are_the_sum_of_areas():
    model = load_model(CONFIG)
    for day_type in ("weekday", "weekend"):
        for fut in (False, True):
            res = compute_state(model, day_type, future=fut)
            for h in range(HOURS):
                assert res.occ[h] == pytest.approx(sum(a.occ[h] for a in res.areas), abs=TOL)
                assert res.arr[h] == pytest.approx(sum(a.arr[h] for a in res.areas), abs=TOL)
                assert res.queue[h] == pytest.approx(sum(a.queue[h] for a in res.areas), abs=TOL)
                assert res.space_cap[h] == pytest.approx(sum(a.space_cap[h] for a in res.areas), abs=TOL)


def test_floor_capacity_is_area_capacity_plus_the_floating_pool():
    """Floor medical/nursing capacity is every area's own staff plus the floating
    pool - the allocated shares must not be double-counted."""
    model = load_model(CONFIG)
    res = compute_state(model, "weekday")
    for h in range(HOURS):
        allocated = sum(a.alloc_med_cap[h] for a in res.areas)
        own = sum(a.own_med_cap[h] for a in res.areas)
        assert res.med_cap[h] == pytest.approx(own + allocated, abs=TOL)
        assert sum(a.med_cap[h] for a in res.areas) == pytest.approx(res.med_cap[h], abs=TOL)


# --------------------------------------------------------------------------
# the floating pool
# --------------------------------------------------------------------------
def test_all_floating_staff_gives_every_area_the_floor_pressure():
    """When every clinician circulates (the seeded default), an area's medical
    pressure is the floor's - the per-area view degrades gracefully rather than
    reporting "no cover" everywhere."""
    model = load_model(CONFIG)
    assert all(l.area_id is None for l in model.med_lines), "seed should keep medical lines floating"
    res = compute_state(model, "weekday")
    for area in res.areas:
        for h in range(HOURS):
            if area.occ[h] <= 0 and area.arr[h] <= 0:
                continue
            assert area.med_pressure[h] == pytest.approx(res.med_pressure[h], abs=1e-6), (area.area_id, h)


def test_tagging_a_clinician_to_one_area_moves_capacity_there():
    model = load_model(CONFIG)
    before = compute_state(model, "weekday").area("hot-clinic")
    for line in model.med_lines:
        if line.label == "Long Day GP":
            line.area_id = "hot-clinic"
    after = compute_state(model, "weekday").area("hot-clinic")
    assert sum(after.own_med_cap) > sum(before.own_med_cap) == 0
    assert sum(after.med_cap) > sum(before.med_cap)


def test_floating_allocation_never_exceeds_the_pool():
    model = load_model(CONFIG)
    res = compute_state(model, "weekday")
    float_lines = lines_for_area(model.med_lines, None)
    assert float_lines
    for h in range(HOURS):
        allocated = sum(a.alloc_med_cap[h] for a in res.areas)
        assert allocated <= res.med_cap[h] + TOL


# --------------------------------------------------------------------------
# physical space
# --------------------------------------------------------------------------
def test_hold_capacity_is_capped_at_physical_spaces():
    """A bay's 2 WTE x 4 patients = 8 of nursing capacity is capped by its 6
    physical spaces, so the effective holding ceiling is 6."""
    model = load_model(CONFIG)
    res = compute_state(model, "weekday")
    bay = res.area("bay-1")
    on_shift = [h for h in range(HOURS) if bay.nur_cap[h] > 0]
    assert on_shift
    for h in on_shift:
        assert bay.nur_cap[h] == pytest.approx(8.0)
        assert bay.space_cap[h] == pytest.approx(6.0)
        assert bay.hold_cap[h] == pytest.approx(6.0)


def test_area_without_a_nursing_role_uses_spaces_alone():
    model = load_model(CONFIG)
    res = compute_state(model, "weekday")
    amb = res.area("ambulatory-chairs")
    assert not amb.nursing_role
    for h in range(HOURS):
        assert amb.hold_cap[h] == pytest.approx(amb.space_cap[h])
        assert amb.nur_pressure[h] == 0.0  # no nursing constraint concept here
        assert "nursing" not in amb.hour_class[h]


def test_space_constraint_is_reported_per_area():
    """The hot clinic rooms overflow their 2 rooms under the placeholder demand -
    exactly the per-area signal a floor-wide total (57 spaces) cannot show."""
    model = load_model(CONFIG)
    res = compute_state(model, "weekday")
    hot = res.area("hot-clinic")
    assert any("space" in c for c in hot.hour_class)
    assert res.space_util < 1.0, "the floor as a whole has plenty of space"
    assert "space" not in "".join(res.hour_class), "so the floor strip never says space"


def test_future_space_lever_relieves_the_area_it_targets():
    model = load_model(CONFIG)
    before = compute_state(model, "weekday", future=True).area("hot-clinic")
    model.future.area_space_deltas = {"hot-clinic": 6}
    after = compute_state(model, "weekday", future=True).area("hot-clinic")
    assert after.physical_spaces == before.physical_spaces + 6
    assert after.space_util < before.space_util
    assert sum(1 for c in after.hour_class if "space" in c) < sum(1 for c in before.hour_class if "space" in c)


def test_space_addition_rows_are_reported_but_not_costed():
    model = load_model(CONFIG)
    model.future.area_space_deltas = {"bay-1": 2}
    summary = cost_summary(model)
    assert summary.total_spaces == 2
    assert summary.total_cost == 0.0
    assert [r.area_id for r in space_addition_rows(model)] == ["bay-1"]


# --------------------------------------------------------------------------
# seeding / migration of the old floor-wide inputs
# --------------------------------------------------------------------------
def test_seed_splits_bay_nursing_evenly_across_the_two_bays():
    model = load_model(CONFIG)
    for days, expected in (("wd", 2.0), ("we", 1.0)):
        for area_id in ("bay-1", "bay-2"):
            wte = sum(l.n for l in lines_for_area(model.nur_lines, area_id)
                      if l.days == days and l.bay)
            assert wte == pytest.approx(expected), (area_id, days)


def test_seed_keeps_floor_roles_floating_and_emts_in_darting():
    model = load_model(CONFIG)
    floating = {l.label for l in lines_for_area(model.nur_lines, None)}
    assert {"B7 NIC (co-ordinator)", "B6 Hello Nurse", "B6 In-reach Nurse", "Triage Nurse"} <= floating
    darting = {l.label for l in lines_for_area(model.nur_lines, "darting")}
    assert all("EMT" in label for label in darting) and darting


def test_seed_demand_split_sums_back_to_the_legacy_floor_profile():
    legacy_wd = [0, 0, 0, 0, 0, 0, 0, 0.69, 0.69, 5.73, 12.05, 16.09, 18.16, 20.03,
                 20.95, 21.26, 20.83, 18.99, 16.98, 14.52, 10.95, 7.57, 4.91, 3.09]
    model = load_model(CONFIG)
    for h in range(HOURS):
        assert model.occ("weekday")[h] == pytest.approx(legacy_wd[h], abs=1e-9)
    assert model.sources == pytest.approx(
        {"edPull": 14.2, "gp": 8.0, "followup": 9.9, "oneoneone": 0.1, "hotClinic": 5.0})


def test_seed_physical_layout_matches_the_confirmed_footprint():
    model = load_model(CONFIG)
    spaces = {a.id: (a.trolleys, a.chairs, a.rooms) for a in model.areas}
    assert spaces["bay-1"] == (3, 3, 0)
    assert spaces["bay-2"] == (3, 3, 0)
    assert spaces["ambulatory-chairs"] == (0, 40, 0)
    assert spaces["hot-clinic"] == (0, 0, 2)
    assert spaces["darting"] == (0, 0, 2)
    assert spaces["triage"] == (0, 0, 1)  # 2 rooms exist; only 1 is SDEC's
    assert model.physical_spaces == 57


def test_areas_without_placeholder_demand_are_flagged_not_silently_green():
    model = load_model(CONFIG)
    res = compute_state(model, "weekday")
    empty = {a.area_id for a in res.areas if not a.has_demand}
    assert empty == {"bay-1", "bay-2", "darting", "triage"}
    assert all(a.demand_is_placeholder for a in res.areas)


# --------------------------------------------------------------------------
# per-source future multipliers reach areas through their own mix
# --------------------------------------------------------------------------
def test_hot_clinic_multiplier_only_moves_the_hot_clinic_area():
    model = load_model(CONFIG)
    base = compute_state(model, "weekday", future=True)
    model.future.src_mult["hotClinic"] = 2.0
    after = compute_state(model, "weekday", future=True)
    assert after.area("hot-clinic").attend == pytest.approx(2 * base.area("hot-clinic").attend, abs=1e-9)
    assert after.area("ambulatory-chairs").attend == pytest.approx(
        base.area("ambulatory-chairs").attend, abs=1e-9)


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
    for area in model.areas:
        arr = deconv(area.occ_weekday, model.mean_los)
        assert all(v >= 0 for v in arr), area.id


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
            for area in res.areas:
                assert all(q >= 0 for q in area.queue), (area.area_id, day_type, fut)


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
# hour classification
# --------------------------------------------------------------------------
def test_classify_hour_names_every_biting_constraint():
    assert classify_hour(False, False, False) == "within"
    assert classify_hour(True, False, False) == "medical"
    assert classify_hour(False, True, False) == "nursing"
    assert classify_hour(False, False, True) == "space"
    assert classify_hour(True, True, False) == "medical+nursing"
    assert classify_hour(True, False, True) == "medical+space"
    assert classify_hour(False, True, True) == "nursing+space"
    assert classify_hour(True, True, True) == "medical+nursing+space"
    assert set(HOUR_CLASSES) == {
        "within", "medical", "nursing", "space", "medical+nursing",
        "medical+space", "nursing+space", "medical+nursing+space", "closed",
    }


def test_classification_values_are_valid():
    model = load_model(CONFIG)
    res = compute_state(model, "weekday", future=False)
    assert set(res.hour_class) <= set(HOUR_CLASSES)
    for area in res.areas:
        assert set(area.hour_class) <= set(HOUR_CLASSES)


def test_classification_consistent_with_pressures():
    """Each hour's class agrees with which pressures exceed 1, floor and area."""
    model = load_model(CONFIG)
    for day_type in ("weekday", "weekend"):
        res = compute_state(model, day_type, future=False)
        for st in [res] + list(res.areas):
            for h in range(HOURS):
                if st.hour_class[h] == "closed":
                    continue
                expected = classify_hour(
                    st.med_pressure[h] > 1 + 1e-6,
                    st.nur_pressure[h] > 1 + 1e-6,
                    st.space_pressure[h] > 1 + 1e-6,
                )
                assert st.hour_class[h] == expected, (day_type, h, st.hour_class[h], expected)


def test_hconstrained_matches_counts():
    model = load_model(CONFIG)
    res = compute_state(model, "weekday", future=False)
    expected = sum(v for k, v in res.hour_counts.items() if k not in ("within", "closed"))
    assert res.h_constrained == expected
    assert sum(res.hour_counts.values()) == HOURS


def test_areas_constrained_surfaces_what_the_floor_hides():
    model = load_model(CONFIG)
    res = compute_state(model, "weekday")
    assert res.areas_constrained, "at least one area is over on the seeded placeholder"
    assert all(a.h_constrained > 0 for a in res.areas_constrained)


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


def test_cost_row_carries_the_area_it_targets():
    model = load_model(CONFIG)
    for lv in model.future.levers:
        if lv.get("id") == "extra-bay":
            lv["enabled"] = True
    rows = cost_summary(model).rows
    assert len(rows) == 1
    assert rows[0].area_id == "bay-1"
    assert rows[0].fam == "nur"


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
    assert summary.space_rows == []
    assert summary.total_cost == 0.0


def test_per_line_cost_override_used_over_default():
    """A custom line's own cost overrides the profession default."""
    model = load_model(CONFIG)
    model.future.custom_nur = [
        Line(label="pricey nurse", profession="nursing", n=2, rate=4,
             s=8.0, e=20.5, days="all", bay=True, area_id="bay-2", cost=50000, base_n=0)
    ]
    summary = cost_summary(model)
    # 2 WTE * 50000 override (not the 42000 default)
    assert summary.nur_cost == pytest.approx(100000.0)


def test_no_cover_pressure_is_infinite_not_a_crash():
    """An area with demand and no capacity at all reports no cover."""
    model = load_model(CONFIG)
    model.med_lines = []
    res = compute_state(model, "weekday")
    amb = res.area("ambulatory-chairs")
    busy = [h for h in range(HOURS) if amb.arr[h] > 0]
    assert busy
    assert all(math.isinf(amb.med_pressure[h]) for h in busy)
    assert all(amb.med_no_cover[h] for h in busy)
