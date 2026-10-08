"""Pure gate tests. No EDA tools, no network, no server. Run anywhere."""

import pytest

from agentic_mcp import gates

GOOD = """module counter (
  input  wire clk,
  input  wire rst_n,
  output reg [7:0] count
);
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) count <= 8'd0;
    else count <= count + 8'd1;
  end
endmodule
"""

GOOD_TB = """module counter_tb;
  reg clk; reg rst_n; wire [7:0] count;
  counter dut (.clk(clk), .rst_n(rst_n), .count(count));
  initial begin clk = 0; forever #5 clk = ~clk; end
  initial begin
    rst_n = 0; #20; rst_n = 1; #200;
    if (count == 8'd20) $display("TEST PASSED");
    else $display("TEST FAILED");
    $finish;
  end
endmodule
"""


def test_spec_freeze_ok():
    spec, missing = gates.freeze_spec(
        "counter", "clk 100 MHz", "active-low async rst_n", "sky130", "counter"
    )
    assert spec and not missing


def test_spec_freeze_lists_everything_missing():
    spec, missing = gates.freeze_spec("", "", "", "")
    assert spec is None
    blob = " ".join(missing)
    assert "module" in blob and "clock" in blob and "reset" in blob and "pdk" in blob


def test_spec_freeze_rejects_vague_reset():
    _, missing = gates.freeze_spec("c", "clk 50 MHz", "reset", "sky130")
    assert any("reset" in m for m in missing)


def test_spec_freeze_rejects_frequency_less_clock():
    spec, missing = gates.freeze_spec("c", "clk", "active-low async rst_n", "sky130")
    assert spec is None and any("clock" in m for m in missing)


def test_contract_clean():
    passed, hits, advisories = gates.contract_gate({"counter.v": GOOD})
    assert passed and hits == []


def test_contract_each_rule():
    bad_inout = "module m (inout wire x); endmodule"
    bad_mem = "module m (input clk); reg [7:0] mem [0:255]; endmodule"
    bad_tri = "module m (output y); assign y = en ? d : 1'bz; endmodule"
    bad_clk = "module m (input clk, input g); wire gc; assign gc = clk & g; endmodule"
    bad_rst = (
        "module m (input clk, input clk2, input rst_n, output reg q); "
        "always @(posedge clk or negedge rst_n) if (!rst_n) q <= 0; else q <= 1; "
        "always @(posedge clk2) q <= 1; endmodule"
    )
    for name, src in (
        ("a.v", bad_inout),
        ("b.v", bad_mem),
        ("c.v", bad_tri),
        ("d.v", bad_clk),
        ("e.v", bad_rst),
    ):
        passed, hits, _ = gates.contract_gate({name: src})
        assert not passed and hits, name
    # pads are exempt from inout
    passed, _, _ = gates.contract_gate({"pads.v": bad_inout})
    assert passed
    # single-clock async reset is advisory, not a violation
    passed, hits, advisories = gates.contract_gate({"counter.v": GOOD})
    assert passed and advisories


def test_cdc_single_and_multi():
    ok, ev, _ = gates.cdc_gate({"a.v": GOOD})
    assert ok and ev["domains"] == ["clk"]
    two = GOOD + "\nmodule b (input clk2, output reg q); always @(posedge clk2) q <= 1; endmodule"
    ok, ev, err = gates.cdc_gate({"a.v": two})
    assert not ok and err and len(ev["domains"]) == 2
    ok, _, _ = gates.cdc_gate(
        {"a.v": two + "\n// cdc via 2ff synchronizer\nmodule s (input a); endmodule"}
    )
    assert ok


def test_tb_gate():
    ok, _, err = gates.tb_gate({"counter.v": GOOD})
    assert not ok and "testbench" in err[0]
    ok, _, err = gates.tb_gate({"counter.v": GOOD, "x_tb.v": "module x_tb; endmodule"})
    assert not ok
    ok, ev, _ = gates.tb_gate({"counter.v": GOOD, "counter_tb.v": GOOD_TB})
    assert ok and ev["dut"] == "counter"


