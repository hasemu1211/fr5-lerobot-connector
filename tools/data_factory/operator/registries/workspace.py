"""Session-owned, preview-first workspace registration without motion authority."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from tools.a4_place_yaw.generate_place_yaw_a4 import build_places, make_manifest
from tools.data_factory.operator.setup.contracts import (
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


PREVIEW_SCHEMA = "data_factory.workspace_registration_preview.v2"
PROMOTION_SCHEMA = "data_factory.workspace_registration_promotion.v2"
WORKSPACE_SCHEMA = "data_factory.workspace.v1"
_ARTIFACT_FILES = frozenset({
    "manifest.json", "measurements.jsonl", "result.json",
    "yaw0_sheet.json", "cell_calibration_candidate.json",
})
CAPTURE_LABELS = ("CENTER", "X_REF", "Y_CHECK")


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value):
        raise ContractError(code)
    return value


def _workspace_identity(display_name: object) -> tuple[str, str, str]:
    if not isinstance(display_name, str):
        raise ContractError("WORKSPACE_DISPLAY_NAME")
    normalized = " ".join(unicodedata.normalize("NFKC", display_name).split())
    if (
        not 1 <= len(normalized) <= 80
        or not any(character.isalnum() for character in normalized)
        or any(
            character in "/\\"
            or unicodedata.category(character).startswith("C")
            for character in normalized
        )
    ):
        raise ContractError("WORKSPACE_DISPLAY_NAME")
    if normalized.isascii() and re.fullmatch(r"[A-Za-z0-9 ._-]+", normalized):
        slug = "_".join(re.findall(r"[A-Za-z0-9]+", normalized)).upper()
        if slug == "PLACE":
            raise ContractError("WORKSPACE_DISPLAY_NAME")
        if not slug.startswith("PLACE_"):
            slug = f"PLACE_{slug}"
        if len(slug) > 56:
            suffix = hashlib.sha256(normalized.casefold().encode()).hexdigest()[:10].upper()
            slug = f"{slug[:45].rstrip('_')}_{suffix}"
    else:
        suffix = hashlib.sha256(normalized.casefold().encode()).hexdigest()[:12].upper()
        slug = f"PLACE_{suffix}"
    place_id = _identifier(slug, "WORKSPACE_PLACE_ID")
    calibration_id = _identifier(
        f"{place_id.lower().replace('_', '-')}-yaw0-r001",
        "WORKSPACE_CALIBRATION_ID",
    )
    return normalized, place_id, calibration_id


def _new_yaw0_manifest(source: Mapping[str, Any], place_id: str) -> dict[str, Any]:
    source = validate_yaw0_sheet(copy.deepcopy(dict(source)))
    u_values = sorted({float(point["local_uv_mm"][0]) for point in source["grid_points"]})
    v_values = sorted({float(point["local_uv_mm"][1]) for point in source["grid_points"]})
    spacing = float(source["place_spacing_mm"])
    try:
        places = build_places(len(u_values), len(v_values), spacing, 0.0)
    except ValueError as exc:
        raise ContractError("WORKSPACE_SHEET_TEMPLATE") from exc
    if {
        tuple(float(value) for value in point["local_uv_mm"])
        for point in places
    } != {
        tuple(float(value) for value in point["local_uv_mm"])
        for point in source["grid_points"]
    }:
        raise ContractError("WORKSPACE_SHEET_TEMPLATE")
    measured = float(source["print_calibration"]["measured_scale_bar_mm"])
    measurement_tag = f"{measured:06.2f}".replace(".", "_")
    suffix = "" if measured == 100.0 else f"_PRINTCAL_{measurement_tag}MM"
    return validate_yaw0_sheet(make_manifest(
        place_id,
        f"{place_id}_YAW_P000_00{suffix}",
        0.0,
        places,
        spacing,
        measured,
    ))


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
        config_root: str | Path, display_name: str,
    ) -> None:
        self.session_id = _identifier(session_id, "WORKSPACE_SESSION_ID")
        self.display_name, self.place_id, self.calibration_id = (
            _workspace_identity(display_name)
        )
        try:
            self.config_root = Path(config_root).resolve(strict=True)
        except OSError as exc:
            raise ContractError("WORKSPACE_CONFIG_ROOT") from exc
        if not self.config_root.is_dir():
            raise ContractError("WORKSPACE_CONFIG_ROOT")
        self.candidate_root = _candidate_path(candidate_root, self.config_root)
        self._assert_identity_available()
        self._captures: dict[str, dict[str, Any]] = {}
        self._preview: dict[str, Any] | None = None
        self._promotion: dict[str, Any] | None = None

    def projection(self) -> dict[str, Any]:
        """Expose wizard progress without publishing robot snapshots or authority."""
        return {
            "session_id": self.session_id,
            "display_name": self.display_name,
            "place_id": self.place_id,
            "calibration_id": self.calibration_id,
            "captures": {label: label in self._captures for label in CAPTURE_LABELS},
            "preview": copy.deepcopy(self._preview),
            "promotion": copy.deepcopy(self._promotion),
            "execution_authorized": False,
            "training_approved": False,
        }

    def _assert_identity_available(self) -> None:
        workspace, cell, sheet = self._targets()
        if any(path.exists() for path in (workspace, cell, sheet)):
            raise ContractError("WORKSPACE_NAME_CONFLICT")
        cell_root = _config_target(self.config_root, Path("cells"))
        if not cell_root.is_dir():
            return
        for path in cell_root.glob("*.json"):
            try:
                value = load_json_strict(path)
            except (ContractError, OSError) as exc:
                raise ContractError("WORKSPACE_CONFIG_COLLISION_SCAN") from exc
            if value.get("place_id") == self.place_id:
                raise ContractError("WORKSPACE_NAME_CONFLICT")

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
        source_yaw0 = validate_yaw0_sheet(load_json_strict(yaw0_sheet))
        if source_yaw0["place_id"] != plane["place_id"]:
            raise ContractError("WORKSPACE_REGISTRATION_BINDING")
        target_yaw0 = _new_yaw0_manifest(source_yaw0, self.place_id)
        derived_plane = {**plane, "place_id": self.place_id}
        derived_plane["reference_digest"] = canonical_digest({
            key: value for key, value in derived_plane.items()
            if key != "reference_digest"
        })
        self.candidate_root.mkdir(parents=True, exist_ok=True)
        descriptor, generated_name = tempfile.mkstemp(
            prefix=f".{self.calibration_id}.yaw0.",
            suffix=".json", dir=self.candidate_root,
        )
        generated = Path(generated_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(
                    target_yaw0, stream, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            result = compile_workspace_registration_candidate(
                center_snapshot=center_snapshot, x_ref_snapshot=x_ref_snapshot,
                y_check_snapshot=y_check_snapshot,
                plane_reference=derived_plane,
                print_measurements=print_measurements,
                calibration_id=self.calibration_id, place_id=self.place_id,
                operator_or_agent_id=operator_or_agent_id,
                yaw0_sheet=generated,
                tcp_candidate_manifest=tcp_candidate_manifest,
                output_root=self.candidate_root, tolerance_mm=tolerance_mm,
                robot_system_id=robot_system_id,
                max_snapshot_age_s=max_snapshot_age_s,
            )
        finally:
            generated.unlink(missing_ok=True)
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
            "display_name": self.display_name,
            "place_id": self.place_id,
            "calibration_id": self.calibration_id,
            "source_plane_calibration_id": plane["source_calibration_id"],
            "source_plane_reference_digest": plane["reference_digest"],
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

    def _targets(self) -> tuple[Path, Path, Path]:
        return (
            _config_target(
                self.config_root, Path("workspaces") / f"{self.place_id}.json",
            ),
            _config_target(
                self.config_root, Path("cells") / f"{self.calibration_id}.json",
            ),
            _config_target(
                self.config_root,
                Path("workspace_sheets") / f"{self.calibration_id}_yaw0_sheet.json",
            ),
        )

    def discard_preview(self, preview_digest: str) -> dict[str, Any]:
        """Discard one rejected temporary preview so its captures can be retried."""
        if (
            self._preview is None
            or self._promotion is not None
            or self._preview["status"] != "CANDIDATE_OUT_OF_TOLERANCE"
            or not isinstance(preview_digest, str)
            or not DIGEST.fullmatch(preview_digest)
            or preview_digest != self._preview["preview_digest"]
        ):
            raise ContractError("WORKSPACE_PREVIEW_DISCARD")
        artifact = self.candidate_root / self.calibration_id
        _documents, artifact_digest = _artifact_documents(artifact)
        if artifact_digest != self._preview["artifact_digest"]:
            raise ContractError("WORKSPACE_PREVIEW_FORGED")
        shutil.rmtree(artifact)
        self._preview = None
        return self.projection()

    def _workspace_document(
        self, yaw0: Mapping[str, Any], plane: Mapping[str, Any],
    ) -> dict[str, Any]:
        u_values = [float(point["local_uv_mm"][0]) for point in yaw0["grid_points"]]
        v_values = [float(point["local_uv_mm"][1]) for point in yaw0["grid_points"]]
        return {
            "schema_version": WORKSPACE_SCHEMA,
            "place_id": self.place_id,
            "display_name": self.display_name,
            "frame_id": self.calibration_id,
            "coordinate_mode": "CONTINUOUS_A4_PLANE",
            "a4_family_digest": yaw0["a4_family_digest"],
            "yaw0_manifest_digest": canonical_digest(yaw0),
            "x_mm": {"minimum": min(u_values), "maximum": max(u_values)},
            "y_mm": {"minimum": min(v_values), "maximum": max(v_values)},
            "yaw_deg": {"minimum": -180.0, "maximum_exclusive": 180.0},
            "table_plane_provenance": {
                "source_place_id": plane["place_id"],
                "source_calibration_id": plane["source_calibration_id"],
                "source_artifact_digest": plane["source_artifact_digest"],
                "reference_digest": plane["reference_digest"],
            },
            "registration_status": "REGISTERED",
            "motion_qualification_status": "REQUIRED",
            "execution_authorized": False,
            "training_approved": False,
        }

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
        source_path = _config_target(
            self.config_root,
            Path("cells")
            / f"{self._preview['source_plane_calibration_id']}.json",
        )
        try:
            source_plane = qualified_table_plane_reference(load_json_strict(source_path))
        except (ContractError, OSError) as exc:
            raise ContractError("WORKSPACE_PLANE_SOURCE") from exc
        if (
            source_plane["reference_digest"]
            != self._preview["source_plane_reference_digest"]
            or self._qualified_plane(source_plane) != source_plane
        ):
            raise ContractError("WORKSPACE_PLANE_SOURCE")
        qualified = {**candidate, "qualification_status": "QUALIFIED"}
        workspace = self._workspace_document(yaw0, source_plane)
        workspace_target, cell_target, sheet_target = self._targets()
        targets = (workspace_target, cell_target, sheet_target)
        existence = tuple(path.exists() for path in targets)
        if any(existence) and not all(existence):
            raise ContractError("WORKSPACE_PROMOTION_PARTIAL")
        if all(existence):
            if not all(self._matches(path, value) for path, value in zip(
                targets, (workspace, qualified, yaw0), strict=True,
            )):
                raise ContractError("WORKSPACE_NAME_CONFLICT")
        else:
            self._assert_identity_available()
            parent_existed = {
                path.parent: path.parent.exists() for path in targets
            }
            written: list[Path] = []
            try:
                _write_json_exclusive(workspace_target, workspace)
                written.append(workspace_target)
                _write_json_exclusive(sheet_target, yaw0)
                written.append(sheet_target)
                promoted = qualify_place(artifact, self.config_root)
                if promoted != qualified or not self._matches(cell_target, qualified):
                    raise ContractError("WORKSPACE_PROMOTION_RESULT")
                written.append(cell_target)
                if not all(self._matches(path, value) for path, value in zip(
                    targets, (workspace, qualified, yaw0), strict=True,
                )):
                    raise ContractError("WORKSPACE_PROMOTION_RESULT")
            except Exception:
                if self._matches(cell_target, qualified):
                    cell_target.unlink(missing_ok=True)
                for path in reversed(written):
                    path.unlink(missing_ok=True)
                for parent, existed in parent_existed.items():
                    if not existed:
                        try:
                            parent.rmdir()
                        except OSError:
                            pass
                raise

        promotion = {
            "schema_version": PROMOTION_SCHEMA,
            "session_id": self.session_id,
            "preview_digest": preview_digest,
            "place_id": self._preview["place_id"],
            "calibration_id": self.calibration_id,
            "workspace_relative_path": str(
                workspace_target.relative_to(self.config_root)
            ),
            "cell_relative_path": str(cell_target.relative_to(self.config_root)),
            "yaw0_sheet_relative_path": str(sheet_target.relative_to(self.config_root)),
            "qualified_cell_digest": canonical_digest(qualified),
            "workspace_digest": canonical_digest(workspace),
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
    "CAPTURE_LABELS", "PREVIEW_SCHEMA", "PROMOTION_SCHEMA", "WORKSPACE_SCHEMA",
    "WorkspaceManager",
]
