"""Opt-in live regression of actual agent nodes using installed Cadence."""
import asyncio
import json
import os
from pathlib import Path
import uuid

import pytest

from backend.agents.symbol import nodes
from backend.agents.symbol.config import SYMBOL_JOB_ROOT


@pytest.mark.integration
@pytest.mark.skipif(os.environ.get("RUN_SYMBOL_CAPTURE_LIVE") != "1", reason="Set RUN_SYMBOL_CAPTURE_LIVE=1 to run Cadence")
def test_agent_generates_native_olb_and_dsn():
    fixture = Path(__file__).parent / "golden/symbol/ads1115/ADS1115_spec.json"
    pins = json.loads(fixture.read_text(encoding="utf-8"))["pins"]
    out_dir = SYMBOL_JOB_ROOT / "live_regression" / uuid.uuid4().hex

    async def scenario():
        state = {"device": "ADS1115", "merged_pins": pins, "work_dir": str(out_dir),
                 "output_dir": str(out_dir), "check_report": {"verdict": "solid", "ok": True}}
        state.update(await nodes.build_layout_node(state))
        state.update(await nodes.generate_capture_node(state))
        assert state["status"] == "capture_ok", state.get("errors")
        state.update(await nodes.finalize_node(state))
        assert state["status"] == "completed"
        assert set(state["artifact_paths"]) == {"tcl", "olb", "dsn", "capture_log"}
        for extension in ("olb", "dsn"):
            assert Path(state["artifact_paths"][extension]).read_bytes().startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
        out_dir.joinpath("agent_result.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nCapture live artifacts: {out_dir}")

    asyncio.run(scenario())
