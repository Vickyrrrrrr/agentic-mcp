"""The 9 MCP tools. Thin orchestration over gates.py + eda.py + store.py.

Conventions (MCP spec 2026-07-28, server/tools):
- names are [a-z0-9_], deterministic tools/list order = registration order
- every tool returns a Verdict: ok + summary + errors + evidence, and the
  summary text always starts with PASS:/FAIL:/ERROR: so any client - even one
  that ignores structuredContent - gets a machine-readable verdict
- missing tools/PKDs are verdicts (ok=False), never exceptions, never fakes
- long jobs return a job_* handle immediately (Tasks-pattern over plain
  tools: works on every client, no extension opt-in needed)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from agentic_mcp import eda, gates, store

logger = logging.getLogger(__name__)

SIGNOFF_LABEL = "OSS_LAYOUT_CANDIDATE"


class Verdict(BaseModel):
    ok: bool
    stage: str
    summary: str
    errors: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


def _fail(stage: str, summary: str, errors: list[str], **evidence) -> Verdict:
    return Verdict(
        ok=False, stage=stage, summary=f"FAIL: {summary}", errors=errors, evidence=evidence
    )


def _pass(stage: str, summary: str, **evidence) -> Verdict:
    return Verdict(ok=True, stage=stage, summary=f"PASS: {summary}", evidence=evidence)


def _design_files(design_id: str) -> tuple[dict[str, str] | None, Verdict | None]:
    spec, err = store.load_spec(design_id)
    if err:
        return None, _fail("load", f"design unavailable ({err})", [err])
    files = {}
    for p in store.rtl_files(design_id):
        try:
            files[p.name] = p.read_text(errors="replace")
        except OSError as exc:
            return None, _fail("load", f"unreadable file {p.name}", [str(exc)[:200]])
    return files, None


# ---------------------------------------------------------------- create ---


def create_design(
    module: str = "", clock: str = "", reset: str = "", pdk: str = "", description: str = ""
) -> Verdict:
    """Freeze a chip spec and open a design. Returns an opaque design_id
    handle used by every other tool. Handles never expire; jobs expire
    after 7 days of inactivity (expired handles return explicit errors).

    All fields are optional at the protocol level so INCOMPLETE specs reach
    this function and come back as an actionable missing-field verdict
    instead of a schema error. Missing clock/reset/pdk/module are reported
    explicitly - resubmit with exactly those filled in."""
    spec, missing = gates.freeze_spec(module, clock, reset, pdk, description)
    if missing:
        return _fail(
            "spec_validate",
            "spec incomplete - resubmit create_design with: " + "; ".join(missing),
            missing,
        )
    design_id = store.new_id("dsg")
    spec["spec_hash"] = store.spec_hash(spec)
    store.save_spec(design_id, spec)
    store.append_ledger(
        {
            "event": "design_created",
            "design": design_id,
            "module": spec["module"],
            "pdk": spec["pdk"],
        }
    )
    return _pass(
        "spec_validate",
        f"design {design_id} frozen ({spec['module']} @ {spec['clock']}, "
        f"{spec['reset']}, {spec['pdk']})",
        design_id=design_id,
        spec_hash=spec["spec_hash"],
    )


# ------------------------------------------------------------------ rtl ---

_FILENAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.(v|sv)$")


def add_rtl(design_id: str, filename: str, content: str) -> Verdict:
    """Write one RTL file into the design and run the contract gate on the
    whole design immediately (fail fast). Overwrites same-name files."""
    spec, err = store.load_spec(design_id)
    if err:
        return _fail("load", f"design unavailable ({err})", [err])
    if not _FILENAME.match(filename or ""):
        return _fail(
            "add_rtl",
            f"refused filename {filename!r}",
            ["filename must match [A-Za-z0-9_.-]+\\.(v|sv)"],
        )
    if not (content or "").strip():
        return _fail("add_rtl", "empty content refused", ["content must be non-empty"])
    if len(content) > 1_000_000:
        return _fail("add_rtl", "file over 1MB refused", ["split the file"])
    try:
        (store.design_dir(design_id) / "rtl" / filename).write_text(content)
    except OSError as exc:
        return _fail("add_rtl", f"write failed: {exc}", [str(exc)[:200]])
    files, verr = _design_files(design_id)
    assert files is not None and verr is None
    dups = gates.duplicate_modules(files)
    if dups:
        try:
            (store.design_dir(design_id) / "rtl" / filename).unlink()
        except OSError:
            pass
        detail = [
            f"{m} also in {', '.join(fs)}" for m, fs in sorted(dups.items()) if filename in fs
        ]
        return _fail(
            "contract_gate",
            f"duplicate module(s) - {filename} quarantined (not stored)",
            detail or [f"{m}: defined in {', '.join(fs)}" for m, fs in sorted(dups.items())][:10],
            file=filename,
        )
    passed, hits, advisories = gates.contract_gate(files)
    if not passed:
        # Quarantine: a rejected file must not poison later gates.
        try:
            (store.design_dir(design_id) / "rtl" / filename).unlink()
        except OSError:
            pass
        store.write_report(
            design_id,
            "contract",
            {"ok": False, "violations": hits, "advisories": advisories, "quarantined": filename},
        )
        return _fail(
            "contract_gate",
            f"{len(hits)} contract violation(s) - {filename} quarantined (not stored)",
            hits[:10],
            file=filename,
        )
    store.write_report(
        design_id,
        "contract",
        {"ok": True, "violations": [], "advisories": advisories, "files": sorted(files)},
    )
    return _pass(
        "contract_gate", f"{filename} accepted ({len(files)} file(s) clean)", file=filename
    )


# ---------------------------------------------------------------- check ---


def _static_lint(files: dict[str, str]) -> list[dict]:
    """Dependency-free lint: block balance + module structure (always runs)."""
    diags: list[dict] = []
    for name, content in files.items():
        stripped = re.sub(r"/\*.*?\*/", "", content or "", flags=re.S)
        stripped = re.sub(r"//.*", "", stripped)
        mods = re.findall(r"(?m)^\s*module\s+[A-Za-z_][A-Za-z0-9_$]*\b", stripped)
        ends = re.findall(r"(?m)^\s*endmodule\b", stripped)
        if not mods:
            diags.append({"file": name, "line": 1, "message": "no module declaration"})
        if len(mods) != len(ends):
            diags.append(
                {
                    "file": name,
                    "line": 1,
                    "message": f"module/endmodule mismatch ({len(mods)}/{len(ends)})",
                }
            )
        for opener, closer in (
            ("begin", "end"),
            ("case", "endcase"),
            ("function", "endfunction"),
            ("task", "endtask"),
        ):
            if len(re.findall(rf"\b{opener}\b", stripped)) != len(
                re.findall(rf"\b{closer}\b", stripped)
            ):
                diags.append({"file": name, "line": 1, "message": f"{opener}/{closer} mismatch"})
    return diags


def check_design(design_id: str) -> Verdict:
    """Run every fast deterministic gate (contract, CDC, static lint,
    testbench gate). No EDA tool needed. Use before simulate/synthesize."""
    files, verr = _design_files(design_id)
    if verr:
        return verr
    assert files is not None
    if not files:
        return _fail("check", "design has no RTL - add files with add_rtl first", ["empty design"])
    errors: list[str] = []
    evidence: dict[str, Any] = {"files": sorted(files)}
    passed, hits, advisories = gates.contract_gate(files)
    evidence["contract"] = {"passed": passed, "violations": hits, "advisories": advisories}
    if not passed:
        errors.extend(hits[:10])
    cdc_ok, cdc_ev, cdc_err = gates.cdc_gate(files)
    evidence["cdc"] = {"passed": cdc_ok, **cdc_ev}
    if not cdc_ok:
        errors.extend(cdc_err)
    diags = _static_lint(files)
    evidence["lint"] = {"diagnostics": diags[:20], "count": len(diags)}
    if diags:
        errors.extend(f"{d['file']}:{d['line']}: {d['message']}" for d in diags[:8])
    tb_ok, tb_ev, tb_err = gates.tb_gate(files)
    evidence["testbench"] = {"passed": tb_ok, **tb_ev}
    if not tb_ok:
        errors.extend(tb_err)
    # Real-tool lint when available (verilator -> iverilog cascade).
    evidence["tool_lint"] = _tool_lint(design_id, files)
    if not evidence["tool_lint"]["ran"]:
        evidence["tool_lint"]["note"] = "no linter installed; static lint above applies"
    elif not evidence["tool_lint"]["clean"]:
        errors.extend(evidence["tool_lint"]["diagnostics"][:8])
    store.write_report(design_id, "check", {"ok": not errors, "evidence": evidence})
    if errors:
        return _fail("check", f"{len(errors)} finding(s)", errors[:15], **evidence)
    return _pass("check", f"all gates clean ({len(files)} file(s))", **evidence)


def _tool_lint(design_id: str, files: dict[str, str]) -> dict:
    workdir = str(store.design_dir(design_id))
    if eda.available("verilator"):
        r = eda.run(
            [
                "verilator",
                "--lint-only",
                "-Irtl",
                "-y",
                "rtl",
                *[f"rtl/{n}" for n in sorted(files)],
            ],
            workdir,
            timeout=60,
        )
        diags = [
            {"message": ln[:200]}
            for ln in (r.stdout + r.stderr).splitlines()
            if ln.startswith("%Error")
        ]
        return {
            "ran": True,
            "tool": "verilator",
            "clean": r.ok,
            "diagnostics": [d["message"] for d in diags[:10]]
            or ([(r.stderr or r.stdout or "")[:300]] if not r.ok else []),
        }
    if eda.available("iverilog"):
        r = eda.run(
            [
                "iverilog",
                "-o",
                "nul" if os.name == "nt" else "/dev/null",
                "-t",
                "null",
                "-Irtl",
                "-y",
                "rtl",
                *[f"rtl/{n}" for n in sorted(files)],
            ],
            workdir,
            timeout=60,
        )
        diags = [
            ln[:200] for ln in (r.stdout + r.stderr).splitlines() if re.search(r"error", ln, re.I)
        ]
        return {"ran": True, "tool": "iverilog", "clean": r.ok, "diagnostics": diags[:10]}
    return {"ran": False, "tool": None, "clean": True, "diagnostics": []}


# --------------------------------------------------------------- simulate ---


def simulate(design_id: str, timeout_s: int = 120) -> Verdict:
    """Compile + run the testbench (iverilog+vvp, else verilator). Verdict
    comes from the TB's TEST PASSED marker, never from prose."""
    files, verr = _design_files(design_id)
    if verr:
        return verr
    assert files is not None
    tb_ok, tb_ev, tb_err = gates.tb_gate(files)
    if not tb_ok:
        return _fail("tb_gate", "simulation refused: " + "; ".join(tb_err), tb_err)
    workdir = store.design_dir(design_id)
    tb = tb_ev.get("tb", "")
    ordered = [f"rtl/{n}" for n in sorted(files) if n != tb] + [f"rtl/{tb}"]
    if eda.available("iverilog") and eda.available("vvp"):
        c = eda.run(
            ["iverilog", "-g2012", "-o", "sim.vvp", *ordered],
            str(workdir),
            timeout=min(timeout_s, 300),
        )
        if not c.ok:
            return _fail(
                "simulate", "iverilog compile failed", [(c.stderr or c.stdout or "")[-2000:]]
            )
        r = eda.run(["vvp", "sim.vvp"], str(workdir), timeout=min(timeout_s, 300))
        log = (r.stdout or "") + "\n" + (r.stderr or "")
        passed = "TEST PASSED" in log and "TEST FAILED" not in log
        verdict = (
            _pass("simulate", "TEST PASSED")
            if (r.ok and passed)
            else _fail(
                "simulate",
                "testbench did not pass",
                [
                    "TEST FAILED marker present"
                    if "TEST FAILED" in log
                    else (
                        "no TEST PASSED marker"
                        if "TEST PASSED" not in log
                        else (log[-2000:] or "failed")
                    )
                ],
            )
        )
        verdict.evidence.update({"tool": "iverilog+vvp", "log_tail": log[-2000:]})
        store.write_report(design_id, "simulate", verdict.model_dump())
        return verdict
    if eda.available("verilator"):
        tb_mods = gates.module_names(files.get(tb, ""))
        tb_top = tb_mods[0] if tb_mods else Path(tb).stem
        exe = "simv.exe" if os.name == "nt" else "simv"
        c = eda.run(
            ["verilator", "--binary", "-j", "0", "--top-module", tb_top, "-o", exe, *ordered],
            str(workdir),
            timeout=min(timeout_s, 300),
        )
        if not c.ok:
            return _fail(
                "simulate", "verilator build failed", [(c.stderr or c.stdout or "")[-2000:]]
            )
        r = eda.run([f"./{exe}"], str(workdir), timeout=min(timeout_s, 300))
        log = (r.stdout or "") + "\n" + (r.stderr or "")
        passed = "TEST PASSED" in log and "TEST FAILED" not in log
        verdict = (
            _pass("simulate", "TEST PASSED")
            if (r.ok and passed)
            else _fail(
                "simulate",
                "testbench did not pass",
                [
                    "TEST FAILED marker present"
                    if "TEST FAILED" in log
                    else (
                        "no TEST PASSED marker"
                        if "TEST PASSED" not in log
                        else (log[-2000:] or "failed")
                    )
                ],
            )
        )
        verdict.evidence.update({"tool": "verilator", "log_tail": log[-2000:]})
        store.write_report(design_id, "simulate", verdict.model_dump())
        return verdict
    return _fail(
        "simulate",
        "no simulator installed (need iverilog+vvp or verilator)",
        ["install https://github.com/YosysHQ/oss-cad-suite-build"],
        needed=["iverilog", "vvp", "verilator"],
    )


