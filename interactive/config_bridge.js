// ======================================================================
// Config bridge - injected by scripts/build_interactive.py
// ----------------------------------------------------------------------
// Converts between the Python-schema JSON config (data/sdec_model.json:
// meta / areas / assumptions / demand / capacity / standards / future) and
// this tool's internal `state` object, and wires the Load/Download JSON
// controls. This keeps the JSON the single source of truth: the tool boots
// from it, and edits export back to the same schema the Python engine reads.
//
// Schema v2 is per-area: `areas` carries the physical layout, `demand.areas`
// one hourly curve and referral mix per area, and every capacity line an
// `areaId` (null = the floating pool of staff who circulate floor-wide).
//
// The build appends, immediately after this block:
//     const CONFIG = { ...data/sdec_model.json... };
//     const state = configToState(CONFIG);
// ======================================================================

// shift time <-> decimal-hour helpers. The tool stores shift times as "HH:MM"
// strings (parsed by the existing parseHM); the JSON stores decimal hours
// (08:30 == 8.5). An endHour of 0 denotes an overnight wrap to midnight.
function decToHM(x) {
  x = Number(x);
  if (!isFinite(x)) x = 0;
  const total = Math.round(x * 60);
  const h = Math.floor(total / 60), m = total % 60;
  return String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0");
}
function daysFromPattern(p) { return p === "weekday" ? "wd" : p === "weekend" ? "we" : "all"; }
function patternFromDays(d) { return d === "wd" ? "weekday" : d === "we" ? "weekend" : "everyDay"; }
function slugify(s) {
  return String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/(^-|-$)/g, "") || "line";
}
function pickNums(o, keys, dflt) {
  const r = {};
  keys.forEach(k => { r[k] = (o && o[k] != null) ? Number(o[k]) : dflt; });
  return r;
}
// "" in state <-> null in JSON: both mean "floating / not tied to one area".
function areaKeyFromConfig(v) { return v == null ? "" : String(v); }
function areaIdToConfig(v) { return (v == null || v === "") ? null : String(v); }
function hourly(a) {
  const out = new Array(24).fill(0);
  (a || []).forEach((v, i) => { if (i < 24) out[i] = Number(v) || 0; });
  return out;
}

const SRC_KEYS = ["edPull", "gp", "followup", "oneoneone", "hotClinic"];

