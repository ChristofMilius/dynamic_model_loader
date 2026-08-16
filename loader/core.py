"""Shared core for the dynamic model loader.

Config parsing (unified presets format + legacy formats), load-config
matching, and the LM Studio SDK wrapper. Imported by both the loader app
(dynamic_model_loader.py) and the watcher (watcher.py).
"""

import contextlib
import json
import os
import re
import time
from dataclasses import dataclass

import lmstudio as lms

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(BASE_DIR)
LOG_FILE = os.path.join(PROJECT_DIR, "dynamic_loader.log")

CONFIG_KEY_ORDER = [
    "gpu",
    "gpuStrictVramCap",
    "offloadKVCacheToGpu",
    "contextLength",
    "evalBatchSize",
    "numExperts",
    "flashAttention",
    "llamaKCacheQuantizationType",
    "llamaVCacheQuantizationType",
]
GPU_KEY_ORDER = ["ratio", "splitStrategy", "disabledGpus"]


def _ordered(mapping, order):
    return {k: mapping[k] for k in order if k in mapping} | {
        k: mapping[k] for k in mapping if k not in order
    }


def ordered_config(config):
    """Return a copy of a load config with keys in the canonical order."""
    cfg = _ordered(dict(config or {}), CONFIG_KEY_ORDER)
    if isinstance(cfg.get("gpu"), dict):
        cfg["gpu"] = _ordered(cfg["gpu"], GPU_KEY_ORDER)
    return cfg


def log_action(action, detail=""):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "action": action, "detail": detail}) + "\n")
    except Exception:
        pass


