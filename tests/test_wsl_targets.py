"""Tests for WSL opencode target discovery and transport."""

import os
import tempfile
import unittest
from unittest import mock

from loader import wsl_targets as wsl


def _proc(returncode=0, stdout=b"", stderr=b""):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class DecodeTest(unittest.TestCase):
    def test_utf8(self):
        self.assertEqual(wsl._decode(b"Ubuntu-26.04"), "Ubuntu-26.04")

    def test_utf16_le_with_nuls(self):
        raw = "Ubuntu-26.04\r\n".encode("utf-16-le")
        self.assertEqual(wsl._decode(raw), "Ubuntu-26.04\r\n")

    def test_empty(self):
        self.assertEqual(wsl._decode(b""), "")


class ListDistrosTest(unittest.TestCase):
    @mock.patch("loader.wsl_targets.run_wsl")
    def test_quiet_utf8(self, run):
        run.return_value = _proc(stdout="Ubuntu-26.04\ndocker-desktop\n".encode())
        self.assertEqual(wsl.list_distros(), ["Ubuntu-26.04", "docker-desktop"])

    @mock.patch("loader.wsl_targets.run_wsl")
    def test_utf16_lines(self, run):
        run.return_value = _proc(stdout="Ubuntu-26.04\r\ndocker-desktop\r\n".encode("utf-16-le"))
        self.assertEqual(wsl.list_distros(), ["Ubuntu-26.04", "docker-desktop"])

    @mock.patch("loader.wsl_targets.run_wsl")
    def test_failure_returns_empty(self, run):
        run.return_value = _proc(returncode=1, stderr=b"no distributions")
        self.assertEqual(wsl.list_distros(), [])

    @mock.patch("loader.wsl_targets.wsl_available", return_value=None)
    def test_no_wsl_binary(self, _avail):
        self.assertEqual(wsl.list_distros(), [])


class DiscoverTest(unittest.TestCase):
    @mock.patch("loader.wsl_targets._is_file", return_value=True)
    @mock.patch("loader.wsl_targets.networking_mode", return_value="mirrored")
    @mock.patch("loader.wsl_targets.distro_home", return_value="/home/wsluser")
    @mock.patch("loader.wsl_targets.list_distros", return_value=["Ubuntu-26.04"])
    def test_discover(self, *_):
        targets = wsl.discover()
        self.assertEqual(len(targets), 1)
        t = targets[0]
        self.assertEqual(t.distro, "Ubuntu-26.04")
        self.assertEqual(t.home, "/home/wsluser")
        self.assertEqual(
            t.linux_config, "/home/wsluser/.config/opencode/opencode.jsonc"
        )
        self.assertEqual(
            t.unc_config,
            "\\\\wsl$\\Ubuntu-26.04\\home\\wsluser\\.config\\opencode\\opencode.jsonc",
        )
        self.assertTrue(t.config_exists)

    @mock.patch("loader.wsl_targets.distro_home", return_value=None)
    @mock.patch("loader.wsl_targets.list_distros", return_value=["Unresolvable"])
    def test_discover_skips_unresolvable_home(self, *_):
        self.assertEqual(wsl.discover(), [])

    @mock.patch("loader.wsl_targets.distro_home", return_value="/root")
    @mock.patch("loader.wsl_targets.list_distros", return_value=["docker-desktop", "docker-desktop-data"])
    def test_discover_skips_infra_distros(self, *_):
        self.assertEqual(wsl.discover(), [])

    @mock.patch("loader.wsl_targets.wsl_available", return_value=None)
    def test_discover_without_wsl(self, _avail):
        self.assertEqual(wsl.discover(), [])

    def test_linux_to_unc(self):
        self.assertEqual(
            wsl.linux_to_unc("Ubuntu-26.04", "/home/wsluser/.config/opencode/opencode.jsonc"),
            "\\\\wsl$\\Ubuntu-26.04\\home\\wsluser\\.config\\opencode\\opencode.jsonc",
        )


