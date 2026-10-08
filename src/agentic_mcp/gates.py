"""Deterministic chip-design gates. No LLM, no network, no subprocess.

Every function is pure (text in, verdict out) so results are reproducible
anywhere. Verdicts are fail-closed: anything unverifiable FAILS with the
exact missing evidence - never a guessed PASS.
"""

from __future__ import annotations

import re

# ---- spec freeze: a chip without declared clock+reset cannot be checked ----

_CLOCK = re.compile(r"(\d+(?:\.\d+)?)\s*(GHz|MHz|kHz)", re.I)
_KNOWN_RST = re.compile(r"\brst\b|\breset\b|\brst_\w+|\breset_\w+", re.I)
_RST_STYLE = re.compile(r"active\s*-\s*(low|high)|\basync\b|\bsync\b", re.I)


def freeze_spec(
    module: str, clock: str, reset: str, pdk: str, description: str = ""
) -> tuple[dict | None, list[str]]:
    """Validate a frozen spec. Returns (spec, missing[]).

    A clock without a frequency is not a clock (STA/SDC need the period),
    so bare names are rejected - not deferred.
    """
    missing: list[str] = []
    if not (module or "").strip() or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module or ""):
        missing.append("module: valid Verilog top-module name")
    if not (clock or "").strip() or not _CLOCK.search(clock or ""):
        missing.append("clock: name + frequency, e.g. 'clk 100 MHz'")
    if not (reset or "").strip() or not _KNOWN_RST.search(reset):
        missing.append("reset: name + polarity, e.g. 'active-low async rst_n'")
    elif not _RST_STYLE.search(reset or ""):
        missing.append("reset: state polarity + sync/async, e.g. 'active-low async rst_n'")
    if not (pdk or "").strip():
        missing.append("pdk: e.g. 'sky130' or 'gf180mcu'")
    if missing:
        return None, missing
    return {
        "module": module.strip(),
        "clock": clock.strip(),
        "reset": reset.strip(),
        "pdk": pdk.strip(),
        "description": (description or "").strip(),
    }, []


# ---- RTL contract gate: PDK-grounded synthesizability rules ----

_INOUT = re.compile(r"\binout\b")
_BEHAV_MEM = re.compile(r"\breg\s*(\[[^\]]+\])?\s+\w+\s*\[[^\]]+\]\s*;")
_TRISTATE = re.compile(r"1\s*'\s*[bBhH]\s*[zZxX]")
_CLKGATE = re.compile(r"assign\s+\w+\s*=\s*[^;]*\bclk\w*\s*[&|]")
_ASYNC_RST = re.compile(
    r"always\s*@\s*\([^)]*\b(negedge|posedge)\s+(rst\w*|reset\w*|arst\w*)", re.I
)
_SYNC_PAT = re.compile(r"(rst_sync|reset_sync|synchronizer|2ff|two_flop|double_flop)", re.I)
_MODULE = re.compile(r"(?ms)^\s*module\s+([A-Za-z_][A-Za-z0-9_$]*)")


def module_names(content: str) -> list[str]:
    """Top-level module names declared in a Verilog source string."""
    return _MODULE.findall(content or "")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//.*", "", text)


