"use strict";
/*
 * JS reference oracle for the Python SDEC port.
 *
 * The functions and seed data below are copied VERBATIM from the source
 * "Medical SDEC Demand and Capacity Tool" (its <script> block). This file is
 * the ground truth: it runs the original JavaScript over the original seed for
 * a battery of scenarios and prints per-hour arrays as JSON. The pytest suite
 * compares the Python engine against this output.
 *
 * Do not "improve" the maths here - any change breaks the whole point of the
 * oracle. Usage:  node tests/js_reference.js  > tests/fixtures/js_reference.json
 */

// ---------- seed data (VERBATIM from the source tool) ----------
const WD_OCC = [0,0,0,0,0,0,0, 0.69,0.69,5.73,12.05,16.09,18.16,20.03,20.95,21.26,20.83,18.99,16.98,14.52,10.95,7.57,4.91,3.09];
const WE_OCC = [0,0,0,0,0,0,0, 0.65,0.65,3.32,6.68,8.85,10.15,11.12,11.29,10.71,10.76,9.24,7.74,6.24,4.21,2.29,1.35,0.91];

const state = {
  meanLos: 5.0,
  nurseRatio: 4,
  chairs: 21,
  openHour: 7,
  closeHour: 24,
  costMedWTE: 110000,
  costNurWTE: 42000,
  weekendView: false,
  ui: { curView: "overall", futView: "overall" },
  demand: { wd: WD_OCC.slice(), we: WE_OCC.slice() },
  sources: { edPull: 14.2, gp: 8.0, followup: 9.9, oneoneone: 0.1, hotClinic: 5.0 },
  capacity: {
    med: [
      {label: "Consultant",          n: 1, rate: 1.0,  cost: 110000, s: "08:30", e: "16:30", days: "all"},
      {label: "Long Day Registrar",  n: 2, rate: 0.67, cost: 110000, s: "08:30", e: "21:00", days: "wd"},
      {label: "Long Day GP",         n: 1, rate: 0.5,  cost: 110000, s: "08:30", e: "20:00", days: "wd"},
      {label: "Twilight IMT",        n: 1, rate: 0.6,  cost: 110000, s: "13:00", e: "22:00", days: "wd"},
      {label: "SHO (assumed rate)",  n: 3, rate: 0.5,  cost: 110000, s: "08:30", e: "17:00", days: "wd"},
      {label: "Registrar (weekend)", n: 1, rate: 0.67, cost: 110000, s: "09:00", e: "21:00", days: "we"},
      {label: "SHO (weekend)",       n: 1, rate: 0.5,  cost: 110000, s: "09:00", e: "17:00", days: "we"},
    ],
    nur: [
      {label: "B7 NIC (co-ordinator)", n: 1, rate: 4, cost: 42000, s: "08:00", e: "20:30", days: "all", bay: false},
      {label: "B6 Hello Nurse",        n: 1, rate: 4, cost: 42000, s: "08:00", e: "20:30", days: "all", bay: false},
      {label: "B6 In-reach Nurse",     n: 1, rate: 4, cost: 42000, s: "08:00", e: "20:30", days: "all", bay: false},
      {label: "Triage Nurse",          n: 1, rate: 4, cost: 42000, s: "08:00", e: "20:30", days: "wd", bay: false},
      {label: "Bay — B6 NIC",          n: 1, rate: 4, cost: 42000, s: "08:00", e: "20:30", days: "wd", bay: true},
      {label: "Bay — B5 nurses",       n: 3, rate: 4, cost: 42000, s: "08:00", e: "20:30", days: "wd", bay: true},
      {label: "Bay nurses (weekend)",  n: 2, rate: 4, cost: 42000, s: "08:00", e: "20:30", days: "we", bay: true},
      {label: "EMT (darting/float)",   n: 3, rate: 4, cost: 42000, s: "08:00", e: "20:30", days: "wd", bay: false},
      {label: "EMT (weekend)",         n: 1, rate: 4, cost: 42000, s: "08:00", e: "20:30", days: "we", bay: false},
    ],
  },
  future: {
    demandMult: 1.0,
    losMult: 1.0,
    c4cPct: 0,
    srcMult: { edPull: 1, gp: 1, followup: 1, oneoneone: 1, hotClinic: 1 },
    customCapacity: { med: [], nur: [] },
    levers: [
      {id: "ext-cons",   fam: "med", on: false, name: "Extend Consultant to 20:00", note: "",
        add: [{label: "Consultant — evening", n: 1, rate: 1.0, s: "16:30", e: "20:00", days: "all"}]},
      {id: "twilight-dr", fam: "med", on: false, name: "Add Twilight Doctor 14:00–24:00", note: "",
        add: [{label: "Twilight doctor", n: 1, rate: 0.6, s: "14:00", e: "00:00", days: "all"}]},
      {id: "extra-bay",  fam: "nur", on: false, name: "Add a Bay Nurse (Day)", note: "",
        add: [{label: "Extra bay nurse", n: 1, rate: 4, s: "08:00", e: "20:30", days: "all", bay: true}]},
      {id: "wknd-uplift", fam: "med", on: false, name: "Weekend Medical Uplift", note: "",
        add: [{label: "Weekend clinician", n: 1, rate: 0.6, s: "10:00", e: "20:00", days: "we"}]},
    ],
  },
};