# ------------------------------------------------------------- synthesize ---

_YOSYS_SYNTH = (
    "read_verilog {files}; hierarchy -check -top {top}; "
    "proc; opt; synth -top {top}; stat; "
    "write_verilog {netlist}; write_spice {spice}; write_json {elab}"
)


def synthesize(design_id: str, timeout_s: int = 300) -> Verdict:
    """Synthesize with Yosys (their proven script: read_verilog, hierarchy
    -check, synth -top, stat, write_verilog + write_json). Reports cells."""
    files, verr = _design_files(design_id)
    if verr:
        return verr
    assert files is not None
    spec, _ = store.load_spec(design_id)
    top = (spec or {}).get("module", "")
    rtl = [f"rtl/{n}" for n in sorted(files) if not (n.endswith("_tb.v") or n.endswith("_tb.sv"))]
    if not rtl:
        return _fail(
            "synthesize", "no synthesizable RTL (only testbenches?)", ["add design files first"]
        )
    if not eda.available("yosys"):
        return _fail(
            "synthesize",
            "yosys not installed",
            ["install https://github.com/YosysHQ/oss-cad-suite-build"],
            needed=["yosys"],
        )
    workdir = store.design_dir(design_id)
    # Starter SDC from the frozen clock (review before signoff).
    sdc, sdc_missing = gates.sdc_from_spec(spec or {})
    if sdc:
        try:
            (workdir / "design.sdc").write_text(sdc)
        except OSError:
            pass
    script = _YOSYS_SYNTH.format(
        files=" ".join(rtl), top=top, netlist="synth_netlist.v", spice="sch.spi", elab="elab.json"
    )
    r = eda.run(["yosys", "-q", "-p", script], str(workdir), timeout=min(timeout_s, 900))
    log = (r.stdout or "") + "\n" + (r.stderr or "")
    if not r.ok:
        v = _fail("synthesize", "yosys failed", [log[-2000:] or "failed"])
        v.evidence["log_tail"] = log[-2000:]
        store.write_report(design_id, "synthesize", v.model_dump())
        return v
    parsed = gates.parse_synth(log)
    cells = parsed["cells"]
    dffs = parsed["dffs"]
    try:
        elab = json.loads((workdir / "elab.json").read_text())
        ports = elab.get("modules", {}).get(top, {}).get("ports", {})
    except (OSError, ValueError):
        ports = {}
    v = _pass(
        "synthesize",
        f"{cells} cells ({dffs} flops), top {top} ({len(ports)} ports)"
        if cells
        else f"synthesis ok, top {top}",
    )
    v.evidence.update(
        {
            "cells": cells,
            "dffs": dffs,
            "ports": sorted(ports),
            "netlist": "synth_netlist.v",
            "spice": "sch.spi",
            "sdc": "design.sdc" if sdc else None,
        }
    )
    store.write_report(design_id, "synthesize", v.model_dump())
    return v