def contract_gate(files: dict[str, str]) -> tuple[bool, list[str], list[str]]:
    """files: {filename: content}. Top-level *pad*/*io* files may use inout.

    Returns (passed, violations, advisories). Async reset without a release
    synchronizer is a violation only across clock domains; in a single domain
    it is standard practice and reported as an advisory.
    """
    hits: list[str] = []
    advisories: list[str] = []
    blob = ""
    for name, content in files.items():
        text = _strip_comments(content or "")
        blob += "\n" + text
        is_pad = bool(re.search(r"pad|_io\b", name, re.I))
        if _INOUT.search(text) and not is_pad:
            hits.append(f"{name}: internal `inout` - only top-level pads may use inout")
        if _BEHAV_MEM.search(text):
            hits.append(f"{name}: behavioral SRAM `reg mem[...]` - use a macro black-box")
        if _TRISTATE.search(text) and not is_pad:
            hits.append(f"{name}: internal tri-state `z` - rewrite as *_i/*_o/*_oe")
        if _CLKGATE.search(text):
            hits.append(f"{name}: gated clock `assign .. clk & ..` - use a clock-gating cell + SDC")
    if _ASYNC_RST.search(blob) and not _SYNC_PAT.search(blob):
        clocks = {m.group(2) for m in _CLK_EDGE.finditer(blob)}
        if len(clocks) > 1:
            hits.append(
                "async reset without synchronizer across clock domains "
                f"{sorted(clocks)} - add an rst_sync release per domain"
            )
        else:
            advisories.append(
                "async reset without release synchronizer - fine for FPGA/single "
                "clock, add rst_sync for ASIC hardening"
            )
    return (not hits), hits, advisories


# ---- CDC: every clock domain crossing must show a synchronizer ----

_CLK_EDGE = re.compile(r"always\s*@\s*\(\s*(posedge|negedge)\s+([A-Za-z_][A-Za-z0-9_$]*)")
_SYNC_MECH = re.compile(
    r"(rst_sync|reset_sync|synchronizer|2ff|two_flop|double_flop|\bfifo\b|\bhandshake\b|\bgray\b)",
    re.I,
)


def cdc_gate(files: dict[str, str]) -> tuple[bool, dict, list[str]]:
    clocks: set[str] = set()
    for content in files.values():
        for m in _CLK_EDGE.finditer(_strip_comments(content or "")):
            clocks.add(m.group(2))
    if len(clocks) <= 1:
        return True, {"domains": sorted(clocks)}, []
    blob = "\n".join(files.values())
    if not _SYNC_MECH.search(blob):
        return (
            False,
            {"domains": sorted(clocks)},
            [
                f"clocks {sorted(clocks)} cross with no synchronizer/fifo/handshake - "
                "add a 2-flop synchronizer"
            ],
        )
    return True, {"domains": sorted(clocks)}, []


# ---- testbench gate: the TB must prove it tests the DUT ----


def tb_gate(files: dict[str, str]) -> tuple[bool, dict, list[str]]:
    tbs = {n: c for n, c in files.items() if n.endswith("_tb.v") or n.endswith("_tb.sv")}
    if not tbs:
        return False, {}, ["missing *_tb.v testbench"]
    dut_names: set[str] = set()
    for name, content in files.items():
        if name not in tbs:
            dut_names.update(_MODULE.findall(content or ""))
    if not dut_names:
        return False, {}, ["no DUT module found"]
    for tb_name, tb_text in tbs.items():
        for dut in dut_names:
            if re.search(
                r"\b" + re.escape(dut) + r"\s*(#\s*\(.*?\))?\s+\w+\s*\(", tb_text or "", re.S
            ):
                checks = ("TEST PASSED" in (tb_text or "")) or ("$finish" in (tb_text or ""))
                if not checks:
                    return (
                        False,
                        {"tb": tb_name, "dut": dut},
                        [
                            f"{tb_name}: self-checking markers missing - add TEST PASSED/FAILED + $finish"
                        ],
                    )
                return True, {"tb": tb_name, "dut": dut}, []
    return False, {"tbs": sorted(tbs)}, ["no DUT instantiation found in *_tb.v"]


# ---- log parsers: tool output -> verdicts (never LLM prose) ----

_WNS = re.compile(r"(?:worst\s+slack|WNS)\s*[:=]\s*(-?\d+(?:\.\d+)?)", re.I)
_VIOLATED = re.compile(r"\bVIOLAT\w*|failed|ERROR", re.I)
_MET = re.compile(r"\bMET\b", re.I)
_DRC_TOTAL = re.compile(
    r"(?:total\s+)?DRC\s+(?:errors?\s+found|clean|violations?)\s*:?\s*(\d+|clean|0)", re.I
)
_LINT_ERR = re.compile(r"%Error(?:-[A-Z0-9_]+)?\s*:\s*([^:]+):(\d+)", re.I)
_CELL_COUNT = re.compile(r"number of (?:cells|wires|cell):\s*(\d+)", re.I)
_DFF_LINE = re.compile(r"\$_DFF\S*\s+(\d+)")