// ---- config -> internal state ----------------------------------------
function lineFromConfig(l) {
  const fam = l.profession === "nursing" ? "nur" : "med";
  const row = {
    label: l.name,
    n: Number(l.wte) || 0,
    rate: Number(l.patientsPerHour) || 0,
    s: decToHM(l.startHour),
    e: decToHM(l.endHour),
    days: daysFromPattern(l.dayPattern),
    area: areaKeyFromConfig(l.areaId),
  };
  if (l.costPerWtePerYear != null) row.cost = Number(l.costPerWtePerYear);
  if (fam === "nur") row.bay = (l.countsTowardsProfessionCapacity !== false);
  return row;
}
function addFromConfig(a, fam) {
  const row = {
    label: a.label,
    n: Number(a.wte) || 0,
    rate: Number(a.patientsPerHour) || 0,
    s: decToHM(a.startHour),
    e: decToHM(a.endHour),
    days: daysFromPattern(a.dayPattern),
  };
  if (a.areaId !== undefined) row.area = areaKeyFromConfig(a.areaId);
  if (fam === "nur") row.bay = (a.countsTowardsProfessionCapacity !== false);
  return row;
}
function configToState(cfg) {
  const a = cfg.assumptions || {};
  const win = a.operatingWindow || {};
  const los = (cfg.demand && cfg.demand.losProfiles && cfg.demand.losProfiles[0]) || {};
  const lines = (cfg.capacity && cfg.capacity.lines) || [];
  const fut = cfg.future || {};
  const cc = fut.customCapacityLines || {};

  // demand is keyed by areaId so the two lists need not be in the same order
  const demandByArea = {};
  ((cfg.demand && cfg.demand.areas) || []).forEach(d => { demandByArea[String(d.areaId)] = d; });

  const areas = (cfg.areas || [])
    .filter(ar => ar.active !== false)
    .slice()
    .sort((x, y) => (Number(x.order) || 0) - (Number(y.order) || 0))
    .map(ar => {
      const sp = ar.spaces || {};
      const d = demandByArea[String(ar.id)] || {};
      const present = d.patientsPresentOverride || {};
      return {
        id: String(ar.id),
        name: String(ar.name || ar.id),
        trolleys: Number(sp.trolleys) || 0,
        chairs: Number(sp.chairs) || 0,
        rooms: Number(sp.rooms) || 0,
        nursingRole: !!ar.nursingConcurrency,
        placeholderDemand: ar.demandIsPlaceholder !== false,
        note: String(ar.notes || ""),
        demand: { wd: hourly(present.weekday), we: hourly(present.weekend) },
        sources: pickNums(d.sources || {}, SRC_KEYS, 0),
      };
    });

  const customOf = (rows, fam) => (rows || []).map(r => {
    const x = addFromConfig(r, fam);
    x.area = areaKeyFromConfig(r.areaId);
    x.baseN = Number(r.baseWte) || 0;
    if (r.costPerWtePerYear != null) x.cost = Number(r.costPerWtePerYear);
    return x;
  });

  const deltas = {};
  Object.keys(fut.areaSpaceDeltas || {}).forEach(k => { deltas[k] = Number(fut.areaSpaceDeltas[k]) || 0; });

  return {
    meanLos: Number(los.averageLosHours) || 5,
    nurseRatio: Number(a.defaultNurseRatio) || 4,
    openHour: Number(win.openHour) || 0,
    closeHour: Number(win.closeHour) || 24,
    costMedWTE: Number(a.defaultCostMedWtePerYear) || 0,
    costNurWTE: Number(a.defaultCostNurWtePerYear) || 0,
    weekendView: false,
    ui: { curView: "overall", futView: "overall" },
    areas: areas,
    capacity: {
      med: lines.filter(l => l.profession !== "nursing" && l.active !== false).map(lineFromConfig),
      nur: lines.filter(l => l.profession === "nursing" && l.active !== false).map(lineFromConfig),
    },
    future: {
      demandMult: Number(fut.demandMultiplier) || 1,
      losMult: Number(fut.losMultiplier) || 1,
      c4cPct: Number(fut.c4cPercent) || 0,
      srcMult: pickNums(fut.sourceMultipliers || {}, SRC_KEYS, 1),
      areaSpaceDeltas: deltas,
      customCapacity: { med: customOf(cc.medical, "med"), nur: customOf(cc.nursing, "nur") },
      levers: (fut.schemes || []).map(s => {
        const isSpace = s.type === "physicalSpaceAddition";
        const fam = isSpace ? "none" : (s.profession === "nursing" ? "nur" : "med");
        const lv = {
          id: s.id, fam, kind: isSpace ? "space" : "staff", on: !!s.enabled,
          area: areaKeyFromConfig(s.areaId), name: s.name, note: s.note || "",
          add: (s.add || []).map(x => addFromConfig(x, fam)),
        };
        if (isSpace) lv.addSpaces = Number(s.addSpaces) || 0;
        return lv;
      }),
    },
  };
}

