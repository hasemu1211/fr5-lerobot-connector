"""Read-only projection of immutable run events."""
from pathlib import Path
from ..core.jsonio import CuratorError, load_json

EVENTS = ("request.json", "candidate_materialization.json", "candidate_ready.json", "review_ready.json", "decision.json", "receipt.json", "failure.json")

def project_state(run_dir: str | Path) -> dict[str, object]:
    run = Path(run_dir)
    if run.is_symlink() or not run.is_dir(): raise CuratorError("RUN_NOT_FOUND", str(run))
    events = {name: load_json(run / name, code="RUN_EVENT") for name in EVENTS if (run / name).is_file()}
    if "failure.json" in events: status = "FAILED"
    elif "receipt.json" in events and events["receipt.json"].get("publication", {}).get("state") == "COMMITTED_DURABLE": status = "PUBLISHED"
    elif "decision.json" in events: status = events["decision.json"].get("decision", "DECIDED")
    elif "review_ready.json" in events: status = "REVIEW_READY"
    elif "candidate_ready.json" in events: status = "CANDIDATE_READY"
    else: status = "PREPARING"
    return {"ok": True, "run_id": run.name, "status": status, "events": sorted(events)}

__all__ = ["project_state"]
