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
| `loader/wsl_targets.py` | Discovery + transport for opencode configs inside WSL distros (`\\wsl$` UNC primary, `wsl` command fallback). |
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
runtime: windows | LM Studio endpoint: auto
Known load presets:
    1. mistralai/devstral-small-2-2512  [full-context, ctx=65536]
Type 'help' for the command list.
dynamic-loader>
```

### Supported environments

The loader detects where it is running and adapts two things: which LM Studio
server it connects to, and which opencode config it edits (see `sync-opencode`
below).

| Run inside | LM Studio endpoint | opencode config edited |
|---|---|---|
| Windows | local LM Studio (auto) | `<home>\.config\opencode\opencode.jsonc` |
| WSL, mirrored networking | `127.0.0.1:1234` (Windows-host LMS) | the distro's own config |
| WSL, NAT networking | the WSL gateway IP `:1234` (Windows-host LMS) | the distro's own config |
| Linux (native) | local LM Studio (auto) | `~/.config/opencode/opencode.jsonc` (XDG-aware) |

The startup banner reports the detected runtime and endpoint. Cross-distro WSL
target management (`wsl list` / WSL sync in `sync-opencode`) is a **Windows**
loader feature; when the loader runs inside WSL it manages only its own config.

Override the endpoint explicitly with `LM_BASE_URL` (or `LMSTUDIO_BASE_URL`),
e.g. when LM Studio runs on another machine on your LAN. The value may be a
bare host, `host:port`, or a full `http://host:1234/v1` URL — the loader
normalizes it down to `host:port`.

## Commands

| Command | Action |
|---|---|
| `help` | Lists the command names from the command dictionary. |
| `models` | Models available in LM Studio (model key + display name). |
| `loaded` | Loaded instances with their current load config. |
| `load [N]` | Numbered menu of known load presets → load via the SDK. |
| `unload [N]` | Numbered menu of loaded instances → unload. |
| `remove [N]` | Numbered menu of configured load presets (one entry per model) → delete a model's load config from the loader config, then prune its entry from opencode's LM Studio model lists. Prompts for confirmation first. |
| `import [N]` | Import a loaded instance's current load config as a named preset (e.g. copy a well-tuned config already running in LM Studio). Prompts for a preset name and whether the watcher should enforce it. The preset attaches to the base model key (an instance suffix like `:2` is stripped); enabling the watcher replaces the model's previous watched preset. Import probes the model first and merges every available parameter field into the saved preset (see "Probing model parameters" below). |
| `presets` | Configured load presets, marking which are enforced by the watcher. |
| `watch start` | Start the config watcher in the background. |
| `watch stop` | Stop the watcher. |
| `watch status` | Show watcher state, last scan and last fix. |
| `opencode [args]` | Launch opencode (native CLI) in a separate window, passing args; the launcher stays responsive while opencode runs. |
| `sync-opencode` | Update opencode's LM Studio model lists with the watched presets' context limits — in the Windows config (`~/.config/opencode/opencode.jsonc`) and in every reachable WSL distro's own config. |
| `wsl list` | Discover WSL opencode targets: distro, home, networking mode, and the `\\wsl$` config path. |
| `wsl sync` | Shortcut for `sync-opencode`. |
| `status` | Connection summary + configured/loaded overlap + watcher state. |
| `probe [key]` | Probe a model's capabilities across every local API exposure and print its full available parameter set (alias `capabilities`). See "Probing model parameters". |
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
  preset dict is passed straight to the SDK as the load config (extra keys the
  SDK does not know are ignored by it, but kept in the config file — see
  *Probing model parameters* below).

### Probing model parameters

The `probe` / `capabilities` command queries every local API exposure (LM
Studio SDK, native `/api/v1/models` and `/api/v0/models`, OpenAI-compat
`/v1/models`) and reports the full parameter set the model advertises:
vision, tool use, max context, type, publisher, architecture, format,
quantization, parameters, variants, and — for reasoning models — the allowed
reasoning-effort options:

