"""Python implementation of the Medical SDEC demand-and-capacity model.

The calculation logic is a faithful port of the browser-based
"Medical SDEC Demand and Capacity Tool" (HTML + embedded JavaScript). Inputs
live in a JSON config (``data/sdec_model.json``) that follows the same
structural conventions as the ED workforce model JSON. Output is a
self-contained HTML report.

Modules:
    schema  -- load, validate and normalise the JSON config
    engine  -- the ported calculation logic (deconvolution, queue, pressure)
    report  -- render the self-contained HTML report
"""

from .schema import Model, load_model
from .engine import compute_state, StateResult, capacity_addition_rows, cost_summary

__all__ = [
    "Model",
    "load_model",
    "compute_state",
    "StateResult",
    "capacity_addition_rows",
    "cost_summary",
]
