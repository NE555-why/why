# -*- coding: utf-8 -*-
"""pdf转 图形界面（tkinter，仅标准库）。

界面分三层：左侧导航栏 → 各功能页 → 独立窗口（设置/日志）。
GUI 自己不碰 pipeline：通过子进程调用
    python -m why_harness run <pdf> --工作区 <dir> [--配置 <cfg>]
并实时读它的输出。子进程的 [进度] 行被界面解析成进度条，其余行进日志。

纯逻辑（构造命令行、读写设置、扫描目录、日志清洗、进度折算）都抽成模块级函数，
供 tests/test_gui.py 离线测试。
"""
from __future__ import annotations

import copy
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
try:
    import tkinter as tk
    from tkinter import filedialog, font as tkfont, messagebox, ttk
    _TK_AVAILABLE = True
except Exception:  # pragma: no cover
    tk = None
    ttk = None
    filedialog = messagebox = tkfont = None
    _TK_AVAILABLE = False
from pathlib import Path
from . import __version__, gui_settings, progress, promptlib
from .paths import LEGACY_ROOT_NAME, ROOT_NAME, migrate_legacy

try:  # 拖放是可选的：ctypes 子类化失败就退回"点按钮添加"，不影响其它功能。
    from .dnd import enable_drop
except Exception:  # pragma: no cover - 非 Windows 或模块缺失
    def enable_drop(widget, callback):
        return False

DEFAULT_KEY_ENV = "WHY_API_KEY"
DEFAULT_CONCURRENCY = 4
CONCURRENCY_MIN = 1
CONCURRENCY_MAX = 32
EXIT_DEPENDENCY = 3
DROP_HINT = "把 PDF 拖到这里，或者点一下选择文件 / 文件夹"

EXIT_TEXTS = {
    0: "完成",
    1: "输入有误",
    2: "阶段失败",
    3: "依赖或模型缺失",
    4: "有缺口",
    130: "用户中断",
}

_STAGE_TITLES = {
    "s1_pages": "① 切页",
    "s2_layout": "② 版面",
    "s3_ocr": "③ 识别",
    "s4_tables": "④ 表格",
    "s5_outline": "⑤ 切章",
    "s6_summary": "⑥ 摘要",
    "s7_pack": "⑦ 打包",
    "s8_deliver": "⑧ 交付",
}

# 配色与字体
BG = "#f4f5f7"
SIDEBAR_BG = "#222836"
SIDEBAR_HOVER = "#2c3446"
SIDEBAR_ACTIVE = "#33405c"
SIDEBAR_TEXT = "#c8d0e0"
SIDEBAR_TEXT_ACTIVE = "#ffffff"
ACCENT = "#2f6fed"
ACCENT_HOVER = "#1f5bd6"
LOG_BG = "#ffffff"
LOG_FG = "#1f2329"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_TQDM_RE = re.compile(r"^\s*\d+%\|.*\|\s*\d+(?:\.\d+)?/\d+")
_STAGE_START_RE = re.compile(r"^开始\s+(s\d_[a-z_]+)")
_STAGE_DONE_RE = re.compile(r"^完成\s+(s\d_[a-z_]+)")


# ---------------------------------------------------------------- 纯函数


def program_dir() -> Path:
    """程序目录：打包后 = exe 所在目录，开发时 = 仓库根；WHY_HARNESS_DIR 可覆盖。"""
    override = os.environ.get("WHY_HARNESS_DIR")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def python_executable() -> str:
    """子进程用的解释器：pythonw 启动时换成同目录的 python.exe，才能读到 stdout。"""
    exe = Path(sys.executable)
    if os.name == "nt" and exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(exe)


def workspace_base(base_dir=None) -> Path:
    """工作区基目录（默认 程序目录\\产物；旧版 运行时\\ 第一次使用时会改名过来）。"""
    return migrate_legacy(Path(base_dir) if base_dir else program_dir())


def default_workspace_name(pdf_stem, base_dir=None) -> Path:
    """默认工作区名：<基目录>/产物/<书名>。"""
    return workspace_base(base_dir) / pdf_stem


def workspace_field_default(pdf_stem=None) -> str:
    """工作区输入框的默认显示：产物/<书名>（相对程序目录）。"""
    return str(Path(ROOT_NAME) / pdf_stem) if pdf_stem else str(Path(ROOT_NAME))


def find_pdf_in_argv(argv):
    """在命令行参数里找第一个 .pdf 路径（拖到 exe/图标上的情形）；找不到返回 None。"""
    for arg in argv or ():
        if isinstance(arg, (str, os.PathLike)) and str(arg).lower().endswith(".pdf"):
            return Path(arg)
    return None


def find_paths_in_argv(argv):
    """命令行里所有存在的文件/目录（拖到图标上、或从命令行带参启动）。"""
    found = []
    for arg in argv or ():
        if not isinstance(arg, (str, os.PathLike)):
            continue
        try:
            path = Path(arg)
            if path.exists():
                found.append(path)
        except OSError:
            continue
    return found


# 队列能收的来源：PDF 走完整八阶段，Markdown/文本直接当正文稿（跳过版面检测和视觉模型）。
INPUT_SUFFIXES = (".pdf", ".md", ".markdown", ".txt")


def scan_pdfs(folder, recursive=True):
    """扫描目录里的 PDF / Markdown：按目录+文件名排序，跳过 Office 临时文件；坏路径返回空表。"""
    try:
        folder = Path(folder)
    except (TypeError, ValueError):
        return []
    if not folder.is_dir():
        return []
    pattern = folder.rglob("*") if recursive else folder.glob("*")
    found = []
    try:
        for path in pattern:
            try:
                if not path.is_file():
                    continue
                if path.suffix.lower() not in INPUT_SUFFIXES:
                    continue
                if path.name.startswith("~$"):
                    continue
                found.append(path)
            except OSError:
                continue
    except OSError:
        return sorted(found, key=lambda p: (str(p.parent).lower(), p.name.lower()))
    return sorted(found, key=lambda p: (str(p.parent).lower(), p.name.lower()))


