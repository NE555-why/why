# -*- coding: utf-8 -*-
"""图形界面的「界面设置」与「多套模型服务」持久化逻辑。

纯逻辑模块：不导入 tkinter，不碰 gui.py / cli.py / pipeline，除标准库外只依赖
urllib。GUI 只需要调用这里的函数读写界面设置文件，并可用 fetch_models /
test_service 探测模型服务是否可用。

设置文件是 UTF-8 JSON，键为中文；结构见 default_settings()。文件可能被用户手工
改坏，因此 load_settings() / active_service() / upsert_service() /
remove_service() 对任何输入都不抛异常，只会把不合适的数据纠正回默认值。
"""
import copy
import json
import os
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SETTINGS_FILENAME = "界面设置.json"
VERSION = 1
DEFAULT_KEY_ENV = "WHY_API_KEY"
DEFAULT_CONCURRENCY = 4
CONCURRENCY_MIN = 1
CONCURRENCY_MAX = 32

EFFORT_CHOICES = ("关", "最小", "低", "中", "高")
# 版面档位：面板模型越精确越慢。M=快速档（约 24 MB），plus-L=精确档（约 130 MB）。
TIER_CHOICES = (("快速档（M）", "M"), ("精确档（plus-L）", "plus-L"))
_TIER_VALUES = {label: value for label, value in TIER_CHOICES}
_TIER_LABELS = {value: label for label, value in TIER_CHOICES}

# 交付格式：both = Markdown + .why（默认）；md = 只要 Markdown；why = 只要 .why。
OUTPUT_CHOICES = (("Markdown + .why", "both"), ("只要 Markdown", "md"), ("只要 .why", "why"))
_OUTPUT_VALUES = {label: value for label, value in OUTPUT_CHOICES}
_OUTPUT_LABELS = {value: label for label, value in OUTPUT_CHOICES}


def output_value(label) -> str:
    """交付格式中文标签 → 设置值（both/md/why）；未知按 both。"""
    text = str(label or "").strip()
    if text in _OUTPUT_VALUES:
        return _OUTPUT_VALUES[text]
    if text in _OUTPUT_LABELS:
        return text
    return "both"


def output_label(value) -> str:
    """交付格式设置值 → 中文标签；未知值按 both。"""
    return _OUTPUT_LABELS.get(str(value or "").strip(), _OUTPUT_LABELS["both"])


def tier_value(label) -> str:
    """档位中文标签 → 设置值（M / plus-L）；已是设置值原样返回，未知回退 M。"""
    text = str(label or "").strip()
    if text in _TIER_VALUES:
        return _TIER_VALUES[text]
    if text in _TIER_LABELS:
        return text
    return "M"


def tier_label(value) -> str:
    """档位设置值 → 中文标签；未知值按 M 处理。"""
    return _TIER_LABELS.get(str(value or "").strip(), _TIER_LABELS["M"])

_EFFORT_TO_VALUE = {
    "关": "",
    "最小": "minimal",
    "低": "low",
    "中": "medium",
    "高": "high",
}
_VALUE_TO_EFFORT = {
    "": "关",
    "none": "关",
    "minimal": "最小",
    "low": "低",
    "medium": "中",
    "high": "高",
}

_SERVICE_KEYS = ("名称", "地址", "key", "key_环境变量", "识别模型", "摘要模型")


def effort_value(label) -> str:
    """思考强度中文标签 → API 参数值；未知标签返回空串。"""
    if not isinstance(label, str):
        return ""
    return _EFFORT_TO_VALUE.get(label.strip(), "")


def effort_label(value) -> str:
    """思考强度 API 参数值 → 中文标签；空值/None 视为「关」，未知值原样返回。"""
    if value is None:
        return "关"
    text = value if isinstance(value, str) else str(value)
    key = text.strip()
    if key == "":
        return "关"
    low = key.lower()
    if low in _VALUE_TO_EFFORT:
        return _VALUE_TO_EFFORT[low]
    if key in _EFFORT_TO_VALUE:
        return key
    return text


def _safe_home() -> Path:
    try:
        return Path.home()
    except Exception:  # noqa: BLE001 - Path.home() 在极简环境可能抛 RuntimeError
        try:
            return Path(tempfile.gettempdir())
        except Exception:  # noqa: BLE001
            return Path.cwd()


def _default_scan_dir() -> str:
    home = _safe_home()
    try:
        desktop = home / "Desktop"
        if desktop.is_dir():
            return str(desktop)
    except OSError:
        pass
    return str(home)


def _empty_service() -> dict:
    return {key: "" for key in _SERVICE_KEYS}


def default_settings() -> dict:
    """返回一份全新的默认界面设置。"""
    return {
        "版本": VERSION,
        "服务": [],
        "当前服务": 0,
        "扫描目录": _default_scan_dir(),
        "递归扫描": True,
        "工作区基目录": "",
        "并发": DEFAULT_CONCURRENCY,
        "版面档位": "M",
        "识别思考强度": "中",
        "摘要思考强度": "关",
        "自动补跑轮数": 2,
        "输出格式": "both",
        "保留中间数据": True,
        "溯源段": True,
        "完成后打开文件夹": False,
        "上次产物目录": "",
    }


