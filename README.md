# SDEC demand and capacity model

A Python implementation of the **Medical SDEC (Same Day Emergency Care) demand
and capacity model**, plus a standalone **interactive tool**. Inputs live in a
single JSON config; the calculation logic is a faithful port of the browser-based
*Medical SDEC Demand and Capacity Tool*, and the JSON follows the same structural
conventions as the trust's ED workforce model JSON (`meta` / `areas` / `demand` /
`capacity` / `standards` / `future`).

## Which file do I open?

There are two deliverables, both self-contained HTML (double-click to open, no
server, no internet) — pick by what you need:

| I want to… | Open / share | Built by |
|---|---|---|
| **Edit inputs live** — sliders, editable staffing, toggle levers, and see the numbers update | **`interactive/sdec_interactive.html`** | `python -m scripts.build_interactive` |
| **Share a fixed snapshot / report** of one scenario | `build/sdec_report.html` (or wherever you output it) | `python -m scripts.build_report …` |

Both are driven by the **same `data/sdec_model.json`**. The interactive tool can
**Load JSON** and **Download JSON** in that exact schema, so you can edit in the
browser, download the config, and the Python report/engine reads the very same
file — and vice-versa. The interactive tool runs the maths in the browser (it has
to, to be a serverless live editor); that in-browser maths is the code the test
suite pins against the Python engine, so the two stay numerically identical.

## What it computes

For each day type (weekday / weekend), current state and a configurable future
state:

- **Arrivals** derived from the hourly *patients-present* profile by
  deconvolving a box-car length of stay (the reverse of the ED model's
  arrivals→occupancy direction).
- **Medical pressure** — arrivals plus the queue entering each hour, over the
  seeing capacity of the clinicians on shift (a *flow* measure).
- **Nursing pressure** — patients present, over the concurrency capacity of the
  bay nurses on shift (a *concurrency* measure).
- These two are **never added together**. Each hour is classified independently
  as *within capacity*, *medical constraint*, *nursing constraint*, *both*, or
  *closed*.
- **Future state** rescales demand (overall multiplier, per-referral-source
  multipliers, a length-of-stay multiplier and a Care-Closer-to-Home diversion)
  and adds capacity via toggleable *schemes* (levers) or custom capacity lines,
  with an indicative annual **cost or saving**.

## Layout

```
sdec_model/
  __init__.py
  schema.py         # load + validate + normalise sdec_model.json
  engine.py         # the ported calculation logic
  report.py         # builds the self-contained static HTML report
data/
  sdec_model.json   # seeded default config (the tool's demo data) — source of truth
scripts/
  build_report.py       # CLI: JSON in, static HTML report out
  build_interactive.py  # CLI: JSON in, standalone interactive tool out
interactive/
  base_tool.html        # the original browser tool (UI + in-browser engine)
  config_bridge.js      # JSON<->tool-state converters + Load/Download JSON wiring
  sdec_interactive.html # BUILT deliverable: the live editor, seeded from the JSON
tests/
  test_engine.py    # engine pytest suite (incl. JS-parity check)
  test_interactive.py   # interactive build + bridge tests
  js_reference.js   # the original JavaScript, run by node as a parity oracle
  bridge_check.js   # config<->state round-trip + seed check (node)
  fixtures/
    js_reference.json  # committed oracle output (so tests run without node)
```

## Usage

The runtime has **no third-party dependencies** (standard library only).

Build the **interactive tool** (the live editor) from the config:

```bash
python -m scripts.build_interactive
# -> interactive/sdec_interactive.html  (open / share this)
```

Generate the **static report** from the config:

```bash
python -m scripts.build_report data/sdec_model.json build/sdec_report.html
# or:  python scripts/build_report.py data/sdec_model.json build/sdec_report.html
```

Then open the resulting `.html` in any browser. After editing
`data/sdec_model.json`, rerun the relevant build to refresh the HTML (a test
guards against the committed interactive tool drifting out of sync with the JSON).

### Modelling a future scenario

Edit `data/sdec_model.json` and regenerate. The future state is driven by the
`future` block:

- Toggle a preset lever by setting a scheme's `enabled` to `true`, e.g. the
  *Extend Consultant to 20:00* scheme (`future.schemes[].id == "ext-cons"`).
- Adjust the scalar dials: `demandMultiplier`, `losMultiplier`, `c4cPercent`,
  and `sourceMultipliers` (per referral source).