def parse_synth(text: str) -> dict:
    m = _CELL_COUNT.search(text or "")
    cells = int(m.group(1)) if m else None
    dffs = sum(int(n) for n in _DFF_LINE.findall(text or "")) or None
    return {
        "kind": "synthesis",
        "cells": cells,
        "dffs": dffs,
        "ok": cells is not None and cells > 0,
    }


# ---- duplicate modules: the same top redefined in two files (LLM slop) ----


def duplicate_modules(files: dict[str, str]) -> dict[str, list[str]]:
    owners: dict[str, list[str]] = {}
    for name, content in files.items():
        for mod in set(_MODULE.findall(content or "")):
            owners.setdefault(mod, []).append(name)
    return {m: sorted(fs) for m, fs in owners.items() if len(fs) > 1}


# ---- STA depth: critical paths + max frequency from WNS ----

_STARTPOINT = re.compile(r"Startpoint:\s*(.+)", re.I)
_ENDPOINT = re.compile(r"Endpoint:\s*(.+)", re.I)
_PATH_SLACK = re.compile(r"slack\s*(?:\(.+?\))?\s*(-?\d+(?:\.\d+)?)", re.I)


def critical_paths(text: str, limit: int = 3) -> list[dict]:
    paths: list[dict] = []
    cur: dict = {}
    for line in (text or "").splitlines():
        sm = _STARTPOINT.search(line)
        if sm:
            if cur:
                paths.append(cur)
            cur = {"startpoint": sm.group(1).strip()}
            continue
        em = _ENDPOINT.search(line)
        if em and cur and "endpoint" not in cur:
            cur["endpoint"] = em.group(1).strip()
            continue
        pm = _PATH_SLACK.search(line)
        if pm and cur and "slack_ns" not in cur:
            try:
                cur["slack_ns"] = float(pm.group(1))
            except ValueError:
                pass
    if cur:
        paths.append(cur)
    paths = [p for p in paths if "slack_ns" in p]
    paths.sort(key=lambda p: p["slack_ns"])
    return paths[:limit]


def fmax_mhz(period_ns: float | None, wns_ns: float | None) -> float | None:
    """Achievable frequency from the period constraint and worst slack."""
    if not period_ns or period_ns <= 0 or wns_ns is None:
        return None
    actual = period_ns - wns_ns
    return round(1000.0 / actual, 3) if actual > 0 else None


def spec_period_ns(spec: dict) -> float | None:
    m = _FREQ.search((spec or {}).get("clock", "") or "")
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).lower()
    return {"ghz": 1.0 / val, "mhz": 1000.0 / val, "khz": 1e6 / val}[unit]


# ---- Magic DRC + Netgen LVS output parsers (their physical.ts, cleaned) ----

_DRC_RULE_COORD = re.compile(
    r"^\s*(?:Rule\s+(?:violated:\s*)?)?(\S+?)[\s:]+.*?\(\s*([-\d.]+)\s*um?\s*[,;\s]\s*([-\d.]+)\s*um?\s*\)(.*)$"
)
_DRC_LAYER = re.compile(r"^([a-zA-Z][a-zA-Z0-9_]*)")


