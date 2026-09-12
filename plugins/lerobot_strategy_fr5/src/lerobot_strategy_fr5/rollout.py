import json
import logging
from dataclasses import dataclass
from pathlib import Path

from lerobot.configs import parser
from lerobot.rollout import RolloutConfig, build_rollout_context, create_strategy
from lerobot.utils.import_utils import register_third_party_plugins
from lerobot.utils.process import ProcessSignalHandler
from lerobot.utils.utils import init_logging
from lerobot.utils.visualization_utils import (
    init_visualization,
    shutdown_visualization,
)

from .evidence import JsonlEvidenceSink
from .processor_bridge import make_fr5_robot_action_processor

logger = logging.getLogger(__name__)

def _saved_rename_map(pretrained_path) -> dict[str, str]:
    path = Path(pretrained_path) / "policy_preprocessor.json"
    if not path.is_file():
        return {}

    data = json.loads(path.read_text())
    found = []

    def walk(value):
        if isinstance(value, dict):
            rename = value.get("rename_map")
            if isinstance(rename, dict) and rename:
                found.append(dict(rename))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)

    unique = []
    for item in found:
        if item not in unique:
            unique.append(item)

    if len(unique) > 1:
        raise RuntimeError("FR5_MULTIPLE_RENAME_MAPS")

    return unique[0] if unique else {}




@dataclass
class FR5RolloutConfig(RolloutConfig):
    evidence_run_id: str = ""
    evidence_path: str = ""


@parser.wrap()
def rollout(cfg: FR5RolloutConfig):
    init_logging()

    if cfg.robot is None or cfg.robot.type != "fr5":
        raise ValueError("FR5_ROLLOUT_REQUIRES_ROBOT_TYPE_FR5")

    if not cfg.evidence_run_id:
        raise ValueError("--evidence_run_id is required")

    if not cfg.rename_map:
        cfg.rename_map = _saved_rename_map(
            cfg.policy.pretrained_path
        )

    path = Path(
        cfg.evidence_path
        or f".agent-local/work/lerobot-fr5/evidence/"
           f"{cfg.evidence_run_id}.jsonl"
    )

    sink = JsonlEvidenceSink(path, cfg.evidence_run_id)
    ctx = None
    strategy = None
    outcome = "ERROR"

    try:
        sink.emit("RUN_START", {
            "strategy_type": cfg.strategy.type,
            "inference_type": cfg.inference.type,
            "robot_type": cfg.robot.type,
            "physical_io_enabled": bool(
                getattr(cfg.robot, "physical_io_enabled", False)
            ),
            "rename_map": dict(cfg.rename_map),
        })

        if cfg.display_data:
            init_visualization(
                cfg.display_mode,
                session_name="fr5-rollout",
                ip=cfg.display_ip,
                port=cfg.display_port,
            )

        signal_handler = ProcessSignalHandler(
            use_threads=True,
            display_pid=False,
        )
        shutdown_event = signal_handler.shutdown_event

        robot_action_processor = make_fr5_robot_action_processor(
            sink,
            upper_m=cfg.robot.gripper_upper_m,
            projection_quanta=cfg.robot.gripper_projection_quanta,
        )

        ctx = build_rollout_context(
            cfg,
            shutdown_event,
            robot_action_processor=robot_action_processor,
        )

        attach = getattr(
            ctx.hardware.robot_wrapper.inner,
            "attach_evidence_sink",
            None,
        )
        if callable(attach):
            attach(sink)

        strategy = create_strategy(cfg.strategy)
        strategy.setup(ctx)

        strategy.run(ctx)

        outcome = "COMPLETED"

    except KeyboardInterrupt:
        outcome = "INTERRUPTED"
        logger.info("Interrupted by user")

    finally:
        try:
            if strategy is not None and ctx is not None:
                strategy.teardown(ctx)
        finally:
            try:
                sink.emit("RUN_END", {"outcome": outcome})
            finally:
                sink.close()
                if cfg.display_data:
                    shutdown_visualization(cfg.display_mode)


def main():
    register_third_party_plugins()
    rollout()


if __name__ == "__main__":
    main()