- Add ad-hoc capacity under `future.customCapacityLines` (each line's net change
  vs its `baseWte` is what counts and gets costed).

Example — extend consultant cover and add a twilight doctor, with 10% demand
growth:

```jsonc
"future": {
  "demandMultiplier": 1.10,
  "schemes": [
    { "id": "ext-cons",    "enabled": true,  /* ... */ },
    { "id": "twilight-dr", "enabled": true,  /* ... */ }
  ]
}
```

## Tests

```bash
pip install pytest        # dev-only dependency
python -m pytest tests/
```

The suite includes a **JS-parity check**: `tests/js_reference.js` runs the
original tool's own JavaScript (functions and seed copied verbatim) over a
battery of scenarios and emits per-hour arrays; the Python engine is asserted to
reproduce them within floating-point tolerance. If `node` is available the
oracle is regenerated on the fly; otherwise the committed
`tests/fixtures/js_reference.json` is used. The remaining tests cover the
deconvolution round-trip, queue non-negativity, overnight (wraparound) shift
lines, the four-way hour classification, and the cost/saving sign convention.

## JSON schema notes and deviations from the ED model

The mapping follows the ED workforce model's conventions, with these
SDEC-specific choices:

- **`areas`** is a single-element list — SDEC is one unit, not multiple physical
  zones. `physicalSpaces` is the funded "chairs" ceiling (21). This footprint is
  documented but, mirroring the source tool, **not wired into the pressure
  calculations**.
- **`demand.method`** is `"occupancyDeconvolution"`: the primary raw input is
  hourly *patients present* (`patientsPresentOverride`), and arrivals are
  derived at runtime — so the `arrivals` arrays are intentionally empty (the same
  pattern the ED JSON uses for its computed `departures`/`transfers`).
- **`demand.sources`** and **`future.sourceMultipliers`** are new objects for the
  referral-source volumes (no ED equivalent).
- **`assumptions`** is a small new top-level block for the SDEC operating window,
  default nurse:patient ratio and default per-WTE costs — kept out of
  `standards.staffingRules`, which is a different (rule-type) concept.
- **`capacity.lines`** reuse the ED line shape. `countsTowardsProfessionCapacity`
  carries the source tool's "bay" flag: always effectively true for medical
  lines; for nursing, true only for bay lines that add concurrency capacity
  (false for co-ordinator/triage/hello/in-reach/EMT floor roles).
- **`capacity.productivity`** is a **placeholder** for schema consistency only —
  the source tool works off raw WTE × rate with no leave/headroom adjustment, so
  it is not wired into the engine.
- **`standards`** is intentionally **empty** — the source tool encodes no
  staffing rules or benchmarks, and none were invented.
- **`future.schemes`** use a new `type: "capacityAddition"` with `add[]` lines
  (the ED model only has `"arrivals"`/`"lengthOfStay"`). The scalar future dials
  live alongside the schemes rather than being forced into scheme shape.
- Shift times are stored as **decimal hours** (`08:30` → `8.5`), the ED
  convention. An `add` line whose `endHour` is `0` denotes an overnight wrap to
  midnight (e.g. a twilight doctor 14:00–00:00), matching the source's parsing of
  `"00:00"`.

## Data to confirm

The seed is **synthetic demonstration data** (`meta.isSyntheticDemoData: true`),
seeded from the source tool's demo values. Before any formal use, the SDEC team
needs to supply or confirm (carried over verbatim from the source tool's own
"Data to Confirm" list):

- **Nursing shift start/end times** — the template gives roles and counts but not
  timings (roster lead).
- **SHO patients-per-hour rate** — assumed 0.5/hr within the 0.5–1 envelope;
  confirm from job plans.
- **Mean and distribution of length of stay by referral source**, to replace the
  ~5h data-implied value.
- **Physical footprint** — number of funded chairs and trolley spaces.
- **Patients-per-nurse ratio** the bays are actually run to (assumed ~1:4).
- **Hot-clinic schedule by day of week** (frailty, cardiology, rheumatology,
  ACT/respiratory) — demand is day-specific.
- Whether **weekend capacity and demand** should be modelled separately per
  Saturday and Sunday.
- **Arrivals-by-hour** (not just presence-by-hour), if available, to replace the
  derived arrivals curve.

Costs and establishment figures are indicative — confirm with finance.
