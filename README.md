# Dynamic Model Loader for LM Studio

A standalone terminal app that manages LM Studio model loads and keeps their
load configs correct. Two components work together in one process:

- **Menu-driven loading**: pick a named load preset from a numbered menu and
  load the model through the LM Studio Python SDK
  (`client.llm.load_new_instance`).
- **Config watcher**: a background watcher makes sure a loaded model keeps its
  configured load config. It **never loads a model on its own** and never
  reloads a model that was manually unloaded — it only reacts to models that
  are already loaded and that it has a load config for.
- The menu also launches **opencode** (native CLI) while the watcher keeps
  running.

Both components share one config file and one process: the loader app imports
the watcher module (`watcher.py`), which runs as a background daemon thread.

---

## Components

| File | Role |
|---|---|
| `loader/dynamic_model_loader.py` | The app: interactive CLI. Commands are dispatched through a command dictionary. |
| `loader/watcher.py` | `Watcher`: configuration watcher — background daemon thread (scan / settle / fix / backoff). No stdout; state read via `status()`. |
| `loader/core.py` | Shared: unified `ConfigStore` + `Preset`, load-config matching, `LMStudio` SDK wrapper, action log. |
| `loader/model_configs.json` | The unified config file: models, named load presets, and which preset the watcher enforces. |

---

## Usage

The project is managed with [uv](https://docs.astral.sh/uv/). Sync the
environment once (`uv sync`) then launch the app:

```powershell
uv run dynamic-model-loader
```

or run the generated console script directly:

```powershell
.\.venv\Scripts\dynamic-model-loader.exe
```

On startup the app connects to LM Studio (the SDK resolves the local API host
itself), loads the config file, prints the known load presets, and drops into
the prompt:

```
dynamic model loader
Known load presets:
    1. mistralai/devstral-small-2-2512  [full-context, ctx=65536]
Type 'help' for the command list.
dynamic-loader>
```

## Commands

| Command | Action |
|---|---|
| `help` | Lists the command names from the command dictionary. |
| `models` | Models available in LM Studio (model key + display name). |
| `loaded` | Loaded instances with their current load config. |
| `load [N]` | Numbered menu of known load presets → load via the SDK. |
| `unload [N]` | Numbered menu of loaded instances → unload. |
| `import [N]` | Import a loaded instance's current load config as a named preset (e.g. copy a well-tuned config already running in LM Studio). Prompts for a preset name and whether the watcher should enforce it. The preset attaches to the base model key (an instance suffix like `:2` is stripped); enabling the watcher replaces the model's previous watched preset. |
| `presets` | Configured load presets, marking which are enforced by the watcher. |
| `watch start` | Start the config watcher in the background. |
| `watch stop` | Stop the watcher. |
| `watch status` | Show watcher state, last scan and last fix. |
| `opencode [args]` | Launch opencode (native CLI) in a separate window, passing args; the launcher stays responsive while opencode runs. |
| `sync-opencode` | Update opencode's LM Studio model lists (`~/.config/opencode/opencode.jsonc`) with the watched presets' context limits. |
| `status` | Connection summary + configured/loaded overlap + watcher state. |
| `reload` | Re-read `model_configs.json` (applies to menu and running watcher). |
| `quit` | Stop the watcher and exit (also `exit`/`q`/Ctrl+C). |

Unknown commands print a hint; SDK errors are caught, logged, and do not kill
the interactive CLI.

Intended flow: start app → `watch start` → `opencode` (the config watcher
keeps running during the session) → back at the menu → `quit`.

---

## Configuration

The app reads `loader/model_configs.json`. That file is personal (your model
presets) and is **not** tracked by git; a generic starter is provided in
`loader/model_configs.example.json`. On a fresh clone, copy it over:

```powershell
Copy-Item loader\model_configs.example.json loader\model_configs.json
```

Example format:

```json
{
  "poll": { "base": 10, "max": 3600, "settle": 10 },
  "models": {
    "mistralai/devstral-small-2-2512": {
      "watch": true,
      "watchPreset": "full-context",
      "presets": {
        "full-context": {
          "contextLength": 65536,
          "gpu": { "ratio": 1.0 },
          "llamaKCacheQuantizationType": "q8_0",
          "llamaVCacheQuantizationType": "q8_0",
          "flashAttention": true,
          "offloadKVCacheToGpu": true
        }
      }
    }
  }
}
```

- **`poll`** — watcher timing. `base` = seconds between scans when attention is
  needed, `max` = backoff ceiling while everything is correct, `settle` =
  grace period before unloading a wrong-config model (lets an in-flight JIT
  load finish).
- **`models.<key>.presets`** — named load configs. Each preset becomes one
  numbered entry in the `load` menu.
- **`models.<key>.watch: true`** — the watcher enforces this model when loaded.
  If omitted/false, the model is only loadable via the menu and is never
  touched by the watcher.
- **`models.<key>.watchPreset`** — which preset the watcher enforces. Defaults
  to a `default` preset, else the first preset. Exactly one preset per model is
  enforced: a model is watched by a single preset, and importing a watched
  preset replaces the previous one.
- **Preset fields** use the same names LM Studio's SDK uses; anything in a
  preset dict is passed straight to the SDK as the load config.

### `sync-opencode` and the `opencode` section

`sync-opencode` edits the opencode global config
(`~/.config/opencode/opencode.jsonc`) so opencode knows the context sizes of the
models it offers. For each model the watcher enforces (a model with
`watch: true`), it sets `limit.context` / `limit.input` to the watched preset's
`contextLength` and `limit.output` to a quarter of it (min 1024). Only models
that already exist in the opencode config are updated — missing ones are
reported and skipped. Restart opencode afterwards for the changes to apply.

An optional top-level `opencode` section overrides per-model sync fields:

```json
{
  "opencode": {
    "models": {
      "org/example-model": {
        "reasoning": true,
        "output": 32768
      }
    }
  }
}
```

- **`output`** — explicit `limit.output` for that model instead of the
  context/4 default.
- **`reasoning`** — write `"reasoning": true` on the model entry. Without an
  override, it is inferred from the model key (enabled when the key contains
  `reasoning`).

### Watcher semantics

- Matches loaded identifiers against configured model keys (substring match,
  longest key wins), so identifiers with suffixes like `:2` are still caught.
- Only fields present in the enforced preset are checked.
- A loaded model whose config drifted is unloaded and reloaded once after
  `settle` seconds (re-checked first, so an in-flight startup finishes).
- Never loads a model that isn't loaded — manually unloaded models stay
  unloaded.

### Legacy formats

The parser also accepts the old shapes:

- A model entry that is itself a load config (no `presets`) is treated as one
  implicit preset named `default`, and is watched by default unless
  `"watch": false` is set. This covers the two legacy config shapes: a model
  entry holding a single load config, and a flat desired-config form.

---

## Notes

- Action log: `dynamic_loader.log` in the project root (JSON lines: loads,
  unloads, watcher fixes, errors). Ignore or delete; it is not part of the
  install.
- The config file must sit in the same directory as the script; it is resolved
  relative to the loader's own location (`BASE_DIR` in `core.py`).
- The opencode launcher shim was archived and removed so `opencode` resolves
  to the native `OpenCode.exe` CLI again. The `devstral-normalize.js` plugin
  in `~/.config/opencode/plugin/` remains required and untouched.

## Dependencies

- uv, which manages Python 3.13 (pinned in `.python-version`) and the `.venv`
  environment (installs the `lmstudio` SDK per `pyproject.toml`/`uv.lock`).
- LM Studio with the local server running (API-token auth disabled; the SDK
  build in use cannot send a token).