# ------------------------------------------------------------------ flow ---

FlowKind = Literal["sta", "pnr", "drc", "lvs", "dft", "spice"]


def _flow_prereqs(design_id: str, kind: str) -> tuple[bool, list[str], dict]:
    """Honesty gate: refuse long jobs whose collateral is missing."""
    workdir = store.design_dir(design_id)
    if kind == "sta":
        libs = sorted(workdir.glob("*.lib")) + sorted((workdir / "rtl").glob("*.lib"))
        sdc = list(workdir.glob("*.sdc")) + sorted((workdir / "rtl").glob("*.sdc"))
        if (workdir / "design.sdc").is_file() and not sdc:
            sdc = [workdir / "design.sdc"]
        net = workdir / "synth_netlist.v"
        missing = []
        if not eda.available("opensta"):
            missing.append("opensta not installed")
        if not libs:
            missing.append("no .lib liberty file in design (add yours)")
        if not sdc:
            missing.append("no .sdc constraints in design")
        if not net.is_file():
            missing.append("no synth_netlist.v - run synthesize first")
        return (not missing), missing, {"libs": len(libs), "corners": len(libs)}
    if kind == "pnr":
        missing = []
        if not (eda.available("openroad") or eda.available("openlane")):
            missing.append("neither openroad nor openlane installed")
        if not (workdir / "synth_netlist.v").is_file():
            missing.append("no synth_netlist.v - run synthesize first")
        return (not missing), missing, {}
    if kind == "drc":
        gds = list(workdir.glob("*.gds")) + list(workdir.glob("*.oas"))
        tech = list(workdir.glob("*.tech"))
        missing = []
        if not eda.available("magic"):
            missing.append("magic not installed")
        if not gds:
            missing.append("no layout (.gds/.oas) in design — run pnr first")
        if not tech:
            missing.append("no .tech file in design")
        return (not missing), missing, {}
    if kind == "lvs":
        missing = []
        for tool, _what in (("magic", "magic (extraction)"), ("netgen", "netgen")):
            if not eda.available(tool):
                missing.append(f"{tool} not installed")
        if not list(workdir.glob("*.gds")):
            missing.append("no .gds layout in design — run pnr first")
        if not list(workdir.glob("*.tech")):
            missing.append("no .tech file in design")
        if not (sorted(workdir.glob("*setup*.tcl")) + sorted(workdir.glob("*.lvs.tcl"))):
            missing.append("no netgen setup (.lvs.tcl) in design")
        if not (
            sorted(workdir.glob("sch.spice"))
            + sorted(workdir.glob("*.sch.spi"))
            + ([workdir / "sch.spi"] if (workdir / "sch.spi").is_file() else [])
        ):
            missing.append("no schematic SPICE (sch.spice) in design")
        return (not missing), missing, {}
    if kind == "dft":
        # Deterministic testability analysis needs no tool; scan insertion
        # itself is experimental (their --experimental flag, honored here).
        return True, [], {"mode": "testability"}
    if kind == "spice":
        # Scoped post-layout only (their default): a deck or GDS+tech+scope.
        deck = list(workdir.glob("*.spice")) + list(workdir.glob("*.cir"))
        gds = list(workdir.glob("*.gds"))
        tech = list(workdir.glob("*.tech"))
        missing = []
        if not eda.available("ngspice"):
            missing.append("ngspice not installed")
        if not deck and not (gds and tech):
            missing.append("need a .spice/.cir deck, or GDS+tech for scoped extraction")
        return (not missing), missing, {"decks": len(deck)}
    return False, [f"unknown flow {kind}"], {}


