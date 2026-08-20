"""Dynamic model loader for LM Studio.

A unified interactive CLI combining model loading and configuration management:

- Proactive: pick a named load preset from a menu and load the model through
  the LM Studio Python SDK.
- Automatic: a background watcher (watcher.py) makes sure a loaded model keeps
  its configured load config. It never force-loads and never reloads a model
  that was manually unloaded.
- The CLI can also launch opencode (native CLI) while the watcher keeps
  running, and quit.
"""

import os
import shutil
import subprocess
import time

from loader.core import ConfigStore, LMStudio, log_action
from loader.opencode_sync import DEFAULT_CONFIG_PATH, PROVIDERS, sync
from loader.watcher import Watcher


def resolve_opencode():
    for name in ("OpenCode.exe", "opencode.exe", "opencode.cmd", "opencode"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _launch_detached(args):
    """Launch a process without blocking the launcher or sharing its console."""
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)


OPENCODE_WARNING = (
    "warning: opencode executable not found on PATH.\n"
    "  Install opencode (https://opencode.ai/) or add its executable directory\n"
    "  to PATH, otherwise the 'opencode' menu command cannot launch it."
)


class Menu:
    """Renders a numbered choice list and reads a valid selection."""

    @staticmethod
    def choose(title, options, prompt="Your choice"):
        while True:
            print(title)
            for i, opt in enumerate(options, 1):
                print(f"{i:>3}. {opt}")
            try:
                raw = input(f"{prompt} (Enter to cancel): ").strip()
            except EOFError:
                print()
                return None
            except KeyboardInterrupt:
                print()
                return None
            if not raw:
                return None
            try:
                n = int(raw)
            except ValueError:
                print(f"  Invalid number: {raw!r}")
                continue
            if 1 <= n <= len(options):
                return n - 1
            print(f"  Out of range: {n} (1-{len(options)})")


class CommandDispatcher:
    """Routes typed commands through the command dictionary."""

    def __init__(self, app):
        self.app = app
        self.commands = {
            "exit": {"handler": app.cmd_quit, "help": "stop the watcher and exit (alias for 'quit')"},
            "help": {"handler": app.cmd_help, "help": "show help; 'help <command>' for details"},
            "import": {"handler": app.cmd_import, "help": "import a loaded instance's load config as a preset; 'import N' selects instance N directly"},
            "load": {"handler": app.cmd_load, "help": "load a model preset; 'load N' selects preset N directly"},
            "loaded": {"handler": app.cmd_loaded, "help": "list loaded instances with their load config"},
            "models": {"handler": app.cmd_models, "help": "list models available in LM Studio"},
            "opencode": {"handler": app.cmd_opencode, "help": "launch opencode (native CLI); args are passed through"},
            "presets": {"handler": app.cmd_presets, "help": "list configured load presets, marking watched ones"},
            "q": {"handler": app.cmd_quit, "help": "stop the watcher and exit (alias for 'quit')"},
            "quit": {"handler": app.cmd_quit, "help": "stop the watcher and exit (also 'exit'/'q')"},
            "reload": {"handler": app.cmd_reload, "help": "re-read model_configs.json"},
            "status": {"handler": app.cmd_status, "help": "connection summary + watcher state"},
            "sync-opencode": {"handler": app.cmd_sync_opencode, "help": "update opencode's LM Studio model lists with the watched presets' context limits"},
            "unload": {"handler": app.cmd_unload, "help": "unload a loaded instance; 'unload N' selects instance N directly"},
            "watch": {"handler": app.cmd_watch, "help": "watch start | stop | status"},
        }

    def dispatch(self, line):
        parts = line.strip().split()
        if not parts:
            return True
        cmd = parts[0].lower()
        entry = self.commands.get(cmd)
        if entry is None:
            print(f"Unknown command: {cmd}. Type 'help' for the command list.")
            return True
        handler = entry["handler"]
        try:
            return handler(parts[1:]) is not False
        except KeyboardInterrupt:
            print()
            return True
        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}")
            log_action("error", {"cmd": cmd, "error": str(e)})
            return True


