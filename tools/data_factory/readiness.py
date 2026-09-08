"""Shared operational readiness values; no lifecycle or dataset authority."""

RECORDER_READINESS_CONTRACT = {
    "schema_version": "data_factory.recorder_readiness_contract.v2",
    "deadline_s": 5.0,
    "min_durable_rows": 60,
    "target_fps": 30,
    "row_fps_min": 27.0,
    "row_fps_max": 33.0,
    "min_camera_source_fps": 28.5,
    "status_max_age_heartbeat_fraction": 0.5,
    "require_writer_alive": True,
    "max_writer_queue_drops": 0,
    "max_alignment_failures": 0,
    "require_quality_accepted": True,
    "scene_camera": "up",
    "min_scene_brightness": 20.0,
}