_JOBS: dict[str, Any] = {}
_JOB_THREADS: dict[str, threading.Thread] = {}
_JOB_CANCEL: dict[str, threading.Event] = {}
_JOB_PROC: dict[str, Any] = {}
_JOB_LOCK = threading.Lock()


def run_flow(design_id: str, kind: FlowKind, timeout_s: int = 1800, scope: str = "") -> Verdict:
    """Start a long flow (sta/pnr/drc/lvs/dft/spice). Returns a job_*
    handle immediately - poll get_job, cancel with cancel_job. Jobs expire
    after 7 days. Prerequisite collateral is checked BEFORE queuing.
    spice is scoped-post-layout only: pass scope=<cell> (full-chip refused).
    timeout_s bounds total tool runtime (clamped 10..3600s)."""
    spec, err = store.load_spec(design_id)
    if err:
        return _fail("load", f"design unavailable ({err})", [err])
    ok, missing, ev = _flow_prereqs(design_id, kind)
    if not ok:
        return _fail(kind, "flow refused: " + "; ".join(missing), missing, **ev)
    rec = store.new_job(design_id, kind, [kind])
    store.append_ledger(
        {"event": "job_started", "design": design_id, "job": rec["job_id"], "kind": kind}
    )
    cancel = threading.Event()
    with _JOB_LOCK:
        _JOB_CANCEL[rec["job_id"]] = cancel
    budget = max(10, min(int(timeout_s or 1800), 3600))

    def _runner(argv: list[str], timeout: int = 600):
        def _capture(proc) -> None:
            with _JOB_LOCK:
                _JOB_PROC[rec["job_id"]] = proc

        try:
            return eda.run(
                argv,
                str(store.design_dir(design_id)),
                timeout=min(timeout, budget),
                cancel=cancel,
                on_proc=_capture,
            )
        finally:
            with _JOB_LOCK:
                _JOB_PROC.pop(rec["job_id"], None)

    t = threading.Thread(
        target=_run_job,
        args=(design_id, rec["job_id"], kind, scope, _runner),
        daemon=True,
        name=f"flow-{rec['job_id']}",
    )
    _JOB_THREADS[rec["job_id"]] = t
    t.start()
    v = _pass(kind, f"job {rec['job_id']} queued - poll get_job (suggested interval 15s)")
    v.evidence.update({"job_id": rec["job_id"], "poll_interval_s": 15})
    return v


def _run_job(design_id: str, job_id: str, kind: str, scope: str, run) -> None:
    store.update_job(design_id, job_id, status="working", message=f"{kind} running")
    try:
        if kind == "sta":
            result = _do_sta(design_id, run)
        elif kind == "pnr":
            result = _do_pnr(design_id, run)
        elif kind == "dft":
            result = _do_dft(design_id, run)
        elif kind == "spice":
            result = _do_spice(design_id, scope, run)
        elif kind == "drc":
            result = _do_drc(design_id, run)
        elif kind == "lvs":
            result = _do_lvs(design_id, run)
        else:
            result = {
                "ok": False,
                "summary": f"unknown flow {kind}",
                "errors": [f"unknown flow {kind}"],
                "label": SIGNOFF_LABEL,
            }
    except Exception as exc:  # fail-closed, never a lost job
        logger.exception("job %s crashed", job_id)
        result = {"ok": False, "summary": f"job crashed: {exc}"[:300], "errors": [str(exc)[:300]]}
    with _JOB_LOCK:
        cancelled = _JOB_CANCEL.get(job_id) is not None and _JOB_CANCEL[job_id].is_set()
        _JOB_CANCEL.pop(job_id, None)
        _JOB_PROC.pop(job_id, None)
    if cancelled:
        result = {
            "ok": False,
            "summary": "job cancelled by user",
            "errors": ["cancelled"],
            "label": SIGNOFF_LABEL,
        }
        store.update_job(
            design_id, job_id, status="cancelled", message="cancelled by user", error=result
        )
    else:
        store.update_job(
            design_id,
            job_id,
            status="completed" if result.get("ok") else "failed",
            message=result.get("summary", ""),
            result=result if result.get("ok") else None,
            error=None if result.get("ok") else result,
        )
    store.write_report(design_id, kind, result)
    store.append_ledger(
        {"event": "job_done", "design": design_id, "job": job_id, "ok": result.get("ok")}
    )
    _JOB_THREADS.pop(job_id, None)