def parse_drc_violations(text: str) -> list[dict]:
    """Rule/layer/coordinate violations from a Magic DRC report."""
    out: list[dict] = []
    for line in (text or "").splitlines():
        m = _DRC_RULE_COORD.match(line)
        if m:
            layer = _DRC_LAYER.match(m.group(1))
            try:
                x, y = float(m.group(2)), float(m.group(3))
            except ValueError:
                x, y = 0.0, 0.0
            out.append(
                {
                    "rule": m.group(1),
                    "layer": layer.group(1) if layer else "unknown",
                    "x_um": x,
                    "y_um": y,
                    "message": (m.group(4) or "").strip() or m.group(1),
                }
            )
    if not out:
        for line in (text or "").splitlines():
            m = re.match(r"^\s*(\S+)\s*:\s*(.+?)\s*$", line)
            if m and re.search(r"violation|error|spacing|width", line, re.I):
                out.append(
                    {
                        "rule": m.group(1),
                        "layer": m.group(1).split(".")[0] or "unknown",
                        "x_um": 0.0,
                        "y_um": 0.0,
                        "message": m.group(2).strip(),
                    }
                )
    return out[:50]


def parse_lvs(text: str) -> dict:
    """Netgen LVS verdict: equivalent only on unique match + zero mismatches."""
    equivalent = bool(re.search(r"(circuits|netlists)\s+match\s+uniquely", text or "", re.I))
    net_m = re.search(r"Net\s+mismatches?\s*[:=]\s*(\d+)", text or "", re.I)
    pin_m = re.search(r"Pin\s+mismatches?\s*[:=]\s*(\d+)", text or "", re.I)
    net_mm = int(net_m.group(1)) if net_m else 0
    pin_mm = int(pin_m.group(1)) if pin_m else 0
    if net_mm > 0 or pin_mm > 0:
        equivalent = False
    return {
        "kind": "lvs",
        "equivalent": equivalent,
        "net_mismatches": net_mm,
        "pin_mismatches": pin_mm,
    }


def parse_sta(text: str) -> dict:
    wns_m = _WNS.search(text or "")
    wns = float(wns_m.group(1)) if wns_m else None
    violated = bool(_VIOLATED.search(text or ""))
    met = (wns is not None and wns >= 0 and not violated) or (
        wns is None and bool(_MET.search(text or "")) and not violated
    )
    return {"kind": "sta", "wns_ns": wns, "met": met, "violations": 0 if met else 1}


def parse_drc(text: str) -> dict:
    m = _DRC_TOTAL.search(text or "")
    if not m:
        count = len(re.findall(r"violation", text or "", re.I))
        return {"kind": "drc", "violation_count": count, "clean": count == 0}
    raw = m.group(1).lower()
    count = 0 if raw in ("clean", "0") else int(raw)
    return {"kind": "drc", "violation_count": count, "clean": count == 0}


def parse_lint(text: str) -> dict:
    diags = [
        {"file": f.strip(), "line": int(ln), "message": (text or "").splitlines()[0][:200]}
        for f, ln in _LINT_ERR.findall(text or "")
    ]
    warns = len(re.findall(r"%Warning", text or ""))
    return {
        "kind": "lint",
        "errors": len(diags),
        "warnings": warns,
        "clean": not diags,
        "diagnostics": diags[:20],
    }


# ---- SDC from frozen spec: clock intent becomes constraints ----

_FREQ = re.compile(r"(\d+(?:\.\d+)?)\s*(GHz|MHz|kHz)", re.I)
_CLK_NAME = re.compile(r"^(.*?)\s*\d+(?:\.\d+)?\s*(?:GHz|MHz|kHz)\s*$", re.I)