class DynamicModelLoader:
    """Main application: config store, LM Studio wrapper, watcher, CLI."""

    def __init__(self):
        self.config_store = ConfigStore()
        self.lmstudio = LMStudio()
        self.watcher = Watcher(self.config_store, self.lmstudio)
        self.dispatcher = CommandDispatcher(self)

    # -- commands --

    def cmd_help(self, args):
        commands = self.dispatcher.commands
        if args:
            entry = commands.get(args[0].lower())
            if entry is None:
                print(f"No help for unknown command: {args[0]}")
                return True
            print(f"{args[0].lower():<10} {entry['help']}")
            return True
        print("Available commands:")
        for name in sorted(commands):
            print(f"  {name:<10} {commands[name]['help']}")
        print("  'help <command>' shows details for one command.")
        return True

    def cmd_models(self, args):
        models = self.lmstudio.list_downloaded()
        if not models:
            print("No downloaded models found.")
            return True
        print(f"Downloaded models ({len(models)}):")
        for model_key, display in models:
            print(f"  {model_key}  ({display})")
        return True

    def cmd_loaded(self, args):
        rows = self.lmstudio.list_loaded()
        if not rows:
            print("Nothing loaded.")
            return True
        print(f"Loaded instances ({len(rows)}):")
        for i, row in enumerate(rows, 1):
            print(f"  {i:>3}. {row['identifier']}")
            for k, v in row["config"].items():
                print(f"         {k}: {v}")
        return True

    def cmd_load(self, args):
        presets = self.config_store.presets()
        if not presets:
            print("No load presets defined in the config file.")
            return True
        idx = self._resolve_index(args, len(presets))
        if idx is None:
            idx = Menu.choose("Known load presets:", [p.label for p in presets])
        if idx is None:
            print("Cancelled.")
            return True
        preset = presets[idx]
        print(f"Loading {preset.label} ...")
        identifier = self.lmstudio.load(preset.model_key, preset.config)
        print(f"Loaded: {identifier}")
        log_action("load", {"model": preset.model_key, "preset": preset.name, "identifier": identifier})
        watched = self.config_store.watch_preset_names()
        if watched.get(preset.model_key) == preset.name:
            if self.watcher.running:
                print("Watcher running.")
            else:
                self.watcher.start()
                print("Watcher started.")
        return True

    def cmd_unload(self, args):
        rows = self.lmstudio.list_loaded()
        if not rows:
            print("Nothing loaded.")
            return True
        idx = self._resolve_index(args, len(rows))
        if idx is None:
            idx = Menu.choose("Loaded instances:", [r["identifier"] for r in rows])
        if idx is None:
            print("Cancelled.")
            return True
        identifier = rows[idx]["identifier"]
        self.lmstudio.unload(identifier)
        print(f"Unloaded: {identifier}")
        log_action("unload", {"identifier": identifier})
        return True

    def cmd_presets(self, args):
        presets = self.config_store.presets()
        if not presets:
            print("No load presets defined in the config file.")
            return True
        watched = self.config_store.watch_preset_names()
        print(f"Load presets ({len(presets)}):")
        for i, p in enumerate(presets, 1):
            mark = "  [watched]" if watched.get(p.model_key) == p.name else ""
            print(f"  {i:>3}. {p.label}{mark}")
        return True

    def cmd_watch(self, args):
        sub = args[0].lower() if args else "status"
        if sub == "start":
            if self.watcher.start():
                print("Watcher started.")
            else:
                print("Watcher already running.")
        elif sub == "stop":
            if self.watcher.stop():
                print("Watcher stopped.")
            else:
                print("Watcher is not running.")
        else:
            st = self.watcher.status()
            print(f"Watcher: {'running' if st['running'] else 'stopped'}")
            print(f"  state:    {st['state']}")
            if st["last_scan"]:
                print(f"  last scan: {time.strftime('%H:%M:%S', time.localtime(st['last_scan']))}")
            if st["last_fix"]:
                print(f"  last fix:  {time.strftime('%H:%M:%S', time.localtime(st['last_fix']))}")
        return True

    def cmd_opencode(self, args):
        exe = resolve_opencode()
        if not exe:
            print(OPENCODE_WARNING)
            return True
        print("Launching opencode in a separate window ... (launcher stays responsive)")
        try:
            proc = _launch_detached([exe, *args])
        except Exception as e:
            print(f"Error: {type(e).__name__}: {e}")
            return True
        log_action("opencode", {"args": args, "pid": proc.pid})
        print(f"opencode launched (PID {proc.pid}); the launcher remains usable here.")
        return True

    def cmd_import(self, args):
        rows = self.lmstudio.list_loaded()
        if not rows:
            print("Nothing loaded.")
            return True
        idx = self._resolve_index(args, len(rows))
        if idx is None:
            idx = Menu.choose("Loaded instances:", [r["identifier"] for r in rows])
        if idx is None:
            print("Cancelled.")
            return True
        row = rows[idx]
        identifier = row["identifier"]
        config = row["config"]
        if not config:
            print(f"No load config available for {identifier}.")
            return True
        model_key = self.config_store.resolve_key(identifier)
        print(f"Importing load config for {identifier}:")
        for k, v in config.items():
            print(f"  {k}: {v}")
        name = self._prompt("Preset name", "imported")
        if not name:
            print("Cancelled.")
            return True
        if name in self.config_store.preset_names(model_key):
            ans = self._prompt(f"Preset {name!r} already exists. Overwrite", "n").lower()
            if ans not in ("y", "yes"):
                print("Cancelled.")
                return True
        watch = self._prompt("Enable watcher for this model", "n").lower() in ("y", "yes")
        if watch:
            watched = self.config_store.watched_preset(model_key)
            if watched and watched != name:
                print(f"Model {model_key} is currently watched as {watched!r}; "
                      f"{name!r} will replace it as the watched preset.")
        self.config_store.add_preset(model_key, name, config, watch=watch)
        state = f"{model_key}  [{name}, ctx={config.get('contextLength') or 'default'}]"
        print(f"Saved: {state}")
        print(f"  -> {self.config_store.path}")
        log_action("import", {"model": model_key, "preset": name, "watch": watch})
        return True

    def cmd_status(self, args):
        try:
            loaded = self.lmstudio.list_loaded()
            downloaded = self.lmstudio.list_downloaded()
        except Exception as e:
            print(f"LM Studio connection failed: {type(e).__name__}: {e}")
            return True
        presets = self.config_store.presets()
        configured_keys = sorted({p.model_key for p in presets})
        loaded_keys = [row["identifier"] for row in loaded]
        matching = sorted({k for k in loaded_keys if any(c in k for c in configured_keys)})
        print("LM Studio: connected")
        print(f"  loaded:            {len(loaded)}")
        print(f"  downloaded:        {len(downloaded)}")
        print(f"  configured models: {len(configured_keys)}")
        print(f"  loaded+configured: {matching or 'none'}")
        for w in self.config_store.warnings():
            print(f"  config warning: {w}")
        st = self.watcher.status()
        print(f"Watcher: {'running' if st['running'] else 'stopped'} (state: {st['state']})")
        return True

    def cmd_sync_opencode(self, args):
        watched = self.config_store.watch_desired()
        if not watched:
            print("No watched models in the config; nothing to sync.")
            return True
        overrides = self.config_store.raw.get("opencode", {}).get("models", {})
        if not isinstance(overrides, dict):
            overrides = {}
        try:
            changed, missing = sync(DEFAULT_CONFIG_PATH, watched, overrides)
        except OSError as e:
            print(f"Error: cannot update {DEFAULT_CONFIG_PATH}: {e}")
            return True
        print(f"Synced {changed} model(s) into {DEFAULT_CONFIG_PATH}")
        print(f"  providers: {', '.join(PROVIDERS)}")
        for _, key in missing:
            print(f"  not found in opencode config (skipped): {key}")
        print("Restart opencode for the changes to take effect.")
        return True

    def cmd_reload(self, args):
        self.config_store.reload()
        for w in self.config_store.warnings():
            print(f"warning: {w}")
        print(f"Config reloaded from {self.config_store.path}")
        return True

    def cmd_quit(self, args):
        self.watcher.stop()
        print("Bye.")
        return False

    # -- helpers --

    @staticmethod
    def _resolve_index(args, length):
        if not args:
            return None
        try:
            n = int(args[0])
        except ValueError:
            return None
        if 1 <= n <= length:
            return n - 1
        return None

    @staticmethod
    def _prompt(message, default):
        try:
            raw = input(f"{message} (Enter for {default!r}): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""
        return raw or default

    def run(self):
        print("dynamic model loader")
        for w in self.config_store.warnings():
            print(f"warning: {w}")
        if not resolve_opencode():
            print(OPENCODE_WARNING)
        presets = self.config_store.presets()
        if presets:
            print("Known load presets:")
            for i, p in enumerate(presets, 1):
                print(f"  {i:>3}. {p.label}")
        print("Type 'help' for the command list.")
        while True:
            try:
                line = input("dynamic-loader> ")
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print()
                break
            if self.dispatcher.dispatch(line) is False:
                break
        self.watcher.stop()


def main():
    log_action("start", {})
    try:
        DynamicModelLoader().run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
