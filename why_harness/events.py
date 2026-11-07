"""运行事件 JSONL 日志。"""
from __future__ import annotations
import json, time, traceback
from datetime import datetime
from pathlib import Path
from typing import Any

class EventLog:
    def __init__(self, path: str | Path):
        self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a", encoding="utf-8"); self._started = time.perf_counter(); self._closed = False
        self._stages = {"success": 0, "failed": 0}; self._call_seconds = 0.0; self._tokens = 0
        self.log("run_start")
    @staticmethod
    def _now(): return datetime.now().astimezone().isoformat(timespec="milliseconds")
    def log(self, kind: str, **fields: Any):
        event = {"t": self._now(), "kind": kind, **fields}
        self._fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n"); self._fh.flush(); return event
    def stage(self, name: str, status: str, **fields: Any):
        elapsed = fields.pop("elapsed", fields.pop("wall", 0.0))
        if status in self._stages: self._stages[status] += 1
        return self.log("stage_end", stage=name, status=status, elapsed=float(elapsed or 0), wall=float(elapsed or 0), usage={"call_seconds": round(self._call_seconds, 6), "tokens": self._tokens}, **fields)
    def call(self, usage: Any):
        data = dict(vars(usage)) if hasattr(usage, "__dict__") else dict(usage or {})
        self._call_seconds += float(data.get("call_seconds", data.get("seconds", data.get("duration", 0))) or 0)
        tokens = data.get("tokens")
        if tokens is None: tokens = (data.get("prompt_tokens") or 0) + (data.get("completion_tokens") or 0)
        self._tokens += int(tokens or 0); data["call_seconds"] = round(self._call_seconds, 6); data["tokens"] = self._tokens
        return self.log("call", usage=data)
    def error(self, exc: BaseException, **fields):
        summary = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return self.log("error", error=summary, traceback=summary, **fields)
    def close(self):
        if self._closed: return
        wall = round(time.perf_counter() - self._started, 6)
        self.log("run_end", elapsed=wall, wall=wall, usage={"call_seconds": round(self._call_seconds, 6), "tokens": self._tokens}, success_stages=self._stages["success"], failed_stages=self._stages["failed"])
        self._fh.close(); self._closed = True
    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        if exc: self.error(exc)
        self.close()
