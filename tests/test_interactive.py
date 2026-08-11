"""Tests for the standalone interactive tool build and its config bridge."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.build_interactive import build

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "data" / "sdec_model.json"
COMMITTED = ROOT / "interactive" / "sdec_interactive.html"
BRIDGE_CHECK = ROOT / "tests" / "bridge_check.js"

_CONFIG_RE = re.compile(r"/\*CONFIG_START\*/\s*const CONFIG =\s*(\{.*?\});\s*/\*CONFIG_END\*/", re.S)


def _embedded_config(html: str) -> dict:
    m = _CONFIG_RE.search(html)
    assert m, "embedded CONFIG block not found"
    return json.loads(m.group(1))


def test_build_embeds_exact_config(tmp_path):
    out = build(CONFIG, tmp_path / "tool.html")
    html = out.read_text(encoding="utf-8")
    assert _embedded_config(html) == json.loads(CONFIG.read_text(encoding="utf-8"))


def test_build_is_self_contained(tmp_path):
    out = build(CONFIG, tmp_path / "tool.html")
    html = out.read_text(encoding="utf-8")
    # no external stylesheets/scripts/images
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html)
    # the config bridge and its controls are present and wired
    assert "function configToState" in html
    assert "function stateToConfig" in html
    assert 'id="btn-load-config"' in html
    assert 'id="btn-download-config"' in html
    assert "initConfigIO();" in html
    # the hardcoded seed is gone (config-driven now)
    assert "const WD_OCC" not in html
    # exactly one real (line-start) declaration; the bridge header comment also
    # mentions it, so match the statement rather than the substring
    assert html.count("\nconst state = configToState(CONFIG);") == 1


def test_committed_tool_matches_config():
    """The committed interactive HTML must be in sync with the JSON config.
    Regenerate with `python -m scripts.build_interactive` if this fails."""
    assert COMMITTED.exists(), "interactive/sdec_interactive.html has not been built/committed"
    html = COMMITTED.read_text(encoding="utf-8")
    assert _embedded_config(html) == json.loads(CONFIG.read_text(encoding="utf-8")), (
        "committed interactive tool is stale - rerun scripts/build_interactive.py"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
def test_config_bridge_roundtrip_via_node():
    """The config<->state bridge round-trips losslessly and reproduces the
    original tool's seed numbers (checked by tests/bridge_check.js)."""
    proc = subprocess.run(["node", str(BRIDGE_CHECK)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr or proc.stdout
