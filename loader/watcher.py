"""Configuration watcher for the dynamic model loader.

A background daemon thread that polls LM Studio and corrects the load config of
models that are already loaded. It never loads a model on its own and never
reloads a model that is not loaded (a manually unloaded model stays unloaded).
It only reacts to loaded models it has a load config for.

The watcher writes no output to stdout (the interactive CLI owns the terminal);
state and fixes go to the shared action log, and the CLI reads status via
status().
"""

import threading
import time

import lmstudio as lms

from loader.core import ConfigStore, LMStudio, config_matches, log_action, match_key, to_dict


def _load_config_of(handle):
    try:
        return to_dict(handle.get_load_config())
    except Exception:
        return None


class Watcher:
    def __init__(self, config_store=None, lmstudio=None):
        self.config_store = config_store if config_store is not None else ConfigStore()
        self.lmstudio = lmstudio if lmstudio is not None else LMStudio()
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.state = "stopped"
        self.last_scan = None
        self.last_fix = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dynamic-watcher", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        was_running = self.running
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        if was_running:
            self._set_state("stopped")
            log_action("watch_stop", {})
        return was_running

    def status(self):
        with self._lock:
            return {
                "running": self.running,
                "state": self.state,
                "last_scan": self.last_scan,
                "last_fix": self.last_fix,
            }

    # -- internals --

    def _set_state(self, state):
        with self._lock:
            self.state = state

    def _scan(self, client, desired):
        loaded = client.llm.list_loaded()
        keys = list(desired.keys())
        present = {}
        for h in loaded:
            k = match_key(h.identifier or "", keys)
            if k:
                present.setdefault(k, []).append(h)
        fixes = []
        for k, handles in present.items():
            want = desired[k]
            cfg_list = [_load_config_of(h) for h in handles]
            if any(cfg is None for cfg in cfg_list):
                continue
            if any(config_matches(cfg, want) for cfg in cfg_list):
                continue
            fixes.append(k)
        return present, fixes

    def _fix_model(self, client, key, desired):
        loaded = client.llm.list_loaded()
        for h in loaded:
            if key in (h.identifier or ""):
                try:
                    client.llm.unload(h.identifier)
                except Exception:
                    pass
        return client.llm.load_new_instance(key, config=desired).identifier

    def _run(self):
        self._set_state("running")
        log_action("watch_start", {})
        interval = None
        first = True
        while not self._stop.is_set():
            try:
                with lms.Client() as client:
                    while not self._stop.is_set():
                        if not first:
                            self._stop.wait(interval)
                            if self._stop.is_set():
                                break
                        first = False
                        self.config_store.reload()
                        poll = self.config_store.poll_settings()
                        base = poll["base"]
                        cap = poll["max"]
                        settle = poll["settle"]
                        interval = base if interval is None else max(base, min(interval, cap))
                        desired = self.config_store.watch_desired()
                        try:
                            present, fixes = self._scan(client, desired)
                        except Exception:
                            break
                        if fixes and settle > 0:
                            # Grace period: a configured model may have just
                            # been JIT-loaded by an in-flight request. Let the
                            # engine startup finish before unloading it, then
                            # re-check and only fix models still wrong.
                            log_action("settling", ",".join(sorted(fixes)))
                            self._set_state("fixing")
                            self._stop.wait(settle)
                            if self._stop.is_set():
                                break
                            try:
                                present, fixes = self._scan(client, desired)
                            except Exception:
                                break
                        state = "absent" if not present else ("fixing" if fixes else "ok:" + ",".join(sorted(present)))
                        if fixes:
                            for k in fixes:
                                try:
                                    self._fix_model(client, k, desired[k])
                                    log_action("fixed", k)
                                    with self._lock:
                                        self.last_fix = time.time()
                                except Exception as e:
                                    log_action("fix_failed", {"key": k, "error": str(e)})
                            interval = base
                        elif present:
                            interval = min(interval * 2, cap)
                        else:
                            interval = base
                        self._set_state(state)
                        with self._lock:
                            self.last_scan = time.time()
            except Exception as e:
                interval = 10
                first = True
                log_action("reconnect", str(e))
                self._stop.wait(10)