def human_size(count):
    """字节数 → 人类可读（B/KB/MB/GB）；拿不到返回空串。"""
    try:
        value = float(count)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return ""


def human_duration(seconds):
    """秒数 → mm:ss / h:mm:ss。"""
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        seconds = 0
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def strip_model_prefix(value):
    """模型名只保留最后一个 / 之后的部分（去掉 提供商/ 前缀）。"""
    value = (value or "").strip()
    return value.rsplit("/", 1)[-1].strip() if "/" in value else value


def strip_trailing_slashes(value):
    """接口地址去掉结尾多余的 /。"""
    return (value or "").rstrip("/")


def normalize_base_url(value):
    """把用户随手粘的地址修成能用的形式：补协议、去掉 /chat/completions、去尾斜杠。

    防呆：很多人会粘 "api.xxx.com/v1" 或整条 "https://.../v1/chat/completions"，
    原样发给 urllib 只会报一个看不懂的 URLError；这里先按最常见的两种手误纠正。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = "https://" + text
    for suffix in ("/chat/completions", "/completions"):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)]
            break
    return strip_trailing_slashes(text)


def normalize_api_key(value):
    """去掉用户复制时带上的 "Bearer " 前缀和首尾空白。"""
    text = str(value or "").strip()
    if text.lower().startswith("bearer "):
        text = text[7:].strip()
    return text


def parse_settings_text(text):
    """解析设置文本 → 扁平 dict {section.key: value}（与 config.load 同口径）。"""
    data = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            continue
        if "=" in line:
            key, value = (x.strip() for x in line.split("=", 1))
            data[f"{section}.{key}"] = value
    return data


def clamp_concurrency(value):
    """把任意值夹到 1–32 的整数；解析不了回退默认 4。"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = DEFAULT_CONCURRENCY
    return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, value))