// ---------- helpers (VERBATIM) ----------
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
function parseHM(t) { const [h, m] = String(t).split(":").map(Number); return (h || 0) + (m || 0) / 60; }
function inWindow(h) {
  const open = Math.max(0, Math.min(23, state.openHour || 0));
  const close = Math.max(open + 1, Math.min(24, state.closeHour || 24));
  return h >= open && h < close;
}
function lineActive(line) {
  if (line.days === "we") return state.weekendView;
  if (line.days === "wd") return !state.weekendView;
  return true;
}
function covers(line, h) {
  if (!inWindow(h)) return false;
  const t = h + 0.5, s = parseHM(line.s), e = parseHM(line.e);
  if (Math.abs(s - e) < 0.01) return false;
  return s < e ? (t >= s && t < e) : (t >= s || t < e);
}
function medCapacity(lines) {
  return Array.from({length: 24}, (_, h) =>
    lines.reduce((a, l) => a + ((lineActive(l) && covers(l, h)) ? l.n * (l.rate || 0) : 0), 0));
}
function nurCapacity(lines) {
  return Array.from({length: 24}, (_, h) =>
    lines.reduce((a, l) => {
      if (!(l.bay && lineActive(l) && covers(l, h))) return a;
      const perNurse = (Number.isFinite(l.rate) && l.rate > 0) ? l.rate : state.nurseRatio;
      return a + l.n * perNurse;
    }, 0));
}
function deconv(occ, los) {
  const arr = new Array(24).fill(0);
  for (let h = 0; h < 24; h++) {
    let s = 0, k = 1;
    while (los - k > 0) { const i = h - k; if (i >= 0) s += arr[i] * Math.min(1, los - k); k++; }
    arr[h] = Math.max(0, occ[h] - s);
  }
  return arr;
}
function activeLevers(fam) {
  const out = [];
  for (const lv of state.future.levers) if (lv.on && lv.fam === fam) for (const a of lv.add) out.push(a);
  return out;
}
function customAdditionLines(fam) {
  return state.future.customCapacity[fam]
    .map(r => ({...r, n: (r.n || 0) - (r.baseN || 0)}))
    .filter(r => Math.abs(r.n) > 1e-9);
}
function computeState(future) {
  const occBase = (state.weekendView ? state.demand.we : state.demand.wd).slice();
  const arrBase = deconv(occBase, state.meanLos);
  let occ = occBase, arr = arrBase;
  let medLines = state.capacity.med, nurLines = state.capacity.nur;

  if (future) {
    const s = state.sources, m = state.future.srcMult;
    const base = s.edPull + s.gp + s.followup + s.oneoneone + s.hotClinic || 1;
    const fut = s.edPull * m.edPull + s.gp * m.gp + s.followup * m.followup + s.oneoneone * m.oneoneone + s.hotClinic * m.hotClinic;
    const dscale = (fut / base) * state.future.demandMult;
    const c4c = 1 - clamp(state.future.c4cPct || 0, 0, 100) / 100;
    occ = occBase.map(v => v * dscale * state.future.losMult * c4c);
    arr = arrBase.map(v => v * dscale);
    medLines = medLines.concat(activeLevers("med")).concat(customAdditionLines("med"));
    nurLines = nurLines.concat(activeLevers("nur")).concat(customAdditionLines("nur"));
  }

  const medCap = medCapacity(medLines);
  const nurCap = nurCapacity(nurLines);

  for (let h = 0; h < 24; h++) {
    if (inWindow(h)) continue;
    occ[h] = 0; arr[h] = 0; medCap[h] = 0; nurCap[h] = 0;
  }

  const queueEnter = new Array(24).fill(0);
  const queue = new Array(24).fill(0);
  let q = 0;
  for (let h = 0; h < 24; h++) {
    queueEnter[h] = q;
    q = Math.max(0, q + arr[h] - medCap[h]);
    queue[h] = q;
  }

  const PRESSURE_EPS = 1e-6;
  const medPressure = new Array(24).fill(0);
  const nurPressure = new Array(24).fill(0);
  const overallPressure = new Array(24).fill(0);
  const medNoCover = new Array(24).fill(false);
  const nurNoCover = new Array(24).fill(false);
  const hourClass = new Array(24).fill("closed");
  const hourCounts = {within: 0, medical: 0, nursing: 0, both: 0, closed: 0};
  for (let h = 0; h < 24; h++) {
    if (!inWindow(h)) { hourClass[h] = "closed"; hourCounts.closed++; continue; }
    const medDemand = arr[h] + queueEnter[h];
    if (medCap[h] > PRESSURE_EPS) medPressure[h] = medDemand / medCap[h];
    else if (medDemand > PRESSURE_EPS) { medPressure[h] = Infinity; medNoCover[h] = true; }
    else medPressure[h] = 0;
    if (nurCap[h] > PRESSURE_EPS) nurPressure[h] = occ[h] / nurCap[h];
    else if (occ[h] > PRESSURE_EPS) { nurPressure[h] = Infinity; nurNoCover[h] = true; }
    else nurPressure[h] = 0;
    overallPressure[h] = Math.max(medPressure[h], nurPressure[h]);
    const medBad = medPressure[h] > 1 + PRESSURE_EPS;
    const nurBad = nurPressure[h] > 1 + PRESSURE_EPS;
    const cls = medBad && nurBad ? "both" : medBad ? "medical" : nurBad ? "nursing" : "within";
    hourClass[h] = cls;
    hourCounts[cls]++;
  }
  const hConstrained = hourCounts.medical + hourCounts.nursing + hourCounts.both;

  const attend = arr.reduce((a, b) => a + b, 0);
  const pth = occ.reduce((a, b) => a + b, 0);
  let peak = 0, peakH = 0;
  occ.forEach((v, h) => { if (v > peak) { peak = v; peakH = h; } });
  let nurUtil = 0, nurOverHrs = 0, nurPeakOver = 0, nurPeakOverH = 0;
  for (let h = 0; h < 24; h++) {
    if (!inWindow(h)) continue;
    if (nurCap[h] > 0) nurUtil = Math.max(nurUtil, occ[h] / nurCap[h]);
    if (occ[h] > nurCap[h] + 1e-6) nurOverHrs++;
    const over = occ[h] - nurCap[h];
    if (over > nurPeakOver) { nurPeakOver = over; nurPeakOverH = h; }
  }
  const medCapDay = medCap.reduce((a, b) => a + b, 0);
  const medUtil = medCapDay > 0 ? attend / medCapDay : 0;
  const peakQueue = Math.max(...queue);
  let queueH = 0; queue.forEach((v, h) => { if (v === peakQueue) queueH = h; });

  return {occ, arr, medCap, nurCap, queue, queueEnter, attend, pth, peak, peakH, nurUtil, nurOverHrs, nurPeakOver, nurPeakOverH,
    medUtil, medCapDay, peakQueue, queueH, hourCounts, hConstrained,
    // Infinity is not valid JSON; encode no-cover hours via the boolean masks instead
    medPressure: medPressure.map(v => Number.isFinite(v) ? v : null),
    nurPressure: nurPressure.map(v => Number.isFinite(v) ? v : null),
    medNoCover, nurNoCover, hourClass};
}

