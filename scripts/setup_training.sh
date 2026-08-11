#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
if ! .venv/bin/python -c 'import torch; raise SystemExit(0 if torch.cuda.is_available() else 1)' 2>/dev/null; then
  TORCH_ARGS=(--upgrade --force-reinstall torch torchvision)
  [[ -z "${PYTORCH_INDEX_URL:-}" ]] || TORCH_ARGS+=(--index-url "$PYTORCH_INDEX_URL")
  .venv/bin/python -m pip install "${TORCH_ARGS[@]}"
fi
.venv/bin/python -m pip install -r requirements-lerobot.txt
scripts/train_smolvla.sh --check-env
