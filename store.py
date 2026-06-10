"""信号存储：JSON 文件，按 id 去重，最多保留 N 条，线程安全。仿 claworld 的 store.py。"""
import json
import threading
from datetime import datetime
from pathlib import Path


class SignalStore:
    def __init__(self, data_dir="data", max_keep=1000):
        self.path = Path(data_dir) / "signals.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_keep = max_keep
        self._lock = threading.Lock()
        self._items = self._load()
        self._items.sort(key=lambda x: x.get("ts", ""), reverse=True)
        self._ids = {x.get("id") for x in self._items}

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._items, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, sig):
        sid = sig.get("id")
        with self._lock:
            if not sid or sid in self._ids:
                return False
            self._ids.add(sid)
            self._items.append(sig)
            self._items.sort(key=lambda x: x.get("ts", ""), reverse=True)
            if len(self._items) > self.max_keep:
                for x in self._items[self.max_keep:]:
                    self._ids.discard(x.get("id"))
                self._items = self._items[: self.max_keep]
            self._save()
            return True

    def all(self):
        with self._lock:
            return list(self._items)

    def get(self, sid):
        return next((x for x in self._items if x.get("id") == sid), None)

    def by_ticker(self, ticker):
        return [x for x in self.all() if x.get("ticker") == ticker]

    def latest_by_ticker(self, ticker):
        for x in self.all():
            if x.get("ticker") == ticker:
                return x
        return None
