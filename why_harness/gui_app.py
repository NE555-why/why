# -*- coding: utf-8 -*-
"""pdf转 图形界面主窗口（tkinter，仅标准库）。

界面本身不碰流水线：每本书起一个子进程
    python -m why_harness run <pdf> --工作区 <基目录>/<书名>
读它的 stdout/stderr，把 [进度] 行折算成总进度。

纯逻辑（命令行、设置、扫描、日志清洗、进度折算）在 gui.py；
界面搭建在 gui_app_pages.py；扫描/队列/子进程在 gui_app_run.py。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import time
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import messagebox
except Exception:  # pragma: no cover - 无 tkinter 时模块仍可 import
    tk = messagebox = None

from . import gui, gui_settings
from .gui import SettingsWindow
from .gui_app_pages import _PagesMixin
from .gui_app_run import _RunMixin


class PdfGui(_PagesMixin, _RunMixin):
    """主窗口：左侧导航 + 转换/提示词/关于三页 + 底部状态栏。"""

    def __init__(self, root, initial_paths=None):
        self.root = root
        self._closed = False
        self._program = gui.program_dir()
        self.settings_path = gui_settings.settings_path(self._program)
        self.settings = gui_settings.load_settings(self.settings_path)
        # 旧设置文件可能只有 [版面] 档位（写在 设置.txt），没有界面设置的「版面档位」。
        # 记下这次是不是显式存过「版面档位」，没存过就继续尊重旧的 版面.档位。
        self._tier_explicit = False
        try:
            raw = json.loads(Path(self.settings_path).read_text(encoding="utf-8-sig"))
            self._tier_explicit = isinstance(raw, dict) and "版面档位" in raw
        except Exception:  # noqa: BLE001 - 文件缺失/损坏按没存过处理
            pass
        self.settings_window = None
        self.current_page = "convert"

        self._queue = queue.Queue()
        self._pump_job = None
        self._tick_job = None
        self._books = {}
        self._order = []
        self._known = set()
        self._next_iid = 0
        self._scan_seq = 0
        self._running = False
        self._cancel_requested = False
        self._proc = None
        self._run_books = []
        self._run_total = 0
        self._run_index = 0
        self._run_started = 0.0
        self._retry_mode = False
        self._retry_book = None
        self._stage_state = {}
        self._book_fraction = 0.0
        self._byte_buffer = b""
        self._token = 0
        self._current_workspace = None
        self._last_md = None
        self._last_why = None
        self._last_output_dir = None
        self._failed = []
        self._error_lines = []
        self._current_prompt = "ocr.md"
        self._prompt_by_iid = {}
        self.log_window = None

        self.scan_var = tk.StringVar(value=str(self.settings.get("扫描目录") or ""))
        self.recursive_var = tk.BooleanVar(value=bool(self.settings.get("递归扫描", True)))
        self.concurrency_var = tk.StringVar(
            value=str(gui.clamp_concurrency(self.settings.get("并发", gui.DEFAULT_CONCURRENCY))))
        self.tier_var = tk.StringVar(
            value=gui_settings.tier_label(self.settings.get("版面档位", "M")))
        self.status_var = tk.StringVar(value="就绪")
        self.stage_var = tk.StringVar(value="就绪")
        self.percent_var = tk.StringVar(value="0%")
        self.duration_var = tk.StringVar(value="00:00")
        self.queue_count_var = tk.StringVar(value="共 0 本 · 已勾选 0 本")
        self.result_var = tk.StringVar(value="还没有结果。")

        self.root.title("pdf转 · 把扫描 PDF 做成电子书")
        self.root.geometry("1180x820")
        self.root.minsize(1020, 680)
        self.root.configure(bg=gui.BG)
        try:
            gui.setup_style(self.root)
        except Exception:  # noqa: BLE001 - 样式失败不影响功能
            pass

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._pump_job = self.root.after(80, self._pump)
        self._tick_job = self.root.after(1000, self._tick)
        self.root.after(200, self._install_drop)
        self._consume_initial_paths(initial_paths)
        self.root.after(600, self._startup_scan)

    # ------------------------------------------------------------ 设置/目录
    def program_dir(self) -> Path:
        return self._program

    def model_dir_text(self) -> str:
        try:
            from .media.models import MODELS_ROOT
            return str(MODELS_ROOT)
        except Exception:  # noqa: BLE001 - 模型模块缺失时退回约定目录
            return str(self._program / "模型")

    def open_settings(self):
        if self.settings_window is not None:
            try:
                self.settings_window.win.lift()
                return self.settings_window
            except tk.TclError:
                self.settings_window = None
        self.settings_window = SettingsWindow(self)
        return self.settings_window

    _open_settings = open_settings

    def apply_settings(self, data):
        """设置窗口点保存：写盘、重新读一遍（纠正类型），同步界面变量。"""
        old_tier = gui_settings.tier_value(self.settings.get("版面档位", "M"))
        gui_settings.save_settings(self.settings_path, data)
        self.settings = gui_settings.load_settings(self.settings_path)
        self._tier_explicit = True
        self._sync_settings_vars()
        refresh = getattr(self, "_refresh_setup_hint", None)
        if callable(refresh):
            refresh()
        refresh = getattr(self, "_refresh_settings_summary", None)
        if callable(refresh):
            refresh()
        if gui_settings.tier_value(self.settings.get("版面档位", "M")) != old_tier:
            self.root.after(100, self._check_model_on_startup)

    def _sync_settings_vars(self):
        self.scan_var.set(str(self.settings.get("扫描目录") or ""))
        self.recursive_var.set(bool(self.settings.get("递归扫描", True)))
        self.concurrency_var.set(
            str(gui.clamp_concurrency(self.settings.get("并发", gui.DEFAULT_CONCURRENCY))))
        self.tier_var.set(gui_settings.tier_label(self.settings.get("版面档位", "M")))

    def open_path(self, path):
        """用系统默认程序打开文件/目录；目录不存在时先建出来。"""
        try:
            target = Path(path)
        except (TypeError, ValueError) as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)
            return
        if not target.exists():
            if target.suffix:
                messagebox.showwarning("文件不存在", str(target), parent=self.root)
                return
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("创建目录失败", str(exc), parent=self.root)
                return
        try:
            if os.name == "nt":
                os.startfile(str(target))
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    # ------------------------------------------------------------ 拖放/启动扫描
    def _install_drop(self):
        if self._closed:
            return
        try:
            gui.enable_drop(self.root, self._on_drop)
        except Exception:  # noqa: BLE001 - 拖放是可选功能
            pass

    def _on_drop(self, paths):
        for raw in paths or ():
            try:
                path = Path(raw)
            except (TypeError, ValueError):
                continue
            if path.is_dir():
                self._scan_paths(path, self._recursive(), replace=False, silent=True)
            elif path.is_file() and path.suffix.lower() in gui.INPUT_SUFFIXES:
                self._add_paths([path])

    def _consume_initial_paths(self, paths):
        pdfs, folders = [], []
        for raw in paths or ():
            try:
                path = Path(raw)
            except (TypeError, ValueError):
                continue
            if path.is_dir():
                folders.append(path)
            elif path.is_file() and path.suffix.lower() in gui.INPUT_SUFFIXES:
                pdfs.append(path)
        if pdfs:
            self._add_paths(pdfs)
        for folder in folders:
            self._scan_paths(folder, self._recursive(), replace=False, silent=True)

    def _startup_scan(self):
        if self._closed:
            return
        folder = self.scan_var.get().strip()
        if folder and Path(folder).is_dir():
            self._scan_paths(folder, self._recursive(), replace=False, silent=True)

    # ------------------------------------------------------------ 计时/关闭
    def _tick(self):
        if self._running and self._run_started:
            self.duration_var.set(gui.human_duration(time.monotonic() - self._run_started))
        try:
            self._tick_job = self.root.after(1000, self._tick)
        except tk.TclError:
            self._tick_job = None

    def on_close(self):
        if self._closed:
            return
        if self._running:
            if not messagebox.askyesno("正在转换", "还有任务在跑，确定退出并终止子进程？",
                                       parent=self.root):
                return
            self._cancel_requested = True
            proc = self._proc
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass
        self._closed = True
        try:
            self.root.destroy()
        except tk.TclError:
            pass