# ---------------------------------------------------------------- 数据纠正


def _as_text(value, default: str = "") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return str(value)
    return default


def _as_int(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return default
    if isinstance(value, str):
        try:
            return int(value.strip())
        except (TypeError, ValueError):
            return default
    return default


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "y", "on", "是", "真", "开"):
            return True
        if text in ("0", "false", "no", "n", "off", "否", "假", "关", ""):
            return False
    return default


def _as_effort(value, default: str) -> str:
    if isinstance(value, str):
        return effort_label(value)
    return default


def _normalize_service(service) -> dict:
    result = _empty_service()
    if not isinstance(service, dict):
        return result
    for key, value in service.items():
        if key not in result:
            try:
                result[key] = copy.deepcopy(value)
            except Exception:  # noqa: BLE001 - 极端不可拷贝对象也不能崩
                result[key] = value
    for key in _SERVICE_KEYS:
        value = service.get(key, "")
        result[key] = _as_text(value, "")
    result["地址"] = str(result.get("地址", "")).strip().rstrip("/")
    return result


def _normalize_services(value) -> list:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            result.append(_normalize_service(item))
    return result


def _normalize_settings(data) -> dict:
    """补默认值、纠正类型，保留未知键；不修改入参。"""
    result = default_settings()
    if not isinstance(data, dict):
        return result
    try:
        source = copy.deepcopy(data)
    except Exception:  # noqa: BLE001
        source = dict(data)

    for key, value in source.items():
        if key == "版本":
            result[key] = _as_int(value, VERSION)
        elif key == "服务":
            result[key] = _normalize_services(value)
        elif key == "当前服务":
            result[key] = _as_int(value, 0)
        elif key in ("扫描目录", "工作区基目录", "上次产物目录"):
            result[key] = _as_text(value, result[key])
        elif key in ("递归扫描", "完成后打开文件夹", "保留中间数据", "溯源段"):
            result[key] = _as_bool(value, result[key])
        elif key == "输出格式":
            result[key] = output_value(_as_text(value, "both"))
        elif key == "并发":
            value_int = _as_int(value, DEFAULT_CONCURRENCY)
            result[key] = max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, value_int))
        elif key == "版面档位":
            result[key] = tier_value(_as_text(value, "M"))
        elif key == "自动补跑轮数":
            value_int = _as_int(value, 2)
            result[key] = max(0, min(10, value_int))
        elif key == "识别思考强度":
            result[key] = _as_effort(value, result[key])
        elif key == "摘要思考强度":
            result[key] = _as_effort(value, result[key])
        else:
            # 未知键保留，方便以后加字段而旧代码不丢数据。
            result[key] = value
    return result


# ---------------------------------------------------------------- 读写


def load_settings(path) -> dict:
    """读取设置；文件缺失、损坏、类型不对都只补默认值，绝不抛异常。"""
    try:
        file_path = Path(path)
    except Exception:  # noqa: BLE001
        return default_settings()
    try:
        text = file_path.read_text(encoding="utf-8-sig")
    except Exception:  # noqa: BLE001 - 不存在/权限/目录/编码错误都退回默认
        return default_settings()
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return default_settings()
    return _normalize_settings(data)


def _user_settings_path() -> Path:
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "pdf转" / SETTINGS_FILENAME
        return _safe_home() / ".pdf转" / SETTINGS_FILENAME
    return _safe_home() / ".local" / "share" / "pdf转" / SETTINGS_FILENAME


def _writable_probe(directory: Path) -> bool:
    """真写一个临时文件来探测目录可写；比 os.access 在 Windows 上可靠。"""
    probe = None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / (
            f".{SETTINGS_FILENAME}.probe-{os.getpid()}-{time.time_ns()}.tmp"
        )
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("")
        return True
    except OSError:
        return False
    finally:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass


def _default_program_dir() -> Path:
    """与 gui.program_dir() 相同口径：环境变量 → 打包 exe 目录 → 仓库根。"""
    try:
        override = os.environ.get("WHY_HARNESS_DIR")
        if override:
            return Path(override).resolve()
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parents[1]
    except Exception:  # noqa: BLE001
        try:
            return Path.cwd()
        except Exception:  # noqa: BLE001
            return Path(".")


def settings_path(program_dir=None) -> Path:
    """设置文件路径：优先程序目录，目录不可写时退回用户数据目录。"""
    if program_dir is None or (isinstance(program_dir, str) and not program_dir.strip()):
        base = _default_program_dir()
    else:
        try:
            base = Path(program_dir)
        except Exception:  # noqa: BLE001
            base = _default_program_dir()

    candidate = base / SETTINGS_FILENAME
    if _writable_probe(base):
        if candidate.exists():
            try:
                with open(candidate, "a", encoding="utf-8"):
                    pass
            except OSError:
                return _user_settings_path()
        return candidate
    return _user_settings_path()


