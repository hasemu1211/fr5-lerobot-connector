"""Export explicit Collection episode selections to the native training request.

This selects existing human-reviewed evidence, never issues approval or creates
a candidate. The training consumer revalidates current bytes before approval.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from tools.data_factory.episode_ledger import validate_episode_state
from tools.data_factory.training_entrypoint import prepare_approvals
from tools.data_factory.training_split import selected_train_eval
from tools.fr5_data_factory import ContractError, load_json_strict
from tools.fr5_training_profile import read_metadata

from ..core.errors import CuratorError
from ..core.filesystem import reject_symlink_components, write_json_exclusive


def check_evaluation_cohort(
    request: dict, *, eval_split: float, expected_eval_episodes: Sequence[int],
) -> dict:
    """Check an explicit comparison cohort against the existing native splitter.

    This is a preview, not a launch split or admission artifact. Selection never
    changes metadata or silently moves examples to satisfy the expected cohort.
    """
    expected = list(expected_eval_episodes)
    if (not expected or any(type(index) is not int or index < 0 for index in expected)
            or expected != sorted(set(expected))):
        raise CuratorError("SELECTION_EVALUATION_COHORT")
    metadata = read_metadata(Path(request["dataset_root"]))
    selected = [episode["episode_index"] for episode in request["episodes"]]
    train, heldout = selected_train_eval(metadata["episode_tasks"], selected, eval_split)
    if sorted(heldout) != expected:
        raise CuratorError("SELECTION_EVALUATION_CHANGED", f"expected {expected}; native {sorted(heldout)}")
    return {"eval_split": eval_split, "train_episodes": train, "eval_episodes": heldout}


def export_training_request(
    run_directories: Sequence[str | Path], output: str | Path,
    *, dataset_id: str, eval_split: float | None = None,
    expected_eval_episodes: Sequence[int] | None = None,
) -> dict:
    """Publish one request after native preparation validates selected runs.

    Output parents must already exist. An existing output is never overwritten,
    including on replay; requests confer no training or semantic authority.
    """
    if not run_directories:
        raise CuratorError("SELECTION_RUNS_REQUIRED")
    if (eval_split is None) != (expected_eval_episodes is None):
        raise CuratorError("SELECTION_EVALUATION_OPTIONS")
    target = reject_symlink_components(output, "SELECTION_OUTPUT").resolve()
    if target.exists():
        raise CuratorError("EVENT_EXISTS", str(target))
    episodes = []
    selected = set()
    dataset = None
    protected = set()
    observed_states = []
    try:
        for directory in run_directories:
            root = reject_symlink_components(directory, "SELECTION_RUN").resolve(strict=True)
            ledger_path = root / "episode_ledger.json"
            ledger = load_json_strict(ledger_path)
            # This existing owner reopens and validates the ledger's source graph.
            state = validate_episode_state(
                load_json_strict(root / "episode_ledger_state.json"), ledger=ledger,
            )
            observed_states.append((root, state))
            # Collection identities vary as episodes append to one root. They
            # are not the current frozen byte identity owned by training.
            current = {
                key: ledger["dataset"][key] for key in ("dataset_root", "repo_id")
            }
            if dataset is not None and current != dataset:
                raise CuratorError("SELECTION_DATASET_MISMATCH")
            dataset = current
            episode = ledger["episode"]
            index = episode["episode_index"]
            if index in selected:
                raise CuratorError("SELECTION_DUPLICATE_EPISODE")
            selected.add(index)
            if (
                ledger["admission"]["technical_status"] != "PASS"
                or state["review"]["semantic_status"] != "PASS"
            ):
                raise CuratorError("SELECTION_REVIEW_REQUIRED", episode["run_id"])
            protected.update((root, Path(dataset["dataset_root"])))
            protected.update(
                Path(ref["artifact_path"]).parent for ref in ledger["artifacts"].values()
            )
            protected.add(Path(state["candidate"]["artifact_path"]).parent)
            episodes.append({
                "episode_id": episode["run_id"], "episode_index": index,
                "episode_ledger_path": str(ledger_path),
                "technical_validator_path": ledger["artifacts"]["technical"]["artifact_path"],
                "human_semantic_evidence_path": state["candidate"]["artifact_path"],
            })
        if any(target.is_relative_to(path.resolve()) for path in protected):
            raise CuratorError("SELECTION_OUTPUT_OVERLAP")
        request = {**dataset, "dataset_id": dataset_id}
        request["episodes"] = sorted(episodes, key=lambda episode: episode["episode_index"])
        # Exercise the actual read-only consumer before publication. Its drafts
        # stay in memory; this identifier is not a human approval or attribution.
        # The consumer owns byte identity, metadata, production scope and lineage.
        prepare_approvals(request, target.parent, "curator-preview-only")
        cohort = None if eval_split is None else check_evaluation_cohort(
            request, eval_split=eval_split, expected_eval_episodes=expected_eval_episodes,
        )
        # Preparation may read a large dataset while input artifacts change.
        # Reopen the existing owner's state; do not publish a selection based on
        # evidence that changed during that work. This is not a source lock.
        for root, expected_state in observed_states:
            current_state = validate_episode_state(
                load_json_strict(root / "episode_ledger_state.json"),
                ledger=load_json_strict(root / "episode_ledger.json"),
            )
            if current_state != expected_state:
                raise CuratorError("SELECTION_INPUT_CHANGED", str(root))
        write_json_exclusive(target, request)
    except ContractError as exc:
        raise CuratorError("SELECTION_SOURCE_INVALID", str(exc)) from exc
    except OSError as exc:
        raise CuratorError("SELECTION_IO", str(exc)) from exc
    return {
        "ok": True, "status": "REQUEST_NOT_APPROVED", "request_path": str(target),
        "episode_indices": [episode["episode_index"] for episode in request["episodes"]],
        "training_authority": False,
        **({"evaluation_cohort": cohort} if cohort is not None else {}),
    }


__all__ = ["check_evaluation_cohort", "export_training_request"]
