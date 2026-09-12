import hashlib
from dataclasses import dataclass
from importlib.metadata import version

import torch


LEROBOT_VERSION = "0.6.1"


def _tensor_sha256(value: torch.Tensor) -> str:
    cpu = value.detach().cpu().contiguous()
    return hashlib.sha256(cpu.numpy().tobytes()).hexdigest()


@dataclass(frozen=True)
class CapturedPolicyChunk:
    sequence: int
    raw: torch.Tensor
    raw_sha256: str


class ExactSmolVLAChunkTap:
    """Read-only tap on the exact chunk consumed by LeRobot 0.6.1 Sync."""

    def __init__(
        self,
        *,
        policy,
        on_chunk,
        action_dim: int = 7,
    ):
        installed = version("lerobot")
        if installed != LEROBOT_VERSION:
            raise RuntimeError(
                f"LEROBOT_CHUNK_TAP_UNAUDITED: {installed}"
            )

        if getattr(policy, "name", None) != "smolvla":
            raise RuntimeError("FR5_CHUNK_TAP_REQUIRES_SMOLVLA")

        if not callable(getattr(policy, "_get_action_chunk", None)):
            raise RuntimeError("FR5_CHUNK_TAP_SEAM_MISSING")

        self.policy = policy
        self.on_chunk = on_chunk
        self.action_dim = int(action_dim)

        self._original = None
        self._sequence = 0

    @property
    def installed(self) -> bool:
        return self._original is not None

    def install(self) -> None:
        if self.installed:
            raise RuntimeError("FR5_CHUNK_TAP_ALREADY_INSTALLED")

        original = self.policy._get_action_chunk
        self._original = original

        def wrapped(batch, *args, **kwargs):
            raw = original(batch, *args, **kwargs)

            if (
                not isinstance(raw, torch.Tensor)
                or raw.ndim != 3
                or raw.shape[0] != 1
                or raw.shape[-1] != self.action_dim
            ):
                raise RuntimeError(
                    f"FR5_CHUNK_SHAPE: {getattr(raw, 'shape', None)}"
                )

            expected = int(self.policy.config.n_action_steps)
            if raw.shape[1] != expected:
                raise RuntimeError(
                    f"FR5_CHUNK_LENGTH: {raw.shape[1]} != {expected}"
                )

            raw_copy = raw.detach().clone()

            self._sequence += 1
            self.on_chunk(
                CapturedPolicyChunk(
                    sequence=self._sequence,
                    raw=raw_copy,
                    raw_sha256=_tensor_sha256(raw_copy),
                )
            )

            # Critical: return the untouched original tensor.
            return raw

        self.policy._get_action_chunk = wrapped

    def remove(self) -> None:
        if self._original is None:
            return

        self.policy._get_action_chunk = self._original
        self._original = None