def save_settings(path, data) -> None:
    """原子写入 UTF-8 JSON：同目录 .tmp 写满后 os.replace。"""
    file_path = Path(path)
    if not isinstance(data, dict):
        data = default_settings()
    payload = json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n"
    parent = file_path.parent
    parent.mkdir(parents=True, exist_ok=True)

    tmp_path = parent / (file_path.name + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp_path, file_path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 服务操作


def active_service(data) -> dict:
    """当前服务；索引越界取第一个，服务为空返回默认空服务。"""
    services = data.get("服务") if isinstance(data, dict) else None
    if not isinstance(services, list) or not services:
        return _empty_service()
    index = _as_int(data.get("当前服务", 0), 0) if isinstance(data, dict) else 0
    if not (0 <= index < len(services)):
        index = 0
    service = services[index]
    if isinstance(service, dict):
        return _normalize_service(service)
    for item in services:
        if isinstance(item, dict):
            return _normalize_service(item)
    return _empty_service()


def upsert_service(data, service, index=None) -> dict:
    """追加或替换一套服务，返回新的 data，不修改入参。"""
    result = _normalize_settings(data)
    services = result["服务"]
    new_service = _normalize_service(service)

    position = None
    if index is not None:
        position = _as_int(index, -1)
    if position is not None and 0 <= position < len(services):
        services[position] = new_service
    else:
        services.append(new_service)
    result["服务"] = services
    return result


def remove_service(data, index) -> dict:
    """删除一套服务并修正当前索引；索引越界时原样返回（新对象）。"""
    result = _normalize_settings(data)
    services = result["服务"]
    position = _as_int(index, -1)
    if not (0 <= position < len(services)):
        return result

    current = _as_int(result.get("当前服务", 0), 0)
    if not (0 <= current < len(services)):
        current = 0
    del services[position]
    if not services:
        current = 0
    else:
        if current > position:
            current -= 1
        if current < 0:
            current = 0
        if current >= len(services):
            current = len(services) - 1
    result["服务"] = services
    result["当前服务"] = current
    return result


# ---------------------------------------------------------------- 模型接口


def _resolve_key(key) -> str:
    text = _as_text(key, "").strip()
    if text:
        return text
    return _as_text(os.environ.get(DEFAULT_KEY_ENV, ""), "").strip()


def _clean_base(base_url) -> str:
    text = _as_text(base_url, "").strip()
    if not text:
        raise RuntimeError("地址为空")
    return text.rstrip("/")


def _models_url(base_url: str) -> str:
    if base_url.lower().endswith("/models"):
        return base_url
    return base_url + "/models"


def _extract_model_ids(payload) -> list:
    if isinstance(payload, dict):
        items = payload.get("data")
        if items is None:
            items = payload.get("models")
    elif isinstance(payload, list):
        items = payload
    else:
        items = None
    if not isinstance(items, list):
        raise RuntimeError("返回格式不对（缺少模型列表）")

    result = []
    seen = set()
    for item in items:
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            model_id = item.get("id")
            if not isinstance(model_id, str):
                model_id = item.get("name")
        else:
            model_id = None
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        if model_id in seen:
            continue
        seen.add(model_id)
        result.append(model_id)
    return result


def fetch_models(base_url, key, timeout=10) -> list[str]:
    """GET {base}/models，返回去重保序的模型 id 列表。失败抛中文 RuntimeError。"""
    base = _clean_base(base_url)
    url = _models_url(base)
    resolved_key = _resolve_key(key).replace("\r", "").replace("\n", "")

    headers = {"Accept": "application/json", "User-Agent": "why-harness-gui/1.0"}
    if resolved_key:
        headers["Authorization"] = f"Bearer {resolved_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        code = getattr(exc, "code", 0)
        try:  # HTTPError 本身是响应对象，不关会在 GC 时冒 ResourceWarning。
            exc.close()
        except Exception:  # noqa: BLE001
            pass
        if code in (401, 403):
            raise RuntimeError("key 无效或无权限") from exc
        if code == 404:
            raise RuntimeError("地址不对（找不到 /models）") from exc
        raise RuntimeError(f"接口返回 HTTP {code}") from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RuntimeError(f"连接超时：{exc}") from exc
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"连接失败：{reason}") from exc
    except OSError as exc:
        raise RuntimeError(f"连接失败：{exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"地址不对：{exc}") from exc

    text = raw.decode("utf-8-sig", errors="replace")
    try:
        payload = json.loads(text)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"返回内容不是 JSON：{exc}") from exc
    return _extract_model_ids(payload)


def test_service(base_url, key, timeout=8) -> tuple[bool, str]:
    """探测服务是否可用，返回 (可用?, 中文说明)，永不抛异常。"""
    try:
        models = fetch_models(base_url, key, timeout=timeout)
    except RuntimeError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"接口不可用：{exc}"
    return True, f"接口正常，共 {len(models)} 个模型"