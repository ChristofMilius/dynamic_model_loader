"""Detect the environment the loader is running in and adapt.

The loader was architected Windows-first, but the underlying components (the
``lmstudio`` SDK, the uv-managed dependency tree, and the opencode config) are
portable. This module answers the three environment questions the rest of the
code needs:

* :meth:`detect` — which runtime are we in (Windows, WSL, or a plain Linux)?
* :func:`local_opencode_config` — where is *this* machine's opencode global
  config? (WSL distros are only ever targets of the *Windows* loader; when the
  loader itself runs in WSL it manages the distro's own config.)
* :func:`lmstudio_api_host` — which ``host:port`` should the LM Studio SDK
  talk to? The SDK auto-discovers localhost only when given ``None``; inside
  WSL we must point it at the Windows host explicitly.
"""

import os
import re
import subprocess
from enum import Enum


class RuntimeKind(Enum):
    """Which operating environment the loader is running in."""

    WINDOWS = "windows"
    WSL = "wsl"
    LINUX = "linux"


def _in_wsl():
    """Best-effort detection of a WSL (not native Linux) environment."""
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        with open("/proc/version", "r", encoding="utf-8", errors="replace") as fh:
            if "microsoft" in fh.read().lower():
                return True
    except OSError:
        pass
    return os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")


def detect():
    """Return the :class:`RuntimeKind` for the current process; never raises."""
    if os.name == "nt":
        return RuntimeKind.WINDOWS
    return RuntimeKind.WSL if _in_wsl() else RuntimeKind.LINUX


def local_opencode_config():
    """Absolute path to *this* machine's opencode global config.

    Follows opencode's XDG convention on POSIX and the same `~/.config` path on
    Windows. Paths are built with ``/`` separators without using
    ``os.path.join`` (which would adopt the host platform's separator, not the
    runtime's). Never raises.
    """
    if os.name == "nt":
        home = os.path.expanduser("~")
        return home.replace("\\", "/") + "/.config/opencode/opencode.jsonc"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = (xdg if xdg else os.path.expanduser("~/.config")).replace("\\", "/")
    return base.rstrip("/") + "/opencode/opencode.jsonc"


def _normalize_host(host):
    """Fill in a scheme/port for a bare host:port override.

    ``192.0.2.20`` -> ``192.0.2.20:1234`` (no scheme needed by the SDK,
    which expects ``host:port``). ``localhost:1234`` passes through unchanged.
    Returns the raw value unchanged if it already looks like ``host:port``.
    """
    host = (host or "").strip()
    if not host:
        return None
    # strip scheme if a user pasted a full URL
    host = re.sub(r"^https?://", "", host)
    # drop a trailing OpenAI-compat path segment, if any
    host = re.sub(r"/v1$", "", host)
    if ":" in host:
        return host
    return f"{host}:1234"


def _env_api_host():
    """Respect the LM Studio endpoint override, or None when unset.

    Uses the same ``LM_BASE_URL`` / ``LMSTUDIO_BASE_URL`` variables that
    ``capabilities`` already honors, so one convention governs the loader.
    """
    for name in ("LM_BASE_URL", "LMSTUDIO_BASE_URL"):
        value = os.environ.get(name)
        if value:
            return _normalize_host(value)
    return None


def _wsl_gateway_host():
    """Resolve the Windows host's gateway IP under WSL NAT mode.

    Returns ``<gateway>:1234`` or None when it cannot be determined.
    """
    try:
        proc = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            timeout=8,
        )
        text = proc.stdout.decode("utf-8", errors="replace")
    except Exception:
        return None
    m = re.search(r"default via (\d{1,3}(?:\.\d{1,3}){3})", text)
    if not m:
        return None
    return f"{m.group(1)}:1234"


def wsl_networking():
    """WSL networking mode name ('mirrored', 'nat') or None.

    Delegates to the transport module so runtime and targets agree.
    """
    try:
        from loader import wsl_targets as wsl

        if not wsl.wsl_available():
            return None
        distros = wsl.list_distros()
        if not distros:
            return None
        return wsl.networking_mode(distros[0])
    except Exception:
        return None


def lmstudio_api_host():
    """The ``host:port`` the LM Studio SDK should connect to, or None.

    ``None`` lets the SDK auto-discover the local LM Studio server, which is
    correct on Windows (where LM Studio runs locally) and on a plain Linux
    box that runs its own LM Studio. Inside WSL we must point the SDK at the
    Windows host explicitly: ``127.0.0.1`` under mirrored networking, or the
    NAT gateway IP otherwise. Never raises.
    """
    override = _env_api_host()
    if override:
        return override
    kind = detect()
    if kind is not RuntimeKind.WSL:
        return None
    if wsl_networking() == "mirrored":
        return "127.0.0.1:1234"
    return _wsl_gateway_host()
