r"""Discovery and transport for opencode configs inside WSL distros.

opencode also runs inside WSL, and each WSL distro keeps its own copy of the
global config (``~/.config/opencode/opencode.jsonc`` on the Linux filesystem).
With *mirrored* WSL networking that file is reachable from Windows over the
``\\wsl$\<distro>\...`` UNC share, so the loader can apply the same model
restrictions (limits, modalities, reasoning) to every WSL instance as it does
to the native CLI.

Discovery::

    wsl.exe --list --quiet                 -> distro names
    wsl -d <distro> -- sh -c 'echo $HOME'  -> home of the distro's default user

Transport is primary over the UNC share; when the share is not mounted the
file content is moved in and out with ``wsl`` commands instead.
"""

import os
import re
import shutil
import subprocess
from dataclasses import dataclass

CONFIG_REL = os.path.join(".config", "opencode", "opencode.jsonc")

# Internal WSL distros created by tooling (e.g. Docker Desktop's VM backend).
# They are not user environments, never host an opencode config, and should
# not surface as loader targets.
INFRA_DISTRO_NAMES = {"docker-desktop", "docker-desktop-data"}


def _shq(value):
    """Wrap ``value`` in single quotes for use inside a ``sh -c`` string.

    WSL interop joins everything after ``--`` and runs it through ``/bin/sh
    -c``, so paths must be inlined into the script body (positional ``$1``
    arguments are not preserved across the boundary).
    """
    return "'" + value.replace("'", "'\\''") + "'"


def wsl_available():
    """Path to the WSL interop binary, or None when WSL is not installed."""
    return shutil.which("wsl.exe") or shutil.which("wsl")


def run_wsl(args, input_bytes=None, timeout=20):
    """Run ``wsl.exe`` (or ``wsl``) with ``args``, capturing output."""
    return subprocess.run(
        [wsl_available() or "wsl.exe", *args],
        capture_output=True,
        input=input_bytes,
        timeout=timeout,
    )


def _decode(payload):
    """Decode WSL output, tolerating UTF-8 and UTF-16LE (with NUL padding)."""
    if not payload:
        return ""
    if b"\x00" in payload:
        return payload.decode("utf-16-le", errors="replace").lstrip("\ufeff")
    return payload.decode("utf-8", errors="replace")


def list_distros():
    """Return the names of all installed WSL distributions."""
    if not wsl_available():
        return []
    proc = run_wsl(["--list", "--quiet"], timeout=20)
    if proc.returncode != 0:
        return []
    names = []
    for line in re.split(r"[\x00\r\n]+", _decode(proc.stdout)):
        line = line.strip()
        if not line:
            continue
        if line.lower() in ("name", "windows subsystem for linux distributions"):
            continue
        names.append(line)
    return names


def distro_home(distro):
    """Return the default user's home inside ``distro``, or None."""
    proc = run_wsl(
        ["--distribution", distro, "--", "sh", "-c", "printf '%s' \"$HOME\""],
        timeout=20,
    )
    if proc.returncode != 0:
        return None
    home = _decode(proc.stdout).strip()
    return home or None


def networking_mode(distro):
    """Report the WSL networking mode ('mirrored', 'nat') or None."""
    proc = run_wsl(
        ["--distribution", distro, "--", "wslinfo", "--networking-mode"], timeout=8
    )
    if proc.returncode != 0:
        return None
    mode = _decode(proc.stdout).strip().lower()
    return mode if mode in ("mirrored", "nat") else None


def linux_to_unc(distro, linux_path):
    """Map an absolute Linux path inside ``distro`` to ``\\wsl$`` UNC form."""
    rel = linux_path.replace("\\", "/").lstrip("/")
    return "\\\\wsl$\\" + distro + "\\" + rel.replace("/", "\\")


@dataclass
class WslTarget:
    distro: str
    home: str
    linux_config: str
    unc_config: str
    networking: str | None
    config_exists: bool


def _is_file(path):
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def discover():
    """Enumerate WSL opencode config targets (never raises)."""
    if not wsl_available():
        return []
    try:
        distros = list_distros()
    except Exception:
        return []
    targets = []
    for distro in distros:
        if distro.lower() in INFRA_DISTRO_NAMES:
            continue
        try:
            home = distro_home(distro)
        except Exception:
            home = None
        if not home:
            continue
        linux = home.rstrip("/") + "/" + CONFIG_REL.replace("\\", "/")
        unc = linux_to_unc(distro, linux)
        try:
            net = networking_mode(distro)
        except Exception:
            net = None
        targets.append(
            WslTarget(
                distro=distro,
                home=home,
                linux_config=linux,
                unc_config=unc,
                networking=net,
                config_exists=_is_file(unc),
            )
        )
    return targets


def read_config(target):
    """Read a target's opencode config over the UNC share."""
    with open(target.unc_config, "r", encoding="utf-8") as fh:
        return fh.read()


def write_config(target, text):
    """Write a target's opencode config over the UNC share."""
    os.makedirs(os.path.dirname(target.unc_config), exist_ok=True)
    with open(target.unc_config, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def read_config_via_wsl(target):
    """Read a target's opencode config through a ``wsl`` command."""
    proc = run_wsl(
        ["--distribution", target.distro, "--", "sh", "-c",
         "cat " + _shq(target.linux_config)],
        timeout=20,
    )
    if proc.returncode != 0:
        raise OSError(
            "wsl read failed: " + _decode(proc.stderr).strip() or f"exit {proc.returncode}"
        )
    return _decode(proc.stdout)


def write_config_via_wsl(target, text):
    """Write a target's opencode config through a ``wsl`` command."""
    script = (
        "mkdir -p " + _shq(os.path.dirname(target.linux_config))
        + " && cat > " + _shq(target.linux_config)
    )
    proc = run_wsl(
        ["--distribution", target.distro, "--", "sh", "-c", script],
        input_bytes=text.encode("utf-8"),
        timeout=30,
    )
    if proc.returncode != 0:
        raise OSError(
            "wsl write failed: " + _decode(proc.stderr).strip() or f"exit {proc.returncode}"
        )