def test_parsers():
    sta_bad = gates.parse_sta("Worst slack: -0.420\nslack (VIOLATED) -0.420")
    assert sta_bad["wns_ns"] == -0.42 and not sta_bad["met"]
    sta_good = gates.parse_sta("Worst slack: 0.150\nslack (MET) 0.150")
    assert sta_good["met"] and sta_good["wns_ns"] == 0.15
    assert gates.parse_drc("Total DRC errors found: 3")["violation_count"] == 3
    assert gates.parse_drc("DRC clean")["clean"]
    lint = gates.parse_lint("%Error-WIDTH: rtl/u.v:10:5: width mismatch")
    assert not lint["clean"] and lint["diagnostics"][0]["line"] == 10
    assert gates.parse_lint("all good")["clean"]
    assert gates.parse_synth("Number of cells: 42")["cells"] == 42


def test_eda_funnel_refuses():
    import tempfile

    from agentic_mcp import eda

    with tempfile.TemporaryDirectory() as tmp:
        r = eda.run(["rm", "-rf", tmp], tmp)
        assert not r.ok and r.refused
        r = eda.run(["yosys", "--version"], tmp)
        # either present (ok) or honestly missing - never refused/crash
        assert r.ok or r.missing_tool == "yosys"


def test_sdc_from_frozen_clock():
    sdc, missing = gates.sdc_from_spec({"clock": "clk 100 MHz"})
    assert sdc and not missing
    assert "create_clock -name clk -period 10.000" in sdc
    sdc, missing = gates.sdc_from_spec({"clock": "vague"})
    assert sdc is None and missing


def test_formal_parser():
    assert gates.parse_formal("sby: PASS reached")["proven"] is True
    assert gates.parse_formal("sby: FAIL cex found")["proven"] is False
    assert gates.parse_formal("garbage")["verdict"] == "UNKNOWN"


def test_power_paths():
    assert gates.parse_power("total power 1.24 mW")["watts"] == pytest.approx(0.00124)
    est = gates.static_power_estimate(1000, 50.0, 1.8)
    assert est["watts"] is not None and not est["from_tool"]
    assert gates.static_power_estimate(None, 50.0, 1.8)["watts"] is None
    assert gates.vdd_default("sky130") == 1.8


def test_testability():
    rep = gates.testability({"a.v": GOOD})
    assert rep["scan_flop_candidates"] >= 1 and rep["risks"] == []
    gated = "module m (input clk, input g); wire gc; assign gc = clk & g; endmodule"
    rep = gates.testability({"g.v": gated})
    assert rep["risks"]


def test_module_names():
    assert gates.module_names(GOOD) == ["counter"]
    assert gates.module_names("no modules here") == []


def test_duplicate_modules():
    dups = gates.duplicate_modules({"a.v": GOOD, "b.v": GOOD})
    assert dups == {"counter": ["a.v", "b.v"]}
    assert gates.duplicate_modules({"a.v": GOOD}) == {}


def test_critical_paths_and_fmax():
    log = (
        "Startpoint: u_core/reg_a\nEndpoint: u_core/reg_b\n"
        "slack (VIOLATED) -0.420\n"
        "Startpoint: u_core/reg_c\nEndpoint: u_core/reg_d\nslack (MET) 0.150"
    )
    paths = gates.critical_paths(log)
    assert len(paths) == 2 and paths[0]["slack_ns"] == -0.42
    assert paths[0]["startpoint"] == "u_core/reg_a"
    assert gates.fmax_mhz(10.0, -0.42) == pytest.approx(95.923, rel=1e-3)
    assert gates.fmax_mhz(10.0, 0.15) == pytest.approx(101.523, rel=1e-3)
    assert gates.fmax_mhz(None, 0.1) is None
    assert gates.spec_period_ns({"clock": "clk 100 MHz"}) == 10.0
    assert gates.spec_period_ns({"clock": "vague"}) is None


def test_drc_lvs_parsers():
    viols = gates.parse_drc_violations("met1.spacing:(12.340um,56.780um) too close")
    assert viols and viols[0]["x_um"] == pytest.approx(12.34)
    assert gates.parse_drc_violations("all clean\n") == []
    lvs = gates.parse_lvs("Circuits match uniquely.\nNet mismatches: 0\nPin mismatches: 0")
    assert lvs["equivalent"] is True
    lvs = gates.parse_lvs("Net mismatches: 3\nPin mismatches: 1")
    assert lvs["equivalent"] is False and lvs["net_mismatches"] == 3


def test_parse_synth_dffs():
    assert gates.parse_synth("Number of cells: 42\n$_DFF_P_ 10\n$_DFF_N_ 4")["dffs"] == 14
