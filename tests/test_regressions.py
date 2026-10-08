"""Bug-regression tests: quarantine, cancel honesty, sby paths, verilator sim.

EDA binaries are faked with monkeypatch - these tests run anywhere.
"""

import os
import types

import test_gates as G

from agentic_mcp import eda, store, tools


def _home(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTIC_MCP_HOME", str(tmp_path / "home"))


def _mkdesign():
    d = tools.create_design(
        module="counter", clock="clk 100 MHz", reset="active-low async rst_n", pdk="sky130"
    )
    assert d.ok
    return d.evidence["design_id"]


def test_rejected_rtl_is_quarantined(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    did = _mkdesign()
    bad = tools.add_rtl(
        design_id=did, filename="bad.v", content="module bad (inout wire x); endmodule"
    )
    assert not bad.ok and "quarantined" in bad.summary
    assert not (store.design_dir(did) / "rtl" / "bad.v").exists()
    # design still usable afterwards
    assert tools.add_rtl(design_id=did, filename="counter.v", content=G.GOOD).ok


def test_cancel_marks_working_job_cancelled(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    did = _mkdesign()
    rec = store.new_job(did, "dft", ["dft"])
    assert rec["status"] == "working"
    v = tools.cancel_job(design_id=did, job_id=rec["job_id"])
    assert v.ok
    rec2, err = store.get_job_record(did, rec["job_id"])
    assert err is None and rec2["status"] == "cancelled"


def test_prove_uses_relative_sby_path(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    did = _mkdesign()
    (store.design_dir(did) / "rtl" / "task.sby").write_text("[tasks]\n")
    seen = {}

    def fake_which(tool):
        return "/fake/sby" if tool == "sby" else None

    def fake_run(argv, workdir, timeout=60, **kw):
        seen["argv"] = argv
        return types.SimpleNamespace(
            ok=True,
            code=0,
            stdout="sby: PASS reached",
            stderr="",
            truncated=False,
            timed_out=False,
            cancelled=False,
            refused="",
            missing_tool="",
        )

    monkeypatch.setattr(eda, "which", fake_which)
    monkeypatch.setattr(eda, "run", fake_run)
    v = tools.prove(design_id=did)
    assert v.ok, v.errors
    assert seen["argv"][0] == "sby"
    assert seen["argv"][2].replace(os.sep, "/") == "rtl/task.sby", seen["argv"]


def test_simulate_verilator_path(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    did = _mkdesign()
    assert tools.add_rtl(design_id=did, filename="counter.v", content=G.GOOD).ok
    assert tools.add_rtl(design_id=did, filename="counter_tb.v", content=G.GOOD_TB).ok
    calls = []

    def fake_which(tool):
        return f"/fake/{tool}" if tool == "verilator" else None

    def fake_run(argv, workdir, timeout=60, **kw):
        calls.append(argv[0])
        if argv[0] == "verilator":
            assert "--top-module" in argv and "counter_tb" in argv
            return types.SimpleNamespace(ok=True, code=0, stdout="", stderr="")
        assert argv[0] in ("./simv", "./simv.exe"), argv
        return types.SimpleNamespace(ok=True, code=0, stdout="TEST PASSED\n", stderr="")

    monkeypatch.setattr(eda, "which", fake_which)
    monkeypatch.setattr(eda, "run", fake_run)
    v = tools.simulate(design_id=did)
    assert v.ok and v.summary == "PASS: TEST PASSED", v.errors
    assert v.evidence["tool"] == "verilator"
    assert calls[0] == "verilator"


def test_eda_cancel_kills_process(monkeypatch, tmp_path):
    """Deterministic: fake Popen blocks until killed; cancel event set
    from another thread must produce a cancelled (not hung, not ok) result."""
    import subprocess
    import threading
    import time

    monkeypatch.setattr(eda, "which", lambda tool: f"/fake/{tool}")

    class FakeProc:
        def __init__(self):
            self.killed = threading.Event()
            self.returncode = None

        def communicate(self):
            self.killed.wait(timeout=30)
            self.returncode = -9
            return ("", "")

        def kill(self):
            self.killed.set()

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    ev = threading.Event()
    out = {}
    th = threading.Thread(
        target=lambda: out.setdefault(
            "r", eda.run(["yosys", "-p", "bogus"], str(tmp_path), timeout=60, cancel=ev)
        )
    )
    th.start()
    time.sleep(0.5)
    ev.set()
    th.join(timeout=15)
    assert not th.is_alive(), "cancel hung - process not killed"
    assert out["r"].cancelled and not out["r"].ok


def test_traversal_handles_rejected(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    for bad in ("../escape", "..\\escape", "job_x", "dsg_" + "x" * 80):
        rec, err = store.get_job_record(bad, "job_" + "y" * 16)
        assert err is not None, bad
        rec, err = store.get_job_record("dsg_" + "z" * 16, bad)
        assert err is not None, bad
    v = tools.get_job(design_id="../../etc", job_id="job_" + "y" * 16)
    assert not v.ok
    v = tools.cancel_job(design_id="../../etc", job_id="job_" + "y" * 16)
    assert not v.ok


def test_power_inputs_clamped(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    did = _mkdesign()
    v = tools.estimate_power(design_id=did, freq_mhz=-50.0, vdd=-1.0)
    assert not v.ok  # no cells yet either way
    assert tools.add_rtl(design_id=did, filename="counter.v", content=G.GOOD).ok
    v = tools.estimate_power(design_id=did, freq_mhz=0, vdd=0)
    assert not v.ok  # still no synth cells: honest, and no crash on zeros
