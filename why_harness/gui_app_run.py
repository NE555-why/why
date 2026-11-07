# -*- coding: utf-8 -*-
"""PdfGui 的扫描、队列、子进程、进度与日志行为（mixin）。

控件在 gui_app_pages.py 里搭好；这里只关心状态和子进程。gui_app.PdfGui
把三个 mixin 拼起来，文件各自保持在可读长度。
"""
from __future__ import annotations

import os
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except Exception:  # pragma: no cover - 无 tkinter 时模块仍可 import
    tk = filedialog = messagebox = ttk = None

from . import gui, gui_settings, progress

# 进度条两种样式：正常蓝条 / 失败红条（样式在 gui.setup_style 里定义）。
BAR_NORMAL = "Horizontal.TProgressbar"
BAR_DANGER = "Danger.Horizontal.TProgressbar"


def _stderr_summary(text, limit=600, max_lines=6):
    """把下载子进程的 stderr 收成弹窗能看的一段：去 ANSI、去空行、只留最后几行。"""
    if not text:
        return ""
    try:
        cleaned = gui.clean_log_text(str(text), drop_progress=False)
    except Exception:  # noqa: BLE001 - 清洗失败也要能显示原文
        cleaned = str(text)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-max_lines:])[-limit:]


class _RunMixin:
    """扫描目录 → 队列 → 逐本子进程 → 进度/结果/日志。"""

    # ------------------------------------------------------------ 扫描
    def _recursive(self) -> bool:
        try:
            return bool(self.recursive_var.get())
        except tk.TclError:
            return True

    def _browse_scan(self):
        folder = filedialog.askdirectory(
            title="选择要扫描的目录", parent=self.root,
            initialdir=self.scan_var.get().strip() or None)
        if folder:
            self.scan_var.set(folder)
            self._scan_now()

    def _scan_now(self):
        self._scan_paths(self.scan_var.get().strip(), self._recursive(),
                         replace=True, silent=False)

    def _scan_paths(self, folder, recursive=True, replace=False, silent=False):
        text = str(folder or "").strip()
        path = Path(text) if text else None
        if path is None or not path.is_dir():
            if not silent:
                messagebox.showwarning("目录无效", f"这个目录不存在：\n{text or '（空）'}",
                                       parent=self.root)
            return
        self._scan_seq += 1
        token = self._scan_seq
        self.status_var.set(f"正在扫描：{path}")
        threading.Thread(
            target=self._scan_worker, args=(token, path, recursive, replace, silent),
            daemon=True).start()

    def _scan_worker(self, token, path, recursive, replace, silent):
        try:
            found = gui.scan_pdfs(path, recursive)
        except Exception:  # noqa: BLE001 - 扫描失败按空结果处理
            found = []
        self._queue.put(("scan", token, found, replace, silent, str(path)))

    def _on_scan(self, token, found, replace, silent, folder):
        if replace and token != self._scan_seq:
            return
        if replace:
            self._clear_queue(ask=False)
        self._add_paths(found)
        self.status_var.set(f"扫描到 {len(found)} 个 PDF/Markdown：{folder}")
        if not found and not silent:
            messagebox.showinfo("没有 PDF", f"这个目录里没找到 PDF：\n{folder}",
                                parent=self.root)

    # ------------------------------------------------------------ 队列
    def _add_paths(self, paths):
        added = 0
        for raw in paths or ():
            try:
                path = Path(raw)
                if not path.is_file() or path.suffix.lower() not in gui.INPUT_SUFFIXES:
                    continue
                key = os.path.normcase(os.path.abspath(str(path)))
            except (OSError, TypeError, ValueError):
                continue
            if key in self._known:
                continue
            iid = f"b{self._next_iid}"
            self._next_iid += 1
            book = {"iid": iid, "path": path, "checked": True, "status": "等待"}
            self._books[iid] = book
            self._order.append(iid)
            self._known.add(key)
            self.tree.insert("", "end", iid=iid,
                             values=("☑", path.name, str(path.parent), "等待"))
            added += 1
        if added:
            self._refresh_count()

    def _refresh_count(self):
        checked = sum(1 for iid in self._order if self._books[iid]["checked"])
        self.queue_count_var.set(f"共 {len(self._order)} 本 · 已勾选 {checked} 本")

    def _refresh_row(self, iid):
        book = self._books.get(iid)
        if book is None:
            return
        try:
            self.tree.item(iid, values=("☑" if book["checked"] else "☐",
                                        book["path"].name, str(book["path"].parent),
                                        book["status"]))
        except tk.TclError:
            pass

    def _on_tree_click(self, event):
        if self.tree.identify_column(event.x) != "#1":
            return None
        iid = self.tree.identify_row(event.y)
        book = self._books.get(iid)
        if book is None:
            return None
        book["checked"] = not book["checked"]
        self._refresh_row(iid)
        self._refresh_count()
        return "break"

    def _select_all(self):
        for iid in self._order:
            self._books[iid]["checked"] = True
            self._refresh_row(iid)
        self._refresh_count()

    def _select_none(self):
        for iid in self._order:
            self._books[iid]["checked"] = False
            self._refresh_row(iid)
        self._refresh_count()

    def _remove_selected(self):
        if self._running:
            messagebox.showwarning("正在转换", "先取消或等转换结束再改队列。", parent=self.root)
            return
        for iid in list(self.tree.selection()):
            book = self._books.pop(iid, None)
            if book is None:
                continue
            try:
                self._order.remove(iid)
            except ValueError:
                pass
            try:
                self._known.discard(os.path.normcase(os.path.abspath(str(book["path"]))))
            except OSError:
                pass
            try:
                self.tree.delete(iid)
            except tk.TclError:
                pass
        self._refresh_count()

    def _clear_queue(self, ask=True):
        if self._running:
            if ask:
                messagebox.showwarning("正在转换", "先取消或等转换结束再清空队列。",
                                       parent=self.root)
            return
        if ask and self._order:
            if not messagebox.askyesno("清空队列", "确定移除队列里的全部书？", parent=self.root):
                return
        self.tree.delete(*self.tree.get_children())
        self._books.clear()
        self._order.clear()
        self._known.clear()
        self._last_md = None
        self._last_output_dir = None
        self.result_var.set("还没有结果。")
        self._set_result_buttons()
        self._refresh_count()

    def _add_files(self):
        chosen = filedialog.askopenfilenames(
            title="选择 PDF 或 Markdown", parent=self.root,
            filetypes=[("PDF / Markdown", "*.pdf;*.md;*.markdown;*.txt"),
                       ("PDF 文件", "*.pdf"), ("Markdown", "*.md;*.markdown;*.txt"),
                       ("所有文件", "*.*")])
        if chosen:
            self._add_paths([Path(item) for item in chosen])

    def _add_folder(self):
        folder = filedialog.askdirectory(title="添加文件夹里的 PDF / Markdown", parent=self.root)
        if folder:
            self._scan_paths(folder, self._recursive(), replace=False, silent=False)

    # ------------------------------------------------------------ 并发
    def _concurrency(self) -> int:
        return gui.clamp_concurrency(self.concurrency_var.get())

    def _on_concurrency_change(self, _event=None):
        value = self._concurrency()
        if self.concurrency_var.get() != str(value):
            self.concurrency_var.set(str(value))
        self.settings["并发"] = value
        if self._running and self._current_workspace is not None:
            path = Path(self._current_workspace) / "状态" / "并发.txt"
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(str(value), encoding="utf-8")
                self._append_log(f"并发改为 {value}（已写 {path}）")
            except OSError as exc:
                self._append_log("写并发失败：" + str(exc))

    # ------------------------------------------------------------ 运行
    def _start_run(self):
        if self._running:
            return
        if getattr(self, "_model_ready", None) is False:
            messagebox.showwarning(
                "缺版面模型",
                "还没装好版面模型，暂时不能开始转换。\n\n"
                + self._manual_help_text(self._layout_tier()),
                parent=self.root)
            return
        checked = [self._books[iid] for iid in self._order
                   if self._books[iid]["checked"]]
        if not checked:
            messagebox.showwarning("没有选中的书", "先在队列第一列勾选要转换的书。",
                                   parent=self.root)
            return
        self._run_books = checked
        self._run_total = len(checked)
        self._run_index = 0
        self._run_started = time.monotonic()
        self._cancel_requested = False
        self._retry_mode = False
        self._retry_book = None
        self._running = True
        self._stage_state = {}
        self._book_fraction = 0.0
        self._failed = []
        self._error_lines = []
        self._set_progress_style(BAR_NORMAL)
        self._set_progress(0.0)
        self.duration_var.set("00:00")
        self.stage_var.set("准备…")
        self.status_var.set(f"准备转换 {self._run_total} 本…")
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        for book in checked:
            book["status"] = "等待"
            self._refresh_row(book["iid"])
        self._append_log(f"开始运行：共 {self._run_total} 本")
        self._start_current()

    def _start_current(self):
        if not self._running:
            return
        if self._cancel_requested:
            self._finish_run("已取消")
            return
        if self._run_index >= self._run_total:
            self._finish_run("全部完成")
            return
        book = self._run_books[self._run_index]
        name = book["path"].stem
        self._token += 1
        token = self._token
        self._stage_state = {}
        self._book_fraction = 0.0
        self._byte_buffer = b""
        self._error_lines = []
        self._set_progress_style(BAR_NORMAL)
        self.stage_var.set("准备…")
        self.status_var.set(f"正在转换（{self._run_index + 1}/{self._run_total}）：{name}")
        self._set_progress(self._run_index / self._run_total if self._run_total else 0.0)
        book["status"] = "运行中"
        self._refresh_row(book["iid"])
        try:
            workspace = self._prepare_workspace(book)
            self._current_workspace = workspace
            command = gui.build_command(
                book["path"], workspace,
                subcommand="补跑" if self._retry_mode else "run")
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            self._append_log("$ " + " ".join(command))
            self._proc = subprocess.Popen(
                command, cwd=str(self._program), stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                bufsize=0, creationflags=flags)
        except Exception as exc:  # noqa: BLE001 - 启动失败要显示在界面上
            self._append_log(f"《{name}》启动失败：{exc}")
            book["status"] = "失败"
            self._refresh_row(book["iid"])
            self.result_var.set(f"《{name}》启动失败：{exc}")
            self._run_index += 1
            if self._cancel_requested:
                self._finish_run("已取消")
            elif self._run_index >= self._run_total:
                self._finish_run("全部完成")
            else:
                self.root.after(200, self._start_current)
            return
        proc = self._proc
        reader = threading.Thread(target=self._reader_loop, args=(proc, token),
                                  daemon=True)
        reader.start()
        threading.Thread(target=self._waiter_loop, args=(proc, token, reader, book),
                         daemon=True).start()

    def _prepare_workspace(self, book) -> Path:
        base_text = str(self.settings.get("工作区基目录") or "").strip()
        if base_text:
            base = Path(base_text).expanduser()
            if not base.is_absolute():
                base = self._program / base
        else:
            base = gui.workspace_base()
        workspace = base / book["path"].stem
        (workspace / "状态").mkdir(parents=True, exist_ok=True)
        service = gui_settings.active_service(self.settings)
        concurrency = self._concurrency()
        updates = [
            ("提供商", "地址", str(service.get("地址") or "")),
            ("提供商", "key", str(service.get("key") or "")),
            ("提供商", "key_环境变量",
             str(service.get("key_环境变量") or gui.DEFAULT_KEY_ENV)),
            ("阶段.识别", "模型", str(service.get("识别模型") or "")),
            ("阶段.识别", "推理强度",
             gui_settings.effort_value(self.settings.get("识别思考强度", "关"))),
            ("阶段.摘要", "模型", str(service.get("摘要模型") or "")),
            ("阶段.摘要", "推理强度",
             gui_settings.effort_value(self.settings.get("摘要思考强度", "关"))),
            ("全局", "并发", str(concurrency)),
            ("全局", "重试次数", str(self.settings.get("重试次数", 2))),
            ("全局", "自动补跑轮数", str(self.settings.get("自动补跑轮数", 2))),
            ("全局", "超时秒", str(self.settings.get("超时秒", 180))),
            ("版面", "档位", self._layout_tier()),
            ("输出", "格式", gui_settings.output_value(self.settings.get("输出格式", "both"))),
            ("输出", "保留中间数据",
             "true" if self.settings.get("保留中间数据", True) else "false"),
            ("输出", "溯源段", "true" if self.settings.get("溯源段", True) else "false"),
        ]
        gui.write_fields(workspace / "设置.txt", updates)
        (workspace / "状态" / "并发.txt").write_text(str(concurrency), encoding="utf-8")
        return workspace

    def _reader_loop(self, proc, token):
        stream = proc.stdout
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                self._queue.put(("bytes", token, chunk))
        except (OSError, ValueError):
            pass
        finally:
            try:
                stream.close()
            except Exception:  # noqa: BLE001
                pass

    def _waiter_loop(self, proc, token, reader, book):
        try:
            code = proc.wait()
        except Exception:  # noqa: BLE001 - 进程状态拿不到按失败算
            code = -1
        try:
            reader.join(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        self._queue.put(("done", token, code, book))

    def _retry_last(self):
        """只补上一本失败的书：已完成/成功的页跳过，只重试失败页和后续阶段。"""
        if self._running:
            return
        book = self._retry_book
        if book is None:
            messagebox.showinfo("没有要补跑的", "还没有失败的任务。先点「开始」。", parent=self.root)
            return
        try:
            exists = Path(book["path"]).is_file()
        except (TypeError, OSError):
            exists = False
        if not exists:
            messagebox.showwarning("找不到 PDF", f"原文件不在了：{book['path']}", parent=self.root)
            return
        self._retry_mode = True
        self._run_books = [book]
        self._run_total = 1
        self._run_index = 0
        self._run_started = time.monotonic()
        self._cancel_requested = False
        self._running = True
        self._stage_state = {}
        self._book_fraction = 0.0
        self._failed = []
        self._error_lines = []
        self._set_progress_style(BAR_NORMAL)
        self._set_progress(0.0)
        self.duration_var.set("00:00")
        self.stage_var.set("准备补跑…")
        self.status_var.set(f"补跑：{book['path'].stem}")
        self.start_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        try:
            self.retry_btn.configure(state="disabled")
        except (AttributeError, tk.TclError):
            pass
        book["status"] = "补跑中"
        self._refresh_row(book["iid"])
        self._append_log(f"补跑：{book['path'].stem}（已完成/成功的页不重做）")
        self._start_current()

    def _on_tier_change(self, _event=None):
        """运行栏的质量档下拉：立即写盘并在缺模型时按新档位重新检查。"""
        value = gui_settings.tier_value(self.tier_var.get())
        self.tier_var.set(gui_settings.tier_label(value))
        if value == gui_settings.tier_value(self.settings.get("版面档位", "M")):
            return
        data = dict(self.settings)
        data["版面档位"] = value
        try:
            self.apply_settings(data)
        except Exception as exc:  # noqa: BLE001 - 保存失败要说清楚
            messagebox.showerror("切换质量档失败", str(exc), parent=self.root)
            return
        self._append_log(f"质量档改为 {gui_settings.tier_label(value)}")

    def _cancel_run(self):
        if not self._running:
            return
        self._cancel_requested = True
        self.status_var.set("正在取消…")
        self.stage_var.set("正在取消…")
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass
        self._append_log("用户取消：已终止子进程")

    def _finish_run(self, message):
        if not self._running:
            return
        self._running = False
        self._proc = None
        self._current_workspace = None
        self.start_btn.configure(
            state="disabled" if getattr(self, "_model_ready", None) is False else "normal")
        self.cancel_btn.configure(state="disabled")
        try:
            self.retry_btn.configure(
                state="normal" if self._retry_book is not None else "disabled")
        except (AttributeError, tk.TclError):
            pass
        failed = list(getattr(self, "_failed", []) or [])
        if message == "全部完成" and failed:
            message = f"全部完成（{len(failed)} 本失败）"
        self.status_var.set(message)
        self.stage_var.set(message)
        if self._run_started:
            self.duration_var.set(gui.human_duration(time.monotonic() - self._run_started))
        if message == "全部完成":
            self._set_progress(1.0)
        elif message == "已取消":
            for book in self._run_books[self._run_index:]:
                book["status"] = "已取消"
                self._refresh_row(book["iid"])
        elif failed:
            # 有书失败：收尾时也保持红条，别用最后那本成功的进度把失败盖掉。
            self._mark_failed()
        self._cancel_requested = False
        self._append_log(message)

    # ------------------------------------------------------------ 版面模型启动检查
    def _layout_tier(self) -> str:
        """版面档位：设置里的 版面.档位 → CATALOG 里的 M → 清单第一项（缺省 M）。"""
        try:
            from .media import models
            catalog = dict(getattr(models, "CATALOG", {}) or {})
        except Exception:  # noqa: BLE001 - 模型模块异常不能拦住界面
            catalog = {}
        try:
            wanted = ""
            if getattr(self, "_tier_explicit", False):
                wanted = str(self.settings.get("版面档位") or "").strip()
            if not wanted:
                wanted = str(self.settings.get("版面.档位") or "").strip()
            wanted = gui_settings.tier_value(wanted)
        except Exception:  # noqa: BLE001
            wanted = ""
        if wanted in catalog:
            return wanted
        if "M" in catalog:
            return "M"
        return next(iter(catalog), "M")

    def _model_dir_path(self, tier):
        """模型该放的目录：models.path(tier)，模块异常时退回 程序目录\\模型\\<档位>。"""
        try:
            from .media import models
            return Path(models.path(tier))
        except Exception:  # noqa: BLE001
            return Path(self.program_dir()) / "模型" / str(tier)

    def _download_command(self, tier):
        """自动下载命令：用控制台 python，让终端里看得见进度。"""
        return [gui.python_executable(), "-m", "why_harness", "模型", "download", str(tier)]

    def _manual_help_text(self, tier):
        """手动安装要显示的命令与模型目录。"""
        return (
            f"python -m why_harness 模型 download {tier}\n"
            "或者手动下载后，把 inference.json / inference.pdiparams / inference.yml "
            f"三个文件放进：{self._model_dir_path(tier)}"
        )

    def _set_start_enabled(self, enabled):
        button = getattr(self, "start_btn", None)
        if button is None:
            return
        try:
            button.configure(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    def _check_model_on_startup(self):
        """窗口建好后查一次版面模型：已装 → 完全静默；缺 → 弹三选一，不静默下载。"""
        if self._closed:
            return "closed"
        tier = self._layout_tier()
        try:
            from .media import models
            ready = bool(models.installed(tier))
        except Exception:  # noqa: BLE001 - 查不到也按缺模型处理，让用户决定
            ready = False
        if ready:
            self._model_ready = True
            return "installed"
        self._model_ready = False
        self._set_start_enabled(False)
        self.status_var.set("缺版面模型")
        try:
            choice = self._ask_missing_model(tier)
        except Exception as exc:  # noqa: BLE001 - 弹窗失败也不能让界面起不来
            self._append_log("模型选择窗打开失败：" + str(exc))
            choice = "cancel"
        if choice == "auto":
            self._start_model_download(tier)
        elif choice == "manual":
            self._show_manual_model_help(tier)
            if self._model_ready is not True:
                self.status_var.set("缺版面模型")
        else:
            self.status_var.set("缺版面模型")
            self._append_log("缺版面模型：「开始」已禁用，装好模型后重新打开程序。")
        return choice

    def _ask_missing_model(self, tier):
        """缺模型的三选一小窗，返回 auto / manual / cancel（直接关窗算取消）。"""
        result = {"choice": "cancel"}
        win = tk.Toplevel(self.root)
        win.title("缺少版面模型")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg=gui.BG)
        box = ttk.Frame(win, padding=16)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=f"缺少版面模型（{tier} 档）",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(box, justify="left", wraplength=560,
                  text=("本机还没装转换要用的版面模型。\n"
                        "自动下载会打开一个终端窗口，逐个百分比显示进度。\n"
                        f"模型目录：{self._model_dir_path(tier)}")).pack(
            anchor="w", pady=(8, 14))
        row = ttk.Frame(box)
        row.pack(fill="x")

        def choose(value):
            result["choice"] = value
            try:
                win.destroy()
            except tk.TclError:
                pass

        ttk.Button(row, text="自动下载", command=lambda: choose("auto")).pack(side="left")
        ttk.Button(row, text="手动安装", command=lambda: choose("manual")).pack(
            side="left", padx=8)
        ttk.Button(row, text="取消", command=lambda: choose("cancel")).pack(side="left")
        win.protocol("WM_DELETE_WINDOW", lambda: choose("cancel"))
        try:
            win.grab_set()
        except tk.TclError:  # 主窗口还没显示出来时 grab 会失败，不影响选择
            pass
        win.update_idletasks()
        try:
            win.focus_force()
        except tk.TclError:
            pass
        self.root.wait_window(win)
        return result["choice"]

    def _start_model_download(self, tier):
        """在独立终端窗口跑 CLI 下载；期间禁用开始，后台线程等它结束。"""
        self._model_ready = False
        self._set_start_enabled(False)
        self.status_var.set(f"正在下载版面模型（{tier}）… 进度见终端窗口")
        self.stage_var.set("下载版面模型…")
        command = self._download_command(tier)
        flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if os.name == "nt" else 0
        handle = None
        err_path = None
        try:
            handle = tempfile.NamedTemporaryFile(
                mode="w+b", prefix="why-model-", suffix=".log", delete=False)
            err_path = handle.name
            self._append_log("$ " + subprocess.list2cmdline(command))
            proc = subprocess.Popen(
                command, cwd=str(self.program_dir()),
                stdin=subprocess.DEVNULL, stderr=handle, creationflags=flags)
        except Exception as exc:  # noqa: BLE001 - 起不来要当场告诉用户
            if err_path:
                try:
                    Path(err_path).unlink()
                except OSError:
                    pass
            self._on_model_download_done(tier, None, str(exc))
            return None
        finally:
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._model_proc = proc
        threading.Thread(target=self._wait_model_download,
                         args=(proc, tier, err_path), daemon=True).start()
        return proc

    def _wait_model_download(self, proc, tier, err_path=None):
        """后台线程：等下载进程退出，收 stderr 摘要，再交回界面线程处理。"""
        try:
            code = proc.wait()
        except Exception:  # noqa: BLE001 - 进程状态拿不到按失败算
            code = -1
        self._model_proc = None
        text = ""
        if err_path:
            try:
                text = gui.decode_console(Path(err_path).read_bytes()[-12000:])
            except OSError:
                text = ""
            try:
                Path(err_path).unlink()
            except OSError:
                pass
        try:
            self._queue.put(("model", tier, code, text))
        except Exception:  # noqa: BLE001 - 队列没了就算了
            pass

    def _on_model_download_done(self, tier, code, stderr_text=""):
        """下载结束：退出码 0 且 installed 为真才算成功；失败弹 stderr 摘要并禁用开始。"""
        installed = False
        if code == 0:
            try:
                from .media import models
                installed = bool(models.installed(tier))
            except Exception:  # noqa: BLE001
                installed = False
        folder = self._model_dir_path(tier)
        if installed:
            self._model_ready = True
            self.status_var.set("版面模型已就绪")
            self.stage_var.set("就绪")
            self._set_start_enabled(True)
            self._append_log(f"版面模型已就绪：{folder}")
            if messagebox is not None:
                messagebox.showinfo(
                    "模型已安装",
                    f"版面模型（{tier} 档）已就绪，可以开始转换了。",
                    parent=self.root)
            return True
        self._model_ready = False
        self._set_start_enabled(False)
        self.status_var.set("缺版面模型")
        self.stage_var.set("缺版面模型")
        summary = _stderr_summary(stderr_text)
        if not summary:
            summary = (f"下载进程退出码 {code}，模型文件仍不齐。"
                       if code is not None else "下载进程没有正常结束。")
        self._append_log("模型下载失败：" + summary.replace("\n", " "))
        if messagebox is not None:
            messagebox.showerror(
                "模型下载失败",
                f"版面模型（{tier} 档）没有安装成功。\n\n{summary}\n\n"
                f"也可以手动安装：\n{self._manual_help_text(tier)}",
                parent=self.root)
        return False

    def _show_manual_model_help(self, tier):
        """手动安装窗：显示命令与模型目录，可打开目录、放好后重新检查。"""
        folder = self._model_dir_path(tier)
        win = tk.Toplevel(self.root)
        win.title("手动安装版面模型")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg=gui.BG)
        box = ttk.Frame(win, padding=16)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text=f"手动安装版面模型（{tier} 档）",
                  style="Title.TLabel").pack(anchor="w")
        ttk.Label(box, justify="left", wraplength=600,
                  text="在命令行里运行：").pack(anchor="w", pady=(8, 4))
        entry = ttk.Entry(box, width=64)
        entry.insert(0, f"python -m why_harness 模型 download {tier}")
        entry.configure(state="readonly")
        entry.pack(fill="x")
        ttk.Label(box, justify="left", wraplength=600, style="Sub.TLabel",
                  text=f"模型目录：{folder}").pack(anchor="w", pady=(10, 2))
        ttk.Label(box, justify="left", wraplength=600, style="Sub.TLabel",
                  text="把 inference.json / inference.pdiparams / inference.yml "
                       "放进这个目录后，点「我已放好，重新检查」。").pack(anchor="w")
        row = ttk.Frame(box)
        row.pack(fill="x", pady=(14, 0))
        ttk.Button(row, text="打开模型目录",
                   command=lambda: self.open_path(folder)).pack(side="left")
        ttk.Button(row, text="我已放好，重新检查",
                   command=lambda: self._recheck_model(tier, win)).pack(
            side="left", padx=8)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        try:
            win.grab_set()
        except tk.TclError:  # 同上：主窗口不可见时不抢焦点
            pass
        win.update_idletasks()
        try:
            win.focus_force()
        except tk.TclError:
            pass
        self.root.wait_window(win)
        return folder

    def _recheck_model(self, tier, win=None):
        """手动放好后重新检查；就绪则恢复开始按钮并关窗。"""
        try:
            from .media import models
            ready = bool(models.installed(tier))
        except Exception:  # noqa: BLE001
            ready = False
        if ready:
            self._model_ready = True
            self._set_start_enabled(True)
            self.status_var.set("版面模型已就绪")
            self.stage_var.set("就绪")
            self._append_log(f"版面模型已就绪：{self._model_dir_path(tier)}")
            if win is not None:
                try:
                    win.destroy()
                except tk.TclError:
                    pass
            if messagebox is not None:
                messagebox.showinfo("模型已就绪", "版面模型已就绪，可以开始转换了。",
                                    parent=self.root)
            return True
        if messagebox is not None:
            messagebox.showwarning(
                "还没找到",
                f"模型目录里仍然缺文件：\n{self._model_dir_path(tier)}\n\n"
                "请确认 inference.json / inference.pdiparams / inference.yml 三个文件都在。",
                parent=win if win is not None else self.root)
        return False

    # ------------------------------------------------------------ 子进程输出
    def _pump(self):
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "bytes":
                    self._feed_bytes(item[1], item[2])
                elif kind == "done":
                    self._on_done(*item[1:])
                elif kind == "scan":
                    self._on_scan(*item[1:])
                elif kind == "model":
                    self._on_model_download_done(*item[1:])
        except queue.Empty:
            pass
        except Exception as exc:  # noqa: BLE001 - 单个坏消息不能卡死界面
            self._append_log("界面处理出错：" + str(exc))
        try:
            self._pump_job = self.root.after(80, self._pump)
        except tk.TclError:
            self._pump_job = None

    def _feed_bytes(self, token, raw):
        if token != self._token:
            return
        self._byte_buffer += raw
        while True:
            index, sep = -1, b""
            for candidate in (b"\n", b"\r"):
                position = self._byte_buffer.find(candidate)
                if position != -1 and (index == -1 or position < index):
                    index, sep = position, candidate
            if index == -1:
                break
            line = self._byte_buffer[:index]
            self._byte_buffer = self._byte_buffer[index + 1:]
            if sep == b"\r" and self._byte_buffer.startswith(b"\n"):
                self._byte_buffer = self._byte_buffer[1:]
            self._handle_output_line(gui.decode_console(line))

    def _flush_bytes(self):
        if not self._byte_buffer:
            return
        text = gui.decode_console(self._byte_buffer)
        self._byte_buffer = b""
        for line in text.splitlines():
            self._handle_output_line(line)

    def _handle_output_line(self, line):
        if not line:
            return
        cleaned = gui.clean_log_text(line, drop_progress=False)
        if not cleaned:
            return
        for piece in cleaned.split("\n"):
            record = progress.parse_line(piece)
            if record is not None:
                self._handle_progress(record)
            elif piece.strip():
                self._append_log(piece)
                if gui.looks_like_error(piece):
                    self._error_lines = (getattr(self, "_error_lines", []) + [piece.strip()[:300]])[-8:]

    def _handle_progress(self, item):
        try:
            stage, done, total = str(item[0]), int(item[1]), int(item[2])
        except (IndexError, TypeError, ValueError):
            return
        note = str(item[3]) if len(item) > 3 else ""
        self._stage_state[stage] = (done, total)
        fraction = progress.overall_fraction(self._stage_state)
        self._book_fraction = fraction
        if self._running and self._run_total:
            self._set_progress((self._run_index + fraction) / self._run_total)
        else:
            self._set_progress(fraction)
        title = gui._STAGE_TITLES.get(stage, stage)
        self.stage_var.set(f"{title} {done}/{total}" + (f" · {note}" if note else ""))

    def _on_done(self, token, code, book):
        if token != self._token:
            return
        self._flush_bytes()
        self._proc = None
        name = book["path"].stem
        text = gui.exit_code_text(code)
        book["status"] = text
        self._refresh_row(book["iid"])
        self._append_log(f"《{name}》{text}（退出码 {code}）")
        if code == 0:
            self._retry_book = None
            self._retry_mode = False
            self._show_result(book)
        else:
            self._retry_book = book
            self._failed = (getattr(self, "_failed", []) or []) + [name]
            hint = "可以点「补跑」只重试失败页和后续阶段，已成功的不重做。" if code in (2, 3, 4) else ""
            self.result_var.set(f"《{name}》{text}（退出码 {code}）。{hint}")
            self._set_result_buttons()
            try:
                self.retry_btn.configure(state="normal")
            except (AttributeError, tk.TclError):
                pass
            # 失败就停在失败那一刻：红条 + 弹窗，不假装跑完了 100%。
            self._mark_failed()
            self._show_failure(name, code)
        self._run_index += 1
        if self._run_total and code == 0:
            self._set_progress(self._run_index / self._run_total)
        if self._cancel_requested:
            self._finish_run("已取消")
        elif self._run_index >= self._run_total:
            self._finish_run("全部完成")
        else:
            self.root.after(200, self._start_current)

    # ------------------------------------------------------------ 结果/进度/日志
    def _show_result(self, book):
        name = book["path"].stem
        workspace = self._current_workspace or (Path("产物") / name)
        output = gui.output_dir(Path(workspace), name)
        self._last_output_dir = output
        report_path = output / "说明.txt"
        try:
            report = gui.parse_report(report_path.read_text(encoding="utf-8"))
        except OSError:
            report = {}
        self.result_var.set(f"《{name}》{gui.report_summary(report)}")
        md_path = output / f"{name}.md"
        why_path = output / f"{name}.why"
        self._last_md = md_path if md_path.is_file() else None
        self._last_why = why_path if why_path.is_file() else None
        if not report and self._last_why:
            try:
                size = gui.human_size(self._last_why.stat().st_size)
            except OSError:
                size = "未知"
            self.result_var.set(f"《{name}》已生成 {why_path.name}（{size}）")
        self._set_result_buttons()

    def _set_result_buttons(self):
        def state(path):
            try:
                return "normal" if path and Path(path).exists() else "disabled"
            except OSError:
                return "disabled"
        try:
            self.open_md_btn.configure(state=state(self._last_md or getattr(self, "_last_why", None)))
            self.open_dir_btn.configure(state=state(self._last_output_dir))
        except (AttributeError, tk.TclError):
            pass

    def _open_last_md(self):
        target = self._last_md or getattr(self, "_last_why", None)
        if target and Path(target).is_file():
            self.open_path(target)
        else:
            messagebox.showwarning("还没有成品", "这一本还没生成 Markdown 或 .why。",
                                   parent=self.root)

    def _open_last_dir(self):
        if self._last_output_dir and Path(self._last_output_dir).is_dir():
            self.open_path(self._last_output_dir)
        else:
            messagebox.showwarning("还没有目录", "这一本还没生成产物目录。", parent=self.root)

    def _set_progress_style(self, style):
        """把两条进度条切换成蓝条/红条。"""
        for bar in (getattr(self, "run_progress", None),
                    getattr(self, "status_progress", None)):
            if bar is not None:
                try:
                    bar.configure(style=style)
                except tk.TclError:
                    pass

    def _mark_failed(self):
        """失败状态：进度条变红、百分比位置写「失败」，不装作跑满。"""
        self._set_progress_style(BAR_DANGER)
        self.percent_var.set("失败")

    def _show_failure(self, name, code):
        """失败时弹窗说明，附上日志里最后几条报错。"""
        if messagebox is None:
            return
        lines = list(getattr(self, "_error_lines", []) or [])
        body = f"《{name}》转换失败（退出码 {code}）。"
        if lines:
            body += "\n\n" + "\n".join(lines[-6:])
        if code in (2, 3, 4):
            body += "\n\n可以点「补跑」只重试失败页和后续阶段，已成功的不重做。"
        body += "\n\n详细日志见下方日志区。"
        try:
            messagebox.showerror("转换失败", body, parent=self.root)
        except tk.TclError:
            pass

    def _set_progress(self, fraction):
        try:
            value = max(0.0, min(1.0, float(fraction))) * 100.0
        except (TypeError, ValueError):
            value = 0.0
        self.percent_var.set(f"{value:.0f}%")
        for bar in (getattr(self, "run_progress", None),
                    getattr(self, "status_progress", None)):
            if bar is not None:
                try:
                    bar.configure(value=value)
                except tk.TclError:
                    pass

    def _append_log(self, text):
        text = str(text or "")
        if not text or not hasattr(self, "log_text"):
            return
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", text + "\n", gui.line_tag(text))
            lines = int(self.log_text.index("end-1c").split(".")[0])
            if lines > 2500:
                self.log_text.delete("1.0", f"{lines - 2000}.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _clear_log(self):
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass