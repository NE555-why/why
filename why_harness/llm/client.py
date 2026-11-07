"""生产级 OpenAI 兼容客户端：重试、响应校验、图片和用量账本。"""
from __future__ import annotations

import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .errors import EmptyResponseError, FatalLLMError, LLMError, TruncatedError

DEFAULT_MAX_TOKENS = 65536       # 配置没写"单页最大token/token上限"时的兜底：必须发出去，否则上限由服务商默认值决定


class Client:
    def __init__(self, config):
        self.config = config
        self.base = str(self._get("提供商", "地址", "") or "").strip().rstrip("/")
        env_name = str(self._get("提供商", "key_环境变量", "WHY_API_KEY") or "WHY_API_KEY")
        self.key = str(self._get("提供商", "key", "") or os.environ.get(env_name, ""))
        self.timeout = self._number("全局", "超时秒", 180.0)
        self.retry_count = max(0, int(self._number("全局", "重试次数", 2)))
        self.backoff_factor = self._number("全局", "重试退避秒", 1.0)
        self._started = time.monotonic()
        self._call_seconds = 0.0
        # ③⑥ 用线程池并发调用同一个 Client，账本（calls / tokens / retries / 秒数）是共享可变状态，
        # 不加锁的话 `+=` 会互相覆盖、最后统计的调用次数偏少。
        # 选"单 Client + 锁"而不是"每线程一个 Client 再合并"：用量只有一份真相，重试和超时策略也统一，
        # 汇总时不用再写合并逻辑；锁只包住计数，不包住网络请求，所以不会把并发串行化。
        self._lock = threading.Lock()
        self._ledger = {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0, "retries": 0,
            "wall_seconds": 0.0, "call_seconds": 0.0,
        }

    @property
    def calls(self):
        with self._lock:
            return self._ledger["calls"]

    @property
    def prompt_tokens(self):
        with self._lock:
            return self._ledger["prompt_tokens"]

    @property
    def completion_tokens(self):
        with self._lock:
            return self._ledger["completion_tokens"]

    def _get(self, section: str, key: str, default: Any = None) -> Any:
        """支持扁平配置字典，也兼容 ConfigParser 风格对象。"""
        flat = f"{section}.{key}"
        if hasattr(self.config, "get"):
            try:
                value = self.config.get(flat, None)
                if value is not None:
                    return value
            except (TypeError, AttributeError):
                pass
            try:
                value = self.config.get(section, {}).get(key, default)
                return default if value is None else value
            except (AttributeError, TypeError):
                pass
        return default

    def _number(self, section: str, key: str, default: float) -> float:
        try:
            return float(self._get(section, key, default))
        except (TypeError, ValueError):
            return default

    def _image_url(self, image) -> str:
        raw = Path(image).read_bytes() if isinstance(image, (str, os.PathLike, Path)) else bytes(image)
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif raw.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            mime = "image/webp"
        else:
            raise LLMError("图片格式无法识别：仅支持 PNG、JPEG 或 WebP")
        return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"

    def _body(self, prompt: str, image, stage: str) -> dict:
        model = str(self._get(f"阶段.{stage}", "模型", "") or "").split("/", 1)[-1]
        if not model:
            raise LLMError(f"未配置模型：[阶段.{stage}] 模型")
        content: list[dict] = [{"type": "text", "text": prompt}]
        if image is not None:
            content.append({"type": "image_url", "image_url": {"url": self._image_url(image)}})
        body = {"model": model, "messages": [{"role": "user", "content": content}]}
        effort = self._get(f"阶段.{stage}", "推理强度", "")
        if effort is not None and str(effort).strip():
            body["reasoning_effort"] = str(effort).strip()
        limit = self._get(f"阶段.{stage}", "单页最大token", None)
        if limit is None or not str(limit).strip():
            limit = self._get(f"阶段.{stage}", "token上限", None)
        if limit is None or not str(limit).strip():
            limit = DEFAULT_MAX_TOKENS       # 配置缺省也要发：不发就等于把上限交给服务商默认值
        try:
            limit = int(float(limit))
        except (TypeError, ValueError):
            pass
        parameter = str(self._get(f"阶段.{stage}", "token上限参数名", "max_tokens") or "max_tokens")
        body[parameter] = limit
        return body

    @staticmethod
    def _error_message(status: int, server_message: str = "") -> str:
        if status == 402:
            return "余额不足：请充值或换 key"
        if status in (401, 403):
            return "key 无效或无权限"
        if status == 404:
            return "模型名不存在"
        if status == 400:
            return f"请求参数错误：{server_message}" if server_message else "请求参数错误"
        return f"模型请求失败（HTTP {status}）"

    @staticmethod
    def _server_message(raw: bytes) -> str:
        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
            error = data.get("error", data)
            if isinstance(error, dict):
                return str(error.get("message", ""))
            return str(error)
        except (ValueError, UnicodeError):
            return raw.decode("utf-8", errors="replace")[:500]

    def _bump(self, name: str, amount: int = 1) -> None:
        """账本计数原子自增：③⑥ 的并发调用会从多个线程同时写同一份账本。"""
        with self._lock:
            self._ledger[name] += amount

    def _charge_seconds(self, elapsed: float) -> None:
        """累计调用墙钟时间（同样要在锁里，否则并发时会被覆盖）。"""
        with self._lock:
            self._call_seconds += elapsed

    def _record_usage(self, out: dict) -> None:
        usage = out.get("usage") or {}
        with self._lock:
            self._ledger["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
            self._ledger["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
            self._ledger["reasoning_tokens"] += int(usage.get("reasoning_tokens", 0) or usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0) or 0)
            self._ledger["cached_tokens"] += int(usage.get("cached_tokens", 0) or usage.get("prompt_tokens_details", {}).get("cached_tokens", 0) or 0)

    def key_error_message(self) -> str:
        """缺 key 的提示：点名配置里的 key_环境变量 名；没配该键则回退通用提示。"""
        env_name = str(self._get("提供商", "key_环境变量", "") or "").strip()
        if env_name:
            return f"未配置 key：请设置环境变量 {env_name}（[提供商] key_环境变量）"
        return "未配置 key：请在设置文件 [提供商] key_环境变量 填环境变量名，或填 [提供商] key"

    def chat(self, prompt, image=None, stage="识别") -> str:
        if not self.base:
            raise LLMError("未配置接口地址：[提供商] 地址")
        body = self._body(prompt, image, stage)
        if not self.key:
            raise RuntimeError(self.key_error_message())
        raw_url = self.base + "/chat/completions"
        last_error: Exception | None = None
        for attempt in range(self.retry_count + 1):
            started = time.monotonic()
            try:
                req = urllib.request.Request(
                    raw_url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                    headers={"Authorization": "Bearer " + self.key, "Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    out = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                elapsed = time.monotonic() - started
                self._charge_seconds(elapsed)
                status = exc.code
                message = self._server_message(exc.read())
                if status in (400, 401, 402, 403, 404):
                    error = FatalLLMError(self._error_message(status, message))
                    error.fatal = True
                    raise error
                last_error = LLMError(f"模型请求失败（HTTP {status}）：{message}" if message else f"模型请求失败（HTTP {status}）")
                retry_after = exc.headers.get("Retry-After")
                delay = self._delay(attempt, retry_after) if status == 429 else self._delay(attempt)
            except (urllib.error.URLError, TimeoutError, TimeoutError) as exc:
                self._charge_seconds(time.monotonic() - started)
                last_error = LLMError(f"网络请求失败：{exc}")
                delay = self._delay(attempt)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._charge_seconds(time.monotonic() - started)
                last_error = LLMError(f"响应解析失败：{exc}")
                delay = self._delay(attempt)
            else:
                self._charge_seconds(time.monotonic() - started)
                choices = out.get("choices")
                if not choices:
                    raise EmptyResponseError("内容被拦截或返回为空")
                choice = choices[0] or {}
                if choice.get("finish_reason") == "length":
                    raise TruncatedError("输出被 token 上限截断")
                message = choice.get("message") or {}
                content = message.get("content")
                if not content and message.get("reasoning_content"):
                    content = message["reasoning_content"]
                    import warnings
                    warnings.warn("message.content 为空，已使用 reasoning_content 兜底", RuntimeWarning, stacklevel=2)
                self._record_usage(out)
                self._bump("calls")
                return content or ""
            if attempt >= self.retry_count:
                break
            self._bump("retries")
            time.sleep(delay)
        raise last_error or LLMError("模型请求失败")

    def _delay(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        table = (0.0, 8.0, 25.0, 60.0)
        return table[min(attempt, len(table) - 1)] * self.backoff_factor

    def ledger(self) -> dict:
        with self._lock:
            result = dict(self._ledger)
            result["call_seconds"] = self._call_seconds
        result["wall_seconds"] = time.monotonic() - self._started
        return result
