"""CPU-only tests for the LeRobot/FR5 integration boundary."""

# Test checked-out plugin source without mutating the shared environment with an
# editable install. LeRobot itself remains the repository's pinned dependency.
import sys
from pathlib import Path

_root = Path(__file__).resolve().parents[2]
for _package in ("lerobot_robot_fr5", "lerobot_strategy_fr5"):
    sys.path.insert(0, str(_root / "plugins" / _package / "src"))
