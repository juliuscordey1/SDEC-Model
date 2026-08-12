# SDEC demand and capacity model

A Python implementation of the **Medical SDEC (Same Day Emergency Care) demand
and capacity model**, plus a standalone **interactive tool**. Inputs live in a
single JSON config, which follows the same structural conventions as the trust's
ED workforce model JSON (`meta` / `areas` / `demand` / `capacity` / `standards` /
`future`).

The model is built **per area**. SDEC is not one pool: each physical area carries
its own spaces, its own staff roster and its own hourly demand curve, and the
floor-wide figures are the roll-up of those areas. The physical layout is
confirmed by the SDEC Service Coordinator:

| Area | Physical make-up | Spaces | Nurses hold patients here |
|---|---|---|---|
| Bay 1 | 3 trolleys + 3 chairs | 6 | yes |
| Bay 2 | 3 trolleys + 3 chairs | 6 | yes |
| Ambulatory Chairs | 40 general chairs | 40 | no |
| Hot Clinic Rooms | 2 rooms | 2 | no |
| Darting Rooms | 2 rooms | 2 | no |
| Triage Room | 1 usable room (of 2 — the other is AMU's) | 1 | no |

Staff who circulate rather than sitting in one area — the floor co-ordinator,
hello, in-reach and triage nursing roles, and by default every clinician — live
in a **floating pool**: they count towards floor-wide capacity, and for the
per-area view they are attributed to areas each hour in proportion to that area's
demand. That attribution is a display convention for circulating staff, not a
model of which patient goes where, and it leaves the floor-wide totals untouched.

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
state, **for every area and then for the floor as a whole**:

- **Arrivals** derived from that area's hourly *patients-present* profile by
  deconvolving a box-car length of stay (the reverse of the ED model's
  arrivals→occupancy direction).
- **Medical pressure** — arrivals plus the queue entering each hour, over the
  seeing capacity of the clinicians on shift in that area, including its share of
  the floating pool (a *flow* measure).
- **Nursing pressure** — patients present, over the holding capacity of the
  nurses on shift there (a *concurrency* measure). Only applies where nurses
  actually hold the patients — the bays, or any area with staff ticked as holding
  patients.
- **Physical-space pressure** — patients present, over that area's trolleys,
  chairs and rooms (a *hard ceiling*). An area's effective holding capacity is
  its nursing capacity capped at its physical spaces.
- The three are **never added together**. Each hour of each area is classified
  independently as *within capacity*, *closed*, or by naming whichever
  constraints bite: `medical`, `nursing`, `space`, or a combination such as
  `medical+space`.
- **Roll-up** — floor-wide demand, medical capacity, nursing capacity, physical
  spaces and the queue are all sums across areas, so the existing top-line stats
  still work. Both views are always presented: a floor-wide "within capacity"
  reading must not be allowed to hide one area quietly overflowing, so the
  per-area small-multiples grid (area rows × hour columns) sits beside the
  floor-wide strip and the headline cards report **areas constrained** as well as
  hours.
- **Future state** rescales demand (overall multiplier, per-referral-source
  multipliers, a length-of-stay multiplier and a Care-Closer-to-Home diversion)
  and adds capacity via toggleable *schemes* (levers), custom capacity lines or
  extra physical spaces — each targeting a **named area** (or the floating pool)
  — with an indicative annual **cost or saving**. A per-source demand multiplier
  reaches an area through that area's own referral mix, so scaling hot clinics up
  scales the Hot Clinic Rooms and nothing else. Physical space is reported but
  not priced: capital and estates cost is out of scope.

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
  test_engine.py        # engine pytest suite (incl. both JS-parity checks)
  test_interactive.py   # interactive build + bridge tests
  js_engine_check.js    # extracts the tool's MODEL CORE and runs it as an oracle
  js_reference.js       # the PRE-AREA floor-wide JavaScript, kept verbatim
  bridge_check.js       # config<->state round-trip + seed check (node)
  fixtures/
    js_engine.json      # committed oracle output (so tests run without node)
    js_reference.json   # committed legacy-oracle output
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
  Every scheme has an `areaId` naming where the change lands (`null` = the
  floating pool) — point *Add a Bay Nurse* at `bay-2` and only Bay 2's holding
  capacity moves.
- Add physical space with a `physicalSpaceAddition` scheme, or ad hoc via
  `future.areaSpaceDeltas` (`{"hot-clinic": 2}` opens two more rooms). Space
  changes are reported but not costed.
- Adjust the scalar dials: `demandMultiplier`, `losMultiplier`, `c4cPercent`,
  and `sourceMultipliers` (per referral source — each reaches an area in
  proportion to that area's own `sources` mix).
- Add ad-hoc capacity under `future.customCapacityLines` (each line's net change
  vs its `baseWte` is what counts and gets costed, and each carries its own
  `areaId`).

Example — extend consultant cover, add a twilight doctor and a third hot clinic
room, with 10% demand growth:

```jsonc
"future": {
  "demandMultiplier": 1.10,
  "areaSpaceDeltas": { "hot-clinic": 1 },
  "schemes": [
    { "id": "ext-cons",    "enabled": true, "areaId": null,    /* ... */ },
    { "id": "twilight-dr", "enabled": true, "areaId": null,    /* ... */ },
    { "id": "extra-bay",   "enabled": true, "areaId": "bay-2", /* ... */ }
  ]
}
```

## Tests

```bash
pip install pytest        # dev-only dependency
python -m pytest tests/
```

Two oracles pin the maths from both sides. Each is regenerated on the fly when
`node` is available, and otherwise read from its committed fixture.

1. **Browser-engine parity.** `tests/js_engine_check.js` extracts the
   `MODEL CORE` region of `interactive/base_tool.html` — the maths the live
   editor actually runs — boots it from the JSON via the config bridge, and
   emits per-hour arrays floor-wide *and per area* for a battery of scenarios
   (levers, dials, space changes, a clinician re-tagged to one area, demand moved
   into a bay). The Python engine must reproduce every one of them exactly. A
   test also asserts that region stays DOM-free, since that is what makes it
   extractable.
2. **Migration fidelity.** `tests/js_reference.js` is the *pre-rearchitecture*
   floor-wide tool, kept verbatim. The per-area model's floor-wide roll-up must
   still reproduce it on the seeded placeholder split — the check that says the
   rearchitecture took the model apart without moving the totals. The one
   scenario with unequal per-source multipliers is allowed to differ in the third
   decimal, because the placeholder area split is stored to 2dp.

The remaining tests cover the deconvolution round-trip, queue non-negativity,
overnight (wraparound) shift lines, the floating-pool allocation (including the
property that when every clinician floats, each area reads the floor's medical
pressure), physical-space capping, the nine-way hour classification, the seeding
rules above, and the cost/saving sign convention.

## JSON schema notes and deviations from the ED model

The mapping follows the ED workforce model's conventions, with these
SDEC-specific choices:

- **`areas`** carries the confirmed physical layout, one entry per area, each
  with a `spaces` breakdown (`trolleys` / `chairs` / `rooms`), a derived
  `physicalSpaces` total and a `nursingConcurrency` flag marking the areas whose
  patients are held by nurses. Unlike v1, `physicalSpaces` **is** wired into the
  calculations, as a hard ceiling on patients concurrently present in that area.
- **`capacity.lines[].areaId`** replaces v1's `areaIds` list: a single area tag,
  or `null` for the floating pool. (The ED model uses a list; a line here belongs
  to one place or to nobody, so a scalar is the honest shape.)
- **`demand.areas`** is one entry per area — its own `patientsPresentOverride`
  curves and its own referral-source `sources` mix. Floor-wide demand and
  floor-wide source volumes are the sums.
- **`future.areaSpaceDeltas`** and the `physicalSpaceAddition` scheme type are
  new: physical space is now a lever, targeted at a named area and reported
  without a price.
- **`demand.method`** is `"occupancyDeconvolution"`: the primary raw input is
  hourly *patients present* (`patientsPresentOverride`), and arrivals are
  derived at runtime — so the `arrivals` arrays are intentionally empty (the same
  pattern the ED JSON uses for its computed `departures`/`transfers`).
- **`demand.areas[].sources`** and **`future.sourceMultipliers`** are new objects
  for the referral-source volumes (no ED equivalent). Sources are now a secondary
  lens rather than a driver: the editable primitive is area × hour attendance,
  and a source mix only decides how a per-source future multiplier reaches each
  area.
- **`assumptions`** is a small new top-level block for the SDEC operating window,
  default nurse:patient ratio and default per-WTE costs — kept out of
  `standards.staffingRules`, which is a different (rule-type) concept.
- **`capacity.lines`** reuse the ED line shape. `countsTowardsProfessionCapacity`
  carries the source tool's "bay" flag — whether the line *holds patients* and so
  adds holding capacity to its area: always effectively true for medical lines;
  for nursing, true only for the bay lines (false for
  co-ordinator/triage/hello/in-reach/EMT roles).
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

## Seeding and migration from the floor-wide model

The tool is not empty on first load: the v1 floor-wide seed was split across the
new areas **mechanically**, so every number below is a clearly-marked placeholder
and none of it should be read as observed. `meta.placeholderSplit` in the JSON
records exactly this, and each area and demand row carries its own
`isPlaceholder` flag and a note.

**Staffing**

| v1 line | Now | Why |
|---|---|---|
| Bay — B6 NIC (1 WTE), Bay — B5 nurses (3 WTE) | Bay 1: NIC + 1 B5; Bay 2: 2 B5 | Even split by whole person — 2 WTE per bay, preserving the v1 floor total of 4 |
| Bay nurses, weekend (2 WTE) | 1 WTE per bay | Same even split |
| EMT (darting/float), EMT (weekend) | Darting Rooms | The role names the area; still holds no patients, as in v1 |
| B7 NIC co-ordinator, B6 hello, B6 in-reach, triage nurse | **Floating** | Genuinely floor-wide roles — no area tag was forced where one does not make sense |
| Every medical line | **Floating** | Working assumption that clinicians circulate. Re-tag a line (e.g. a GP who only ever runs hot clinics) and its capacity moves to that area |

**Demand** — the v1 floor-wide patients-present profile was split by referral
source: hot clinics (5.0/37.2 ≈ 13.4% of attendances) to the Hot Clinic Rooms,
every other source (ED pull, GP, follow-up, 111 ≈ 86.6%) to the ambulatory
chairs, both on the floor-wide hourly shape because no per-area shape exists yet.
The split sums back to the v1 profile exactly, which is what lets the
migration-fidelity test above hold.

**Bay 1, Bay 2, Darting and Triage therefore carry no placeholder demand at
all.** Nothing in the referral-source split maps to them, so nothing was
invented. They show staffed capacity against zero demand, and the per-area views
render those hours as hatched "no demand entered" rather than green "within
capacity" — an unmodelled area must not read as a reassuring one.

## Data to confirm

The seed is **synthetic demonstration data** (`meta.isSyntheticDemoData: true`).
Before any formal use, the SDEC team needs to supply or confirm:

**Opened up by the per-area rearchitecture**

- **Real per-area staffing** — headcounts and shift patterns area by area. The v1
  roster was not area-tagged at all; the splits above are mechanical.
- **Real per-area hourly attendance**, to replace the referral-source split — in
  particular anything at all for Bay 1, Bay 2, Darting and Triage.
- Whether the **40 general chairs** have any dedicated staff or float coverage.
  They currently have no nursing establishment of their own, so only the 40-space
  ceiling constrains them.
- Whether **"patients present" already includes time in the Hot Clinic, Darting
  and Triage rooms**, or whether those are shorter, separate throughput events
  with different concurrency dynamics from a bay stay. On the placeholder split
  the 2 hot clinic rooms overflow for most of the day, which is more likely an
  artefact of applying a bay-length stay to a clinic slot than a real finding.
- Whether the **one usable Triage room holds patients** (an occupancy to model)
  or is a pure process step with no "present" time at all.
- **Which clinicians are genuinely fixed to an area**, rather than floating.

**Carried over from the v1 list**

- **Nursing shift start/end times** — the template gives roles and counts but not
  timings (roster lead).
- **SHO patients-per-hour rate** — assumed 0.5/hr within the 0.5–1 envelope;
  confirm from job plans.
- **Mean and distribution of length of stay by area** and by referral source, to
  replace the single ~5h data-implied value applied everywhere.
- **Patients-per-nurse ratio** the bays are actually run to (assumed ~1:4).
- **Hot-clinic schedule by day of week** (frailty, cardiology, rheumatology,
  ACT/respiratory) — demand is day-specific and booked to a template.
- Whether **weekend capacity and demand** should be modelled separately per
  Saturday and Sunday.
- **Arrivals-by-hour per area** (not just presence-by-hour), if available, to
  replace the derived arrivals curve.

Costs and establishment figures are indicative — confirm with finance. Physical
space changes are reported without a price; capital and estates cost is out of
scope for this model.