def _do_sta(design_id: str, run) -> dict:
    """OpenSTA run from their proven Tcl (read_liberty, read_verilog,
    link_design, read_sdc, report_checks, report_worst_slack) - once per
    .lib corner, worst corner decides (their --multi-corner)."""
    import glob as _glob

    workdir = store.design_dir(design_id)
    spec, _ = store.load_spec(design_id)
    top = (spec or {}).get("module", "")
    libs = sorted(_glob.glob(str(workdir / "*.lib")))
    sdc_list = sorted(_glob.glob(str(workdir / "*.sdc")))
    if (workdir / "design.sdc").is_file() and not sdc_list:
        sdc_list = [str(workdir / "design.sdc")]
    sdc = sdc_list[0]
    per_corner: dict[str, float | None] = {}
    worst: float | None = None
    last_log = ""
    max_rpt = ""
    for lib in libs:
        corner = Path(lib).stem
        tcl = "\n".join(
            [
                f"read_liberty {Path(lib).name}",
                "read_verilog synth_netlist.v",
                f"link_design {top}",
                f"read_sdc {Path(sdc).name}",
                f"report_checks -path_delay max -format full_clock_expanded > sta_max_{corner}.rpt",
                f"report_worst_slack -max > sta_wns_{corner}.rpt",
                "write_sdf sta.sdf",
                "exit",
            ]
        )
        (workdir / "run_sta.tcl").write_text(tcl)
        r = run(["opensta", "run_sta.tcl"], timeout=900)
        last_log = (r.stdout or "") + "\n" + (r.stderr or "")
        txt = (
            (workdir / f"sta_wns_{corner}.rpt").read_text(errors="replace")
            if (workdir / f"sta_wns_{corner}.rpt").is_file()
            else last_log
        )
        parsed = gates.parse_sta(txt or "")
        wns = parsed["wns_ns"]
        per_corner[corner] = wns
        if isinstance(wns, (int, float)) and (worst is None or wns < worst):
            worst = wns
            max_rpt = (
                (workdir / f"sta_max_{corner}.rpt").read_text(errors="replace")
                if (workdir / f"sta_max_{corner}.rpt").is_file()
                else ""
            )
    if worst is None:
        return {
            "ok": False,
            "summary": "STA produced no timing numbers (tool error?)",
            "errors": [last_log[-2000:] or "no STA output"],
            "corners": per_corner,
            "worst_wns_ns": None,
            "label": SIGNOFF_LABEL,
        }
    ok = worst >= 0
    crit = gates.critical_paths(max_rpt)
    spec, _ = store.load_spec(design_id)
    fmax = gates.fmax_mhz(gates.spec_period_ns(spec or {}), worst)
    sdf = "sta.sdf" if (workdir / "sta.sdf").is_file() else None
    return {
        "ok": ok,
        "summary": (
            f"timing met on {len(libs)} corner(s), worst WNS {worst} ns"
            + (f", fmax ~{fmax} MHz" if fmax else "")
            if ok
            else f"timing violated, worst WNS {worst} ns"
        ),
        "errors": []
        if ok
        else (
            [
                "critical path: "
                + (crit[0].get("startpoint", "?") + " -> " + crit[0].get("endpoint", "?"))
            ]
            if crit
            else ["setup/hold violation - see sta_wns_*.rpt"]
        ),
        "corners": per_corner,
        "worst_wns_ns": worst,
        "critical_paths": crit,
        "fmax_mhz": fmax,
        "sdf": sdf,
        "label": SIGNOFF_LABEL,
    }


def _do_dft(design_id: str, run) -> dict:
    """Fault scan-chain + ATPG when the native `fault` binary and Liberty /
    cell-model collateral resolve (their recipe); otherwise deterministic
    testability analysis, honestly labeled."""
    files, verr = _design_files(design_id)
    if verr:
        return {"ok": False, "summary": verr.summary, "errors": verr.errors}
    assert files is not None
    workdir = store.design_dir(design_id)
    spec, _ = store.load_spec(design_id)
    net = workdir / "synth_netlist.v"
    libs = sorted(workdir.glob("*.lib"))
    cell_models = sorted(workdir.glob("*cells*.v")) + sorted((workdir / "rtl").glob("*cells*.v"))
    if eda.available("fault") and net.is_file() and libs and cell_models:
        clock = ((spec or {}).get("clock", "") or "clk").split()[0] or "clk"
        reset = re.sub(r"\W+", "", ((spec or {}).get("reset", "") or "rst").split()[0]) or "rst"
        active_low = bool(re.search(r"active\s*-\s*low|_n\b", (spec or {}).get("reset", ""), re.I))
        scan_net = "scan_netlist.v"
        chain = [
            "fault",
            "chain",
            "--clock",
            clock,
            "--reset",
            reset,
            "-l",
            libs[0].name,
            "-c",
            cell_models[0].name,
            "-o",
            scan_net,
            "synth_netlist.v",
        ]
        if active_low:
            chain.insert(chain.index("-l"), "--activeLow")
        r = run(chain, timeout=1200)
        if r.ok and (workdir / scan_net).is_file():
            cut = "cut.v"
            run(
                ["fault", "cut", "-o", cut, scan_net, "--clock", clock, "--reset", reset]
                + (["--activeLow"] if active_low else []),
                timeout=1200,
            )
            a = run(
                [
                    "fault",
                    "-c",
                    cell_models[0].name,
                    "-v",
                    "100",
                    "-r",
                    "50",
                    "-m",
                    "95",
                    "--ceiling",
                    "1000",
                    cut,
                    "--clock",
                    clock,
                    "--reset",
                    reset,
                ]
                + (["--activeLow"] if active_low else []),
                timeout=1200,
            )
            log = (a.stdout or "") + "\n" + (a.stderr or "")
            m = re.search(r"(?:coverage|fault coverage)\s*[:=]\s*(\d+(?:\.\d+)?)\s*%?", log, re.I)
            cov = float(m.group(1)) if m else None
            rep = gates.testability(files)
            return {
                "ok": a.ok,
                "summary": f"fault scan+ATPG: coverage {cov}%"
                if cov is not None
                else "fault scan+ATPG ran",
                "errors": [] if a.ok else [log[-2000:] or "atpg failed"],
                "coverage_percent": cov,
                "scan_netlist": scan_net,
                **rep,
                "label": "EXPERIMENTAL - open-source ATPG evidence, not ATE signoff",
            }
    rep = gates.testability(files)
    return {
        "ok": True,
        "summary": (
            f"{rep['scan_flop_candidates']} scan-flop candidate(s), "
            f"{len(rep['risks'])} risk(s) - testability only"
        ),
        "errors": [],
        **rep,
        "label": "EXPERIMENTAL - not scan signoff",
    }


