import unittest
from unittest import mock

from tools.data_factory.curator.review.decision import read_foreground_decision


class DecisionTest(unittest.TestCase):
    def test_foreground_decision_accepts_only_explicit_choice(self):
        with (
            mock.patch(
                "tools.data_factory.curator.review.decision.os.open", return_value=7
            ),
            mock.patch(
                "tools.data_factory.curator.review.decision.os.fstat",
                return_value=mock.Mock(st_mode=0o020000),
            ),
            mock.patch(
                "tools.data_factory.curator.review.decision.os.isatty",
                return_value=True,
            ),
            mock.patch(
                "tools.data_factory.curator.review.decision.os.tcgetpgrp",
                return_value=12,
            ),
            mock.patch(
                "tools.data_factory.curator.review.decision.os.getpgrp", return_value=12
            ),
            mock.patch("tools.data_factory.curator.review.decision.os.write"),
            mock.patch(
                "tools.data_factory.curator.review.decision.os.read",
                side_effect=[bytes([value]) for value in b"APPROVE\n"],
            ),
            mock.patch("tools.data_factory.curator.review.decision.os.close"),
        ):
            self.assertEqual(read_foreground_decision("review.mp4"), "APPROVE")
        with (
            mock.patch(
                "tools.data_factory.curator.review.decision.os.open", return_value=7
            ),
            mock.patch(
                "tools.data_factory.curator.review.decision.os.fstat",
                return_value=mock.Mock(st_mode=0o020000),
            ),
            mock.patch(
                "tools.data_factory.curator.review.decision.os.isatty",
                return_value=True,
            ),
            mock.patch(
                "tools.data_factory.curator.review.decision.os.tcgetpgrp",
                return_value=12,
            ),
            mock.patch(
                "tools.data_factory.curator.review.decision.os.getpgrp", return_value=12
            ),
            mock.patch("tools.data_factory.curator.review.decision.os.write"),
            mock.patch(
                "tools.data_factory.curator.review.decision.os.read",
                side_effect=[bytes([value]) for value in b"maybe\nREJECT\n"],
            ),
            mock.patch("tools.data_factory.curator.review.decision.os.close"),
        ):
            self.assertEqual(read_foreground_decision("review.mp4"), "REJECT")


if __name__ == "__main__":
    unittest.main()
