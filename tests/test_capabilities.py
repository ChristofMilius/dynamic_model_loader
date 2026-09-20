"""Tests for probing model parameters and the reasoning-effort default."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loader.capabilities import (
    DEFAULT_REASONING_EFFORT,
    available_parameters,
    default_reasoning_effort,
)
from loader.core import ConfigStore

V1_LEG = {
    "ok": True,
    "source": "http://lms:1234/api/v1/models",
    "vision": True,
    "tool_use": True,
    "reasoning": {
        "allowed_options": ["off", "low", "medium", "xhigh", "on"],
        "default": "xhigh",
    },
    "max_context": 262144,
    "type": "llm",
    "publisher": "qwen",
    "architecture": "qwen35",
    "format": "gguf",
    "params_string": "27B",
    "display_name": "Qwen3.8 27B",
    "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
    "size_bytes": 123456789,
    "variants": ["qwen/qwen3.8-27b@q4_k_m"],
    "selected_variant": "qwen/qwen3.8-27b@q4_k_m",
}

SDK_LEG = {
    "ok": True,
    "source": "lmstudio-sdk:list_downloaded",
    "vision": True,
    "tool_use": True,
    "reasoning": None,
    "max_context": 262144,
    "type": "llm",
    "architecture": "qwen35",
    "params_string": "27B",
}

DEAD_LEG = {"ok": False, "error": "no bases"}


def _probe(sdk=DEAD_LEG, v1=DEAD_LEG, v0=DEAD_LEG):
    return {
        "lmstudio_sdk": sdk,
        "lmstudio_api_v1": v1,
        "lmstudio_api_v0": v0,
        "openai_compat": DEAD_LEG,
        "merged": {"vision": True, "tool_use": True, "max_context": 262144},
    }


class AvailableParametersTest(unittest.TestCase):
    def test_collects_full_parameter_set_from_v1_leg(self):
        params = available_parameters(_probe(v1=V1_LEG))
        self.assertEqual(params["vision"], True)
        self.assertEqual(params["toolUse"], True)
        self.assertEqual(params["maxContextLength"], 262144)
        self.assertEqual(params["type"], "llm")
        self.assertEqual(params["publisher"], "qwen")
        self.assertEqual(params["architecture"], "qwen35")
        self.assertEqual(params["quantization"]["name"], "Q4_K_M")
        self.assertEqual(params["paramsString"], "27B")

    def test_reasoning_normalized_to_camelcase_options(self):
        params = available_parameters(_probe(v1=V1_LEG))
        self.assertEqual(
            params["reasoning"],
            {
                "allowedOptions": ["off", "low", "medium", "xhigh", "on"],
                "default": "xhigh",
            },
        )

    def test_reasoning_bool_passes_through(self):
        leg = dict(V1_LEG, reasoning=True)
        self.assertEqual(available_parameters(_probe(v1=leg))["reasoning"], True)

    def test_falls_back_to_sdk_leg(self):
        params = available_parameters(_probe(sdk=SDK_LEG))
        self.assertEqual(params["architecture"], "qwen35")
        self.assertIsNone(params.get("publisher"))

    def test_none_when_no_leg_probed(self):
        self.assertIsNone(available_parameters(_probe()))

    def test_default_effort_is_medium(self):
        self.assertEqual(DEFAULT_REASONING_EFFORT, "medium")

    def test_binary_off_on_capability_defaults_effort_to_none(self):
        for allowed in (["off", "on"], ("off", "on"), ["off", "on", "on"]):
            self.assertEqual(
                default_reasoning_effort({"allowedOptions": allowed, "default": "on"}),
                "none",
            )

    def test_effort_level_capability_defaults_effort_to_medium(self):
        cap = {"allowedOptions": ["off", "low", "medium", "xhigh", "on"], "default": "xhigh"}
        self.assertEqual(default_reasoning_effort(cap), "medium")

    def test_bool_capability_defaults_effort_to_medium(self):
        self.assertEqual(default_reasoning_effort(True), "medium")

    def test_capability_without_options_defaults_effort_to_medium(self):
        self.assertEqual(default_reasoning_effort({"default": "on"}), "medium")
        self.assertEqual(default_reasoning_effort({}), "medium")


class ImportMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mktemp(suffix=".json")
        with open(self.tmp, "w", encoding="utf-8") as fh:
            json.dump({"models": {}}, fh)
        self.addCleanup(lambda: os.path.exists(self.tmp) and os.unlink(self.tmp))

    def _app(self):
        from loader.dynamic_model_loader import CommandDispatcher, DynamicModelLoader

        app = DynamicModelLoader.__new__(DynamicModelLoader)
        app.config_store = ConfigStore(self.tmp)
        app.watcher = mock.MagicMock()
        app.dispatcher = CommandDispatcher(app)
        app.lmstudio = mock.MagicMock()
        app.lmstudio.list_loaded.return_value = [
            {"identifier": "qwen/qwen3.8-27b:1", "config": {"contextLength": 65536}}
        ]
        return app

    def test_cmd_import_merges_params_and_defaults_reasoning_effort(self):
        app = self._app()
        probe = _probe(v1=V1_LEG)
        with mock.patch("loader.capabilities.probe_all", return_value=probe), \
             mock.patch("builtins.input", side_effect=["1", "", "n"]):
            res = app.cmd_import([])
        self.assertTrue(res)
        cfg = app.config_store._preset_config("qwen/qwen3.8-27b", "imported")
        self.assertEqual(cfg["reasoningEffort"], "medium")
        self.assertEqual(cfg["contextLength"], 65536)
        self.assertEqual(cfg["vision"], True)
        self.assertEqual(cfg["reasoning"]["allowedOptions"], ["off", "low", "medium", "xhigh", "on"])

    def test_cmd_import_no_reasoning_means_no_effort(self):
        app = self._app()
        leg = dict(V1_LEG, reasoning=None)
        probe = _probe(v1=leg)
        with mock.patch("loader.capabilities.probe_all", return_value=probe), \
             mock.patch("builtins.input", side_effect=["1", "", "n"]):
            res = app.cmd_import([])
        self.assertTrue(res)
        cfg = app.config_store._preset_config("qwen/qwen3.8-27b", "imported")
        self.assertNotIn("reasoningEffort", cfg)
        self.assertNotIn("reasoning", cfg)

    def test_cmd_import_binary_reasoning_defaults_effort_to_none(self):
        app = self._app()
        probe = _probe(v1=dict(V1_LEG, reasoning={"allowed_options": ["off", "on"], "default": "on"}))
        with mock.patch("loader.capabilities.probe_all", return_value=probe), \
             mock.patch("builtins.input", side_effect=["1", "", "n"]):
            res = app.cmd_import([])
        self.assertTrue(res)
        cfg = app.config_store._preset_config("qwen/qwen3.8-27b", "imported")
        self.assertEqual(cfg["reasoningEffort"], "none")
        self.assertEqual(cfg["reasoning"]["allowedOptions"], ["off", "on"])


if __name__ == "__main__":
    unittest.main()