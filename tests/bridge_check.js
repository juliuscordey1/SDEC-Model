"use strict";
/*
 * Validates the config bridge (interactive/config_bridge.js) in isolation:
 *   1. config -> state -> config -> state is lossless (round-trip in state space).
 *   2. configToState(data/sdec_model.json) reproduces the original tool's seed
 *      numbers (so the interactive build boots identically to the source tool).
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

// 2) seed reproduces the original tool numbers
check(s1.meanLos === 5.0, "meanLos");
check(s1.nurseRatio === 4, "nurseRatio");
check(s1.chairs === 21, "chairs");
check(s1.openHour === 7 && s1.closeHour === 24, "operating window");
check(s1.costMedWTE === 110000 && s1.costNurWTE === 42000, "default costs");
check(s1.demand.wd.length === 24 && s1.demand.wd[15] === 21.26, "weekday profile peak");
check(s1.demand.we.length === 24 && s1.demand.we[14] === 11.29, "weekend profile peak");
check(s1.sources.edPull === 14.2 && s1.sources.hotClinic === 5.0, "sources");
check(s1.capacity.med.length === 7, "medical line count");
check(s1.capacity.nur.length === 9, "nursing line count");

const cons = s1.capacity.med[0];
check(eq(cons, { label: "Consultant", n: 1, rate: 1.0, s: "08:30", e: "16:30", days: "all", cost: 110000 }),
  "first medical line: " + JSON.stringify(cons));

const bayNic = s1.capacity.nur.find(r => r.label === "Bay - B6 NIC");
check(bayNic && bayNic.bay === true && bayNic.days === "wd", "bay B6 NIC line");
const coord = s1.capacity.nur.find(r => r.label === "B7 NIC (co-ordinator)");
check(coord && coord.bay === false, "co-ordinator is non-bay");

check(s1.future.levers.length === 4, "lever count");
const ext = s1.future.levers.find(l => l.id === "ext-cons");
check(ext && ext.fam === "med" && ext.on === false, "ext-cons lever");
check(ext && eq(ext.add[0], { label: "Consultant - evening", n: 1, rate: 1.0, s: "16:30", e: "20:00", days: "all" }),
  "ext-cons add line: " + JSON.stringify(ext && ext.add[0]));
const twi = s1.future.levers.find(l => l.id === "twilight-dr");
check(twi && twi.add[0].e === "00:00", "twilight midnight wrap (endHour 0 -> 00:00)");

if (failures.length) {
  console.error("BRIDGE CHECK FAILED:\n - " + failures.join("\n - "));
  process.exit(1);
}
console.log("bridge check OK (round-trip lossless; seed matches original tool)");
