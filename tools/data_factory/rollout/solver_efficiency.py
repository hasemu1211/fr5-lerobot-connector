"""Offline paired SmolVLA solver experiment; never produces an executable plan.

Partial AdaVLA IV-A reproduction, without MLP pruning. LeRobot integrates from
noise t=1 to actions t=0. The bounded final midpoint jump is an explicit local
choice, not a calibrated error tolerance or physical-confidence test.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.metadata
import inspect
import json
import math
import platform
import statistics
import time
from contextlib import contextmanager
from pathlib import Path
from types import FunctionType, MethodType

import torch

METHODS = ("fixed10", "fixed5", "adaptive")
PAPER = "https://arxiv.org/html/2608.29208v1"


def _sync(device):
    if torch.device(device).type == "cuda":
        torch.cuda.synchronize(device)


def tensor_digest(value):
    value = value.detach().cpu().contiguous()
    return hashlib.sha256(value.numpy().tobytes()).hexdigest()


def integrate(denoise, noise, method, *, threshold=.075, max_nfe=20):
    """Return endpoint plus measured solver work; all midpoint probes count as NFE."""
    from lerobot.policies.common.flow_matching import euler_integrate
    if (method not in METHODS or not math.isfinite(threshold) or threshold <= 0
            or type(max_nfe) is not int or not 2 <= max_nfe <= 40 or max_nfe % 2
            or noise.ndim != 3 or noise.shape[0] != 1 or not torch.isfinite(noise).all()):
        raise ValueError("invalid bounded solver configuration/noise")
    nfe, steps = 0, []
    def field(x, t):
        nonlocal nfe
        nfe += 1
        return denoise(x, t)
    x = noise.clone()
    _sync(noise.device)
    started = time.perf_counter()
    with torch.inference_mode():
        if method != "adaptive":
            x = euler_integrate(field, x, 10 if method == "fixed10" else 5)
        else:
            remaining = 1.
            while remaining > 0:
                t = torch.full((1,), remaining, dtype=torch.float32, device=x.device)
                v = field(x, t)
                mid_v = field(x - remaining * .5 * v, t * .5)
                curvature = float((torch.linalg.vector_norm(v - mid_v)
                                   / (torch.linalg.vector_norm(mid_v) + 1e-8)).item())
                if not math.isfinite(curvature):
                    raise ValueError("non-finite internal vector change")
                forced = curvature >= threshold and (remaining <= .1 or nfe >= max_nfe)
                if curvature < threshold or forced:
                    step = remaining
                else:
                    step = min(remaining / 2, max(.1, threshold / curvature * .2))
                x = x - step * mid_v
                steps.append({"t": remaining, "step": step, "relative_vector_change": curvature,
                              "forced_terminal_jump": forced})
                remaining = 0. if step == remaining else remaining - step
    _sync(noise.device)
    elapsed = time.perf_counter() - started
    if x.shape != noise.shape or not torch.isfinite(x).all():
        raise ValueError("invalid solver endpoint")
    return x, {"nfe": nfe, "solver_wall_s": elapsed, "terminal_t": 0., "steps": steps,
               "forced_terminal_jump": any(step["forced_terminal_jump"] for step in steps)}


@contextmanager
def offline_solver(model, solver):
    """Clone only this offline instance's sample_actions globals; restore on error.

    Installed files and module globals are never patched. The native model must
    be privately owned by the offline experiment for the duration of this call.
    """
    from lerobot.policies.smolvla.modeling_smolvla import VLAFlowMatching
    original = model.sample_actions
    if original.__func__ is not VLAFlowMatching.sample_actions:
        raise ValueError("unsupported sample_actions implementation")
    function = original.__func__
    replacement = FunctionType(function.__code__, {**function.__globals__, "euler_integrate": solver},
                               function.__name__, function.__defaults__, function.__closure__)
    replacement.__kwdefaults__ = function.__kwdefaults__
    owned = "sample_actions" in vars(model)
    previous = vars(model).get("sample_actions")
    model.sample_actions = MethodType(replacement, model)
    try:
        yield
    finally:
        if owned:
            model.sample_actions = previous
        else:
            delattr(model, "sample_actions")


def _native_trial(native, observation, noise, method, settings):
    from tools.data_factory.learned_action_adapter import _action, _rgb
    evidence = []
    def solver(denoise, supplied_noise, _num_steps, **kwargs):
        if kwargs.get("rtc_enabled") or kwargs.get("rtc_processor") is not None:
            raise ValueError("RTC is outside this offline experiment")
        endpoint, measured = integrate(denoise, supplied_noise, method, **settings)
        evidence.append(measured)
        return endpoint
    # Instrumentation setup and output transfer are outside chunk latency.
    with offline_solver(native.policy.model, solver):
        _sync(noise.device)
        started = time.perf_counter()
        native.policy.reset()
        native.preprocessor.reset()
        native.postprocessor.reset()
        batch = {"observation.state": torch.tensor(_action(observation["observation.state"]), dtype=torch.float32),
                 "task": observation["task"]}
        for key in ("observation.images.camera1", "observation.images.camera2"):
            frame = _rgb(observation[key])
            batch[key] = torch.frombuffer(bytearray(frame["data"]), dtype=torch.uint8).reshape(
                frame["shape"]).permute(2, 0, 1).float() / 255
        with torch.inference_mode():
            actions = native.postprocessor(native.policy.predict_action_chunk(native.preprocessor(batch), noise=noise.clone()))
        _sync(noise.device)
        elapsed = time.perf_counter() - started
    if len(evidence) != 1 or actions.ndim != 3 or actions.shape[0] != 1 or actions.shape[-1] != 7:
        raise ValueError("native chunk/solver invocation contract")
    if not torch.isfinite(actions).all():
        raise ValueError("non-finite native actions")
    return actions.detach().cpu(), evidence[0], elapsed


def compare_trials(trial, shape, *, seeds=(0, 1, 2), repeats=3, warmups=1, device="cpu"):
    """Pair each method within each seed/repeat, rotating order to reduce order bias."""
    if (not 1 <= len(seeds) <= 20 or len(set(seeds)) != len(seeds)
            or any(type(seed) is not int or not 0 <= seed < 2**32 for seed in seeds)
            or type(repeats) is not int or not 1 <= repeats <= 20
            or type(warmups) is not int or not 0 <= warmups <= 3):
        raise ValueError("invalid trial bounds")
    rows = []
    for seed_index, seed in enumerate(seeds):
        noise = torch.randn(shape, generator=torch.Generator(device="cpu").manual_seed(seed)).to(device)
        if seed_index == 0:
            for method in METHODS:
                for _ in range(warmups):
                    trial(noise.clone(), method)
        for repeat in range(repeats):
            offset = (seed_index * repeats + repeat) % len(METHODS)
            order = METHODS[offset:] + METHODS[:offset]
            measured = {method: trial(noise.clone(), method) for method in order}
            reference = measured["fixed10"][0]
            for method in METHODS:
                actions, metrics, total = measured[method]
                delta = (actions - reference).double().flatten(0, -2)
                rows.append({"seed": seed, "repeat": repeat, "order": list(order), "method": method,
                             "noise_sha256": tensor_digest(noise), "action_sha256": tensor_digest(actions),
                             "action_shape": list(actions.shape), "total_wall_s": total, **metrics,
                             "rmse_per_dimension_to_fixed10": delta.square().mean(0).sqrt().tolist(),
                             "max_abs_per_dimension_to_fixed10": delta.abs().amax(0).tolist()})
    summary = {}
    for method in METHODS:
        own = [row for row in rows if row["method"] == method]
        times = sorted(row["total_wall_s"] for row in own)
        summary[method] = {"median_total_wall_s": statistics.median(times),
                           "p95_total_wall_s": times[math.ceil(.95 * len(times)) - 1],
                           "median_solver_wall_s": statistics.median(row["solver_wall_s"] for row in own),
                           "median_nfe": statistics.median(row["nfe"] for row in own),
                           "forced_terminal_trials": sum(row["forced_terminal_jump"] for row in own)}
    return {"seeds": list(seeds), "repeats": repeats, "warmups_per_method": warmups,
            "noise_shape": list(shape), "noise_dtype": "float32", "rows": rows, "summary": summary}


def compare_native(native, observation, **options):
    """Consume an already canonically admitted local model; no live execution API."""
    from tools.data_factory.training_receipts import tree_digest
    if not native._inference_lock.acquire(blocking=False):
        raise ValueError("native model is already in use")
    try:
        if tree_digest(native.policy_dir) != native.checkpoint["tree_digest"]:
            raise ValueError("checkpoint changed")
        config = native.policy.config
        if config.rtc_config is not None or config.adapt_to_pi_aloha:
            raise ValueError("unsupported control configuration")
        if config.chunk_size > 50 or config.max_action_dim > 32:
            raise ValueError("model exceeds the bounded offline experiment shape")
        observation = copy.deepcopy(observation)
        settings = {key: options.pop(key) for key in ("threshold", "max_nfe") if key in options}
        device = str(config.device)
        result = compare_trials(lambda noise, method: _native_trial(native, observation, noise, method, settings),
                                (1, config.chunk_size, config.max_action_dim), device=device, **options)
        if tree_digest(native.policy_dir) != native.checkpoint["tree_digest"]:
            raise ValueError("checkpoint changed during experiment")
        result.update(measurement_scope="native_chunk_preprocess_prefix_solver_postprocess",
                      checkpoint=copy.deepcopy(native.checkpoint), device=device,
                      saved_num_steps=config.num_steps, chunk_size=config.chunk_size,
                      max_action_dim=config.max_action_dim, use_cache=config.use_cache,
                      action_units=["rad"] * 6 + ["m"])
        if hasattr(native.policy, "parameters"):
            result["parameter_dtypes"] = sorted({str(p.dtype) for p in native.policy.parameters()})
        return result
    finally:
        native._inference_lock.release()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--synthetic", action="store_true")
    mode.add_argument("--checkpoint", type=Path)
    parser.add_argument("--observation", type=Path, help="offline JSON: state, task, camera1/2 RGB frames with data_hex")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--seeds", default="0,1,2")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--threshold", type=float, default=.075)
    parser.add_argument("--max-nfe", type=int, default=20)
    args = parser.parse_args(argv)
    options = dict(seeds=tuple(int(s) for s in args.seeds.split(",")), repeats=args.repeats, warmups=args.warmups)
    settings = dict(threshold=args.threshold, max_nfe=args.max_nfe)
    report = {"experiment": "offline_solver_efficiency", "paper": PAPER,
              "reproduction": "IV-A only; no MLP pruning; bounded forced terminal midpoint jump",
              "solver_settings": {**settings, "base_step": .1, "epsilon": 1e-8, "time_direction": "1_to_0"},
              "reference": "fixed10 numerical endpoint, not ground truth or task success",
              "physical_qualification": "NOT_MEASURED", "task_success": "NOT_MEASURED",
              "environment": {"python": platform.python_version(), "torch": torch.__version__,
                              "lerobot": importlib.metadata.version("lerobot"), "cpu_threads": torch.get_num_threads(),
                              "deterministic_algorithms": torch.are_deterministic_algorithms_enabled()}}
    from lerobot.policies.common import flow_matching
    from lerobot.policies.smolvla import modeling_smolvla
    report["source_sha256"] = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest() for name, path in (
        ("experiment", __file__), ("flow_matching", inspect.getfile(flow_matching)),
        ("smolvla", inspect.getfile(modeling_smolvla)))}
    if args.synthetic:
        if args.device != "cpu":
            parser.error("synthetic mode is CPU-only")
        fields = {"constant": lambda x, t: torch.ones_like(x),
                  "linear": lambda x, t: x,
                  "hidden_bend": lambda x, t: torch.ones_like(x) * ((t - .5)**2 * (t - 1)**2).reshape(-1, 1, 1)}
        report["measurement_scope"] = "synthetic_ode_only_not_model_speed"
        report["action_units"] = ["dimensionless"] * 7
        report["cases"] = {}
        for name, field in fields.items():
            def trial(noise, method):
                started = time.perf_counter()
                output, measured = integrate(field, noise, method, **settings)
                return output, measured, time.perf_counter() - started
            report["cases"][name] = compare_trials(trial, (1, 4, 7), **options)
    else:
        if args.observation is None:
            parser.error("--observation required for a native checkpoint")
        from tools.data_factory.learned_action_adapter import NativeSmolVLA
        raw = args.observation.read_bytes()
        observation = json.loads(raw)
        for key in ("observation.images.camera1", "observation.images.camera2"):
            observation[key]["data"] = bytes.fromhex(observation[key].pop("data_hex"))
        report["observation_file_sha256"] = hashlib.sha256(raw).hexdigest()
        started = time.perf_counter()
        native = NativeSmolVLA.load(args.checkpoint, device=args.device)
        report["checkpoint_load_wall_s"] = time.perf_counter() - started
        report["comparison"] = compare_native(native, observation, **options, **settings)
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
