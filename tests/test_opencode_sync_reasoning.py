"""Tests for sync-opencode writing per-model reasoningEffort options."""

import json
import os
import re
import tempfile
import unittest

from loader.opencode_sync import sync


def _strip_comments(text):
    return re.sub(r"//[^\n]*", "", text)


def _entry_text(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    return text


def _models_of_network(text):
    return json.loads(_strip_comments(text))["provider"]["lmstudio_local_network"]["models"]


BINARY_CAP = {"allowedOptions": ["off", "on"], "default": "on"}
EFFORT_CAP = {"allowedOptions": ["off", "low", "medium", "xhigh", "on"], "default": "xhigh"}

PRESENT = '''{
  "provider": {
    "lmstudio_local_network": {
      "models": {
        "google/gemma-4-12b-qat": {
          "name": "gemma-4-12b-qat",
          "reasoning": true,
          "limit": { "context": 65536, "input": 65536, "output": 16384 }
        }
      }
    }
  }
}
'''

ABSENT = '''{
  "provider": {
    "lmstudio_local_network": {
      "models": {}
    }
  }
}
'''


class ReasoningOptionsSyncTest(unittest.TestCase):
    def _sync(self, sample, model_key, desired, overrides=None):
        path = tempfile.mktemp(suffix=".jsonc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(sample)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        sync(path, {model_key: desired}, {"google/gemma-4-12b-qat": overrides} if overrides else None)
        return path

    def test_binary_reasoning_model_gets_none(self):
        path = self._sync(
            PRESENT,
            "google/gemma-4-12b-qat",
            {"contextLength": 65536, "reasoning": BINARY_CAP, "reasoningEffort": "medium"},
        )
        entry = _models_of_network(_entry_text(path))["google/gemma-4-12b-qat"]
        self.assertEqual(entry["options"], {"reasoningEffort": "none"})

    def test_effort_level_model_keeps_preset_effort(self):
        path = self._sync(
            PRESENT,
            "google/gemma-4-12b-qat",
            {"contextLength": 65536, "reasoning": EFFORT_CAP, "reasoningEffort": "high"},
        )
        entry = _models_of_network(_entry_text(path))["google/gemma-4-12b-qat"]
        self.assertEqual(entry["options"], {"reasoningEffort": "high"})

    def test_effort_level_model_without_preset_effort_defaults_to_medium(self):
        path = self._sync(
            PRESENT,
            "google/gemma-4-12b-qat",
            {"contextLength": 65536, "reasoning": EFFORT_CAP},
        )
        entry = _models_of_network(_entry_text(path))["google/gemma-4-12b-qat"]
        self.assertEqual(entry["options"], {"reasoningEffort": "medium"})

    def test_opencode_override_wins_over_binary_none(self):
        path = self._sync(
            PRESENT,
            "google/gemma-4-12b-qat",
            {"contextLength": 65536, "reasoning": BINARY_CAP},
            {"reasoningEffort": "high"},
        )
        entry = _models_of_network(_entry_text(path))["google/gemma-4-12b-qat"]
        self.assertEqual(entry["options"], {"reasoningEffort": "high"})

    def test_no_options_for_non_reasoning_model(self):
        path = self._sync(
            PRESENT,
            "google/gemma-4-12b-qat",
            {"contextLength": 65536},
        )
        entry = _models_of_network(_entry_text(path))["google/gemma-4-12b-qat"]
        self.assertNotIn("options", entry)

    def test_capability_in_preset_flags_model_as_reasoning(self):
        path = self._sync(
            ABSENT,
            "google/gemma-4-12b-qat",
            {"contextLength": 65536, "reasoning": BINARY_CAP},
        )
        entry = _models_of_network(_entry_text(path))["google/gemma-4-12b-qat"]
        self.assertEqual(entry["reasoning"], True)
        self.assertEqual(entry["options"], {"reasoningEffort": "none"})

    def test_stale_medium_on_binary_model_is_corrected_to_none(self):
        path = self._sync(
            PRESENT,
            "google/gemma-4-12b-qat",
            {"contextLength": 65536, "reasoning": BINARY_CAP, "reasoningEffort": "medium"},
            {"reasoning": True},
        )
        entry = _models_of_network(_entry_text(path))["google/gemma-4-12b-qat"]
        self.assertEqual(entry["options"], {"reasoningEffort": "none"})


if __name__ == "__main__":
    unittest.main()