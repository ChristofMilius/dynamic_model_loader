"""Tests for environment detection and cross-platform runtime adaptation."""

import os
import unittest
from unittest import mock

from loader import runtime


class DetectTest(unittest.TestCase):
    @mock.patch("os.name", "nt")
    def test_windows(self):
        self.assertIs(runtime.detect(), runtime.RuntimeKind.WINDOWS)

    @mock.patch("os.name", "posix")
    @mock.patch("loader.runtime._in_wsl", return_value=True)
    def test_wsl(self, _):
        self.assertIs(runtime.detect(), runtime.RuntimeKind.WSL)

    @mock.patch("os.name", "posix")
    @mock.patch("loader.runtime._in_wsl", return_value=False)
    def test_linux(self, _):
        self.assertIs(runtime.detect(), runtime.RuntimeKind.LINUX)


class InWslTest(unittest.TestCase):
    @mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}, clear=True)
    def test_env_var(self):
        self.assertTrue(runtime._in_wsl())

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch(
        "builtins.open",
        mock.mock_open(read_data="Linux version ... microsoft-standard-WSL2"),
    )
    @mock.patch("os.path.exists", return_value=False)
    def test_proc_version_microsoft(self, *_):
        self.assertTrue(runtime._in_wsl())

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch(
        "builtins.open",
        side_effect=OSError,
    )
    @mock.patch("os.path.exists", return_value=True)
    def test_binfmt_interop(self, *_):
        self.assertTrue(runtime._in_wsl())


class LocalConfigTest(unittest.TestCase):
    @mock.patch("os.name", "nt")
    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch("os.path.expanduser", return_value="C:\\Users\\me")
    def test_windows(self, *_):
        self.assertEqual(
            runtime.local_opencode_config(),
            "C:/Users/me/.config/opencode/opencode.jsonc",
        )

    @mock.patch("os.name", "posix")
    @mock.patch.dict(os.environ, {"HOME": "/home/user"}, clear=True)
    @mock.patch("os.path.expanduser", side_effect=lambda p: p.replace("~", "/home/user"))
    def test_posix_default(self, *_):
        self.assertEqual(
            runtime.local_opencode_config(),
            "/home/user/.config/opencode/opencode.jsonc",
        )

    @mock.patch("os.name", "posix")
    @mock.patch.dict(
        os.environ, {"XDG_CONFIG_HOME": "/custom/xdg", "HOME": "/home/user"}, clear=True
    )
    @mock.patch("os.path.expanduser", side_effect=lambda p: p.replace("~", "/home/user"))
    def test_posix_xdg(self, *_):
        self.assertEqual(
            runtime.local_opencode_config(),
            "/custom/xdg/opencode/opencode.jsonc",
        )


class LmstudioApiHostTest(unittest.TestCase):
    def test_normalize_bare_host(self):
        self.assertEqual(runtime._normalize_host("192.0.2.20"), "192.0.2.20:1234")

    def test_normalize_host_port_passthrough(self):
        self.assertEqual(runtime._normalize_host("localhost:1234"), "localhost:1234")

    def test_normalize_strips_scheme(self):
        self.assertEqual(
            runtime._normalize_host("http://192.0.2.20:7890"), "192.0.2.20:7890"
        )

    @mock.patch.dict(
        os.environ,
        {"LM_BASE_URL": "192.0.2.20", "LMSTUDIO_BASE_URL": ""},
        clear=True,
    )
    def test_env_override_wins(self):
        self.assertEqual(runtime._env_api_host(), "192.0.2.20:1234")

    @mock.patch.dict(
        os.environ,
        {"LM_BASE_URL": "", "LMSTUDIO_BASE_URL": "http://198.51.100.5:9999/v1"},
        clear=True,
    )
    def test_second_env_var(self):
        self.assertEqual(runtime._env_api_host(), "198.51.100.5:9999")

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_no_env(self):
        self.assertIsNone(runtime._env_api_host())

    @mock.patch.dict(os.environ, {"LM_BASE_URL": "lms.lan"}, clear=True)
    @mock.patch("loader.runtime.detect", return_value=runtime.RuntimeKind.WINDOWS)
    def test_host_env_on_windows(self, *_):
        self.assertEqual(runtime.lmstudio_api_host(), "lms.lan:1234")

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch("loader.runtime._env_api_host", return_value=None)
    @mock.patch("loader.runtime.detect", return_value=runtime.RuntimeKind.WINDOWS)
    def test_windows_auto(self, *_):
        self.assertIsNone(runtime.lmstudio_api_host())

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch("loader.runtime._env_api_host", return_value=None)
    @mock.patch("loader.runtime.detect", return_value=runtime.RuntimeKind.LINUX)
    def test_linux_auto(self, *_):
        self.assertIsNone(runtime.lmstudio_api_host())

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch("loader.runtime._env_api_host", return_value=None)
    @mock.patch("loader.runtime.detect", return_value=runtime.RuntimeKind.WSL)
    @mock.patch("loader.runtime.wsl_networking", return_value="mirrored")
    def test_wsl_mirrored(self, *_):
        self.assertEqual(runtime.lmstudio_api_host(), "127.0.0.1:1234")

    @mock.patch.dict(os.environ, {}, clear=True)
    @mock.patch("loader.runtime._env_api_host", return_value=None)
    @mock.patch("loader.runtime.detect", return_value=runtime.RuntimeKind.WSL)
    @mock.patch("loader.runtime.wsl_networking", return_value="nat")
    @mock.patch("loader.runtime._wsl_gateway_host", return_value="192.0.2.1:1234")
    def test_wsl_nat_gateway(self, *_):
        self.assertEqual(runtime.lmstudio_api_host(), "192.0.2.1:1234")


class WslGatewayTest(unittest.TestCase):
    @mock.patch(
        "subprocess.run",
        return_value=mock.Mock(
            returncode=0,
            stdout=b"default via 198.51.100.1 dev eth0 proto kernel\n",
        ),
    )
    def test_parses_gateway(self, *_):
        self.assertEqual(runtime._wsl_gateway_host(), "198.51.100.1:1234")

    @mock.patch("subprocess.run", side_effect=Exception("boom"))
    def test_failure_returns_none(self, _):
        self.assertIsNone(runtime._wsl_gateway_host())


if __name__ == "__main__":
    unittest.main()