def read_settings_fields(path):
    """读设置文件里 GUI 需要的字段；文件缺失/损坏一律返回默认值，不抛异常。"""
    p = Path(path)
    data = {}
    if p.is_file():
        try:
            data = parse_settings_text(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            data = {}
    return {
        "地址": strip_trailing_slashes(str(data.get("提供商.地址", "") or "")),
        "识别模型": strip_model_prefix(data.get("阶段.识别.模型", "")),
        "摘要模型": strip_model_prefix(data.get("阶段.摘要.模型", "")),
        "key": str(data.get("提供商.key", "") or "").strip(),
        "key_环境变量": str(data.get("提供商.key_环境变量", "") or "").strip(),
        "并发": clamp_concurrency(data.get("全局.并发", "")),
    }


def set_field_in_text(text, section, key, value):
    """在设置文本里把 [section] 的 key 设为 value，其它行原样保留。"""
    header = f"[{section}]"
    lines = text.splitlines()
    in_section = False
    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == header
            continue
        if in_section and not stripped.startswith(("#", ";")) and "=" in stripped:
            if stripped.split("=", 1)[0].strip() == key:
                lines[i] = f"{key} = {value}"
                replaced = True
    if replaced:
        return "\n".join(lines)
    header_index = None
    for i, raw in enumerate(lines):
        if raw.strip() == header:
            header_index = i
            break
    if header_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(header)
        lines.append(f"{key} = {value}")
        return "\n".join(lines)
    j = header_index + 1
    while j < len(lines) and not (lines[j].strip().startswith("[") and lines[j].strip().endswith("]")):
        j += 1
    lines.insert(j, f"{key} = {value}")
    return "\n".join(lines)


def write_fields(path, updates):
    """把一组 (section, key, value) 写进设置文件，保留其它键不变；返回新文本。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = ""
    if p.is_file():
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
    for section, key, value in updates:
        text = set_field_in_text(text, section, key, value)
    p.write_text(text, encoding="utf-8")
    return text


def write_key(path, key):
    """把 key 写到设置文件的 [提供商] key，保留其它键不变。"""
    return write_fields(path, [("提供商", "key", key)])


def ensure_settings_file(path, template=None):
    """确保设置文件存在；缺失就从包内模板复制一份（都不存在则写空文件）。"""
    p = Path(path)
    if p.is_file():
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    src = Path(template) if template else (Path(__file__).resolve().parent / "设置.txt")
    if src.is_file():
        p.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    else:
        p.write_text("", encoding="utf-8")
    return p


def key_env_name(fields_or_value, default=DEFAULT_KEY_ENV):
    """key 的环境变量名：取设置文件里的 [提供商] key_环境变量，空则回退默认。"""
    if isinstance(fields_or_value, dict):
        name = str(fields_or_value.get("key_环境变量", "") or "").strip()
    else:
        name = str(fields_or_value or "").strip()
    return name or default


def build_command(pdf, workspace, config=None, python=None, subcommand="run"):
    """构造子进程命令行：python -m why_harness run|补跑 <pdf> --工作区 <dir> [--配置 <cfg>]。"""
    cmd = [python if python is not None else python_executable(),
           "-m", "why_harness", subcommand, str(pdf),
           "--工作区", str(workspace)]
    if config:
        cmd += ["--配置", str(config)]
    return cmd


def exit_code_text(code):
    """退出码 → 中文说明。"""
    return EXIT_TEXTS.get(code, "失败")


def decode_console(raw):
    """子进程输出解码：先 UTF-8，失败再按 GB18030/GBK（Paddle 的 C++ 日志是本地编码）。"""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        for encoding in ("gb18030", "gbk"):
            try:
                return raw.decode(encoding)
            except (UnicodeDecodeError, AttributeError):
                continue
        try:
            return raw.decode("utf-8", "replace")
        except AttributeError:
            return str(raw)


def clean_log_text(text, drop_progress=True):
    """清洗子进程输出：去 ANSI、拆回车、丢进度条残片；返回可显示的多行文本。"""
    text = _ANSI_RE.sub("", str(text or "").replace("\r\n", "\n").replace("\r", "\n"))
    kept = []
    for line in text.split("\n"):
        line = line.rstrip()
        if not line.strip():
            continue
        if drop_progress and line.strip().startswith(progress.LINE_PREFIX):
            continue
        if _TQDM_RE.match(line):
            continue
        if line.startswith("WARNING: Logging before InitGoogleLogging"):
            continue
        kept.append(line)
    return "\n".join(kept)


def line_tag(line):
    """一行日志 → 颜色标签。"""
    text = line.strip()
    if text.startswith("完成 s") or text.startswith("完成，"):
        return "ok"
    if "失败" in text or "Traceback" in text or "Error" in text or "error" in text:
        return "error"
    if "警告" in text or "warn" in text.lower():
        return "warn"
    if text.startswith("开始 ") or text.startswith("$ "):
        return "gray"
    return "info"


def _first_int(value):
    """取字符串里第一个整数；没有就返回 None。"""
    m = re.search(r"-?\d+", value or "")
    return int(m.group(0)) if m else None


_REPORT_FIELDS = {
    "交付项": ("书名", "text"),
    "正文": ("正文", "text"),
    "节点数": ("节点数", "int"),
    "最深层数": ("最深层数", "int"),
    "图片": ("图片", "count"),
    "表格数": ("表格数", "int"),
    "过期摘要数": ("过期摘要数", "int"),
}


def parse_report(text):
    """把 产物/<书名>/说明.txt 解析成 dict（纯函数，不碰文件系统）。"""
    data = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        for sep in ("：", ":"):
            if sep in line:
                key, _, value = line.partition(sep)
                break
        else:
            continue
        key, value = key.strip(), value.strip()
        name, kind = _REPORT_FIELDS.get(key, (None, None))
        if name is None:
            continue
        if kind == "text":
            if value:
                data[name] = value
        elif kind == "count":
            m = re.search(r"共\s*(\d+)\s*张", value) or re.search(r"\d+", value)
            if m:
                data[name] = int(m.group(1) if m.groups() else m.group(0))
        else:
            n = _first_int(value)
            if n is not None:
                data[name] = n
    return data


def report_summary(report):
    """parse_report 的结果 → 一行摘要；一个字段都没解析到就只给「完成」。"""
    report = report or {}
    parts = []
    if "节点数" in report:
        parts.append(f"{report['节点数']} 个章节")
    if "图片" in report:
        parts.append(f"{report['图片']} 张图")
    if "表格数" in report:
        parts.append(f"{report['表格数']} 张表")
    if "过期摘要数" in report:
        parts.append(f"过期摘要 {report['过期摘要数']}")
    if not parts:
        return "完成"
    return "完成：" + " · ".join(parts)


def output_dir(workspace, stem):
    """成品目录：新版成品直接放在书目录（产物/<书>/<书>.why）。

    旧版多套了一层 产物/<书>/，这里做个兼容：书目录里既没有 .why 也没有 .md 时，
    再看旧的嵌套目录。"""
    workspace = Path(workspace)
    try:
        if not (workspace / f"{stem}.why").is_file() and not (workspace / f"{stem}.md").is_file():
            legacy = workspace / "产物" / stem
            if legacy.is_dir():
                return legacy
    except OSError:
        pass
    return workspace


_ERROR_HINTS = ("失败：", "：失败", "Traceback", "Error:", "错误：", "异常：")


def looks_like_error(line):
    """这行日志像不像报错：用来在失败弹窗里摘两句真正的原因。"""
    text = str(line or "").strip()
    if not text:
        return False
    if text.startswith(("Traceback", "Error:", "error:")):
        return True
    return any(hint in text for hint in _ERROR_HINTS)


def ui_font(size=10, bold=False):
    family = "Microsoft YaHei UI" if os.name == "nt" else "DejaVu Sans"
    return (family, size, "bold" if bold else "normal")


def mono_font(size=9):
    family = "Consolas" if os.name == "nt" else "DejaVu Sans Mono"
    return (family, size)


def setup_style(root):
    """统一 ttk 外观：clam 主题 + 中文正文字体 + 卡片/标题样式。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", font=ui_font(10), background=BG)
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground="#1f2329")
    style.configure("Card.TFrame", background="#ffffff")
    style.configure("TLabelframe", background=BG, bordercolor="#dfe3ea")
    style.configure("TLabelframe.Label", background=BG, foreground="#3a4150", font=ui_font(10, True))
    style.configure("Title.TLabel", font=ui_font(16, True), background=BG)
    style.configure("Sub.TLabel", font=ui_font(9), foreground="#6b7280", background=BG)
    style.configure("Hint.TLabel", font=ui_font(9), foreground="#c0392b", background=BG)
    style.configure("Stage.TLabel", font=ui_font(11, True), background=BG)
    style.configure("TButton", padding=(10, 5))
    style.configure("Treeview", rowheight=26, font=ui_font(10), background="#ffffff",
                    fieldbackground="#ffffff", bordercolor="#dfe3ea")
    style.configure("Treeview.Heading", font=ui_font(10, True), background="#eef1f6")
    style.map("Treeview", background=[("selected", "#d8e4ff")], foreground=[("selected", "#1f2329")])
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", padding=(16, 8), font=ui_font(10))
    style.configure("TEntry", padding=4)
    style.configure("TCombobox", padding=4)
    style.configure("TProgressbar", thickness=14)
    # 进度条两种状态：正常是蓝色，失败时整条变红（clam 主题才吃 background，换成 vista 主题会失效）。
    style.configure("Horizontal.TProgressbar", thickness=14, background="#2f6fed",
                    troughcolor="#e6e9ef", bordercolor="#d5d9e0",
                    lightcolor="#2f6fed", darkcolor="#2f6fed")
    style.configure("Danger.Horizontal.TProgressbar", thickness=14, background="#c0392b",
                    troughcolor="#f6e2e0", bordercolor="#e0b3ad",
                    lightcolor="#c0392b", darkcolor="#c0392b")





