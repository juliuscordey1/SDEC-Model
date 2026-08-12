"use strict";
/*
 * Validates the config bridge (interactive/config_bridge.js) in isolation:
 *   1. config -> state -> config -> state is lossless (round-trip in state space).
 *   2. configToState(data/sdec_model.json) reproduces the seeded per-area layout,
 *      roster tags and demand split (so the interactive build boots correctly).
 * Exits non-zero on any mismatch; the pytest suite asserts a clean exit.
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const bridgeSrc = fs.readFileSync(path.join(ROOT, "interactive", "config_bridge.js"), "utf8");
const CONFIG = JSON.parse(fs.readFileSync(path.join(ROOT, "data", "sdec_model.json"), "utf8"));

// parseHM is provided by the host tool; supply the identical implementation.
function parseHM(t) { const [h, m] = String(t).split(":").map(Number); return (h || 0) + (m || 0) / 60; }

const failures = [];
function check(cond, msg) { if (!cond) failures.push(msg); }
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

// Evaluate the bridge + tests in one scope so its function declarations are visible.
const harness = `
  ${bridgeSrc}
  globalThis.__configToState = configToState;
  globalThis.__stateToConfig = stateToConfig;
`;
eval(harness);
const configToState = globalThis.__configToState;
const stateToConfig = globalThis.__stateToConfig;

// 1) round-trip lossless in state space
const s1 = configToState(CONFIG);
const s2 = configToState(stateToConfig(s1));
check(eq(s1, s2), "round-trip config->state->config->state is not lossless");

// 2) scalars
check(s1.meanLos === 5.0, "meanLos");
check(s1.nurseRatio === 4, "nurseRatio");
check(s1.openHour === 7 && s1.closeHour === 24, "operating window");
check(s1.costMedWTE === 110000 && s1.costNurWTE === 42000, "default costs");
check(s1.chairs === undefined, "the old floor-wide `chairs` scalar should be gone (spaces are per area now)");

// 3) the confirmed physical layout, in order
const layout = s1.areas.map(a => [a.id, a.trolleys, a.chairs, a.rooms, a.nursingRole]);
check(eq(layout, [
  ["bay-1", 3, 3, 0, true],
  ["bay-2", 3, 3, 0, true],
  ["ambulatory-chairs", 0, 40, 0, false],
  ["hot-clinic", 0, 0, 2, false],
  ["darting", 0, 0, 2, false],
  ["triage", 0, 0, 1, false],
]), "physical layout: " + JSON.stringify(layout));
const floorSpaces = s1.areas.reduce((a, ar) => a + ar.trolleys + ar.chairs + ar.rooms, 0);
check(floorSpaces === 57, "floor footprint should be 57 spaces, got " + floorSpaces);

// 4) per-area demand: the split sums back to the legacy floor-wide profile
const LEGACY_WD = [0,0,0,0,0,0,0, 0.69,0.69,5.73,12.05,16.09,18.16,20.03,20.95,21.26,20.83,18.99,16.98,14.52,10.95,7.57,4.91,3.09];
const LEGACY_WE = [0,0,0,0,0,0,0, 0.65,0.65,3.32,6.68,8.85,10.15,11.12,11.29,10.71,10.76,9.24,7.74,6.24,4.21,2.29,1.35,0.91];
for (const [key, legacy] of [["wd", LEGACY_WD], ["we", LEGACY_WE]]) {
  for (let h = 0; h < 24; h++) {
    const total = s1.areas.reduce((a, ar) => a + ar.demand[key][h], 0);
    check(Math.abs(total - legacy[h]) < 1e-9,
      `area demand split (${key}) does not sum back at h=${h}: ${total} vs ${legacy[h]}`);
  }
}
const amb = s1.areas.find(a => a.id === "ambulatory-chairs");
const hot = s1.areas.find(a => a.id === "hot-clinic");
check(amb.demand.wd[15] === 18.4 && hot.demand.wd[15] === 2.86, "peak-hour split across chairs/hot clinic");
for (const id of ["bay-1", "bay-2", "darting", "triage"]) {
  const ar = s1.areas.find(a => a.id === id);
  check(ar.demand.wd.every(v => v === 0) && ar.demand.we.every(v => v === 0),
    `${id} should carry no placeholder demand`);
}

// 5) referral sources per area sum to the legacy floor-wide volumes
const SRC = ["edPull", "gp", "followup", "oneoneone", "hotClinic"];
const srcTotal = {};
SRC.forEach(k => { srcTotal[k] = s1.areas.reduce((a, ar) => a + ar.sources[k], 0); });
check(eq(srcTotal, {edPull: 14.2, gp: 8, followup: 9.9, oneoneone: 0.1, hotClinic: 5}),
  "floor-wide source volumes: " + JSON.stringify(srcTotal));
check(hot.sources.hotClinic === 5 && amb.sources.hotClinic === 0, "hot clinic volume sits in the hot clinic area");

// 6) roster: counts, area tags and the placeholder bay split
check(s1.capacity.med.length === 7, "medical line count");
check(s1.capacity.nur.length === 11, "nursing line count, got " + s1.capacity.nur.length);
check(s1.capacity.med.every(r => r.area === ""), "every medical line should default to floating");

const cons = s1.capacity.med[0];
check(eq(cons, {label: "Consultant", n: 1, rate: 1.0, s: "08:30", e: "16:30", days: "all", area: "", cost: 110000}),
  "first medical line: " + JSON.stringify(cons));

const wteIn = (areaKey, days) => s1.capacity.nur
  .filter(r => r.area === areaKey && r.days === days && r.bay)
  .reduce((a, r) => a + r.n, 0);
check(wteIn("bay-1", "wd") === 2 && wteIn("bay-2", "wd") === 2, "weekday bay nursing split evenly (2 WTE each)");
check(wteIn("bay-1", "we") === 1 && wteIn("bay-2", "we") === 1, "weekend bay nursing split evenly (1 WTE each)");
const legacyBayWte = wteIn("bay-1", "wd") + wteIn("bay-2", "wd");
check(legacyBayWte === 4, "the split preserves the legacy 4 WTE of weekday bay nursing");

const floating = s1.capacity.nur.filter(r => r.area === "").map(r => r.label);
for (const label of ["B7 NIC (co-ordinator)", "B6 Hello Nurse", "B6 In-reach Nurse", "Triage Nurse"]) {
  check(floating.indexOf(label) >= 0, `${label} should stay floating, not be forced into an area`);
}
const darting = s1.capacity.nur.filter(r => r.area === "darting");
check(darting.length === 2 && darting.every(r => r.label.indexOf("EMT") === 0 && r.bay === false),
  "both EMT lines belong to darting and hold no patients");

// 7) future levers keep their target area and the space lever its kind
check(s1.future.levers.length === 5, "lever count");
const ext = s1.future.levers.find(l => l.id === "ext-cons");
check(ext && ext.fam === "med" && ext.kind === "staff" && ext.on === false && ext.area === "", "ext-cons lever");
check(ext && eq(ext.add[0], {label: "Consultant - evening", n: 1, rate: 1.0, s: "16:30", e: "20:00", days: "all"}),
  "ext-cons add line: " + JSON.stringify(ext && ext.add[0]));
const twi = s1.future.levers.find(l => l.id === "twilight-dr");
check(twi && twi.add[0].e === "00:00", "twilight midnight wrap (endHour 0 -> 00:00)");
const bayLever = s1.future.levers.find(l => l.id === "extra-bay");
check(bayLever && bayLever.area === "bay-1" && bayLever.fam === "nur", "the extra bay nurse targets an area");
const roomLever = s1.future.levers.find(l => l.id === "hot-clinic-room");
check(roomLever && roomLever.kind === "space" && roomLever.area === "hot-clinic" && roomLever.addSpaces === 1,
  "the physical-space lever: " + JSON.stringify(roomLever));

// 8) export puts the area tags back as JSON areaIds (null for floating)
const exported = stateToConfig(s1);
const expConsultant = exported.capacity.lines.find(l => l.name === "Consultant");
check(expConsultant && expConsultant.areaId === null, "a floating line exports areaId null");
const expBay = exported.capacity.lines.find(l => l.name === "Bay 1 - B6 NIC" || l.name === "Bay 1 — B6 NIC");
check(expBay && expBay.areaId === "bay-1", "an area-tagged line exports its areaId");
check(exported.areas.length === 6 && exported.areas[0].physicalSpaces === 6, "areas export with physicalSpaces");
check(exported.demand.areas.length === 6, "demand exports one entry per area");

if (failures.length) {
  console.error("BRIDGE CHECK FAILED:\n - " + failures.join("\n - "));
  process.exit(1);
}
console.log("bridge check OK (round-trip lossless; per-area layout, roster tags and demand split all seed correctly)");