// ---------- scenario harness ----------
function resetFuture() {
  state.future.demandMult = 1.0;
  state.future.losMult = 1.0;
  state.future.c4cPct = 0;
  state.future.srcMult = { edPull: 1, gp: 1, followup: 1, oneoneone: 1, hotClinic: 1 };
  state.future.customCapacity = { med: [], nur: [] };
  state.future.levers.forEach(lv => { lv.on = false; });
}
function applyScenario(sc) {
  resetFuture();
  state.weekendView = !!sc.weekend;
  if (sc.levers) sc.levers.forEach(id => { const lv = state.future.levers.find(l => l.id === id); if (lv) lv.on = true; });
  if (sc.dials) {
    if (sc.dials.demandMult != null) state.future.demandMult = sc.dials.demandMult;
    if (sc.dials.losMult != null) state.future.losMult = sc.dials.losMult;
    if (sc.dials.c4cPct != null) state.future.c4cPct = sc.dials.c4cPct;
    if (sc.dials.srcMult) Object.assign(state.future.srcMult, sc.dials.srcMult);
  }
  if (sc.custom) state.future.customCapacity = sc.custom;
}

const SCENARIOS = [
  {name: "wd_current",              weekend: false, future: false},
  {name: "we_current",              weekend: true,  future: false},
  {name: "wd_future_extcons",       weekend: false, future: true,  levers: ["ext-cons"]},
  {name: "we_future_wknduplift",    weekend: true,  future: true,  levers: ["wknd-uplift"]},
  {name: "wd_future_twilight",      weekend: false, future: true,  levers: ["twilight-dr"]},
  {name: "wd_future_extrabay",      weekend: false, future: true,  levers: ["extra-bay"]},
  {name: "wd_future_dials",         weekend: false, future: true,
    dials: {demandMult: 1.2, losMult: 1.1, c4cPct: 10, srcMult: {edPull: 1.5, gp: 0.8}}},
  {name: "wd_future_custom_removal", weekend: false, future: true,
    custom: {med: [{label: "SHO reduced", n: 1, baseN: 3, rate: 0.5, cost: 110000, s: "08:30", e: "17:00", days: "wd"}], nur: []}},
];

const out = {};
for (const sc of SCENARIOS) {
  applyScenario(sc);
  out[sc.name] = {meta: {weekend: !!sc.weekend, future: !!sc.future}, result: computeState(sc.future)};
}
process.stdout.write(JSON.stringify(out, null, 2));
