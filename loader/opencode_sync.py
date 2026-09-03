"""Sync loader presets into the opencode config's LM Studio provider models.

The opencode global config (``~/.config/opencode/opencode.jsonc``) declares the
LM Studio providers' model lists with only a ``name``. This module edits those
entries in place — preserving JSONC comments and formatting — so opencode knows
each model's context window and reasoning capability.

Models already present are updated in place. Models that are watched in the
loader but missing from any provider are added to that provider's model list.
"""

import json
import os

DEFAULT_CONFIG_PATH = os.path.expanduser("~/.config/opencode/opencode.jsonc")
PROVIDERS = ["lmstudio_local_network", "lmstudio_localhost"]

_UNESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}


def _scan_strings(text, start, end):
    """Yield ``(token_start, token_end, value)`` for string literals.

    Whitespace and ``//`` / ``/* */`` comments are skipped.
    """
    i = start
    n = min(end, len(text))
    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] in "/*":
            if text[i + 1] == "/":
                j = text.find("\n", i)
                i = len(text) if j < 0 else j + 1
                continue
            j = text.find("*/", i + 2)
            i = len(text) if j < 0 else j + 2
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < n:
                ch = text[j]
                if ch == "\\" and j + 1 < n:
                    buf.append(_UNESCAPES.get(text[j + 1], text[j + 1]))
                    j += 2
                    continue
                if ch == '"':
                    yield (i, j + 1, "".join(buf))
                    i = j + 1
                    break
                buf.append(ch)
                j += 1
            continue
        i += 1


def _skip_ws(text, i):
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    return i


def _match_brace(text, open_idx):
    """Return the index just past the closer matching the ``{``/``[`` at open_idx."""
    depth = 0
    i = open_idx
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            for ts, te, _ in _scan_strings(text, i, n):
                if ts == i:
                    i = te
                    break
            else:
                i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] in "/*":
            if text[i + 1] == "/":
                j = text.find("\n", i)
                i = len(text) if j < 0 else j + 1
                continue
            j = text.find("*/", i + 2)
            i = len(text) if j < 0 else j + 2
            continue
        if c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _find_key(text, key, start, end):
    """Return the index of the ``:`` after a string token equal to ``key``, or None."""
    for ts, te, val in _scan_strings(text, start, end):
        if val == key:
            j = _skip_ws(text, te)
            if j < end and text[j] == ":":
                return j
    return None


def _value_span(text, colon_idx):
    """Return ``(start, end)`` of the value after a ``:``, only for object values."""
    j = _skip_ws(text, colon_idx + 1)
    if j < len(text) and text[j] == "{":
        return j, _match_brace(text, j)
    return None, None


def _existing_entry(text, vstart, vend):
    try:
        obj = json.loads(text[vstart:vend])
    except Exception:
        return {}
    return obj if isinstance(obj, dict) else {}


def _existing_name(text, vstart, vend):
    name = _existing_entry(text, vstart, vend).get("name")
    return name if isinstance(name, str) and name else None


def _resolve_modalities(ov, existing):
    """Resolve modalities for a model entry.

    Precedence: explicit ``modalities`` override > ``vision`` override >
    preserved existing value. ``vision: true`` maps to text+image input,
    ``vision: false`` maps to no modalities (text-only default).
    """
    ov_mod = ov.get("modalities")
    if isinstance(ov_mod, dict) and ov_mod:
        return ov_mod
    ov_vision = ov.get("vision")
    if ov_vision is True:
        return {"input": ["text", "image"], "output": ["text"]}
    if ov_vision is False:
        return None
    existing_mod = (existing or {}).get("modalities")
    if isinstance(existing_mod, dict) and existing_mod:
        return existing_mod
    return None


def _resolve_attachment(ov, existing):
    ov_att = ov.get("attachment")
    if isinstance(ov_att, bool):
        return ov_att
    if ov.get("vision") is True:
        return True
    existing_att = (existing or {}).get("attachment")
    if isinstance(existing_att, bool):
        return existing_att
    return None


def _render(entry, key_indent):
    body = json.dumps(entry, indent=2).splitlines()
    pad = " " * (key_indent + 2)
    lines = ["{"]
    for line in body[1:-1]:
        lines.append(pad + line)
    lines.append(" " * key_indent + "}")
    return "\n".join(lines)


