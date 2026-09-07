"""Tests for sync-opencode's stale-model removal path."""

import json
import os
import re
import tempfile
import unittest

from loader.opencode_sync import sync


def _strip_comments(text):
    return re.sub(r"//[^\n]*", "", text)


def _models_of_network(text):
    return json.loads(_strip_comments(text))["provider"]["lmstudio_local_network"]["models"]


SAMPLE = '''{
  // global opencode config
  "provider": {
    "lmstudio_local_network": {
      "models": {
        "gpt-oss-20b": {
          "name": "gpt-oss-20b",
          "limit": { "context": 32768, "input": 32768, "output": 8192 }
        },
        "gone-model": {
          "name": "gone-model",
          "limit": { "context": 8192, "input": 8192, "output": 2048 }
        }
      }
    },
    "lmstudio_localhost": {
      "models": {
        "gpt-oss-20b": {
          "name": "gpt-oss-20b"
        },
        "gone-model": {
          "name": "gone-model"
        }
      }
    },
    "openrouter": {
      "models": {
        "anthropic/claude": { "name": "claude" }
      }
    }
  }
}
'''


class SyncRemovalTest(unittest.TestCase):
    def _write(self, sample):
        path = tempfile.mktemp(suffix=".jsonc")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(sample)
        return path

    def test_removes_stale_models_from_lmstudio_providers_only(self):
        path = self._write(SAMPLE)
        try:
            changed, added, removed = sync(
                path,
                {"gpt-oss-20b": {"contextLength": 32768}},
                remove_missing=["gpt-oss-20b"],
            )
            with open(path, encoding="utf-8") as fh:
                models = _models_of_network(fh.read())
            self.assertNotIn("gone-model", models)
            self.assertIn("gpt-oss-20b", models)
            self.assertEqual(
                set(removed),
                {
                    ("lmstudio_local_network", "gone-model"),
                    ("lmstudio_localhost", "gone-model"),
                },
            )
            self.assertEqual(added, [])
        finally:
            os.unlink(path)

    def test_other_providers_untouched(self):
        path = self._write(SAMPLE)
        try:
            sync(
                path,
                {"gpt-oss-20b": {"contextLength": 32768}},
                remove_missing=["gpt-oss-20b"],
            )
            with open(path, encoding="utf-8") as fh:
                raw = json.loads(_strip_comments(fh.read()))
            self.assertIn("anthropic/claude", raw["provider"]["openrouter"]["models"])
        finally:
            os.unlink(path)

    def test_no_removal_without_remove_missing(self):
        path = self._write(SAMPLE)
        try:
            changed, added, removed = sync(path, {"gpt-oss-20b": {}})
            with open(path, encoding="utf-8") as fh:
                models = _models_of_network(fh.read())
            self.assertIn("gone-model", models)
            self.assertEqual(removed, [])
        finally:
            os.unlink(path)

    def test_removes_last_member_while_adding_another(self):
        sample = '''{
  "provider": {
    "lmstudio_local_network": {
      "models": {
        "doomed": { "name": "doomed" }
      }
    }
  }
}
'''
        path = self._write(sample)
        try:
            changed, added, removed = sync(
                path, {"fresh": {}}, remove_missing=["fresh"]
            )
            with open(path, encoding="utf-8") as fh:
                models = _models_of_network(fh.read())
            self.assertIn("fresh", models)
            self.assertNotIn("doomed", models)
            self.assertNotIn("doomed", json.dumps(models))
        finally:
            os.unlink(path)

    def test_removed_entry_key_absent_from_output(self):
        path = self._write(SAMPLE)
        try:
            sync(path, {"gpt-oss-20b": {}}, remove_missing=["gpt-oss-20b"])
            with open(path, encoding="utf-8") as fh:
                out = fh.read()
            self.assertNotIn("gone-model", out)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
