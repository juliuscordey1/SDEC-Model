"use strict";
/*
 * Browser-engine oracle for the Python port.
 *
 * Extracts the MODEL CORE region of interactive/base_tool.html verbatim - the
 * maths the live editor actually runs - boots it from data/sdec_model.json via
 * the config bridge, and prints per-hour arrays (floor-wide AND per area) as
 * JSON for a battery of scenarios. The pytest suite asserts sdec_model/engine.py
 * reproduces it, so the browser tool and the Python engine cannot silently drift.
 *
 * Usage:  node tests/js_engine_check.js > tests/fixtures/js_engine.json
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const tool = fs.readFileSync(path.join(ROOT, "interactive", "base_tool.html"), "utf8");
const bridgeSrc = fs.readFileSync(path.join(ROOT, "interactive", "config_bridge.js"), "utf8");
const CONFIG = JSON.parse(fs.readFileSync(path.join(ROOT, "data", "sdec_model.json"), "utf8"));

const START = "// MODEL CORE START";
const END = "// MODEL CORE END";
const i0 = tool.indexOf(START), i1 = tool.indexOf(END);
if (i0 < 0 || i1 < 0) {
  console.error("MODEL CORE markers not found in interactive/base_tool.html");
  process.exit(2);
}
const core = tool.slice(i0, i1);

// The core reads a few things the surrounding tool provides.
const SOURCES = [
  ["edPull", "ED pull"],
  ["gp", "GP referrals"],
  ["followup", "Follow-up"],
  ["oneoneone", "111"],
  ["hotClinic", "Hot clinics"],
];
let state = null;

// Evaluate bridge + core in one scope so their declarations see each other.
// `state` is assigned per scenario; the core reads it through the closure.
const harness = `
  ${bridgeSrc}
  ${core}
  globalThis.__configToState = configToState;
  globalThis.__stateToConfig = stateToConfig;
  globalThis.__computeState = computeState;
  globalThis.__setState = s => { state = s; };
`;
eval(harness);
const configToState = globalThis.__configToState;
const computeState = globalThis.__computeState;
const setState = globalThis.__setState;

function freshState() {
  const s = configToState(CONFIG);
  setState(s);
  state = s;
  return s;
}

function resetFuture(s) {
  s.future.demandMult = 1.0;
  s.future.losMult = 1.0;
  s.future.c4cPct = 0;
  s.future.srcMult = { edPull: 1, gp: 1, followup: 1, oneoneone: 1, hotClinic: 1 };
  s.future.customCapacity = { med: [], nur: [] };
  s.future.areaSpaceDeltas = {};
  s.future.levers.forEach(lv => { lv.on = false; });
}

function applyScenario(sc) {
  const s = freshState();
  resetFuture(s);
  s.weekendView = !!sc.weekend;
  if (sc.levers) sc.levers.forEach(id => { const lv = s.future.levers.find(l => l.id === id); if (lv) lv.on = true; });
  if (sc.dials) {
    if (sc.dials.demandMult != null) s.future.demandMult = sc.dials.demandMult;
    if (sc.dials.losMult != null) s.future.losMult = sc.dials.losMult;
    if (sc.dials.c4cPct != null) s.future.c4cPct = sc.dials.c4cPct;
    if (sc.dials.srcMult) Object.assign(s.future.srcMult, sc.dials.srcMult);
  }
  if (sc.custom) s.future.customCapacity = sc.custom;
  if (sc.spaceDeltas) s.future.areaSpaceDeltas = sc.spaceDeltas;
  if (sc.retag) sc.retag.forEach(([fam, label, area]) => {
    const row = s.capacity[fam].find(r => r.label === label);
    if (row) row.area = area;
  });
  if (sc.mutate) sc.mutate(s);
  return s;
}

const SCENARIOS = [
  {name: "wd_current", weekend: false, future: false},
  {name: "we_current", weekend: true, future: false},
  {name: "wd_future_extcons", weekend: false, future: true, levers: ["ext-cons"]},
  {name: "we_future_wknduplift", weekend: true, future: true, levers: ["wknd-uplift"]},
  {name: "wd_future_twilight", weekend: false, future: true, levers: ["twilight-dr"]},
  {name: "wd_future_extrabay", weekend: false, future: true, levers: ["extra-bay"]},
  {name: "wd_future_hotclinicroom", weekend: false, future: true, levers: ["hot-clinic-room"]},
  {name: "wd_future_dials", weekend: false, future: true,
    dials: {demandMult: 1.2, losMult: 1.1, c4cPct: 10, srcMult: {edPull: 1.5, gp: 0.8}}},
  {name: "wd_future_hotclinic_only", weekend: false, future: true,
    dials: {srcMult: {hotClinic: 2.0}}},
  {name: "wd_future_custom_removal", weekend: false, future: true,
    custom: {med: [{label: "SHO reduced", n: 1, baseN: 3, rate: 0.5, cost: 110000, s: "08:30", e: "17:00", days: "wd", area: ""}], nur: []}},
  {name: "wd_future_space_delta", weekend: false, future: true,
    spaceDeltas: {"hot-clinic": 4, "ambulatory-chairs": -30}},
  // a clinician pinned to one area instead of floating - the case that makes the
  // per-area seeing capacities genuinely diverge from each other
  {name: "wd_retag_gp_to_hotclinic", weekend: false, future: false,
    retag: [["med", "Long Day GP", "hot-clinic"]]},
  // demand moved into a bay, so the bays' nursing/space ceilings actually bite
  {name: "wd_demand_in_bays", weekend: false, future: false,
    mutate: s => {
      const bay = s.areas.find(a => a.id === "bay-1");
      const amb = s.areas.find(a => a.id === "ambulatory-chairs");
      for (let h = 0; h < 24; h++) { bay.demand.wd[h] = amb.demand.wd[h] * 0.5; amb.demand.wd[h] *= 0.5; }
      bay.sources.edPull = 7.1; amb.sources.edPull = 7.1;
    }},
];

const AREA_FIELDS = ["occ", "arr", "ownMedCap", "allocMedCap", "medCap", "ownNurCap", "allocNurCap",
  "nurCap", "spaceCap", "holdCap", "queue", "queueEnter"];
const FLOOR_FIELDS = ["occ", "arr", "medCap", "nurCap", "spaceCap", "queue", "queueEnter"];
const SCALARS = ["attend", "pth", "peak", "peakH", "nurUtil", "nurOverHrs", "medUtil", "medCapDay",
  "spaceUtil", "peakQueue", "hConstrained"];
const AREA_SCALARS = ["attend", "pth", "peak", "peakH", "medUtil", "medCapDay", "nurUtil", "spaceUtil",
  "peakQueue", "hConstrained", "physicalSpaces"];

// Infinity is not valid JSON; encode no-cover hours via the boolean masks instead
const finite = a => a.map(v => Number.isFinite(v) ? v : null);

function pack(res) {
  const out = {scalars: {}, arrays: {}, hourClass: res.hourClass, hourCounts: res.hourCounts,
    medPressure: finite(res.medPressure), nurPressure: finite(res.nurPressure),
    spacePressure: finite(res.spacePressure), medNoCover: res.medNoCover, nurNoCover: res.nurNoCover,
    areas: []};
  SCALARS.forEach(k => { out.scalars[k] = res[k]; });
  FLOOR_FIELDS.forEach(k => { out.arrays[k] = res[k]; });
  for (const ar of res.areas) {
    const a = {areaId: ar.areaId, name: ar.name, hasDemand: ar.hasDemand, scalars: {}, arrays: {},
      hourClass: ar.hourClass, hourCounts: ar.hourCounts,
      medPressure: finite(ar.medPressure), nurPressure: finite(ar.nurPressure),
      spacePressure: finite(ar.spacePressure), medNoCover: ar.medNoCover, nurNoCover: ar.nurNoCover};
    AREA_SCALARS.forEach(k => { a.scalars[k] = ar[k]; });
    AREA_FIELDS.forEach(k => { a.arrays[k] = ar[k]; });
    out.areas.push(a);
  }
  return out;
}

const out = {};
for (const sc of SCENARIOS) {
  applyScenario(sc);
  out[sc.name] = {meta: {weekend: !!sc.weekend, future: !!sc.future}, result: pack(computeState(sc.future))};
}
process.stdout.write(JSON.stringify(out, null, 2));