def _do_spice(design_id: str, scope: str, run) -> dict:
    """Scoped post-layout SPICE (their default): a user deck runs as-is;
    GDS+tech extraction requires an explicit scope cell - full-chip SPICE
    is refused by construction."""
    workdir = store.design_dir(design_id)
    decks = sorted(workdir.glob("*.spice")) + sorted(workdir.glob("*.cir"))
    if decks:
        deck = decks[0]
        r = run(["ngspice", "-b", deck.name], timeout=900)
        log = (r.stdout or "") + "\n" + (r.stderr or "")
        ok = r.ok and bool(re.search(r"no errors|simulation.*complete|transient.*done", log, re.I))
        (workdir / "spice.log").write_text(log[-20000:])
        return {
            "ok": ok,
            "summary": f"ngspice {deck.name}: {'clean' if ok else 'see spice.log'}",
            "errors": [] if ok else [log[-2000:] or "spice failed"],
            "deck": deck.name,
            "label": SIGNOFF_LABEL,
        }
    if not scope:
        return {
            "ok": False,
            "summary": "scoped extraction needs scope=<cell>",
            "errors": ["full-chip SPICE is not in the OSS flow - pass scope=<cell name>"],
        }
    return {
        "ok": False,
        "summary": "magic extraction not attempted without tech-verified setup",
        "errors": ["add extracted <scope>.spice deck and re-run run_flow(spice)"],
    }


def _do_pnr(design_id: str, run) -> dict:
    if eda.available("openlane"):
        return {
            "ok": False,
            "summary": "openlane present but needs a config.json flow setup",
            "errors": ["add openlane config.json, then re-run run_flow(pnr)"],
            "label": SIGNOFF_LABEL,
        }
    run(["openroad", "-version"], timeout=60)  # liveness probe only
    return {
        "ok": False,
        "summary": "openroad present but needs flow scripts (ORFS)",
        "errors": ["point AGENTIC_ORFS_ROOT at OpenROAD-flow-scripts, then re-run"],
        "label": SIGNOFF_LABEL,
    }


def _do_drc(design_id: str, run) -> dict:
    """Magic batch DRC (their Tcl, with the `drc catchall` typo corrected to
    `drc catchup`) + antenna check. Verdict from the report, not the exit code."""
    workdir = store.design_dir(design_id)
    spec, _ = store.load_spec(design_id)
    top = (spec or {}).get("module", "")
    gds = sorted(workdir.glob("*.gds")) + sorted(workdir.glob("*.oas"))
    tech = sorted(workdir.glob("*.tech"))
    if not gds or not tech:
        return {
            "ok": False,
            "summary": "drc refused: need .gds/.oas + .tech in design",
            "errors": ["add layout GDS and magic tech file, then re-run"],
            "label": SIGNOFF_LABEL,
        }
    gds_name, tech_name = gds[0].name, tech[0].name
    tcl = "\n".join(
        [
            f"tech load {tech_name}",
            f"gds read {gds_name}",
            f"load {top}",
            "select top cell",
            "drc on",
            "drc check",
            "drc catchup",
            "drc report drc.rpt",
            "quit",
            "",
        ]
    )
    (workdir / "drc_run.tcl").write_text(tcl)
    r = run(["magic", "-dnull", "-noconsole", "drc_run.tcl"], timeout=1200)
    rep = (
        (workdir / "drc.rpt").read_text(errors="replace")
        if (workdir / "drc.rpt").is_file()
        else (r.stdout or "") + "\n" + (r.stderr or "")
    )
    violations = gates.parse_drc_violations(rep)
    ant: dict = {}
    if tech:
        atcl = "\n".join(
            [
                f"tech load {tech_name}",
                f"gds read {gds_name}",
                f"load {top}",
                "select top cell",
                "antennacheck -ratio 4.0",
                "antennacheck report antenna.rpt",
                "quit",
                "",
            ]
        )
        (workdir / "antenna_run.tcl").write_text(atcl)
        run(["magic", "-dnull", "-noconsole", "antenna_run.tcl"], timeout=600)
        if (workdir / "antenna.rpt").is_file():
            ant = {
                "report": "antenna.rpt",
                "text": (workdir / "antenna.rpt").read_text(errors="replace")[-2000:],
            }
    ok = r.ok and not violations
    return {
        "ok": ok,
        "summary": ("DRC clean" if ok else f"{len(violations)} DRC violation(s)"),
        "errors": [] if ok else [v["rule"] + " @ " + v["layer"] for v in violations[:10]],
        "violations": violations[:50],
        "antenna": ant,
        "label": SIGNOFF_LABEL,
    }


