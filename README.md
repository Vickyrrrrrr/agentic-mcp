# agentic-mcp

An MCP server for chip design. Any MCP-capable agent (Claude Code, Cursor,
Windsurf, OpenCode, Copilot, …) connects over stdio and gets everything it
needs to take a chip from a one-line spec to a verified layout candidate:
deterministic gates, real EDA tools, job tracking, reports, and prompts.

No app. No IDE. No accounts, license server, or cloud. One server, one
protocol, local tools.

## What the agent gets

**12 tools** — `create_design`, `add_rtl`, `check_design`, `simulate`,
`synthesize`, `probe_tools`, `prove`, `estimate_power`, `run_flow`,
`get_job`, `cancel_job`, `get_report`

**3 resources** — `design://{id}/spec.json`, `design://{id}/files`,
`design://{id}/report.json`

**3 prompts** — `spec-freeze`, `debug-sim-fail`, `close-timing`

Every verdict comes from a deterministic gate or a real tool log parser.
A missing tool is reported as missing — never faked. Finished output is
labeled `OSS_LAYOUT_CANDIDATE`: open-source evidence ready to hand off to a
foundry flow for final signoff, never claimed as signoff itself.

## Requirements

- Python 3.10+
- [`uv`](https://docs.astral.sh/uv/) (recommended)
- Optional EDA tools, probed automatically: Yosys, Icarus Verilog, Verilator,
  SymbiYosys, OpenSTA, OpenROAD, Magic, KLayout, Netgen. Lint, contract, CDC,
  and testbench gates need no tools at all.

## Run

```bash
uv run agentic-mcp                  # stdio (for MCP clients)
uv run agentic-mcp --check          # self-test: gates only, no tools needed
uv run agentic-mcp --transport streamable-http --port 8000   # team/remote deploy
```

Inspect and debug with the official Inspector:

```bash
uv run mcp dev src/agentic_mcp/server.py
```

## Security model (local-first)

- **No auth, by design**: the stdio server runs as your OS user; the OS
  account is the identity and filesystem permissions are the boundary
  (per the MCP local-server security guide). Do not expose stdio over a
  network. Use Streamable HTTP + your own auth (reverse proxy / VPN) for
  team deploys, bound to loopback by default.
- **Allowlisted execution only**: 14 EDA binaries, argv-only (never shell),
  cwd jailed to the design directory, timeouts + output caps, cooperative
  cancellation that really kills the process.
- **Handles**: opaque `dsg_*`/`job_*` IDs, path-traversal rejected. Designs
  never expire; jobs expire after 7 days with explicit expired-handle errors.
- **Secrets**: the server never asks for API keys or credentials (no
  elicitation, no sampling — it runs zero LLM calls). EDA license env vars
  are only *reported present/absent*, never read or transmitted.
- **Destructive tools** (`run_flow`, `cancel_job`) carry
  `destructive_hint` annotations so clients prompt before running them.

## Connect any agent

```json
{
  "mcpServers": {
    "agentic-mcp": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/agentic-mcp", "run", "agentic-mcp"]
    }
  }
}
```

Windows: use `"C:\\ABSOLUTE\\PATH\\TO\\agentic-mcp"` and the full path to
`uv` if it is not on `PATH` (`where uv`).

## One-prompt chip flow (what the agent does)

1. `create_design` with module, clock + frequency, reset + polarity, PDK.
   Missing clock/reset → explicit error listing what's missing.
2. `add_rtl` per file — contract gate runs on every write, fail fast
   (rejected files are quarantined, never stored).
3. `check_design` — contract + CDC + lint-content + testbench gates.
4. `simulate` (iverilog+vvp), `synthesize` (yosys, writes `design.sdc`
   from the frozen clock), `prove` (SymbiYosys), `estimate_power`
   (OpenSTA `report_power` when netlist+lib+sdc exist, else labeled
   static estimate) — real tools when installed, honest
   `tool-not-installed` verdicts otherwise.
5. `run_flow` — long jobs (`sta` multi-corner, `dft` testability,
   `pnr`, `drc`, `lvs`, scoped `spice`) return a `job_*` handle
   immediately; `get_job` polls, `cancel_job` cancels. Jobs expire after
   7 days; expired handles return an explicit error.
6. `get_report` — every stage verdict + `OSS_LAYOUT_CANDIDATE` label.
7. `probe_tools` — live EDA adapter inventory (what's installed, what
   stages each covers).

## Design rules enforced (fail-closed)

- No internal `inout` or tri-state (top-level pads only)
- No behavioral SRAM (`reg mem[...]` — use a macro black-box)
- No gated clocks (`assign x = clk & …`)
- No async reset without a 2-flop synchronizer
- Multi-clock designs must show a synchronizer/FIFO/handshake
- Testbench must instantiate the DUT and be self-checking
- Timing met only when WNS ≥ 0 on all reported corners

## License

MIT
