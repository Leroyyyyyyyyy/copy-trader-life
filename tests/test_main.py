import argparse
import os
import unittest
from unittest import mock

from src.main import resolve_dry_run


class DryRunInterlockTests(unittest.TestCase):
    def args(self, **kwargs):
        values = {"live": False, "paper": False}
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_config_cannot_enable_live_trading(self):
        with mock.patch.dict(os.environ, {"LIVE_TRADING": "1"}, clear=False):
            self.assertTrue(resolve_dry_run({"dry_run": False}, self.args()))

    def test_live_flag_and_environment_are_both_required(self):
        with mock.patch.dict(os.environ, {"LIVE_TRADING": "0"}, clear=False):
            self.assertTrue(resolve_dry_run({"dry_run": False}, self.args(live=True)))
        with mock.patch.dict(os.environ, {"LIVE_TRADING": "1"}, clear=False):
            self.assertFalse(resolve_dry_run({"dry_run": True}, self.args(live=True)))

    def test_paper_flag_overrides_live_interlock(self):
        with mock.patch.dict(os.environ, {"LIVE_TRADING": "1"}, clear=False):
            self.assertTrue(resolve_dry_run({"dry_run": False}, self.args(live=True, paper=True)))


if __name__ == "__main__":
    unittest.main()