class _NavButton:
    """左侧导航按钮（tk.Label 组合，方便控制深色底色的悬停/选中态）。"""

    def __init__(self, parent, text, command):
        self.command = command
        self.active = False
        self.frame = tk.Frame(parent, bg=SIDEBAR_BG, cursor="hand2")
        self.label = tk.Label(self.frame, text=text, bg=SIDEBAR_BG, fg=SIDEBAR_TEXT,
                              anchor="w", padx=18, pady=9, font=ui_font(11))
        self.label.pack(fill="x")
        for widget in (self.frame, self.label):
            widget.bind("<Button-1>", self._click)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    def _click(self, _event=None):
        self.command()

    def _enter(self, _event=None):
        if not self.active:
            self.frame.configure(bg=SIDEBAR_HOVER)
            self.label.configure(bg=SIDEBAR_HOVER)

    def _leave(self, _event=None):
        if not self.active:
            self.frame.configure(bg=SIDEBAR_BG)
            self.label.configure(bg=SIDEBAR_BG)

    def set_active(self, active):
        self.active = bool(active)
        bg = SIDEBAR_ACTIVE if active else SIDEBAR_BG
        fg = SIDEBAR_TEXT_ACTIVE if active else SIDEBAR_TEXT
        self.frame.configure(bg=bg)
        self.label.configure(bg=bg, fg=fg, font=ui_font(11, active))

    def pack(self, **kwargs):
        self.frame.pack(**kwargs)