def _do_lvs(design_id: str, run) -> dict:
    """Netgen batch LVS (their Tcl). Layout SPICE comes from Magic
    extraction of the GDS; schematic SPICE must be supplied (or is the
    yosys write_spice output recorded at synthesis). Both sides
    tool-derived is stated, not hidden."""
    workdir = store.design_dir(design_id)
    spec, _ = store.load_spec(design_id)
    top = (spec or {}).get("module", "")
    gds = sorted(workdir.glob("*.gds"))
    tech = sorted(workdir.glob("*.tech"))
    setup = sorted(workdir.glob("*setup*.tcl")) + sorted(workdir.glob("*.lvs.tcl"))
    sch = sorted(workdir.glob("sch.spice")) + sorted(workdir.glob("*.sch.spi"))
    if not sch and (workdir / "sch.spi").is_file():
        sch = [workdir / "sch.spi"]
    missing = []
    if not gds:
        missing.append("no .gds layout — run pnr first")
    if not tech:
        missing.append("no .tech file for extraction")
    if not setup:
        missing.append("no netgen setup (.lvs.tcl) in design")
    if not sch:
        missing.append("no schematic SPICE (sch.spice) in design")
    if missing:
        return {
            "ok": False,
            "summary": "lvs refused: " + "; ".join(missing),
            "errors": missing,
            "label": SIGNOFF_LABEL,
        }
    xtcl = "\n".join(
        [
            f"tech load {tech[0].name}",
            f"gds read {gds[0].name}",
            f"load {top}",
            "select top cell",
            "extract all",
            "ext2spice lvs",
            "ext2spice layout_ext.spice",
            "quit",
            "",
        ]
    )
    (workdir / "extract_run.tcl").write_text(xtcl)
    x = run(["magic", "-dnull", "-noconsole", "extract_run.tcl"], timeout=1200)
    layout_spi = workdir / "layout_ext.spice"
    if not layout_spi.is_file():
        return {
            "ok": False,
            "summary": "magic extraction produced no netlist",
            "errors": [(x.stderr or x.stdout or "")[-2000:] or "extraction failed"],
            "label": SIGNOFF_LABEL,
        }
    lvs_tcl = "\n".join(
        [
            f"source {setup[0].name}",
            f"readnet spice {sch[0].name} schematic",
            "readnet spice layout_ext.spice layout",
            "lvs schematic layout lvs.rpt -json",
            "quit",
            "",
        ]
    )
    (workdir / "lvs_run.tcl").write_text(lvs_tcl)
    r = run(["netgen", "-batch", "source", "lvs_run.tcl"], timeout=1200)
    rep = (
        (workdir / "lvs.rpt").read_text(errors="replace")
        if (workdir / "lvs.rpt").is_file()
        else (r.stdout or "") + "\n" + (r.stderr or "")
    )
    parsed = gates.parse_lvs(rep)
    ok = r.ok and parsed["equivalent"]
    return {
        "ok": ok,
        "summary": ("LVS match" if ok else "LVS mismatch"),
        "errors": [] if ok else [rep[-2000:] or "lvs mismatch"],
        "note": "schematic side is yosys-derived unless you supplied sch.spice",
        **parsed,
        "label": SIGNOFF_LABEL,
    }


def get_job(design_id: str, job_id: str) -> Verdict:
    """Poll a flow job. Terminal states: completed/failed/cancelled."""
    rec, err = store.get_job_record(design_id, job_id)
    if err:
        return _fail("get_job", err, [err])
    assert rec is not None
    v = _pass("get_job", f"{rec['job_id']}: {rec['status']} - {rec.get('message', '')}")
    v.evidence.update(
        {k: rec.get(k) for k in ("job_id", "kind", "status", "message", "result", "error")}
    )
    return v


def cancel_job(design_id: str, job_id: str) -> Verdict:
    """Cooperatively cancel a flow job: signals the worker and terminates
    the live tool process, then marks the record cancelled."""
    rec, err = store.get_job_record(design_id, job_id)
    if err:
        return _fail("cancel_job", err, [err])
    assert rec is not None
    if rec["status"] in ("completed", "failed", "cancelled"):
        return _pass("cancel_job", f"job already {rec['status']}", job_id=job_id)
    with _JOB_LOCK:
        ev = _JOB_CANCEL.get(job_id)
        proc = _JOB_PROC.get(job_id)
    if ev is not None:
        ev.set()
    killed = False
    if proc is not None:
        try:
            proc.kill()
            killed = True
        except Exception:
            pass
    # If the worker already exited, the record update below still lands;
    # _run_job honors the event and reports cancelled either way.
    store.update_job(design_id, job_id, status="cancelled", message="cancelled by user")
    store.append_ledger({"event": "job_cancelled", "design": design_id, "job": job_id})
    return _pass(
        "cancel_job",
        f"job {job_id} cancelled" + (" (tool process terminated)" if killed else ""),
        job_id=job_id,
    )


# Ported adapter table (their tool_adapters.py, cleaned): name -> commands,
# stages, vendor, openness. Availability is probed live, never hardcoded.
ADAPTERS = (
    {
        "name": "icarus",
        "commands": ["iverilog", "vvp"],
        "stages": ["simulation"],
        "vendor": "Icarus Verilog",
        "openness": "open_source",
    },
    {
        "name": "verilator",
        "commands": ["verilator"],
        "stages": ["simulation", "lint"],
        "vendor": "Verilator",
        "openness": "open_source",
    },
    {
        "name": "yosys",
        "commands": ["yosys"],
        "stages": ["synthesis"],
        "vendor": "YosysHQ",
        "openness": "open_source",
    },
    {
        "name": "sby",
        "commands": ["sby"],
        "stages": ["formal"],
        "vendor": "SymbiYosys",
        "openness": "open_source",
    },
    {
        "name": "openroad",
        "commands": ["openroad"],
        "stages": ["pnr", "sta"],
        "vendor": "OpenROAD",
        "openness": "open_source",
    },
    {
        "name": "opensta",
        "commands": ["opensta"],
        "stages": ["sta", "power"],
        "vendor": "OpenROAD",
        "openness": "open_source",
    },
    {
        "name": "magic",
        "commands": ["magic"],
        "stages": ["physical_verification", "spice"],
        "vendor": "Magic",
        "openness": "open_source",
    },
    {
        "name": "klayout",
        "commands": ["klayout"],
        "stages": ["physical_verification"],
        "vendor": "KLayout",
        "openness": "open_source",
    },
    {
        "name": "netgen",
        "commands": ["netgen"],
        "stages": ["physical_verification"],
        "vendor": "Netgen",
        "openness": "open_source",
    },
    {
        "name": "ngspice",
        "commands": ["ngspice"],
        "stages": ["spice"],
        "vendor": "ngspice",
        "openness": "open_source",
    },
)


def probe_tools() -> Verdict:
    """Which EDA adapters are installed and what stages they cover.
    Probed live every call - never cached, never assumed."""
    status = eda.tool_status()
    rows = []
    for ad in ADAPTERS:
        avail = {c: status.get(c, {}).get("available", False) for c in ad["commands"]}
        rows.append({**ad, "available": avail, "ready": any(avail.values())})
    ready = sum(1 for r in rows if r["ready"])
    v = _pass("probe", f"{ready}/{len(rows)} adapters ready")
    v.evidence["adapters"] = rows
    return v


