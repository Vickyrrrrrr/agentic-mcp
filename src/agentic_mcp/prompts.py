"""Prompts: versioned agent workflows served by the server."""


def register(mcp) -> None:
    @mcp.prompt(
        name="spec-freeze",
        title="Freeze a chip spec",
        description="Required spec fields before any RTL is written.",
    )
    def spec_freeze() -> str:
        return (
            "Freeze the chip spec BEFORE writing RTL. Call create_design with:\n"
            "1. module: valid Verilog top-module name\n"
            "2. clock: name + frequency (e.g. 'clk 100 MHz')\n"
            "3. reset: name + polarity + sync/async (e.g. 'active-low async rst_n'); "
            "async reset needs an rst_sync 2-flop or switch to sync reset\n"
            "4. pdk: e.g. 'sky130'. If a cell/macro is not in the PDK, STOP - never invent it\n"
            "5. description: what the chip does\n"
            "If create_design reports missing items, ask the user for exactly those. "
            "No RTL until the spec is frozen."
        )

    @mcp.prompt(
        name="debug-sim-fail",
        title="Debug a failing simulation",
        description="RTL-bug vs TB-bug triage from evidence only.",
    )
    def debug_sim_fail() -> str:
        return (
            "A simulation failed. Triage from evidence only:\n"
            "1. Read the TB: does it instantiate the DUT by exact module name, drive "
            "clock/reset, and contain a checker (TEST PASSED/FAILED + $finish)? "
            "If not -> TB bug: patch the testbench.\n"
            "2. Else trace the first mismatched cycle to its driving register -> RTL bug: "
            "minimal patch.\n"
            "3. If check_design reports contract violations, say so first - no sim patch "
            "fixes a contract violation.\n"
            "Never claim PASS without a TEST PASSED verdict from simulate."
        )

    @mcp.prompt(
        name="close-timing",
        title="Close timing",
        description="Fix STA violations in constraint -> RTL -> floorplan order.",
    )
    def close_timing() -> str:
        return (
            "Timing is violated. Fix in this order:\n"
            "1. Constraints: missing create_clock, wrong period, missing input/output "
            "delays, false paths that are real.\n"
            "2. RTL: pipeline the critical stage, retime, cut logic depth, buffer "
            "high-fanout nets.\n"
            "3. Floorplan: move critical macros closer, check congestion.\n"
            "Claim timing-met only when parse shows WNS >= 0 on every corner with zero "
            "unconstrained endpoints. Label stays OSS_LAYOUT_CANDIDATE."
        )
