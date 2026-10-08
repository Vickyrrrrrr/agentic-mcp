# Changelog

## 0.1.0

- 12 MCP tools (spec freeze, RTL add + contract gate, gates, simulate,
  synthesize + SDC, probe, formal, power, flows, jobs, report) with
  JSON-Schema input and structured output on every tool
- 3 resources (`design://{id}/spec.json`, `/files`, `/report.json`),
  3 prompts (spec-freeze, debug-sim-fail, close-timing)
- Long flows as immediate job handles (poll/cancel, 7-day retention)
- Fail-closed honesty: missing tools/collateral are verdicts, never fakes;
  output labeled `OSS_LAYOUT_CANDIDATE`
- Transports: stdio (default) + streamable-http; `--check` self-test