def prove(design_id: str, timeout_s: int = 300) -> Verdict:
    """Formal proof with SymbiYosys. Needs a .sby task file or SVA
    properties in the RTL. Verdict comes from the solver log."""
    spec, err = store.load_spec(design_id)
    if err:
        return _fail("load", f"design unavailable ({err})", [err])
    if not eda.available("sby"):
        return _fail(
            "formal",
            "sby (SymbiYosys) not installed",
            ["install https://github.com/YosysHQ/oss-cad-suite-build"],
            needed=["sby"],
        )
    workdir = store.design_dir(design_id)
    sby_files = sorted(workdir.glob("*.sby")) + sorted((workdir / "rtl").glob("*.sby"))
    files, verr = _design_files(design_id)
    assert files is not None
    has_props = any(
        re.search(r"assert\s+property|assume\s+property", c or "") for c in files.values()
    )
    if not sby_files and not has_props:
        return _fail(
            "formal",
            "no properties to prove",
            ["add a .sby task file or SVA assert/assume properties, then re-run prove"],
        )
    task_path = sby_files[0].relative_to(workdir) if sby_files else None
    task = task_path.as_posix() if task_path is not None else None
    if task is None:
        return _fail(
            "formal",
            "SVA found but no .sby task - write one naming the top module",
            ["see https://github.com/YosysHQ/sby docs"],
        )
    r = eda.run(["sby", "-f", task], str(workdir), timeout=min(timeout_s, 900))
    log = (r.stdout or "") + "\n" + (r.stderr or "")
    parsed = gates.parse_formal(log)
    if parsed["verdict"] == "PASS":
        v = _pass("formal", f"solver proved {task}")
    else:
        v = _fail(
            "formal",
            f"solver did not prove {task}: {parsed['verdict']}",
            [log[-2000:] or parsed["verdict"]],
        )
    v.evidence.update({"task": task, "log_tail": log[-2000:]})
    store.write_report(design_id, "formal", v.model_dump())
    return v


def estimate_power(design_id: str, freq_mhz: float = 0, vdd: float = 0) -> Verdict:
    """Power: OpenSTA report_power when netlist+lib+sdc exist, else a
    labeled static estimate (formula shown). Never presented as measurement
    unless it came from the tool."""
    spec, err = store.load_spec(design_id)
    if err:
        return _fail("load", f"design unavailable ({err})", [err])
    assert spec is not None
    workdir = store.design_dir(design_id)
    net = workdir / "synth_netlist.v"
    libs = sorted(workdir.glob("*.lib"))
    sdc = workdir / "design.sdc"
    if eda.available("opensta") and net.is_file() and libs and sdc.is_file():
        tcl = "\n".join(
            [
                f"read_liberty {libs[0].name}",
                "read_verilog synth_netlist.v",
                f"link_design {spec.get('module', '')}",
                "read_sdc design.sdc",
                "report_power > power.rpt",
                "exit",
            ]
        )
        (workdir / "run_power.tcl").write_text(tcl)
        eda.run(["opensta", "run_power.tcl"], str(workdir), timeout=300)
        txt = (
            (workdir / "power.rpt").read_text(errors="replace")
            if (workdir / "power.rpt").is_file()
            else ""
        )
        parsed = gates.parse_power(txt)
        if parsed["watts"] is not None:
            v = _pass("power", f"{parsed['watts']:.6f} W (OpenSTA report_power)")
            v.evidence.update({**parsed, "label": SIGNOFF_LABEL})
            store.write_report(design_id, "power", v.model_dump())
            return v
    synth_rep = store.read_report(design_id, "synthesize") or {}
    cells = (synth_rep.get("evidence") or {}).get("cells") if isinstance(synth_rep, dict) else None
    freq = freq_mhz if freq_mhz and freq_mhz > 0 else 50.0
    vdd_v = vdd if vdd and vdd > 0 else gates.vdd_default(spec.get("pdk", ""))
    est = gates.static_power_estimate(cells, freq, vdd_v)
    if est["watts"] is None:
        return _fail(
            "power",
            est.get("note", "no power evidence"),
            ["run synthesize first, or add netlist+lib+sdc for measurement"],
        )
    v = _pass("power", f"~{est['watts']:.6f} W ESTIMATE ({est['formula']})")
    v.evidence.update(
        {**est, "freq_mhz": freq, "vdd": vdd_v, "label": "ESTIMATE - not a measurement"}
    )
    store.write_report(design_id, "power", v.model_dump())
    return v


# ---------------------------------------------------------------- report ---


def get_report(design_id: str) -> Verdict:
    """Every recorded stage verdict + signoff label. The label is always
    OSS_LAYOUT_CANDIDATE - open-source evidence for foundry handoff,
    never a signoff claim."""
    spec, err = store.load_spec(design_id)
    if err:
        return _fail("load", f"design unavailable ({err})", [err])
    assert spec is not None
    stages = {}
    for stage in (
        "contract",
        "check",
        "simulate",
        "synthesize",
        "formal",
        "power",
        "sta",
        "dft",
        "pnr",
        "spice",
        "drc",
        "lvs",
    ):
        rep = store.read_report(design_id, stage)
        if rep is not None:
            stages[stage] = rep
    files = [p.name for p in store.rtl_files(design_id)]
    # A stage that never ran is NOT a pass. Mandatory stages gate the verdict;
    # optional ones (formal/power/spice/backend) count only when recorded.
    mandatory = ("contract", "check", "simulate", "synthesize")
    missing = [s for s in mandatory if s not in stages]
    failing = [s for s, r in stages.items() if not r.get("ok")]
    ok = not missing and not failing
    if ok:
        summary = (
            f"{spec.get('module')} [{spec.get('pdk')}] COMPLETE - {len(stages)} stage(s) verified"
        )
    else:
        summary = f"{spec.get('module')} [{spec.get('pdk')}] INCOMPLETE - " + "; ".join(
            (["not run: " + ", ".join(missing)] if missing else [])
            + (["failing: " + ", ".join(failing)] if failing else [])
        )
    v = Verdict(
        ok=ok,
        stage="report",
        summary=("PASS: " if ok else "FAIL: ") + summary,
        evidence={
            "module": spec.get("module"),
            "pdk": spec.get("pdk"),
            "spec_hash": spec.get("spec_hash"),
            "files": files,
            "stages": stages,
            "missing_stages": missing,
            "label": SIGNOFF_LABEL,
            "handoff": "GDS + netlist + SDC + these reports go to "
            "the foundry flow for final DRC/LVS/STA/EM-IR signoff",
        },
    )
    if not ok:
        v.errors = [f"{s}: failing" for s in failing] + [f"{s}: not run" for s in missing]
    return v
