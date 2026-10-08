"""On-disk state: designs + jobs. No database, no network.

Layout per design (opaque handle `dsg_<hex>`):
    designs/{id}/spec.json        frozen spec + spec_hash
    designs/{id}/rtl/*.v|*.sv     sources added via add_rtl
    designs/{id}/reports/*.json   verdicts per stage
    designs/{id}/jobs/{jid}.json  job records (retention: 7 days)

Handles are opaque bearer strings (uuid4 hex). Jobs/designs older than
JOB_TTL_S are treated as expired: calls against them return an explicit
expired error so the agent recovers by creating a new one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

JOB_TTL_S = 7 * 24 * 3600


def home() -> Path:
    root = Path(os.environ.get("AGENTIC_MCP_HOME", Path.home() / ".agentic-mcp"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def design_dir(design_id: str) -> Path:
    return home() / "designs" / design_id


def _guard_id(value: str, prefix: str) -> str | None:
    """Validate an opaque handle. Returns error text or None."""
    if not value or not isinstance(value, str):
        return "handle required"
    if "/" in value or "\\" in value or ".." in value or len(value) > 64:
        return f"malformed handle: {value[:32]}"
    if not value.startswith(prefix + "_"):
        return f"unknown handle (wrong prefix): {value[:32]}"
    return None


def _write_json(path: Path, obj: dict) -> None:
    """Atomic write (tmp + os.replace) so concurrent readers never see
    a half-written record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=2))
    os.replace(tmp, path)


def load_spec(design_id: str) -> tuple[dict | None, str | None]:
    err = _guard_id(design_id, "dsg")
    if err:
        return None, err
    spec_file = design_dir(design_id) / "spec.json"
    if not spec_file.is_file():
        return None, f"expired or unknown design: {design_id}"
    try:
        return json.loads(spec_file.read_text()), None
    except (OSError, ValueError) as exc:
        return None, f"unreadable design: {exc}"[:200]


def save_spec(design_id: str, spec: dict) -> None:
    d = design_dir(design_id)
    (d / "rtl").mkdir(parents=True, exist_ok=True)
    (d / "reports").mkdir(parents=True, exist_ok=True)
    (d / "jobs").mkdir(parents=True, exist_ok=True)
    _write_json(d / "spec.json", spec)


def rtl_files(design_id: str) -> list[Path]:
    d = design_dir(design_id) / "rtl"
    if not d.is_dir():
        return []
    return sorted(d.glob("*.v")) + sorted(d.glob("*.sv"))


def write_report(design_id: str, stage: str, verdict: dict) -> None:
    rep_dir = design_dir(design_id) / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    verdict = {"stage": stage, "ts": time.time(), **verdict}
    _write_json(rep_dir / f"{stage}.json", verdict)


def read_report(design_id: str, stage: str) -> dict | None:
    f = design_dir(design_id) / "reports" / f"{stage}.json"
    if not f.is_file():
        return None
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def spec_hash(spec: dict) -> str:
    blob = json.dumps(spec, sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


def list_designs() -> list[dict]:
    out = []
    base = home() / "designs"
    if not base.is_dir():
        return out
    for d in sorted(base.iterdir()):
        spec_file = d / "spec.json"
        if not d.is_dir() or not spec_file.is_file():
            continue
        try:
            spec = json.loads(spec_file.read_text())
        except (OSError, ValueError):
            continue
        out.append(
            {
                "design_id": d.name,
                "module": spec.get("module", ""),
                "pdk": spec.get("pdk", ""),
                "spec_hash": spec.get("spec_hash", ""),
            }
        )
    return out


# ---- jobs (Tasks-pattern over plain tools: submit -> poll -> cancel) ----


def new_job(design_id: str, kind: str, argv: list[str]) -> dict:
    job_id = new_id("job")
    rec = {
        "job_id": job_id,
        "design_id": design_id,
        "kind": kind,
        "argv": argv,
        "status": "working",
        "created": time.time(),
        "updated": time.time(),
        "message": "queued",
        "result": None,
        "error": None,
    }
    _write_job(design_id, job_id, rec)
    return rec


def _write_job(design_id: str, job_id: str, rec: dict) -> None:
    if _guard_id(design_id, "dsg") or _guard_id(job_id, "job"):
        logger.warning("refusing job write with malformed handle")
        return
    jdir = design_dir(design_id) / "jobs"
    jdir.mkdir(parents=True, exist_ok=True)
    rec["updated"] = time.time()
    _write_json(jdir / f"{job_id}.json", rec)


def get_job_record(design_id: str, job_id: str) -> tuple[dict | None, str | None]:
    derr = _guard_id(design_id, "dsg")
    if derr:
        return None, derr
    err = _guard_id(job_id, "job")
    if err:
        return None, err
    f = design_dir(design_id) / "jobs" / f"{job_id}.json"
    if not f.is_file():
        return None, f"expired or unknown job: {job_id}"
    try:
        rec = json.loads(f.read_text())
    except (OSError, ValueError) as exc:
        return None, f"unreadable job: {exc}"[:200]
    if time.time() - rec.get("created", 0) > JOB_TTL_S:
        return None, f"expired job (retention 7 days): {job_id}"
    return rec, None


def update_job(design_id: str, job_id: str, **fields) -> None:
    rec, err = get_job_record(design_id, job_id)
    if err or rec is None:
        logger.warning("update_job on missing job %s: %s", job_id, err)
        return
    rec.update(fields)
    _write_job(design_id, job_id, rec)


def append_ledger(event: dict) -> None:
    try:
        led = home() / "ledger.jsonl"
        with open(led, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **event}) + "\n")
    except OSError as exc:
        logger.warning("ledger append failed: %s", exc)
