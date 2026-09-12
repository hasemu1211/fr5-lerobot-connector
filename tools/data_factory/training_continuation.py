"""Scoped LeRobot 0.6.1 resume seams; the official trainer owns every update/save."""

from contextlib import contextmanager, ExitStack
import os
from pathlib import Path
from unittest.mock import patch

from tools.validate_training_checkpoint import CONTINUATION_STATE, advance_sample_cursor


@contextmanager
def native_continuation(trainer, *, checkpoint: Path, state: dict, schedule: dict,
                        batch_size: int):
    """Keep native optimizer/RNG restoration and bind a committed single-process cursor."""
    import torch
    from accelerate import Accelerator
    from lerobot.utils.random_utils import get_rng_state, set_rng_state
    from tools.fr5_data_factory import ContractError
    from tools.data_factory.training_approval import _write_exclusive

    if os.environ.get("WORLD_SIZE", "1") != "1":
        raise ContractError("TRAINING_CONTINUATION_WORLD_SIZE")
    cursor = dict(state["cursor"])
    committed_step = state["step"]
    pending = None
    restored_rng = None
    persistent = "persistent_eval_started" in state
    eval_started = state.get("persistent_eval_started", False)
    eval_frequency = 0
    eval_present = False
    worker_loaders = 0
    original_build = trainer.make_optimizer_and_scheduler
    original_prepare = Accelerator.prepare
    original_worker_kwargs = trainer._dataloader_worker_kwargs if persistent else None
    original_load = trainer.load_training_state
    original_cycle = trainer.cycle
    original_update = trainer.update_policy
    original_save = trainer.save_checkpoint

    def sample_state(step, num_frames, *ignored_native_batch_estimate):
        if step != committed_step or num_frames != cursor["num_frames"]:
            raise ContractError("TRAINING_CONTINUATION_SAMPLE_COUNT")
        return {"epoch": cursor["epoch"], "start_index": cursor["offset"]}

    def build(cfg, policy):
        nonlocal eval_frequency
        eval_frequency = cfg.eval_steps
        horizon = cfg.steps
        try:
            cfg.steps = schedule["native_horizon"]
            optimizer, scheduler = original_build(cfg, policy)
        finally:
            cfg.steps = horizon
        if not isinstance(scheduler, torch.optim.lr_scheduler.LambdaLR):
            raise ContractError("TRAINING_CONTINUATION_SCHEDULER")
        boundary = schedule["hold_from_step"]
        if boundary is not None:
            scheduler.lr_lambdas = [lambda step, fn=fn: fn(min(step, boundary))
                                    for fn in scheduler.lr_lambdas]
        return optimizer, scheduler

    def prepare(accelerator, *args, **kwargs):
        nonlocal eval_present
        if persistent:
            loaders = [arg for arg in args if isinstance(arg, torch.utils.data.DataLoader)]
            if not 1 <= len(loaders) <= 2:
                raise ContractError("TRAINING_CONTINUATION_LOADER")
            eval_present = len(loaders) == 2
            for index, loader in enumerate(loaders):
                keep_workers = index == 0 or not eval_started
                if (loader.num_workers != 4 or loader.prefetch_factor != 4
                        or loader.persistent_workers != keep_workers
                        or loader.multiprocessing_context.get_start_method() != "spawn"):
                    raise ContractError("TRAINING_CONTINUATION_LOADER")
                if index == 0 or eval_started:
                    # These persistent workers already existed before the checkpoint.
                    # Recreate them without another policy CPU-RNG base-seed draw.
                    # Their unsaved RNG is irrelevant to this deterministic map path;
                    # sample order belongs to the native (seed, epoch) sampler.
                    loader.generator = torch.Generator().set_state(torch.get_rng_state())
        return original_prepare(accelerator, *args, **kwargs)

    def worker_kwargs(cfg):
        nonlocal worker_loaders
        worker_loaders += 1
        options = original_worker_kwargs(cfg)
        # Native creates TRAIN then EVAL. Already-started EVAL uses isolated
        # worker seeds above, so ending that pool after evaluation preserves
        # policy RNG while releasing host RAM before checkpoint serialization.
        # TRAIN stays persistent; first-ever EVAL retains its native lifetime.
        if worker_loaders == 2 and eval_started:
            options["persistent_workers"] = False
            import logging
            logging.info("Continuation releases recreated EVAL workers after each evaluation; TRAIN remains persistent")
        return options

    def load(path, optimizer, scheduler, **kwargs):
        nonlocal restored_rng
        if Path(path).resolve() != checkpoint.resolve():
            raise ContractError("TRAINING_CONTINUATION_PARENT")
        result = original_load(path, optimizer, scheduler, **kwargs)
        if result[0] != committed_step or scheduler.last_epoch != committed_step:
            raise ContractError("TRAINING_CONTINUATION_STEP")
        import math
        if any(not math.isclose(group["lr"], base * fn(committed_step), rel_tol=1e-12, abs_tol=0)
               for group, base, fn in zip(optimizer.param_groups, scheduler.base_lrs, scheduler.lr_lambdas,
                                          strict=True)):
            raise ContractError("TRAINING_CONTINUATION_LR_PREFIX")
        restored_rng = get_rng_state()
        # Native serialization omits Python's Gaussian cache and rounds NumPy's.
        # New continuation checkpoints retain those scalars alongside native RNG.
        restored_rng["random_state"] = (*restored_rng["random_state"][:2], state["python_gauss"])
        if state["numpy_gauss"] is not None:
            restored_rng["numpy_random_state"] = (*restored_rng["numpy_random_state"][:4], state["numpy_gauss"])
        return result

    def cycle(loader):
        nonlocal pending
        if restored_rng is None or loader.total_batch_size != batch_size:
            raise ContractError("TRAINING_CONTINUATION_LOADER")
        loader.set_epoch(cursor["epoch"])
        set_rng_state(restored_rng)
        iterator = original_cycle(loader)
        first = True
        while True:
            if pending is not None:
                raise ContractError("TRAINING_CONTINUATION_UNCOMMITTED_BATCH")
            rng = get_rng_state() if first and cursor["offset"] and not persistent else None
            batch = next(iterator)
            # Workers0 recreates an iterator every epoch: keep ordinary boundary
            # draws, but remove the extra mid-epoch draw. Persistent workers above
            # use their own seeding generator at either restart position.
            if rng is not None:
                set_rng_state(rng)
            first = False
            pending = len(batch["action"])
            if pending != min(batch_size, cursor["num_frames"] - cursor["offset"]):
                raise ContractError("TRAINING_CONTINUATION_BATCH")
            yield batch

    def update(*args, **kwargs):
        nonlocal cursor, committed_step, pending
        accelerator = kwargs["accelerator"]
        if (accelerator.num_processes != 1 or accelerator.mixed_precision != "no"
                or accelerator.gradient_accumulation_steps != 1 or pending is None):
            raise ContractError("TRAINING_CONTINUATION_RUNTIME")
        result = original_update(*args, **kwargs)
        if accelerator.optimizer_step_was_skipped:
            raise ContractError("TRAINING_CONTINUATION_SKIPPED_UPDATE")
        cursor = advance_sample_cursor(cursor, 1, pending)
        committed_step += 1
        pending = None
        return result

    def save(**kwargs):
        if kwargs["step"] != committed_step or pending is not None:
            raise ContractError("TRAINING_CONTINUATION_SAVE_POSITION")
        rng = get_rng_state()
        original_save(**kwargs)
        saved = {"schema_version": "fr5-native-continuation-state-v1", "step": committed_step,
                 "cursor": cursor, "schedule": schedule,
                 "python_gauss": rng["random_state"][2],
                 "numpy_gauss": float(rng["numpy_random_state"][4])}
        if persistent:
            # Native evaluation precedes saving. Retain its iterator's history even
            # when a later continuation changes the evaluation cadence.
            started = eval_started or (eval_present and eval_frequency > 0
                and committed_step // eval_frequency > state["step"] // eval_frequency)
            saved.update(schema_version="fr5-native-continuation-state-v2",
                         persistent_eval_started=started)
        _write_exclusive(Path(kwargs["checkpoint_dir"]) / "training_state" / CONTINUATION_STATE, saved,
                         "TRAINING_CONTINUATION_STATE_EXISTS")

    with ExitStack() as stack:
        if persistent:
            stack.enter_context(patch.object(Accelerator, "prepare", prepare))
            stack.enter_context(patch.object(trainer, "_dataloader_worker_kwargs", worker_kwargs))
        for name, value in (("compute_sampler_state", sample_state), ("make_optimizer_and_scheduler", build),
                            ("load_training_state", load), ("cycle", cycle), ("update_policy", update),
                            ("save_checkpoint", save)):
            stack.enter_context(patch.object(trainer, name, value))
        yield
