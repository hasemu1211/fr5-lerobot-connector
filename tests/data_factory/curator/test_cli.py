from contextlib import redirect_stderr
import io, json, unittest
from tools.data_factory.curator.cli import main

class CliTest(unittest.TestCase):
    def test_only_v12_commands_and_stable_error(self):
        parser = __import__("tools.data_factory.curator.cli", fromlist=["_parser"])._parser()
        self.assertEqual(set(parser._subparsers._group_actions[0].choices), {"prepare","status","decide"})
        stderr=io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit): main(["derive"])
        self.assertEqual(json.loads(stderr.getvalue())["reason_code"], "CLI_ARGUMENTS")

if __name__ == "__main__": unittest.main()