def to_dict(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ("to_dict", "asdict", "dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                result = fn()
                if isinstance(result, dict):
                    return result
            except Exception:
                pass
    try:
        import msgspec

        if isinstance(obj, msgspec.Struct):
            return msgspec.to_builtins(obj)
    except Exception:
        pass
    return {}


def get_value(mapping, *candidates):
    for key in candidates:
        if key in mapping:
            return mapping[key]
    return None


def config_matches(cfg, desired):
    want_ctx = desired.get("contextLength", desired.get("context_length"))
    if want_ctx is not None:
        ctx = get_value(cfg, "context_length", "contextLength")
        if ctx is None or int(ctx) != int(want_ctx):
            return False
    want_kq = desired.get("llamaKCacheQuantizationType", desired.get("llama_k_cache_quantization_type"))
    if want_kq is not None:
        kq = get_value(cfg, "llama_k_cache_quantization_type", "llamaKCacheQuantizationType")
        if kq is None or str(kq).lower() != str(want_kq).lower():
            return False
    want_vq = desired.get("llamaVCacheQuantizationType", desired.get("llama_v_cache_quantization_type"))
    if want_vq is not None:
        vq = get_value(cfg, "llama_v_cache_quantization_type", "llamaVCacheQuantizationType")
        if vq is None or str(vq).lower() != str(want_vq).lower():
            return False
    dgpu = desired.get("gpu")
    if isinstance(dgpu, dict) and "ratio" in dgpu:
        want_ratio = dgpu["ratio"]
        gpu = get_value(cfg, "gpu")
        if isinstance(gpu, dict):
            ratio = get_value(gpu, "ratio")
            if ratio is not None:
                try:
                    if float(ratio) < float(want_ratio) - 1e-9:
                        return False
                except (TypeError, ValueError):
                    pass
    want_fa = desired.get("flashAttention", desired.get("flash_attention"))
    if want_fa is not None:
        fa = get_value(cfg, "flash_attention", "flashAttention")
        if fa is not None and not bool(fa):
            return False
    want_off = desired.get("offloadKVCacheToGpu", desired.get("offload_kv_cache_to_gpu"))
    if want_off is not None:
        off = get_value(cfg, "offload_kv_cache_to_gpu", "offloadKVCacheToGpu")
        if off is not None and not bool(off):
            return False
    return True


def match_key(identifier, keys):
    for k in sorted(keys, key=len, reverse=True):
        if k and k in identifier:
            return k
    return None


@dataclass
class Preset:
    model_key: str
    name: str
    config: dict

    @property
    def label(self):
        ctx = self.config.get("contextLength") or self.config.get("context_length")
        suffix = f"ctx={ctx}" if ctx else "default ctx"
        return f"{self.model_key}  [{self.name}, {suffix}]"


class ConfigStore:
    """Loads and validates the unified config file.

    New format: a model entry has ``presets`` (named load configs) and may set
    ``watch: true`` plus ``watchPreset`` so the watcher enforces one preset.

    Legacy format: a model entry is itself a load config. It is treated as one
    implicit preset named ``default`` and, to preserve the reactive watcher's
    semantics, is watched unless ``watch: false`` is set.
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(BASE_DIR, "model_configs.json")
        self.raw = {}
        self.data = {}
        self._errors = []
        self.reload()

    def reload(self):
        self._errors = []
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            self._errors.append(f"config not found: {self.path}")
            self.raw = {}
            self.data = {}
            return
        except Exception as e:
            self._errors.append(f"config parse error: {e}")
            self.raw = {}
            self.data = {}
            return
        if not isinstance(data, dict):
            self._errors.append("config root is not an object")
            self.raw = {}
            self.data = {}
            return
        self.raw = data
        models = data.get("models")
        self.data = models if isinstance(models, dict) else {}

    def warnings(self):
        return list(self._errors)

    def poll_settings(self):
        poll = self.raw.get("poll")
        if not isinstance(poll, dict):
            poll = {}
        return {
            "base": int(poll.get("base", 10) or 10),
            "max": int(poll.get("max", 3600) or 3600),
            "settle": int(poll.get("settle", 10) or 10),
        }

    def _entry(self, model_key):
        entry = self.data.get(model_key)
        return entry if isinstance(entry, dict) else None

    def _preset_config(self, model_key, name):
        entry = self._entry(model_key)
        if entry is None:
            return None
        if "presets" in entry:
            presets = entry["presets"]
            if isinstance(presets, dict) and isinstance(presets.get(name), dict):
                return presets[name]
            return None
        return entry if entry else None

    def presets(self):
        """Flatten the config into a stable list of Preset objects."""
        out = []
        for model_key in sorted(self.data):
            entry = self._entry(model_key)
            if entry is None:
                continue
            if "presets" in entry:
                presets = entry["presets"]
                if isinstance(presets, dict):
                    for name in sorted(presets):
                        cfg = presets[name]
                        if isinstance(cfg, dict) and cfg:
                            out.append(Preset(model_key, name, cfg))
            elif entry:
                out.append(Preset(model_key, "default", entry))
        return out

    def watch_preset_names(self):
        """model_key -> preset name the watcher enforces (watched models only)."""
        names = {}
        for model_key in sorted(self.data):
            entry = self._entry(model_key)
            if entry is None:
                continue
            if "presets" in entry:
                if not entry.get("watch"):
                    continue
                presets = entry["presets"]
                if not isinstance(presets, dict):
                    continue
                name = entry.get("watchPreset")
                if name not in presets:
                    name = "default" if "default" in presets else None
                    if name is None and presets:
                        name = next(iter(sorted(presets)))
                if name:
                    names[model_key] = name
            elif entry:
                if entry.get("watch", True) is not False:
                    names[model_key] = "default"
        return names

    def watch_desired(self):
        """model_key -> desired load config dict for the watcher."""
        out = {}
        for model_key, name in self.watch_preset_names().items():
            cfg = self._preset_config(model_key, name)
            if cfg:
                out[model_key] = cfg
        return out

    def preset_names(self, model_key):
        entry = self._entry(model_key)
        if entry is None:
            return []
        presets = entry.get("presets")
        if isinstance(presets, dict):
            return list(presets)
        return ["default"] if entry else []

    def watched_preset(self, model_key):
        return self.watch_preset_names().get(model_key)

    def resolve_key(self, identifier):
        """Map a loaded identifier to the canonical config key.

        A loaded instance may carry an instance suffix (e.g. ``:2``); the
        preset must attach to the base model key so one physical model never
        ends up with duplicate config entries.
        """
        ident = identifier or ""
        if ident in self.data:
            return ident
        stripped = re.sub(r":\d+$", "", ident)
        if stripped in self.data:
            return stripped
        matched = match_key(ident, list(self.data))
        if matched:
            return matched
        return stripped or ident

    def add_preset(self, model_key, name, config, watch=False):
        """Save a load config as a named preset, writing the config file."""
        models = self.raw.get("models")
        if not isinstance(models, dict):
            models = {}
            self.raw["models"] = models
        entry = models.get(model_key)
        if not isinstance(entry, dict):
            entry = {}
            models[model_key] = entry
        if "presets" in entry:
            presets = entry["presets"]
            if not isinstance(presets, dict):
                presets = {}
                entry["presets"] = presets
        else:
            # Legacy: the entry itself is a load config -> becomes `default`.
            legacy = {k: v for k, v in entry.items()}
            presets = {}
            if legacy:
                presets["default"] = legacy
            entry["presets"] = presets
        for cfg in presets.values():
            if isinstance(cfg, dict):
                cfg.clear()
                cfg.update(ordered_config(cfg))
        presets[name] = ordered_config(config)
        if watch:
            entry["watch"] = True
            entry["watchPreset"] = name
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.raw, fh, indent=2)
            fh.write("\n")
        self.reload()


class LMStudio:
    """Thin wrapper around the lmstudio SDK. Each call opens its own client."""

    @contextlib.contextmanager
    def connect(self):
        with lms.Client() as client:
            yield client

    def load(self, model_key, config):
        with lms.Client() as client:
            return client.llm.load_new_instance(model_key, config=config).identifier

    def unload(self, identifier):
        with lms.Client() as client:
            client.llm.unload(identifier)

    def list_loaded(self):
        with lms.Client() as client:
            rows = []
            for h in client.llm.list_loaded():
                try:
                    cfg = to_dict(h.get_load_config())
                except Exception:
                    cfg = {}
                rows.append({"identifier": h.identifier, "config": cfg})
            return rows

    def list_downloaded(self):
        with lms.Client() as client:
            rows = []
            for m in client.llm.list_downloaded():
                try:
                    display = m.info.display_name
                except Exception:
                    display = ""
                rows.append((m.model_key, display))
            return rows
