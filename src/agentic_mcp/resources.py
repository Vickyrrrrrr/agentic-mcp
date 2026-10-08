"""Resources: file-like reads. Agents pull design state without tool calls."""

from __future__ import annotations

import json


def register(mcp) -> None:
    @mcp.resource(
        "design://{design_id}/spec.json",
        title="Frozen chip spec",
        mime_type="application/json",
        description="The frozen spec for a design: module, clock, reset, pdk, spec_hash.",
    )
    def spec_resource(design_id: str) -> str:
        from agentic_mcp import store

        spec, err = store.load_spec(design_id)
        if err:
            raise ValueError(err)
        assert spec is not None
        return json.dumps(spec, indent=2)

    @mcp.resource(
        "design://{design_id}/files",
        title="Design file list",
        mime_type="application/json",
        description="RTL files present in the design workspace.",
    )
    def files_resource(design_id: str) -> str:
        from agentic_mcp import store

        spec, err = store.load_spec(design_id)
        if err:
            raise ValueError(err)
        return json.dumps(
            {"design_id": design_id, "files": [p.name for p in store.rtl_files(design_id)]},
            indent=2,
        )

    @mcp.resource(
        "design://{design_id}/report.json",
        title="Stage verdicts",
        mime_type="application/json",
        description="Every recorded stage verdict plus the signoff label.",
    )
    def report_resource(design_id: str) -> str:
        from agentic_mcp.tools import get_report

        verdict = get_report(design_id)
        return verdict.model_dump_json(indent=2)
