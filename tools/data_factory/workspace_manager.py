"""Session-owned, preview-first workspace registration without motion authority."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any, Mapping

from tools.data_factory.operator_setup import (
    compile_workspace_registration_candidate,
    qualified_table_plane_reference,
    validate_table_plane_reference,
)
from tools.data_factory.motion.pose_snapshot import qualify_place
from tools.fr5_data_factory import (
    ContractError,
    DIGEST,
    SAFE_ID,
    canonical_digest,
    load_json_strict,
    validate_yaw0_sheet,
)


PREVIEW_SCHEMA = "data_factory.workspace_registration_preview.v1"
PROMOTION_SCHEMA = "data_factory.workspace_registration_promotion.v1"
_ARTIFACT_FILES = frozenset({
    "manifest.json", "measurements.jsonl", "result.json",
    "yaw0_sheet.json", "cell_calibration_candidate.json",
})
CAPTURE_LABELS = ("CENTER", "X_REF", "Y_CHECK")


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _candidate_path(value: str | Path, config_root: Path) -> Path:
    raw = Path(value).absolute()
    for parent in reversed(raw.parents):
        if parent.is_symlink():
            raise ContractError("WORKSPACE_CANDIDATE_ROOT")
    if raw.is_symlink():
        raise ContractError("WORKSPACE_CANDIDATE_ROOT")
    resolved = raw.resolve(strict=False)
    try:
        resolved.relative_to(config_root)
    except ValueError:
        return resolved
    raise ContractError("WORKSPACE_CANDIDATE_ROOT")


def _config_target(config_root: Path, relative: Path) -> Path:
    target = config_root / relative
    current = config_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ContractError("WORKSPACE_PROMOTION_PATH")
    try:
        target.resolve(strict=False).relative_to(config_root)
    except (OSError, ValueError) as exc:
        raise ContractError("WORKSPACE_PROMOTION_PATH") from exc
    return target


def _artifact_documents(root: Path) -> tuple[dict[str, Any], str]:
    try:
        if root.is_symlink() or not root.is_dir() or any(path.is_symlink() for path in root.iterdir()):
            raise ContractError("WORKSPACE_PREVIEW_FORGED")
        complete = load_json_strict(root / "_complete.json")
        if (
            not isinstance(complete, dict)
            or set(complete) != {"schema_version", "files"}
            or complete["schema_version"] != "data_factory.artifact_complete.v1"
            or not isinstance(complete["files"], dict)
            or set(complete["files"]) != _ARTIFACT_FILES
        ):
            raise ContractError("WORKSPACE_PREVIEW_FORGED")
        present = {path.name for path in root.iterdir() if path.is_file()}
        if present - _ARTIFACT_FILES - {"_complete.json", "promotion.json"}:
            raise ContractError("WORKSPACE_PREVIEW_FORGED")
        documents = {name: load_json_strict(root / name) for name in _ARTIFACT_FILES}
        if any(
            not isinstance(digest, str)
            or not DIGEST.fullmatch(digest)
            or canonical_digest(documents[name]) != digest
            for name, digest in complete["files"].items()
        ):
            raise ContractError("WORKSPACE_PREVIEW_FORGED")
    except ContractError as exc:
        if exc.code == "WORKSPACE_PREVIEW_FORGED":
            raise
        raise ContractError("WORKSPACE_PREVIEW_FORGED") from exc
    except (OSError, KeyError, TypeError) as exc:
        raise ContractError("WORKSPACE_PREVIEW_FORGED") from exc
    fingerprint = canonical_digest({"complete": complete, "files": documents})
    return documents, fingerprint


def _write_json_exclusive(path: Path, value: object) -> None:
    payload = (json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        published = True
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        if published:
            path.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)


class WorkspaceManager:
    """Own exactly one three-point preview and its digest-gated promotion."""

    def __init__(
        self, *, session_id: str, candidate_root: str | Path,
        config_root: str | Path,
    ) -> None:
        self.session_id = _identifier(session_id, "WORKSPACE_SESSION_ID")
        try:
            self.config_root = Path(config_root).resolve(strict=True)
        except OSError as exc:
            raise ContractError("WORKSPACE_CONFIG_ROOT") from exc
        if not self.config_root.is_dir():
            raise ContractError("WORKSPACE_CONFIG_ROOT")
        self.candidate_root = _candidate_path(candidate_root, self.config_root)
        identity = hashlib.sha256(
            f"{self.session_id}:{uuid.uuid4().hex}".encode()
        ).hexdigest()[:20]
        self.calibration_id = f"workspace-{identity}-r001"
        self._captures: dict[str, dict[str, Any]] = {}
        self._preview: dict[str, Any] | None = None
        self._promotion: dict[str, Any] | None = None

    def projection(self) -> dict[str, Any]:
        """Expose wizard progress without publishing robot snapshots or authority."""
        return {
            "session_id": self.session_id,
            "calibration_id": self.calibration_id,
            "captures": {label: label in self._captures for label in CAPTURE_LABELS},
            "preview": copy.deepcopy(self._preview),
            "promotion": copy.deepcopy(self._promotion),
            "execution_authorized": False,
            "training_approved": False,
        }

    def capture(self, label: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
        """Keep one caller-captured pose per role until preview seals the session."""
        if label not in CAPTURE_LABELS or not isinstance(snapshot, Mapping):
            raise ContractError("WORKSPACE_CAPTURE")
        if self._preview is not None:
            raise ContractError("WORKSPACE_PREVIEW_EXISTS")
        self._captures[label] = copy.deepcopy(dict(snapshot))
        return self.projection()

    def preview_captured(self, **kwargs) -> dict[str, Any]:
        """Compile the exact three captured roles through the ordinary preview path."""
        if set(self._captures) != set(CAPTURE_LABELS):
            raise ContractError("WORKSPACE_CAPTURE_INCOMPLETE")
        return self.preview(
            center_snapshot=self._captures["CENTER"],
            x_ref_snapshot=self._captures["X_REF"],
            y_check_snapshot=self._captures["Y_CHECK"],
            **kwargs,
        )

    def _qualified_plane(self, value: object) -> dict[str, Any]:
        plane = validate_table_plane_reference(value)
        source_path = _config_target(
            self.config_root, Path("cells") / f"{plane['source_calibration_id']}.json",
        )
        if not source_path.is_file():
            raise ContractError("WORKSPACE_PLANE_SOURCE")
        try:
            source = load_json_strict(source_path)
        except (ContractError, OSError) as exc:
            raise ContractError("WORKSPACE_PLANE_SOURCE") from exc
        if (
            canonical_digest(source) != plane["source_artifact_digest"]
            or qualified_table_plane_reference(source) != plane
        ):
            raise ContractError("WORKSPACE_PLANE_SOURCE")
        return plane

    def preview(
        self, *, center_snapshot: Mapping[str, Any],
        x_ref_snapshot: Mapping[str, Any], y_check_snapshot: Mapping[str, Any],
        plane_reference: Mapping[str, Any], print_measurements: Mapping[str, Any],
        operator_or_agent_id: str, yaw0_sheet: str | Path,
        tcp_candidate_manifest: str | Path, tolerance_mm: float,
        robot_system_id: str = "fr5-lab-a", max_snapshot_age_s: float = 0.5,
    ) -> dict[str, Any]:
        """Create one effect-neutral candidate below the caller-owned preview root."""
        if self._preview is not None:
            raise ContractError("WORKSPACE_PREVIEW_EXISTS")
        if any(not isinstance(value, Mapping) for value in (
            center_snapshot, x_ref_snapshot, y_check_snapshot,
        )):
            raise ContractError("WORKSPACE_SNAPSHOT_MISSING")
        plane = self._qualified_plane(plane_reference)
        result = compile_workspace_registration_candidate(
            center_snapshot=center_snapshot, x_ref_snapshot=x_ref_snapshot,
            y_check_snapshot=y_check_snapshot, plane_reference=plane,
            print_measurements=print_measurements,
            calibration_id=self.calibration_id, place_id=plane["place_id"],
            operator_or_agent_id=operator_or_agent_id, yaw0_sheet=yaw0_sheet,
            tcp_candidate_manifest=tcp_candidate_manifest,
            output_root=self.candidate_root, tolerance_mm=tolerance_mm,
            robot_system_id=robot_system_id,
            max_snapshot_age_s=max_snapshot_age_s,
        )
        if (
            result.get("status") not in {
                "CANDIDATE_WITHIN_TOLERANCE", "CANDIDATE_OUT_OF_TOLERANCE",
            }
            or result.get("execution_authorized") is not False
            or result.get("training_approved") is not False
        ):
            raise ContractError("WORKSPACE_PREVIEW_NOT_SAVEABLE")
        documents, artifact_digest = _artifact_documents(
            self.candidate_root / self.calibration_id,
        )
        yaw0 = validate_yaw0_sheet(documents["yaw0_sheet.json"])
        preview = {
            "schema_version": PREVIEW_SCHEMA,
            "session_id": self.session_id,
            "place_id": plane["place_id"],
            "calibration_id": self.calibration_id,
            "plane_reference_digest": plane["reference_digest"],
            "print_measurement_digest": print_measurements.get("measurement_digest"),
            "artifact_digest": artifact_digest,
            "cell_candidate_digest": result["cell_calibration_candidate_digest"],
            "yaw0_manifest_digest": canonical_digest(yaw0),
            "status": result["status"],
            "consumer_contract": "PREVIEW_ONLY",
            "execution_authorized": False,
            "training_approved": False,
        }
        preview["preview_digest"] = canonical_digest(preview)
        self._preview = preview
        return copy.deepcopy(preview)

    def _targets(self) -> tuple[Path, Path]:
        return (
            _config_target(
                self.config_root, Path("cells") / f"{self.calibration_id}.json",
            ),
            _config_target(
                self.config_root,
                Path("workspace_sheets") / f"{self.calibration_id}_yaw0_sheet.json",
            ),
        )

    @staticmethod
    def _matches(path: Path, value: object) -> bool:
        try:
            return path.is_file() and load_json_strict(path) == value
        except (ContractError, OSError):
            return False

    def save(self, preview_digest: str) -> dict[str, Any]:
        """Promote the exact current preview once; identical retries are idempotent."""
        if self._preview is None:
            raise ContractError("WORKSPACE_PREVIEW_REQUIRED")
        if (
            not isinstance(preview_digest, str)
            or not DIGEST.fullmatch(preview_digest)
            or preview_digest != self._preview["preview_digest"]
        ):
            raise ContractError("WORKSPACE_PREVIEW_DIGEST_MISMATCH")
        if self._preview["status"] != "CANDIDATE_WITHIN_TOLERANCE":
            raise ContractError("WORKSPACE_PREVIEW_NOT_SAVEABLE")

        artifact = self.candidate_root / self.calibration_id
        documents, artifact_digest = _artifact_documents(artifact)
        if artifact_digest != self._preview["artifact_digest"]:
            raise ContractError("WORKSPACE_PREVIEW_FORGED")
        yaw0 = validate_yaw0_sheet(documents["yaw0_sheet.json"])
        candidate = documents["cell_calibration_candidate.json"]
        if (
            not isinstance(candidate, dict)
            or candidate.get("calibration_id") != self.calibration_id
            or candidate.get("place_id") != self._preview["place_id"]
            or canonical_digest(yaw0) != self._preview["yaw0_manifest_digest"]
            or canonical_digest(candidate) != self._preview["cell_candidate_digest"]
        ):
            raise ContractError("WORKSPACE_PREVIEW_FORGED")
        qualified = {**candidate, "qualification_status": "QUALIFIED"}
        cell_target, sheet_target = self._targets()
        cell_exists, sheet_exists = cell_target.exists(), sheet_target.exists()
        if cell_exists != sheet_exists:
            raise ContractError("WORKSPACE_PROMOTION_PARTIAL")
        if cell_exists:
            if not self._matches(cell_target, qualified) or not self._matches(sheet_target, yaw0):
                raise ContractError("WORKSPACE_REVISION_CONFLICT")
        else:
            sheet_parent_existed = sheet_target.parent.exists()
            wrote_sheet = False
            try:
                _write_json_exclusive(sheet_target, yaw0)
                wrote_sheet = True
                promoted = qualify_place(artifact, self.config_root)
                if promoted != qualified or not self._matches(cell_target, qualified):
                    raise ContractError("WORKSPACE_PROMOTION_RESULT")
            except Exception:
                if self._matches(cell_target, qualified):
                    cell_target.unlink(missing_ok=True)
                if wrote_sheet:
                    sheet_target.unlink(missing_ok=True)
                if not sheet_parent_existed:
                    try:
                        sheet_target.parent.rmdir()
                    except OSError:
                        pass
                raise

        promotion = {
            "schema_version": PROMOTION_SCHEMA,
            "session_id": self.session_id,
            "preview_digest": preview_digest,
            "place_id": self._preview["place_id"],
            "calibration_id": self.calibration_id,
            "cell_relative_path": str(cell_target.relative_to(self.config_root)),
            "yaw0_sheet_relative_path": str(sheet_target.relative_to(self.config_root)),
            "qualified_cell_digest": canonical_digest(qualified),
            "yaw0_manifest_digest": canonical_digest(yaw0),
            "status": "PROMOTED",
            "execution_authorized": False,
            "training_approved": False,
        }
        promotion["promotion_digest"] = canonical_digest(promotion)
        if self._promotion is not None and self._promotion != promotion:
            raise ContractError("WORKSPACE_PROMOTION_CONFLICT")
        self._promotion = promotion
        return copy.deepcopy(promotion)


__all__ = [
    "CAPTURE_LABELS", "PREVIEW_SCHEMA", "PROMOTION_SCHEMA", "WorkspaceManager",
]