def _build_entry(name, context, reasoning, output, modalities=None, attachment=None):
    entry = {"name": name}
    if reasoning:
        entry["reasoning"] = True
    if modalities:
        entry["modalities"] = modalities
    if attachment is True:
        entry["attachment"] = True
    if context:
        limit = {"context": context, "input": context}
        limit["output"] = output or max(1024, context // 4)
        entry["limit"] = limit
    return entry


def sync(config_path, watched_desired, overrides=None, providers=None):
    """Update model entries in the opencode config.

    ``watched_desired``: ``{model_key: desired_load_config}`` (the loader's
    watched presets). ``overrides``: ``{model_key: {"reasoning": bool,
    "output": int, "vision": bool, "modalities": dict, "attachment": bool}}``
    from the config's optional ``opencode`` section.

    ``vision: true`` writes ``modalities: {input: [text, image], output: [text]}``
    plus ``attachment: true`` so opencode sends images to the model.
    ``vision: false`` leaves the entry text-only. An explicit ``modalities``
    dict wins over ``vision``. Without any override the existing entry's
    ``modalities``/``attachment`` are preserved so manual vision edits survive
    a re-sync.

    Models already present are updated in place. Models not yet present are
    added to each provider that doesn't have them.

    Returns ``(changed, added)`` where ``added`` lists ``(provider, model_key)``
    tuples for models newly inserted.
    """
    overrides = overrides or {}
    providers = providers or PROVIDERS
    with open(config_path, "r", encoding="utf-8") as fh:
        text = fh.read()

    edits = []
    added = []

    for provider in providers:
        root = _find_key(text, "provider", 0, len(text))
        if root is None:
            continue
        pstart, pend = _value_span(text, root)
        if pstart is None:
            continue
        pkey = _find_key(text, provider, pstart, pend)
        if pkey is None:
            continue
        pobj_start, pobj_end = _value_span(text, pkey)
        if pobj_start is None:
            continue
        mkey = _find_key(text, "models", pobj_start, pobj_end)
        if mkey is None:
            continue
        mobj_start, mobj_end = _value_span(text, mkey)
        if mobj_start is None:
            continue
        for model_key, desired in watched_desired.items():
            mk = _find_key(text, model_key, mobj_start, mobj_end)
            if mk is not None:
                vstart, vend = _value_span(text, mk)
                if vstart is not None and text[vstart] == "{":
                    context = desired.get("contextLength") or desired.get("context_length")
                    ov = overrides.get(model_key) or {}
                    reasoning = ov.get("reasoning")
                    if reasoning is None:
                        reasoning = "reasoning" in model_key
                    existing = _existing_entry(text, vstart, vend)
                    name = _existing_name(text, vstart, vend) or model_key.rsplit("/", 1)[-1]
                    entry = _build_entry(
                        name,
                        context,
                        bool(reasoning),
                        ov.get("output"),
                        _resolve_modalities(ov, existing),
                        _resolve_attachment(ov, existing),
                    )
                    key_line_start = text.rfind("\n", 0, mk) + 1
                    key_indent = len(text[key_line_start:mk]) - len(text[key_line_start:mk].lstrip(" "))
                    rep = _render(entry, key_indent)
                    if text[vstart:vend] != rep:
                        edits.append((vstart, vend, rep))
                    continue
            context = desired.get("contextLength") or desired.get("context_length")
            ov = overrides.get(model_key) or {}
            reasoning = ov.get("reasoning")
            if reasoning is None:
                reasoning = "reasoning" in model_key
            name = model_key.rsplit("/", 1)[-1]
            entry = _build_entry(
                name,
                context,
                bool(reasoning),
                ov.get("output"),
                _resolve_modalities(ov, {}),
                _resolve_attachment(ov, {}),
            )
            inner = text[mobj_start + 1:mobj_end - 1].rstrip()
            has_entries = bool(inner.strip())
            key_line_start = text.rfind("\n", 0, mkey) + 1
            key_indent = len(text[key_line_start:mkey]) - len(text[key_line_start:mkey].lstrip(" "))
            entry_indent = key_indent + 2
            rendered = _render(entry, entry_indent)
            entry_line = " " * entry_indent + json.dumps(model_key) + ": " + rendered
            if has_entries:
                last_close = text.rfind("}", mobj_start, mobj_end - 1)
                insert_at = last_close + 1
                rep = ",\n" + entry_line
            else:
                insert_at = mobj_start + 1
                rep = "\n" + entry_line
            edits.append((insert_at, insert_at, rep))
            added.append((provider, model_key))

    for vstart, vend, rep in sorted(edits, reverse=True):
        text = text[:vstart] + rep + text[vend:]
    with open(config_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return len(edits), added