```
dynamic-loader> probe qwen/qwen3.8-27b
Probing qwen/qwen3.8-27b ...
  lmstudio/sdk: no bases
  lmstudio/api/v1: http://localhost:1234/api/v1/models -> vision=True, tool_use=True, reasoning=off|low|medium|xhigh|on, type=llm
  lmstudio/api/v0: no bases
  openai/compat: no bases
merged: vision=True (via None), tool_use=True, max_context=262144, reasoning=off|low|medium|xhigh|on
available parameters:
  architecture: qwen35
  ...
  reasoning: {'allowedOptions': ['off', 'low', 'medium', 'xhigh', 'on'], 'default': 'xhigh'}
```

`import` stores those fields in the model's load config as well. So a preset
records not only the load settings but the model's own spec, for example:

```json
"full-context": {
  "contextLength": 65536,
  "reasoningEffort": "medium",
  "reasoning": { "allowedOptions": ["off", "low", "medium", "xhigh", "on"], "default": "xhigh" },
  "vision": true,
  "architecture": "qwen35"
}
```

- **`reasoningEffort`** — intended reasoning-effort default. Import picks it
  from the probed capability: `medium` for models with real effort levels,
  `none` for models that only report the binary `off`/`on` switch (the gemma-4
  family) — on those, every effort above `none` spends the whole token budget
  thinking and returns no reply, so `none` is the only value that reliably
  forces a direct answer. The default is only applied when the preset does not
  already pick an effort. Edit it to any LM Studio chat `reasoning_effort`
  value (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`) — note this is a
  different namespace than the model's binary `allowedOptions` (`off`/`on`),
  which the chat endpoint rejects.

### `sync-opencode`, the `opencode` section, and WSL targets

`sync-opencode` edits the opencode global config
(`~/.config/opencode/opencode.jsonc`) so opencode knows the context sizes of the
models it offers. For each model the watcher enforces (a model with
`watch: true`), it sets `limit.context` / `limit.input` to the watched preset's
`contextLength` and `limit.output` to a quarter of it (min 1024). Models already
in the opencode config are updated in place. Models not yet present are added to
each provider's model list.

The same model restrictions are applied to opencode running **inside WSL**.
Each WSL distro keeps its own global config
(`~/.config/opencode/opencode.jsonc` on the Linux filesystem), and the loader
writes the identical model entries into every reachable distro over the
`\\wsl$\<distro>\...` share (with a `wsl`-command fallback when the share is not
mounted). This makes the model limits apply to the opencode instances that work
on the Windows drives mounted into WSL. Prerequisites:

- **Mirrored WSL networking** (`networkingMode=mirrored` in `.wslconfig`) so
  WSL's `localhost` reaches LM Studio on Windows. The loader's
  `lmstudio_localhost` provider already points at `127.0.0.1:1234` and works
  unchanged in mirrored mode.
- The distro must be **running** (start it before `sync-opencode`, e.g. by
  launching opencode there) so its config is reachable from Windows.
- The distro's config must contain the LM Studio providers
  (`lmstudio_local_network` / `lmstudio_localhost`), otherwise the loader prints
  a note and leaves it untouched.

`wsl list` shows the discovered targets (distro, home, networking mode, config
path). Internal infrastructure distros (Docker Desktop's `docker-desktop` /
`docker-desktop-data`) are filtered out entirely. A real distro without an
opencode config yet is listed but skipped by the sync.

`sync-opencode` also prunes stale entries. A model that is no longer in the
loader's `models` config (for example after you delete its download from LM
Studio and drop its load config) is removed from both local LM Studio providers'
model lists, so it stops showing up in opencode. This pruning applies to the
Windows config and every WSL target alike. Removal only ever touches the
two local LM Studio providers (`lmstudio_local_network`,
`lmstudio_localhost`) — opencode entries from other providers are never touched.
The loader config's `models` map is the source of truth: any model still listed
there stays, even if it isn't currently watched.

Restart opencode (Windows and each WSL instance) afterwards for the changes to
apply.

For reasoning models, the synced entry additionally carries an `options` block
with the model's `reasoningEffort`:

```jsonc
"google/gemma-4-12b-qat": {
  "name": "gemma-4-12b-qat",
  "reasoning": true,
  "options": { "reasoningEffort": "none" },
  "limit": { "context": 65536, "input": 65536, "output": 16384 }
}
```

opencode forwards this per-model `options` into the request body as
`reasoning_effort` (verified end-to-end: config → AI SDK
`@ai-sdk/openai-compatible` → `reasoning_effort` → enforced by LM Studio). The
value is resolved per model: an explicit `opencode.models.<key>.reasoningEffort`
override wins, then binary `off`/`on` models (the gemma-4 family) get `none`
so their thinking cannot burn the whole reply budget, and effort-level models
keep their preset's `reasoningEffort` (default `medium`).

### Removing a model

To fully drop a model (e.g. after deleting its download from LM Studio's
directory), use the `remove` command:

- Pick the model from the numbered menu, or pass its number directly
  (`remove 2`).
- Confirm when asked. The model's load config (and any presets) is deleted from
  the loader's `model_configs.json`, any `opencode.models` override for it is
  dropped, and the opencode sync runs again so its entry is pruned from both
  local LM Studio providers' model lists.

Alternatively, edit `model_configs.json` by hand and run `sync-opencode` — the
same pruning applies the next time the sync runs.

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
  `reasoning`) or from a probed reasoning capability stored in the watched
  preset.
- **`reasoningEffort`** — force the `options.reasoningEffort` written for this
  model, overriding both the preset default and the binary-`none` behavior for
  the gemma-4 family. Any LM Studio chat `reasoning_effort` value.
- **`vision`** — write `"vision": true` to enable image input
  (`modalities: {input: [text, image], output: [text]}` plus
  `"attachment": true`) so opencode sends images to the model.
  `"vision": false` keeps the entry text-only. An explicit `"modalities"`
  dict wins over `"vision"`. Without any override the existing entry's
  `modalities`/`attachment` are preserved so manual vision edits survive a
  re-sync. Check LM Studio's model `vision` flag
  (`client.llm.list_downloaded()` → `info.vision`) to decide per model.

### Watcher semantics

- Matches loaded identifiers against configured model keys (substring match,
  longest key wins), so identifiers with suffixes like `:2` are still caught.
- Only fields present in the enforced preset are checked.
- A loaded model whose config drifted is unloaded and reloaded once after
  `settle` seconds (re-checked first, so an in-flight startup finishes).
- Never loads a model that isn't loaded — manually unloaded models stay
  unloaded.

### What if a model has no `presets`?

Normally a model entry looks like this:

```json
{
  "models": {
    "org/my-model": {
      "watch": true,
      "watchPreset": "default",
      "presets": {
        "default": { "contextLength": 32768 }
      }
    }
  }
}
```

But the config also accepts a model whose entry *is* the load config directly,
without a `presets` wrapper:

```json
{
  "models": {
    "org/my-model": { "contextLength": 32768 }
  }
}
```

In that shorthand form the entry counts as a single preset named `default`, and
the model is treated as watched by default — set `"watch": false` to opt out.
`import` and menu commands always write the full `presets` form, so you'll only
see the shorthand if you hand-wrote a config or kept one from an older version.

---

## Notes

- Action log: `dynamic_loader.log` in the project root (JSON lines: loads,
  unloads, watcher fixes, errors). Ignore or delete; it is not part of the
  install.
- The config file must sit in the same directory as the script; it is resolved
  relative to the loader's own location (`BASE_DIR` in `core.py`).
### Message normalize plugin

`.opencode/plugins/msg_normalize.js` is an opencode plugin that normalizes
chat messages before they reach the model. It is especially important when
the config watcher reloads a model mid-session: the reload can leave empty
assistant messages or consecutive user messages in opencode's context, which
may confuse the freshly loaded model.

The plugin:
- Removes empty assistant messages (no text or tool parts)
- Merges consecutive user messages into one
- Logs metadata (role, part count, tools) to `<tmpdir>/opencode/msg_normalize.log`

The repo copy is the committed source of truth. Because the loader is used
across all projects, the plugin **self-replicates**: the first time opencode
loads it from the repo's `.opencode/plugins/`, it copies itself into the
global plugins folder (`~/.config/opencode/plugins/`), covering sessions in
every project from then on. The global copy is byte-identical and inert on
the replication side (it never copies back). If you edit the plugin, push the
repo copy; the next repo-session load refreshes the global replica.

## Dependencies

- uv, which manages Python 3.13 (pinned in `.python-version`) and the `.venv`
  environment (installs the `lmstudio` SDK per `pyproject.toml`/`uv.lock`).
- LM Studio with the local server running. If API-token auth is enabled,
  set the `LM_API_TOKEN` environment variable (the SDK reads it automatically).