def sdc_from_spec(spec: dict) -> tuple[str | None, list[str]]:
    """Generate starter SDC from the frozen clock. Returns (sdc, missing[])."""
    clock = (spec or {}).get("clock", "") or ""
    m = _FREQ.search(clock)
    if not m:
        return None, ["spec clock has no frequency - cannot write SDC"]
    val, unit = float(m.group(1)), m.group(2).lower()
    period_ns = {"ghz": 1.0 / val, "mhz": 1000.0 / val, "khz": 1e6 / val}[unit]
    nm = _CLK_NAME.match(clock.strip())
    clk = (nm.group(1).strip() if nm else "clk") or "clk"
    clk = re.sub(r"\W+", "", clk) or "clk"
    sdc = "\n".join(
        [
            "# generated from frozen spec (starter constraints - review before signoff)",
            f"create_clock -name {clk} -period {period_ns:.3f} [get_ports {clk}]",
            f"set_clock_uncertainty 0.100 [get_clocks {clk}]",
            f"set_input_delay  {period_ns * 0.2:.3f} -clock [get_clocks {clk}] [all_inputs]",
            f"set_output_delay {period_ns * 0.2:.3f} -clock [get_clocks {clk}] [all_outputs]",
            "",
        ]
    )
    return sdc, []


# ---- formal (SymbiYosys): PASS/FAIL comes from the solver log ----

_SBY_PASS = re.compile(r"\b(SMT\s+)?(PASS|proved|QED)\b", re.I)
_SBY_FAIL = re.compile(r"\b(FAIL|CEGAR|disproved|error)\b", re.I)


def parse_formal(text: str) -> dict:
    log = text or ""
    if _SBY_FAIL.search(log):
        return {"kind": "formal", "proven": False, "verdict": "FAIL"}
    if _SBY_PASS.search(log):
        return {"kind": "formal", "proven": True, "verdict": "PASS"}
    return {"kind": "formal", "proven": False, "verdict": "UNKNOWN"}


# ---- power: OpenSTA report_power numbers, else labeled static estimate ----

_POWER_TOTAL = re.compile(
    r"total\s+(?:power\s+)?([-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)\s*(uW|mW|W|nW)?", re.I
)
_VDD_DEFAULT = {"sky130": 1.8, "gf180mcu": 5.0}


def parse_power(text: str) -> dict:
    m = _POWER_TOTAL.search(text or "")
    if not m:
        return {"kind": "power", "watts": None, "from_tool": False}
    val, unit = float(m.group(1)), (m.group(2) or "W")
    scale = {"nw": 1e-9, "uw": 1e-6, "mw": 1e-3, "w": 1.0}[unit.lower()]
    return {"kind": "power", "watts": val * scale, "from_tool": True}


def static_power_estimate(cells: int | None, freq_mhz: float, vdd: float) -> dict:
    """Labeled estimate only: P = cells * C_avg * V^2 * f. Never a measurement."""
    if not cells:
        return {
            "kind": "power",
            "watts": None,
            "from_tool": False,
            "note": "no cell count - run synthesize first",
        }
    c_avg = 2e-15  # F per cell, documented assumption
    watts = cells * c_avg * vdd * vdd * freq_mhz * 1e6
    return {
        "kind": "power",
        "watts": watts,
        "from_tool": False,
        "formula": "cells*Cavg*V^2*f",
        "assumptions": {"c_avg_f": c_avg},
    }


def vdd_default(pdk: str) -> float:
    return _VDD_DEFAULT.get((pdk or "").lower(), 1.8)


# ---- DFT testability: deterministic structural analysis (their --testability) ----

_FLOP = re.compile(r"always\s*@\s*\(\s*(posedge|negedge)\s+\w+", re.I)


def testability(files: dict[str, str]) -> dict:
    flops = sum(len(_FLOP.findall(c or "")) for c in files.values())
    mems = sum(len(_BEHAV_MEM.findall(re.sub(r"//.*", "", c or ""))) for c in files.values())
    gated = sum(1 for c in files.values() if _CLKGATE.search(c or ""))
    risks = []
    if gated:
        risks.append(f"{gated} file(s) with gated clocks - not scan-replaceable as-is")
    if mems:
        risks.append(f"{mems} behavioral memor(ies) - need MBIST/BISR, not scan")
    return {
        "kind": "dft",
        "scan_flop_candidates": flops,
        "behavioral_memories": mems,
        "risks": risks,
        "scan_insertion": "EXPERIMENTAL - needs Fault+docker; not attempted",
    }
