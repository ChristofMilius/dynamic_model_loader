"""Probe model capabilities across every local API exposure.

Order (most chatty first):
  1. LM Studio SDK ``list_downloaded`` -> ``info.vision`` / ``trained_for_tool_use``
  2. Native ``GET /api/v1/models`` -> ``capabilities.vision``
  3. Native ``GET /api/v0/models`` -> ``type: vlm`` means vision
  4. OpenAI-compat ``GET /v1/models`` -> exposure only, no capas

``probe_all`` never raises: each leg returns ``ok`` data or an ``error``
string, and ``merged`` holds the best-effort vision/tool verdict.
"""

import json
import os
import urllib.request

DEFAULT_BASES = ("http://localhost:1234", "http://127.0.0.1:1234")


def discover_bases(extra=None):
    bases = []
    for cand in [os.environ.get("LM_BASE_URL"), os.environ.get("LMSTUDIO_BASE_URL")]:
        if cand:
            bases.append(cand.rsplit("/v1", 1)[0].rstrip("/"))
    # also pull bases declared in the opencode global config so a LAN LMS
    # (e.g. http://192.168.0.20:1234/v1) is probed even without LM_BASE_URL
    try:
        from loader.opencode_sync import DEFAULT_CONFIG_PATH, PROVIDERS
        import re

        with open(DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as fh:
            text = fh.read()
        for m in re.finditer(r'"baseURL"\s*:\s*"([^"]+)"', text):
            b = m.group(1).rsplit("/v1", 1)[0].rstrip("/")
            if b and b not in bases:
                bases.append(b)
    except Exception:
        pass
    for b in list(DEFAULT_BASES) + list(extra or []):
        if b and b not in bases:
            bases.append(b.rstrip("/"))
    return bases


def _get_json(url, headers=None, timeout=5):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _auth_headers(api_token=None):
    tok = api_token or os.environ.get("LM_API_TOKEN") or os.environ.get("LMSTUDIO_API_KEY")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _norm_key(key):
    return (key or "").split("@")[0]


def probe_sdk(model_key, api_token=None):
    """Probe via the LM Studio SDK (native, chattiest)."""
    try:
        import lmstudio as lms
    except Exception as e:
        return {"ok": False, "error": f"no sdk: {e}"}
    try:
        with lms.Client(api_token=api_token or os.environ.get("LM_API_TOKEN")) as client:
            for m in client.llm.list_downloaded():
                key = getattr(m, "model_key", "") or ""
                if key == model_key or _norm_key(key) == _norm_key(model_key) or model_key in key:
                    info = m.info
                    return {
                        "ok": True,
                        "source": "lmstudio-sdk:list_downloaded",
                        "vision": bool(getattr(info, "vision", False)),
                        "tool_use": bool(getattr(info, "trained_for_tool_use", False)),
                        "max_context": getattr(info, "max_context_length", None),
                        "arch": getattr(info, "architecture", None),
                    }
        return {"ok": False, "error": f"not in list_downloaded: {model_key}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def probe_lmstudio_v1(model_key, bases=None, api_token=None, timeout=5):
    """Probe native GET /api/v1/models (capabilities.vision)."""
    last_err = "no bases"
    for base in bases or discover_bases():
        try:
            data = _get_json(f"{base}/api/v1/models", _auth_headers(api_token), timeout)
            for m in data.get("models", []):
                key = m.get("key", "")
                if key == model_key or model_key in key or _norm_key(key) == _norm_key(model_key):
                    caps = m.get("capabilities", {}) or {}
                    return {
                        "ok": True,
                        "source": f"{base}/api/v1/models",
                        "vision": caps.get("vision"),
                        "tool_use": caps.get("trained_for_tool_use"),
                        "max_context": m.get("max_context_length"),
                        "type": m.get("type"),
                        "raw_capabilities": caps,
                    }
            last_err = f"{base}: key not listed"
        except Exception as e:
            last_err = f"{base}: {type(e).__name__}: {e}"
    return {"ok": False, "error": last_err}


def probe_lmstudio_v0(model_key, bases=None, api_token=None, timeout=5):
    """Probe native GET /api/v0/models (type vlm => vision)."""
    last_err = "no bases"
    for base in bases or discover_bases():
        try:
            data = _get_json(f"{base}/api/v0/models", _auth_headers(api_token), timeout)
            for m in data.get("data", []):
                mid = m.get("id", "")
                if mid == model_key or model_key in mid or _norm_key(mid) == _norm_key(model_key):
                    mtype = m.get("type")
                    return {
                        "ok": True,
                        "source": f"{base}/api/v0/models",
                        "vision": True if mtype == "vlm" else (False if mtype == "llm" else None),
                        "type": mtype,
                        "max_context": m.get("max_context_length"),
                        "capabilities": m.get("capabilities"),
                    }
            last_err = f"{base}: key not listed"
        except Exception as e:
            last_err = f"{base}: {type(e).__name__}: {e}"
    return {"ok": False, "error": last_err}


def probe_openai(model_key, bases=None, api_token=None, timeout=5):
    """Probe OpenAI-compat GET /v1/models (exposure only, never chatty)."""
    last_err = "no bases"
    for base in bases or discover_bases():
        try:
            data = _get_json(f"{base}/v1/models", _auth_headers(api_token), timeout)
            ids = [m.get("id", "") for m in data.get("data", [])]
            for mid in ids:
                if mid == model_key or model_key in mid or _norm_key(mid) == _norm_key(model_key):
                    return {"ok": True, "source": f"{base}/v1/models", "exposed": True, "vision": None}
            last_err = f"{base}: key not listed"
        except Exception as e:
            last_err = f"{base}: {type(e).__name__}: {e}"
    return {"ok": False, "error": last_err}


def probe_all(model_key, bases=None, api_token=None, timeout=5):
    """Try every exposure, return per-leg results plus a merged verdict."""
    bases = bases or discover_bases()
    legs = {
        "lmstudio_sdk": probe_sdk(model_key, api_token),
        "lmstudio_api_v1": probe_lmstudio_v1(model_key, bases, api_token, timeout),
        "lmstudio_api_v0": probe_lmstudio_v0(model_key, bases, api_token, timeout),
        "openai_compat": probe_openai(model_key, bases, api_token, timeout),
    }
    vision = None
    vision_source = None
    for name in ("lmstudio_sdk", "lmstudio_api_v1", "lmstudio_api_v0"):
        leg = legs[name]
        if leg.get("ok") and isinstance(leg.get("vision"), bool):
            vision = leg["vision"]
            vision_source = leg.get("source", name)
            break
    tool_use = None
    for name in ("lmstudio_sdk", "lmstudio_api_v1"):
        leg = legs[name]
        if leg.get("ok") and isinstance(leg.get("tool_use"), bool):
            tool_use = leg["tool_use"]
            break
    max_context = None
    for name in ("lmstudio_sdk", "lmstudio_api_v1", "lmstudio_api_v0"):
        leg = legs[name]
        if leg.get("ok") and leg.get("max_context"):
            max_context = leg["max_context"]
            break
    legs["merged"] = {
        "vision": vision,
        "vision_source": vision_source,
        "tool_use": tool_use,
        "max_context": max_context,
    }
    return legs
