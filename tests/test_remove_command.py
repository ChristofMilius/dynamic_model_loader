"""Tests for the loader's 'remove' command."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loader.core import ConfigStore


def _write_config(path, models, opencode=None):
    cfg = {"models": models}
    if opencode is not None:
        cfg["opencode"] = opencode
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
    return path


class RemoveModelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".json")
        self.tmpdir = os.path.dirname(self.tmp)
        _write_config(
            self.tmp,
            {
                "gpt-oss-20b": {
                    "watch": True,
                    "watchPreset": "default",
                    "presets": {"default": {"contextLength": 32768}},
                },
                "other": {"presets": {"default": {"contextLength": 8192}}},
            },
            opencode={"models": {"gpt-oss-20b": {"vision": True}}},
        )
        self.addCleanup(lambda: os.path.exists(self.tmp) and os.unlink(self.tmp))

    def test_remove_model_drops_entry_and_opencode_override(self):
        cs = ConfigStore(self.tmp)
        self.assertTrue(cs.remove_model("gpt-oss-20b"))
        self.assertNotIn("gpt-oss-20b", cs.data)
        self.assertNotIn("gpt-oss-20b", cs.raw.get("opencode", {}).get("models", {}))
        self.assertIn("other", cs.data)

    def test_remove_model_missing_is_noop(self):
        cs = ConfigStore(self.tmp)
        self.assertFalse(cs.remove_model("not-there"))

    def _app(self):
        with mock.patch("loader.core.LMStudio"):
            from loader.dynamic_model_loader import DynamicModelLoader, CommandDispatcher

            app = DynamicModelLoader.__new__(DynamicModelLoader)
            app.config_store = ConfigStore(self.tmp)
            app.watcher = mock.MagicMock()
            app.dispatcher = CommandDispatcher(app)
            return app

    def test_cmd_remove_with_index_and_no_confirm(self):
        from loader.opencode_sync import sync as real_sync

        app = self._app()
        with mock.patch("loader.dynamic_model_loader.sync") as mock_sync, \
             mock.patch("builtins.input", return_value="n"):
            res = app.cmd_remove(["1"])
        self.assertTrue(res)
        # cancelled -> model stays
        self.assertIn("gpt-oss-20b", app.config_store.data)
        mock_sync.assert_not_called()

    def test_cmd_remove_with_index_confirmed(self):
        app = self._app()
        with mock.patch("loader.dynamic_model_loader.sync", return_value=(0, [], [])) as mock_sync, \
             mock.patch("builtins.input", return_value="y"):
            res = app.cmd_remove(["1"])
        self.assertTrue(res)
        self.assertNotIn("gpt-oss-20b", app.config_store.data)
        # opencode sync ran and pruned the removed model
        self.assertEqual(mock_sync.call_count, 1)

    def test_cmd_remove_list_path(self):
        app = self._app()
        # no index -> menu shown via input(); select 1 then confirm
        with mock.patch("loader.dynamic_model_loader.sync", return_value=(0, [], [])) as mock_sync, \
             mock.patch("builtins.input", side_effect=["1", "y"]):
            res = app.cmd_remove([])
        self.assertTrue(res)
        self.assertNotIn("gpt-oss-20b", app.config_store.data)
        mock_sync.assert_called_once()


if __name__ == "__main__":
    unittest.main()