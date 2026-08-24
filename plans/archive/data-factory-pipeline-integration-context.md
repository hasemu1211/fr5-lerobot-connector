# Data factory and pipeline integration interview context

> Status: `ARCHIVED`; retained only for decision traceability.

- Task: clarify how the deterministic pose data factory should connect to the existing interactive data pipeline before planning or implementation.
- Desired outcome: preserve independent modules while giving humans and AI agents a convenient, efficient path from pose/task specification through robot execution, recording, validation, approval, and dataset admission.
- Stated solution direction: A4 `(place_id, yaw_deg, x_mm, y_mm)` pose source; `pickup.v1` and `pick_place.v1`; top/side/tilted grasp profiles; deterministic motion planning with existing hardware safety boundaries.
- Probable intent: eliminate repeated manual coordination and inconsistent metadata without turning the current recorder into a monolithic robot mission system.
- Code facts:
  - `scripts/collect.sh` launches `tools/fr5_lerobot_recorder.py`, then optionally runs dataset validation, preview generation, and human training approval.
  - The recorder owns ROS sampling, timestamp alignment, episode keys, hard per-episode quality rejection, LeRobot writes, source provenance, and recording quality records.
  - `scripts/validate_dataset.sh` and `tools/validate_lerobot_dataset.py` are independently callable post-collection gates.
  - Shared capture limits and LeRobot features live in `tools/fr5_dataset_schema.py`.
  - The current interactive control is terminal-key based and requires TTY input; no machine-oriented episode-control interface exists.
- Existing contract: `docs/data-factory.md` defines the deterministic pose/grasp direction and filesystem ownership but not the final runtime handoff boundary.
- Constraints: no implementation during interview; existing FR5 controller/hardware safety is authoritative; data factory and pipeline should remain independently usable; vision is not the authoritative object-pose input.
- Known quality split: recorder/validator already cover timing, RGB, provenance, structure, and human visual approval; the new data factory must add pose registration, path feasibility, tracking, grasp/place outcome, and run-to-episode traceability.
- Unknowns:
  - Whether the primary product experience is one unified session or two explicit tools connected by a job/run manifest.
  - Which component owns episode start/stop/discard during automated execution.
  - Whether semantic success is always human-confirmed, sensor-derived, or hybrid.
  - What failures should be retried automatically versus returned to the operator/agent.
  - Which operations an AI agent may trigger without a human action.
- Decision-boundary unknowns: authority to arm/execute the real robot, approve semantic success, retry motion, and promote episodes to training data.
- Likely touchpoints: `scripts/collect.sh`, `tools/fr5_lerobot_recorder.py`, `tools/fr5_dataset_schema.py`, `scripts/validate_dataset.sh`, a future data-factory engine and profile registry.
- Relevant sources inspected: injected workspace `AGENTS.md`; `README.md`; `docs/data-factory.md`; `docs/architecture-and-quality.md`; `docs/data-collection.md`; files listed above.
- Terminology conflict: “하나의 대화형 인터페이스” currently refers to both terminal episode control and the wrapper's end-of-run validation/approval prompts; the desired future meaning is not yet fixed.
- Prompt-safe initial-context summary: not_needed.