// ---- internal state -> config (export) -------------------------------
function lineToConfig(row, fam, i) {
  const o = {
    id: (fam === "med" ? "med-" : "nur-") + slugify(row.label) + "-" + i,
    name: row.label,
    profession: fam === "med" ? "medical" : "nursing",
    areaId: areaIdToConfig(row.area),
    wte: Number(row.n) || 0,
    patientsPerHour: Number(row.rate) || 0,
    startHour: parseHM(row.s),
    endHour: parseHM(row.e),
    dayPattern: patternFromDays(row.days),
    countsTowardsProfessionCapacity: fam === "med" ? true : (row.bay !== false),
    active: true,
  };
  if (Number.isFinite(row.cost)) o.costPerWtePerYear = Number(row.cost);
  return o;
}
function stateToConfig(st) {
  const out = JSON.parse(JSON.stringify(CONFIG));  // preserve meta/notes/standards/productivity scaffolding
  out.meta = out.meta || {};
  out.meta.savedAt = new Date().toISOString();

  // keep any per-area scaffolding (notes, acuityWeight) the tool does not edit
  const priorAreas = {};
  (out.areas || []).forEach(ar => { priorAreas[String(ar.id)] = ar; });
  const priorDemand = {};
  ((out.demand && out.demand.areas) || []).forEach(d => { priorDemand[String(d.areaId)] = d; });

  out.areas = st.areas.map((ar, i) => Object.assign({}, priorAreas[ar.id] || {}, {
    id: ar.id,
    name: ar.name,
    order: i,
    active: true,
    spaces: { trolleys: Number(ar.trolleys) || 0, chairs: Number(ar.chairs) || 0, rooms: Number(ar.rooms) || 0 },
    physicalSpaces: (Number(ar.trolleys) || 0) + (Number(ar.chairs) || 0) + (Number(ar.rooms) || 0),
    nursingConcurrency: !!ar.nursingRole,
    demandIsPlaceholder: !!ar.placeholderDemand,
    notes: ar.note || "",
  }));

  out.demand = out.demand || {};
  out.demand.areas = st.areas.map(ar => Object.assign({}, priorDemand[ar.id] || {}, {
    areaId: ar.id,
    patientsPresentOverride: { weekday: ar.demand.wd.slice(), weekend: ar.demand.we.slice() },
    arrivals: { weekday: [], weekend: [] },
    sources: {
      edPull: ar.sources.edPull, gp: ar.sources.gp, followup: ar.sources.followup,
      oneoneone: ar.sources.oneoneone, hotClinic: ar.sources.hotClinic,
    },
    isPlaceholder: !!ar.placeholderDemand,
  }));
  out.demand.losProfiles = out.demand.losProfiles && out.demand.losProfiles.length
    ? out.demand.losProfiles : [{ areaId: null, source: "averageLos" }];
  out.demand.losProfiles[0].averageLosHours = st.meanLos;

  out.assumptions = out.assumptions || {};
  out.assumptions.operatingWindow = Object.assign(out.assumptions.operatingWindow || {},
    { openHour: st.openHour, closeHour: st.closeHour });
  out.assumptions.defaultNurseRatio = st.nurseRatio;
  out.assumptions.defaultCostMedWtePerYear = st.costMedWTE;
  out.assumptions.defaultCostNurWtePerYear = st.costNurWTE;

  out.capacity = out.capacity || {};
  out.capacity.lines = st.capacity.med.map((r, i) => lineToConfig(r, "med", i))
    .concat(st.capacity.nur.map((r, i) => lineToConfig(r, "nur", i)));

  out.future = out.future || {};
  out.future.demandMultiplier = st.future.demandMult;
  out.future.losMultiplier = st.future.losMult;
  out.future.c4cPercent = st.future.c4cPct;
  out.future.sourceMultipliers = {
    edPull: st.future.srcMult.edPull, gp: st.future.srcMult.gp, followup: st.future.srcMult.followup,
    oneoneone: st.future.srcMult.oneoneone, hotClinic: st.future.srcMult.hotClinic,
  };
  out.future.areaSpaceDeltas = Object.assign({}, st.future.areaSpaceDeltas || {});
  out.future.customCapacityLines = {
    medical: st.future.customCapacity.med.map((r, i) => { const o = lineToConfig(r, "med", i); o.baseWte = Number(r.baseN) || 0; return o; }),
    nursing: st.future.customCapacity.nur.map((r, i) => { const o = lineToConfig(r, "nur", i); o.baseWte = Number(r.baseN) || 0; return o; }),
  };
  out.future.schemes = st.future.levers.map(lv => {
    const isSpace = lv.kind === "space";
    const o = {
      id: lv.id, name: lv.name,
      type: isSpace ? "physicalSpaceAddition" : "capacityAddition",
      profession: isSpace ? "none" : (lv.fam === "nur" ? "nursing" : "medical"),
      enabled: !!lv.on, areaId: areaIdToConfig(lv.area), note: lv.note || "",
      add: (lv.add || []).map(a => {
        const x = {
          label: a.label, wte: Number(a.n) || 0, patientsPerHour: Number(a.rate) || 0,
          startHour: parseHM(a.s), endHour: parseHM(a.e), dayPattern: patternFromDays(a.days),
        };
        if (a.area !== undefined) x.areaId = areaIdToConfig(a.area);
        if (lv.fam === "nur") x.countsTowardsProfessionCapacity = (a.bay !== false);
        return x;
      }),
    };
    if (isSpace) o.addSpaces = Number(lv.addSpaces) || 0;
    return o;
  });
  return out;
}

// ---- Load / Download JSON wiring --------------------------------------
function applyConfig(cfg) {
  const fresh = configToState(cfg);
  Object.keys(state).forEach(k => delete state[k]);
  Object.assign(state, fresh);
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(state)); } catch (e) { /* storage blocked */ }
  closeAllSrcDetails();
  rebuildAllUI();
  setSaveStatus("Loaded from JSON config");
}
function initConfigIO() {
  const load = document.getElementById("btn-load-config");
  const file = document.getElementById("file-config");
  const dl = document.getElementById("btn-download-config");
  if (load && file) {
    load.addEventListener("click", () => file.click());
    file.addEventListener("change", () => {
      const f = file.files && file.files[0];
      if (!f) return;
      const r = new FileReader();
      r.onload = () => {
        try { applyConfig(JSON.parse(r.result)); }
        catch (e) { setSaveStatus("Couldn't read JSON: " + e.message); }
        file.value = "";
      };
      r.readAsText(f);
    });
  }
  if (dl) {
    dl.addEventListener("click", () => {
      const blob = new Blob([JSON.stringify(stateToConfig(state), null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "sdec_model.json";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setSaveStatus("Config JSON downloaded");
    });
  }
}