class TransportTest(unittest.TestCase):
    def _target(self, unc_path):
        return wsl.WslTarget(
            "Ubuntu", "/home/u", "/home/u/.config/opencode/opencode.jsonc",
            unc_path, "mirrored", True,
        )

    def test_read_write_config_unc(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "opencode.jsonc")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("hello")
            target = self._target(path)
            self.assertEqual(wsl.read_config(target), "hello")
            wsl.write_config(target, "world")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), "world")

    @mock.patch("loader.wsl_targets.run_wsl", return_value=_proc(stdout=b'{"x":1}\n'))
    def test_read_config_via_wsl(self, run):
        target = self._target("\\\\wsl$\\Ubuntu\\home\\u\\.config\\opencode\\opencode.jsonc")
        self.assertEqual(wsl.read_config_via_wsl(target), '{"x":1}\n')
        args = run.call_args.args[0]
        self.assertIn("--distribution", args)
        self.assertEqual(args[4], "-c")
        self.assertIn("cat", args[5])
        self.assertIn("/home/u/.config/opencode/opencode.jsonc", args[5])

    @mock.patch("loader.wsl_targets.run_wsl", return_value=_proc())
    def test_write_config_via_wsl(self, run):
        target = self._target("\\\\wsl$\\Ubuntu\\home\\u\\.config\\opencode\\opencode.jsonc")
        wsl.write_config_via_wsl(target, "text")
        args = run.call_args.args[0]
        self.assertIn("--distribution", args)
        self.assertEqual(args[4], "-c")
        self.assertIn("mkdir -p", args[5])
        self.assertIn("cat >", args[5])
        self.assertEqual(run.call_args.kwargs["input_bytes"], b"text")

    @mock.patch("loader.wsl_targets.run_wsl", return_value=_proc(returncode=3, stderr=b"boom"))
    def test_read_config_via_wsl_failure_raises(self, _run):
        target = self._target("\\\\wsl$\\Ubuntu\\home\\u\\.config\\opencode\\opencode.jsonc")
        with self.assertRaises(OSError):
            wsl.read_config_via_wsl(target)

    def test_shq_escapes_quotes(self):
        self.assertEqual(wsl._shq("/home/u/.config"), "'/home/u/.config'")
        self.assertEqual(
            wsl._shq("/home/u/it's"),
            "'/home/u/it'\\''s'",
        )


class SyncWslTargetTest(unittest.TestCase):
    def test_sync_writes_model_restrictions_over_unc(self):
        from loader import dynamic_model_loader as dml

        tmp = tempfile.mktemp(suffix=".jsonc")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write('{"provider": {"lmstudio_local_network": {"models": {}}}}')
        target = wsl.WslTarget(
            "Ubuntu", "/h", "/h/.config/opencode/opencode.jsonc", tmp, "mirrored", True
        )
        try:
            changed, added, removed, err = dml._sync_wsl_target(
                target, {"gpt-oss-20b": {"contextLength": 32768}}, {}, ["gpt-oss-20b"]
            )
            self.assertIsNone(err)
            self.assertGreaterEqual(changed, 1)
            expected = {
                ("lmstudio_local_network", "gpt-oss-20b"),
            }
            self.assertLessEqual(set(added), expected)
            with open(tmp, encoding="utf-8") as fh:
                out = fh.read()
            self.assertIn("gpt-oss-20b", out)
            self.assertIn('"context": 32768', out)
        finally:
            os.unlink(tmp)

    def test_sync_falls_back_to_wsl_transport(self):
        from loader import dynamic_model_loader as dml

        target = wsl.WslTarget(
            "Ubuntu", "/h", "/h/.config/opencode/opencode.jsonc",
            "\\\\wsl$\\Ubuntu\\home\\h\\.config\\opencode\\opencode.jsonc", "mirrored", True
        )
        original = '{"provider": {"lmstudio_localhost": {"models": {}}}}'
        with mock.patch("loader.dynamic_model_loader.sync", side_effect=[OSError("UNC down"), (1, [], [])]) as mock_sync, \
             mock.patch("loader.dynamic_model_loader.wsl.read_config_via_wsl", return_value=original) as mock_read, \
             mock.patch("loader.dynamic_model_loader.wsl.write_config_via_wsl") as mock_write:
            changed, added, removed, err = dml._sync_wsl_target(
                target, {"gpt-oss-20b": {"contextLength": 32768}}, {}, ["gpt-oss-20b"]
            )
        # first UNC attempt failed, the in-memory fallback succeeded
        self.assertIsNone(err)
        mock_read.assert_called_once_with(target)
        mock_write.assert_called_once()
        self.assertEqual(mock_sync.call_count, 2)


if __name__ == "__main__":
    unittest.main()