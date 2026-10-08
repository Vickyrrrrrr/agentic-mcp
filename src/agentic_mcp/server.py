"""agentic-mcp server: stdio-first, one process per client.

Launch: `uv run agentic-mcp` (any MCP client), `agentic-mcp --check` (self-test).
Logging goes to stderr only - stdout is the protocol (spec: stdio transport).
"""

from __future__ import annotations

import logging
import sys

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

logger = logging.getLogger("agentic-mcp")

READONLY = ToolAnnotations(read_only_hint=True)
DESTRUCTIVE = ToolAnnotations(destructive_hint=True)


def build_server() -> MCPServer:
    mcp = MCPServer(
        "agentic-mcp",
        title="Agentic chip design",
        description=(
            "Take a chip from spec to verified layout candidate. "
            "Deterministic gates + real EDA tools. Output is an "
            "open-source layout candidate for foundry handoff, never signoff."
        ),
        instructions=(
            "Start with the spec-freeze prompt, then create_design. "
            "Write RTL with add_rtl, verify with check_design, then "
            "simulate/synthesize/run_flow. Long flows return job handles; "
            "poll get_job. Finish with get_report."
        ),
        version="0.1.0",
    )

    from agentic_mcp import prompts, resources, tools

    mcp.tool(title="Freeze chip spec", structured_output=True)(tools.create_design)
    mcp.tool(title="Add RTL file", structured_output=True)(tools.add_rtl)
    mcp.tool(title="Run design gates", annotations=READONLY, structured_output=True)(
        tools.check_design
    )
    mcp.tool(title="Simulate testbench", annotations=READONLY, structured_output=True)(
        tools.simulate
    )
    mcp.tool(title="Synthesize with Yosys", annotations=READONLY, structured_output=True)(
        tools.synthesize
    )
    mcp.tool(title="Probe EDA tools", annotations=READONLY, structured_output=True)(
        tools.probe_tools
    )
    mcp.tool(title="Formal proof (SymbiYosys)", annotations=READONLY, structured_output=True)(
        tools.prove
    )
    mcp.tool(title="Estimate power", annotations=READONLY, structured_output=True)(
        tools.estimate_power
    )
    mcp.tool(title="Run long flow", annotations=DESTRUCTIVE, structured_output=True)(tools.run_flow)
    mcp.tool(title="Poll flow job", annotations=READONLY, structured_output=True)(tools.get_job)
    mcp.tool(title="Cancel flow job", annotations=DESTRUCTIVE, structured_output=True)(
        tools.cancel_job
    )
    mcp.tool(title="Full stage report", annotations=READONLY, structured_output=True)(
        tools.get_report
    )

    resources.register(mcp)
    prompts.register(mcp)
    return mcp


def main() -> None:
    """Entry point: stdio by default; --transport streamable-http --port N
    to serve a team/remote deployment (bind loopback unless exposed)."""
    import argparse

    parser = argparse.ArgumentParser(prog="agentic-mcp")
    parser.add_argument(
        "--check", action="store_true", help="self-test (gates + tool probe) and exit"
    )
    parser.add_argument("--transport", default="stdio", choices=["stdio", "streamable-http", "sse"])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    if args.check:
        from agentic_mcp import eda, gates

        probes = [gates.freeze_spec("counter", "clk 100 MHz", "active-low async rst_n", "sky130")]
        print(f"spec gate: {'ok' if probes[0][0] else probes[0][1]}")
        tools = eda.tool_status()
        missing = [t for t, s in tools.items() if not s["available"]]
        print(
            f"eda tools present: {len(tools) - len(missing)}/{len(tools)}"
            + (f" (missing: {', '.join(missing)})" if missing else "")
        )
        print("self-test ok")
        return
    server = build_server()
    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
