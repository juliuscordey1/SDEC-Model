"""CLI: build the self-contained SDEC HTML report from a JSON config.

Usage:
    python -m scripts.build_report data/sdec_model.json out.html
    python scripts/build_report.py data/sdec_model.json out.html

If ``out.html`` is omitted it defaults to ``sdec_report.html`` in the current
directory.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/build_report.py ...) as well
# as a module (python -m scripts.build_report ...).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sdec_model import load_model  # noqa: E402
from sdec_model.report import write_report  # noqa: E402
from sdec_model.schema import SchemaError  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the SDEC demand-and-capacity HTML report.")
    parser.add_argument("config", help="Path to the SDEC JSON config (e.g. data/sdec_model.json)")
    parser.add_argument("output", nargs="?", default="sdec_report.html", help="Output HTML path")
    args = parser.parse_args(argv)

    try:
        model = load_model(args.config)
    except (OSError, SchemaError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out = write_report(model, args.output)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