class SettingsWindow:
    """独立的设置窗口：模型服务 / 运行 / 输出 / 高级 四个分页。"""

    def __init__(self, app):
        self.app = app
        self.root = app.root
        self.data = copy.deepcopy(app.settings)
        self.path = app.settings_path
        self.index = max(0, int(self.data.get("当前服务", 0) or 0))
        # 防呆：第一次打开时没有任何服务也能直接在右边表单里填，不用先找「新增」。
        if not self.data.get("服务"):
            self.data["服务"] = [{
                "名称": "服务 1", "地址": "", "key": "",
                "key_环境变量": DEFAULT_KEY_ENV, "识别模型": "", "摘要模型": "",
            }]
            self.data["当前服务"] = 0
            self.index = 0
        self._closed = False
        self._models = []

        self.win = tk.Toplevel(self.root)
        self.win.title("设置 · pdf转")
        self.win.geometry("920x660")
        self.win.minsize(840, 580)
        self.win.configure(bg=BG)
        try:
            self.win.transient(self.root)
        except tk.TclError:
            pass
        self.win.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build()
        self._refresh_service_list(select=self.index)
        self._load_form(self.index)
        self._load_run_tab()
        self._saved_signature = self._signature()

    # ---------- 布局 ----------
    def _build(self):
        head = ttk.Frame(self.win, padding=(18, 14, 18, 6))
        head.pack(fill="x")
        ttk.Label(head, text="设置", style="Title.TLabel").pack(side="left")
        ttk.Label(head, text="接口、快速/精确档、并发、自动补跑都在这里；改完点「保存」。",
                  style="Sub.TLabel").pack(side="left", padx=12)

        self.notebook = ttk.Notebook(self.win)
        self.notebook.pack(fill="both", expand=True, padx=18, pady=(4, 0))
        self.tab_service = ttk.Frame(self.notebook, padding=12)
        self.tab_run = ttk.Frame(self.notebook, padding=12)
        self.tab_output = ttk.Frame(self.notebook, padding=12)
        self.tab_advanced = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.tab_service, text="  模型服务  ")
        self.notebook.add(self.tab_run, text="  质量与速度  ")
        self.notebook.add(self.tab_output, text="  输出  ")
        self.notebook.add(self.tab_advanced, text="  高级  ")
        self._build_service_tab()
        self._build_run_tab()
        self._build_output_tab()
        self._build_advanced_tab()

        bottom = ttk.Frame(self.win, padding=(18, 8, 18, 14))
        bottom.pack(fill="x")
        self.path_label = ttk.Label(bottom, text=str(self.path), style="Sub.TLabel")
        self.path_label.pack(side="left")
        ttk.Button(bottom, text="保存", command=self._save).pack(side="right", padx=(8, 0))
        ttk.Button(bottom, text="取消", command=self._on_close).pack(side="right")

    def _build_service_tab(self):
        tab = self.tab_service
        left = ttk.Frame(tab)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="保存的接口（可存多套，随时切换）").pack(anchor="w", pady=(0, 6))
        box = ttk.Frame(left)
        box.pack(fill="both", expand=True)
        self.service_tree = ttk.Treeview(box, columns=("name", "model"), show="headings",
                                         height=14, selectmode="browse")
        self.service_tree.heading("name", text="名称")
        self.service_tree.heading("model", text="模型")
        self.service_tree.column("name", width=150)
        self.service_tree.column("model", width=150)
        self.service_tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(box, orient="vertical", command=self.service_tree.yview)
        scroll.pack(side="right", fill="y")
        self.service_tree.configure(yscrollcommand=scroll.set)
        self.service_tree.bind("<<TreeviewSelect>>", self._on_service_select)

        ops = ttk.Frame(left)
        ops.pack(fill="x", pady=(8, 0))
        ttk.Button(ops, text="新增", command=self._new_service).pack(side="left")
        ttk.Button(ops, text="删除", command=self._delete_service).pack(side="left", padx=6)
        ttk.Button(ops, text="设为当前", command=self._set_active).pack(side="left")

        right = ttk.LabelFrame(tab, text="当前这一套", padding=12)
        right.pack(side="left", fill="both", expand=True, padx=(14, 0))
        right.columnconfigure(1, weight=1)

        self.v_name = tk.StringVar()
        self.v_base = tk.StringVar()
        self.v_key = tk.StringVar()
        self.v_env = tk.StringVar(value=DEFAULT_KEY_ENV)
        self.v_ocr = tk.StringVar()
        self.v_sum = tk.StringVar()

        rows = [
            ("名称（随便叫）", ttk.Entry(right, textvariable=self.v_name)),
            ("接口地址", ttk.Entry(right, textvariable=self.v_base)),
            ("key", ttk.Entry(right, textvariable=self.v_key, show="*")),
            ("key 环境变量名", ttk.Entry(right, textvariable=self.v_env)),
        ]
        for row, (label, widget) in enumerate(rows):
            ttk.Label(right, text=label).grid(row=row, column=0, sticky="w", pady=4)
            widget.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=4)

        ttk.Label(right, text="识别模型").grid(row=4, column=0, sticky="w", pady=4)
        self.ocr_combo = ttk.Combobox(right, textvariable=self.v_ocr, values=[])
        self.ocr_combo.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=4)
        ttk.Label(right, text="摘要模型").grid(row=5, column=0, sticky="w", pady=4)
        self.sum_combo = ttk.Combobox(right, textvariable=self.v_sum, values=[])
        self.sum_combo.grid(row=5, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=4)

        hint = ttk.Label(right, style="Sub.TLabel", wraplength=430, justify="left",
                         text="识别和摘要可以填同一个模型；名字只填最后一个 / 之后的部分。\n"
                              "地址会自动补 https:// 并去掉结尾的 /chat/completions；key 不要带 Bearer 前缀。")
        hint.grid(row=6, column=0, columnspan=3, sticky="w", pady=(6, 2))

        buttons = ttk.Frame(right)
        buttons.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.fetch_btn = ttk.Button(buttons, text="获取可用模型", command=self._fetch_models)
        self.fetch_btn.pack(side="left")
        self.test_btn = ttk.Button(buttons, text="测试连接", command=self._test_service)
        self.test_btn.pack(side="left", padx=8)
        ttk.Button(buttons, text="保存这一套", command=self._save_current).pack(side="left")

        self.service_status = ttk.Label(right, text="", style="Sub.TLabel")
        self.service_status.grid(row=8, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_run_tab(self):
        tab = self.tab_run
        form = ttk.LabelFrame(tab, text="质量与速度", padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        self.v_conc = tk.StringVar(value=str(self.data.get("并发", DEFAULT_CONCURRENCY)))
        self.v_tier = tk.StringVar(value=gui_settings.tier_value(self.data.get("版面档位", "M")))
        self.v_effort_ocr = tk.StringVar(value=str(self.data.get("识别思考强度", "中")))
        self.v_effort_sum = tk.StringVar(value=str(self.data.get("摘要思考强度", "关")))
        self.v_retry = tk.StringVar(value=str(self.data.get("重试次数", 2)))
        self.v_auto = tk.StringVar(value=str(self.data.get("自动补跑轮数", 2)))
        self.v_timeout = tk.StringVar(value=str(self.data.get("超时秒", 180)))

        ttk.Label(form, text="并发数").grid(row=0, column=0, sticky="w", pady=5)
        spin = ttk.Spinbox(form, from_=CONCURRENCY_MIN, to=CONCURRENCY_MAX, width=6,
                           textvariable=self.v_conc)
        spin.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=5)
        ttk.Label(form, text="运行中也能改，改完立刻生效（写进工作区 状态\\并发.txt）。",
                  style="Sub.TLabel").grid(row=0, column=2, sticky="w", padx=(10, 0))

        ttk.Label(form, text="版面档位").grid(row=1, column=0, sticky="w", pady=5)
        tier_box = ttk.Frame(form)
        tier_box.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=5)
        for label, value in gui_settings.TIER_CHOICES:
            ttk.Radiobutton(tier_box, text=label, value=value,
                            variable=self.v_tier).pack(side="left", padx=(0, 12))
        ttk.Label(form, text="快速档约 1 秒/页（M，24 MB）；精确档更准但慢 5–8 倍（plus-L，130 MB）。"
                             "缺模型时启动会先问你要不要下载。",
                  style="Sub.TLabel", wraplength=520, justify="left").grid(
            row=1, column=2, sticky="w", padx=(10, 0))

        ttk.Label(form, text="识别思考强度").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Combobox(form, textvariable=self.v_effort_ocr, values=list(gui_settings.EFFORT_CHOICES),
                     state="readonly", width=8).grid(row=2, column=1, sticky="w", padx=(8, 0), pady=5)
        ttk.Label(form, text="扫描页识别多想想 / 少想想；「关」= 不传这个参数。",
                  style="Sub.TLabel").grid(row=2, column=2, sticky="w", padx=(10, 0))

        ttk.Label(form, text="摘要思考强度").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Combobox(form, textvariable=self.v_effort_sum, values=list(gui_settings.EFFORT_CHOICES),
                     state="readonly", width=8).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=5)
        ttk.Label(form, text="摘要量最大，档位越高越慢越贵。",
                  style="Sub.TLabel").grid(row=3, column=2, sticky="w", padx=(10, 0))

        ttk.Label(form, text="单次重试次数").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Spinbox(form, from_=0, to=10, width=6, textvariable=self.v_retry).grid(
            row=4, column=1, sticky="w", padx=(8, 0), pady=5)
        ttk.Label(form, text="一个请求内部最多重试几次（4xx 不重试）。", style="Sub.TLabel").grid(
            row=4, column=2, sticky="w", padx=(10, 0))

        ttk.Label(form, text="自动补跑轮数").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Spinbox(form, from_=0, to=10, width=6, textvariable=self.v_auto).grid(
            row=5, column=1, sticky="w", padx=(8, 0), pady=5)
        ttk.Label(form, text="识别失败的页自动再试几轮（只重试失败的页）。", style="Sub.TLabel").grid(
            row=5, column=2, sticky="w", padx=(10, 0))

        ttk.Label(form, text="超时秒").grid(row=6, column=0, sticky="w", pady=5)
        ttk.Spinbox(form, from_=30, to=1800, width=8, textvariable=self.v_timeout).grid(
            row=6, column=1, sticky="w", padx=(8, 0), pady=5)
        ttk.Label(form, text="单次请求最长等多久。", style="Sub.TLabel").grid(
            row=6, column=2, sticky="w", padx=(10, 0))

        note = ttk.LabelFrame(tab, text="出问题时", padding=12)
        note.pack(fill="x", pady=(14, 0))
        ttk.Label(note, style="Sub.TLabel", wraplength=760, justify="left",
                  text="有页识别失败时，程序会先自动补跑；还不行就停下来。"
                       "关掉设置后点「补跑」，只重试失败页和后面的阶段，已成功的不再花钱。").pack(anchor="w")

    def _build_output_tab(self):
        tab = self.tab_output
        form = ttk.LabelFrame(tab, text="目录", padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        self.v_wsbase = tk.StringVar(value=str(self.data.get("工作区基目录", "") or ""))
        self.v_scan = tk.StringVar(value=str(self.data.get("扫描目录", "") or ""))
        self.v_recursive = tk.BooleanVar(value=bool(self.data.get("递归扫描", True)))
        self.v_open_after = tk.BooleanVar(value=bool(self.data.get("完成后打开文件夹", False)))

        ttk.Label(form, text="工作区基目录").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(form, textvariable=self.v_wsbase).grid(row=0, column=1, sticky="ew", padx=(8, 6), pady=5)
        ttk.Button(form, text="浏览…", command=self._browse_wsbase).grid(row=0, column=2, pady=5)
        ttk.Label(form, text="留空 = 程序目录\\产物；每本书在里面建同名子目录。",
                  style="Sub.TLabel").grid(row=1, column=0, columnspan=3, sticky="w")

        ttk.Label(form, text="扫描目录").grid(row=2, column=0, sticky="w", pady=(10, 5))
        ttk.Entry(form, textvariable=self.v_scan).grid(row=2, column=1, sticky="ew", padx=(8, 6), pady=(10, 5))
        ttk.Button(form, text="浏览…", command=self._browse_scan).grid(row=2, column=2, pady=(10, 5))
        ttk.Checkbutton(form, text="包含子目录", variable=self.v_recursive).grid(
            row=3, column=1, sticky="w", padx=(8, 0))
        ttk.Checkbutton(form, text="全部完成后自动打开产物目录", variable=self.v_open_after).grid(
            row=4, column=1, sticky="w", padx=(8, 0), pady=(4, 0))

        deliver = ttk.LabelFrame(tab, text="交付内容", padding=12)
        deliver.pack(fill="x", pady=(14, 0))
        deliver.columnconfigure(1, weight=1)

        self.v_output = tk.StringVar(
            value=gui_settings.output_value(self.data.get("输出格式", "both")))
        self.v_keep_runtime = tk.BooleanVar(value=bool(self.data.get("保留中间数据", True)))
        self.v_provenance = tk.BooleanVar(value=bool(self.data.get("溯源段", True)))

        ttk.Label(deliver, text="交付格式").grid(row=0, column=0, sticky="w", pady=5)
        formats = ttk.Frame(deliver)
        formats.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=5)
        for label, value in gui_settings.OUTPUT_CHOICES:
            ttk.Radiobutton(formats, text=label, value=value,
                            variable=self.v_output).pack(side="left", padx=(0, 14))
        ttk.Label(deliver, text="Markdown 适合阅读和再加工；.why 是单文件电子书（正文 + 摘要 + 插图 + 表格）。",
                  style="Sub.TLabel", wraplength=520, justify="left").grid(
            row=1, column=1, sticky="w", padx=(8, 0))
        ttk.Checkbutton(deliver, variable=self.v_keep_runtime,
                        text="成功后保留中间数据（状态、页图、日志）；关掉只留成品，"
                             "省空间，但之后不能再「补跑」").grid(
            row=2, column=1, sticky="w", padx=(8, 0), pady=(10, 2))
        ttk.Checkbutton(deliver, variable=self.v_provenance,
                        text=".why 末尾写溯源段（页数、版面模型、识别方式、提示词指纹）").grid(
            row=3, column=1, sticky="w", padx=(8, 0), pady=(2, 0))

    def _build_advanced_tab(self):
        tab = self.tab_advanced
        box = ttk.LabelFrame(tab, text="位置", padding=12)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        info = [
            ("程序目录", str(self.app.program_dir())),
            ("设置文件", str(self.path)),
            ("提示词目录", str(promptlib.override_dir())),
            ("模型目录", self.app.model_dir_text()),
        ]
        self.adv_paths = {}
        for row, (label, value) in enumerate(info):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", pady=4)
            entry = ttk.Entry(box)
            entry.insert(0, value)
            entry.configure(state="readonly")
            entry.grid(row=row, column=1, sticky="ew", padx=(8, 6), pady=4)
            ttk.Button(box, text="打开", command=lambda p=value: self.app.open_path(p)).grid(
                row=row, column=2, pady=4)

        warn = ttk.LabelFrame(tab, text="恢复默认", padding=12)
        warn.pack(fill="x", pady=(14, 0))
        ttk.Label(warn, style="Sub.TLabel",
                  text="恢复默认只会清空界面设置（服务、目录、并发），不会删书稿、产物和提示词。").pack(anchor="w")
        ttk.Button(warn, text="恢复默认设置…", command=self._reset_settings).pack(anchor="w", pady=(8, 0))

    # ---------- 服务列表 ----------
    def _refresh_service_list(self, select=None):
        self.service_tree.delete(*self.service_tree.get_children())
        services = self.data.get("服务") or []
        active = int(self.data.get("当前服务", 0) or 0)
        for i, service in enumerate(services):
            name = str(service.get("名称") or f"服务 {i + 1}")
            if i == active:
                name = "● " + name
            model = str(service.get("识别模型") or "")
            self.service_tree.insert("", "end", iid=str(i), values=(name, model))
        if select is not None and 0 <= select < len(services):
            self.service_tree.selection_set(str(select))
            self.service_tree.see(str(select))
            self.index = select

    def _load_form(self, index):
        services = self.data.get("服务") or []
        service = services[index] if 0 <= index < len(services) else {}
        self.v_name.set(str(service.get("名称") or ""))
        self.v_base.set(str(service.get("地址") or ""))
        self.v_key.set(str(service.get("key") or ""))
        self.v_env.set(str(service.get("key_环境变量") or DEFAULT_KEY_ENV))
        self.v_ocr.set(str(service.get("识别模型") or ""))
        self.v_sum.set(str(service.get("摘要模型") or ""))
        self.index = index if 0 <= index < len(services) else -1

    def _flush_form(self):
        if self.index is None or self.index < 0:
            return
        services = self.data.setdefault("服务", [])
        if self.index >= len(services):
            return
        service = services[self.index]
        base = normalize_base_url(self.v_base.get())
        key = normalize_api_key(self.v_key.get())
        self.v_base.set(base)
        self.v_key.set(key)
        service["名称"] = self.v_name.get().strip()
        service["地址"] = base
        service["key"] = key
        service["key_环境变量"] = self.v_env.get().strip()
        service["识别模型"] = strip_model_prefix(self.v_ocr.get())
        service["摘要模型"] = strip_model_prefix(self.v_sum.get())

    def _on_service_select(self, _event=None):
        selection = self.service_tree.selection()
        if not selection:
            return
        new_index = int(selection[0])
        if new_index == self.index:
            return
        self._flush_form()
        self._refresh_service_list(select=self.index)
        self._load_form(new_index)

    def _new_service(self):
        self._flush_form()
        services = self.data.setdefault("服务", [])
        services.append({"名称": f"服务 {len(services) + 1}", "地址": "", "key": "",
                         "key_环境变量": DEFAULT_KEY_ENV, "识别模型": "", "摘要模型": ""})
        if len(services) == 1:
            self.data["当前服务"] = 0
        self._refresh_service_list(select=len(services) - 1)
        self._load_form(len(services) - 1)
        self._refresh_service_list(select=len(services) - 1)

    def _delete_service(self):
        services = self.data.get("服务") or []
        if not services:
            return
        index = self.index if 0 <= (self.index or 0) < len(services) else 0
        name = services[index].get("名称") or f"服务 {index + 1}"
        if not messagebox.askyesno("删除服务", f"确定删除「{name}」？", parent=self.win):
            return
        self.data = gui_settings.remove_service(self.data, index)
        self.index = min(index, max(0, len(self.data.get("服务") or []) - 1))
        self._refresh_service_list(select=self.index if self.data.get("服务") else None)
        self._load_form(self.index)
        self.service_status.configure(text="已删除")

    def _set_active(self):
        services = self.data.get("服务") or []
        if not services:
            return
        self.data["当前服务"] = max(0, self.index)
        self._refresh_service_list(select=max(0, self.index))
        self.service_status.configure(text="已设为当前使用的接口")

    def _save_current(self):
        self._flush_form()
        self._refresh_service_list(select=max(0, self.index))
        self.service_status.configure(text="已保存到列表（记得点底部「保存」）")

    # ---------- 网络探测 ----------
    def _form_key(self):
        key = self.v_key.get().strip()
        return key or os.environ.get(self.v_env.get().strip() or DEFAULT_KEY_ENV, "")

    def _run_in_thread(self, work, done):
        self._set_busy(True)

        def runner():
            try:
                result, error = work(), None
            except Exception as exc:  # noqa: BLE001 - 网络错误一律弹给用户
                result, error = None, str(exc)
            if self._closed:
                return
            try:
                self.win.after(0, lambda: done(result, error))
            except tk.TclError:
                pass

        threading.Thread(target=runner, daemon=True).start()

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        self.fetch_btn.configure(state=state, text="获取中…" if busy else "获取可用模型")
        self.test_btn.configure(state=state)

    def _fetch_models(self):
        base = normalize_base_url(self.v_base.get())
        self.v_base.set(base)
        key = self._form_key()
        if not base:
            messagebox.showwarning("还没填地址", "先填接口地址，例如 https://api.example.com/v1", parent=self.win)
            return
        self.service_status.configure(text="正在获取模型列表…")
        self._run_in_thread(
            lambda: gui_settings.fetch_models(base, key),
            self._fetch_done)

    def _fetch_done(self, models, error):
        self._set_busy(False)
        if error:
            self.service_status.configure(text="获取失败：" + error)
            return
        self._models = list(models or [])
        self.ocr_combo.configure(values=self._models)
        self.sum_combo.configure(values=self._models)
        if self._models:
            if not self.v_ocr.get().strip():
                self.v_ocr.set(self._models[0])
            if not self.v_sum.get().strip():
                self.v_sum.set(self._models[0])
            self.service_status.configure(text=f"可用模型 {len(self._models)} 个，已填进下拉框")
        else:
            self.service_status.configure(text="接口通了，但没返回任何模型")

    def _test_service(self):
        base = normalize_base_url(self.v_base.get())
        self.v_base.set(base)
        key = self._form_key()
        if not base:
            messagebox.showwarning("还没填地址", "先填接口地址。", parent=self.win)
            return
        self.service_status.configure(text="正在测试连接…")
        self._run_in_thread(
            lambda: gui_settings.test_service(base, key),
            self._test_done)

    def _test_done(self, result, error):
        self._set_busy(False)
        if error:
            self.service_status.configure(text="测试失败：" + error)
            return
        ok, message = result
        self.service_status.configure(text=("✓ " if ok else "✗ ") + message)

    def _load_run_tab(self):
        self.v_conc.set(str(clamp_concurrency(self.data.get("并发", DEFAULT_CONCURRENCY))))
        self.v_tier.set(gui_settings.tier_value(self.data.get("版面档位", "M")))
        self.v_auto.set(str(self.data.get("自动补跑轮数", 2)))

    # ---------- 目录选择 ----------
    def _browse_wsbase(self):
        folder = filedialog.askdirectory(title="选择工作区基目录", parent=self.win)
        if folder:
            self.v_wsbase.set(folder)

    def _browse_scan(self):
        folder = filedialog.askdirectory(title="选择要扫描的目录", parent=self.win)
        if folder:
            self.v_scan.set(folder)

    def _reset_settings(self):
        if not messagebox.askyesno("恢复默认", "确定把界面设置恢复成默认值？", parent=self.win):
            return
        self.data = gui_settings.default_settings()
        self.index = 0
        self._refresh_service_list(select=None)
        self._load_form(0)
        self.v_conc.set(str(self.data["并发"]))
        self.v_tier.set(gui_settings.tier_value(self.data.get("版面档位", "M")))
        self.v_effort_ocr.set(str(self.data["识别思考强度"]))
        self.v_effort_sum.set(str(self.data["摘要思考强度"]))
        self.v_retry.set("2")
        self.v_auto.set(str(self.data.get("自动补跑轮数", 2)))
        self.v_timeout.set("180")
        self.v_wsbase.set("")
        self.v_scan.set(str(self.data.get("扫描目录", "")))
        self.v_recursive.set(True)
        self.v_open_after.set(False)
        self.v_output.set(gui_settings.output_value(self.data.get("输出格式", "both")))
        self.v_keep_runtime.set(True)
        self.v_provenance.set(True)

    # ---------- 保存 ----------
    def _collect(self):
        self._flush_form()
        self.data["并发"] = clamp_concurrency(self.v_conc.get())
        self.data["识别思考强度"] = self.v_effort_ocr.get() or "关"
        self.data["摘要思考强度"] = self.v_effort_sum.get() or "关"
        self.data["版面档位"] = gui_settings.tier_value(self.v_tier.get())
        try:
            self.data["自动补跑轮数"] = max(0, min(10, int(float(self.v_auto.get()))))
        except (TypeError, ValueError):
            self.data["自动补跑轮数"] = 2
        try:
            self.data["重试次数"] = max(0, min(10, int(float(self.v_retry.get()))))
        except (TypeError, ValueError):
            self.data["重试次数"] = 2
        try:
            self.data["超时秒"] = max(30, min(1800, int(float(self.v_timeout.get()))))
        except (TypeError, ValueError):
            self.data["超时秒"] = 180
        self.data["工作区基目录"] = self.v_wsbase.get().strip()
        self.data["扫描目录"] = self.v_scan.get().strip()
        self.data["递归扫描"] = bool(self.v_recursive.get())
        self.data["完成后打开文件夹"] = bool(self.v_open_after.get())
        self.data["输出格式"] = gui_settings.output_value(self.v_output.get())
        self.data["保留中间数据"] = bool(self.v_keep_runtime.get())
        self.data["溯源段"] = bool(self.v_provenance.get())
        services = self.data.get("服务") or []
        index = int(self.data.get("当前服务", 0) or 0)
        if services and not (0 <= index < len(services)):
            self.data["当前服务"] = 0
        return self.data

    def _signature(self):
        """当前所有控件值的指纹，用来判断关窗口时有没有没保存的改动。"""
        try:
            return json.dumps(self._collect(), ensure_ascii=False, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001 - 指纹算不出来时按"没改动"处理，不能拦着用户关窗
            return ""

    def _validate(self):
        """保存前防呆：地址、key、模型名缺一个都会在转换时炸，宁可现在说清楚。"""
        self._flush_form()
        problems = []
        services = self.data.get("服务") or []
        if not services:
            return ["还没有接口：先点「新增」，填一个 OpenAI 兼容接口的地址和 key。"]
        index = int(self.data.get("当前服务", 0) or 0)
        if not (0 <= index < len(services)):
            index = 0
        service = services[index]
        name = str(service.get("名称") or f"服务 {index + 1}")
        base = str(service.get("地址") or "").strip()
        key = str(service.get("key") or "").strip()
        env = str(service.get("key_环境变量") or "").strip()
        if not base:
            problems.append(f"「{name}」还没填接口地址，例如 https://api.example.com/v1")
        elif not re.match(r"^https?://[^s/]+", base):
            problems.append(f"「{name}」的地址不像网址：{base}（应以 http:// 或 https:// 开头）")
        elif any(ch.isspace() for ch in base):
            problems.append(f"「{name}」的地址里有空格，检查一下是否多粘了字符。")
        if not key and not env:
            problems.append(f"「{name}」既没填 key，也没填 key 环境变量名。")
        if not str(service.get("识别模型") or "").strip():
            problems.append(f"「{name}」还没填识别模型。点「获取可用模型」选一个，或手填模型名。")
        if not str(service.get("摘要模型") or "").strip():
            problems.append(f"「{name}」还没填摘要模型。识别和摘要可以填同一个。")
        return problems

    def _save(self):
        problems = self._validate()
        if problems:
            messagebox.showwarning(
                "还不能保存",
                "\n".join(problems) + "\n\n地址会自动补 https://、去掉结尾的 /chat/completions。",
                parent=self.win)
            try:
                self.notebook.select(self.tab_service)
            except tk.TclError:
                pass
            return
        try:
            self.win.destroy()
        except tk.TclError:
            pass
        self.app.settings_window = None


def main(argv=None):
    """入口：cli 的 gui 子命令调用。"""
    if not _TK_AVAILABLE:
        print("当前环境没有 tkinter，无法打开图形界面。请安装带 tkinter 的 Python 3.12。",
              file=sys.stderr)
        return 3
    import tkinter as tk
    from .gui_app import PdfGui
    args = sys.argv[1:] if argv is None else list(argv)
    root = tk.Tk()
    setup_style(root)
    PdfGui(root, find_paths_in_argv(args))
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
