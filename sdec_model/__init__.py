"""Python implementation of the Medical SDEC demand-and-capacity model.

The model is built **per area**: each physical area of the SDEC floor (the two
bays, the ambulatory chairs, the hot clinic rooms, the darting rooms and the
triage room) carries its own spaces, its own staff roster and its own hourly
demand curve, and the floor-wide figures are the roll-up. Inputs live in a JSON
config (``data/sdec_model.json``) that follows the same structural conventions
as the ED workforce model JSON. Output is a self-contained HTML report.

Modules:
    schema  -- load, validate and normalise the JSON config
    engine  -- the calculation logic (deconvolution, queue, per-area pressure)
    report  -- render the self-contained HTML report
"""

from .schema import Area, Model, load_model
from .engine import (
    AreaResult,
    StateResult,
    capacity_addition_rows,
    compute_state,
    cost_summary,
    space_addition_rows,
)

__all__ = [
    "Area",
    "AreaResult",
    "Model",
    "load_model",
    "compute_state",
    "StateResult",
    "capacity_addition_rows",
    "space_addition_rows",
    "cost_summary",
]
