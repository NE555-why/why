# -*- coding: utf-8 -*-
"""PdfGui 的界面搭建（mixin）。

只负责控件布局与提示词页/关于页的交互；队列、扫描、运行、进度等行为在
gui_app.PdfGui 里。拆开是为了两个文件都保持可读。
"""
from __future__ import annotations

import os

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except Exception:  # pragma: no cover - 无 tkinter 时模块仍可 import
    tk = ttk = messagebox = None

from . import __version__, gui, gui_settings, promptlib


class _PagesMixin:
    """给 PdfGui 提供 _build_ui / _show_page / 提示词页逻辑。"""

    # ------------------------------------------------------------ 总体布局
    def _build_ui(self):
        self._build_header()
        self._build_statusbar()
        body = tk.Frame(self.root, bg=gui.BG)
        body.pack(fill="both", expand=True)
        self._build_sidebar(body)
        holder = ttk.Frame(body, padding=(14, 10))
        holder.pack(side="left", fill="both", expand=True)
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        self.pages = {}
        for key, builder in (
            ("convert", self._build_convert_page),
            ("prompt", self._build_prompt_page),
            ("settings", self._build_settings_page),
            ("about", self._build_about_page),
        ):
            page = builder(holder)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[key] = page
        self._show_page("convert")
        # 窗口建好后查一次版面模型：已装就完全静默，缺了才弹三选一。
        self._model_ready = None
        self._model_proc = None
        self.root.after(500, self._check_model_on_startup)

    def _build_header(self):
        head = ttk.Frame(self.root, padding=(18, 10, 18, 6))
        head.pack(fill="x")
        left = ttk.Frame(head)
        left.pack(side="left")
        ttk.Label(left, text="pdf转", style="Title.TLabel").pack(side="left")
        ttk.Label(left, text="把扫描 PDF / Markdown 做成单文件电子书", style="Sub.TLabel").pack(
            side="left", padx=(10, 0))
        right = ttk.Frame(head)
        right.pack(side="right")
        ttk.Button(right, text="设置", command=self.open_settings).pack(side="left", padx=3)
        ttk.Button(right, text="日志", command=self._open_log_window).pack(side="left", padx=3)
        ttk.Button(right, text="打开目录",
                   command=lambda: self.open_path(self.program_dir())).pack(side="left", padx=3)

    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg="#ffffff", highlightthickness=1,
                       highlightbackground="#dfe3ea")
        bar.pack(side="bottom", fill="x")
        inner = ttk.Frame(bar, padding=(14, 6))
        inner.pack(fill="x")
        ttk.Label(inner, textvariable=self.status_var).pack(side="left")
        ttk.Label(inner, textvariable=self.duration_var, style="Sub.TLabel").pack(
            side="right", padx=(10, 0))
        ttk.Label(inner, textvariable=self.percent_var, style="Sub.TLabel").pack(
            side="right", padx=(10, 0))
        self.status_progress = ttk.Progressbar(inner, maximum=100, length=240)
        self.status_progress.pack(side="right", padx=(10, 0))

    def _build_sidebar(self, parent):
        side = tk.Frame(parent, bg=gui.SIDEBAR_BG, width=190)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="pdf转", bg=gui.SIDEBAR_BG, fg="#ffffff",
                 font=gui.ui_font(15, True), anchor="w", padx=18, pady=18).pack(fill="x")
        self.nav_buttons = {}
        for key, text in (("convert", "转换"), ("prompt", "提示词"),
                          ("settings", "设置"), ("about", "关于")):
            button = gui._NavButton(side, text, lambda k=key: self._show_page(k))
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[key] = button
        bottom = tk.Frame(side, bg=gui.SIDEBAR_BG)
        bottom.pack(side="bottom", fill="x", padx=10, pady=12)
        label = tk.Label(bottom, text="接口、质量档、并发\n都在「设置」里",
                         bg=gui.SIDEBAR_BG, fg=gui.SIDEBAR_TEXT, anchor="w",
                         padx=12, pady=9, justify="left", font=gui.ui_font(9),
                         cursor="hand2", wraplength=160)
        label.pack(fill="x")
        label.bind("<Button-1>", lambda _e: self._show_page("settings"))
        label.bind("<Enter>", lambda _e: label.configure(bg=gui.SIDEBAR_HOVER))
        label.bind("<Leave>", lambda _e: label.configure(bg=gui.SIDEBAR_BG))

    def _show_page(self, key):
        page = getattr(self, "pages", {}).get(key)
        if page is None:
            return
        page.tkraise()
        self.current_page = key
        for name, button in getattr(self, "nav_buttons", {}).items():
            button.set_active(name == key)
        if key == "settings":
            self._refresh_settings_summary()

    # ------------------------------------------------------------ 转换页
    def _build_convert_page(self, parent):
        page = ttk.Frame(parent)
        page.columnconfigure(0, weight=1)
        page.rowconfigure(1, weight=2)
        page.rowconfigure(4, weight=3)

        toolbar = ttk.Frame(page)
        toolbar.grid(row=0, column=0, sticky="ew")
        self.setup_banner = tk.Frame(toolbar, bg="#fff4e5", highlightthickness=1,
                                     highlightbackground="#f0c36d")
        self.setup_banner_label = tk.Label(
            self.setup_banner, bg="#fff4e5", fg="#a15c00", anchor="w", justify="left",
            font=gui.ui_font(10), padx=10, pady=6,
            text="还没配置接口：现在点「开始」会失败。先填接口地址和 key。")
        self.setup_banner_label.pack(side="left", fill="x", expand=True)
        ttk.Button(self.setup_banner, text="去设置", command=self.open_settings).pack(
            side="right", padx=8, pady=4)
        scan = ttk.Frame(toolbar)
        self.scan_frame = scan
        scan.pack(fill="x")
        ttk.Label(scan, text="扫描目录").pack(side="left")
        ttk.Entry(scan, textvariable=self.scan_var).pack(
            side="left", fill="x", expand=True, padx=(8, 6))
        ttk.Button(scan, text="浏览…", command=self._browse_scan).pack(side="left")
        ttk.Button(scan, text="立即扫描", command=self._scan_now).pack(side="left", padx=6)
        ttk.Checkbutton(scan, text="包含子目录", variable=self.recursive_var).pack(
            side="left", padx=(4, 0))

        ops = ttk.Frame(toolbar)
        ops.pack(fill="x", pady=(6, 0))
        ttk.Button(ops, text="添加文件", command=self._add_files).pack(side="left")
        ttk.Button(ops, text="添加文件夹", command=self._add_folder).pack(side="left", padx=6)
        ttk.Label(ops, text="也可以把 PDF / Markdown 或文件夹直接拖进窗口", style="Sub.TLabel").pack(
            side="left", padx=8)

        queue_card = ttk.LabelFrame(page, text="待转换的书", padding=8)
        queue_card.grid(row=1, column=0, sticky="nsew")
        queue_card.columnconfigure(0, weight=1)
        queue_card.rowconfigure(1, weight=1)
        queue_ops = ttk.Frame(queue_card)
        queue_ops.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        for text, command in (("全选", self._select_all), ("全不选", self._select_none),
                              ("移除", self._remove_selected), ("清空", self._clear_queue)):
            ttk.Button(queue_ops, text=text, command=command).pack(side="left", padx=(0, 6))
        ttk.Label(queue_ops, textvariable=self.queue_count_var, style="Sub.TLabel").pack(
            side="right")
        self.tree = ttk.Treeview(queue_card, columns=("check", "name", "folder", "status"),
                                 show="headings", height=7, selectmode="extended")
        for column, title, width, anchor in (
            ("check", "勾选", 54, "center"), ("name", "书名", 250, "w"),
            ("folder", "所在目录", 520, "w"), ("status", "状态", 90, "center"),
        ):
            self.tree.heading(column, text=title)
            self.tree.column(column, width=width, anchor=anchor, stretch=column != "check")
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(queue_card, orient="vertical", command=self.tree.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<Button-1>", self._on_tree_click)

        run_card = ttk.LabelFrame(page, text="运行", padding=8)
        run_card.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        row = ttk.Frame(run_card)
        row.pack(fill="x")
        self.start_btn = ttk.Button(row, text="开始", command=self._start_run)
        self.start_btn.pack(side="left")
        self.cancel_btn = ttk.Button(row, text="取消", command=self._cancel_run,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.retry_btn = ttk.Button(row, text="补跑", command=self._retry_last,
                                    state="disabled")
        self.retry_btn.pack(side="left", padx=(0, 6))
        self.run_progress = ttk.Progressbar(row, maximum=100, length=170)
        self.run_progress.pack(side="left", padx=(8, 4))
        ttk.Label(row, textvariable=self.percent_var).pack(side="left")
        ttk.Label(row, textvariable=self.stage_var, style="Sub.TLabel").pack(
            side="left", padx=(10, 0))
        ttk.Label(row, textvariable=self.duration_var, style="Sub.TLabel").pack(
            side="right", padx=(10, 0))
        ttk.Label(row, text="并发").pack(side="right", padx=(10, 0))
        spin = ttk.Spinbox(row, from_=gui.CONCURRENCY_MIN, to=gui.CONCURRENCY_MAX, width=4,
                           textvariable=self.concurrency_var, command=self._on_concurrency_change)
        spin.pack(side="right")
        spin.bind("<FocusOut>", self._on_concurrency_change)
        spin.bind("<Return>", self._on_concurrency_change)

        tier_box = ttk.Frame(row)
        tier_box.pack(side="right", padx=(10, 0))
        ttk.Label(tier_box, text="质量档").pack(side="left")
        self.tier_combo = ttk.Combobox(
            tier_box, state="readonly", width=14,
            values=[label for label, _value in gui_settings.TIER_CHOICES],
            textvariable=self.tier_var)
        self.tier_combo.pack(side="left", padx=(4, 0))
        self.tier_combo.bind("<<ComboboxSelected>>", self._on_tier_change)

        result_card = ttk.LabelFrame(page, text="结果", padding=8)
        result_card.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        result_card.columnconfigure(0, weight=1)
        ttk.Label(result_card, textvariable=self.result_var, wraplength=760).grid(
            row=0, column=0, sticky="w")
        result_ops = ttk.Frame(result_card)
        result_ops.grid(row=0, column=1, sticky="e")
        self.open_md_btn = ttk.Button(result_ops, text="打开成品", state="disabled",
                                      command=self._open_last_md)
        self.open_md_btn.pack(side="left", padx=(6, 0))
        self.open_dir_btn = ttk.Button(result_ops, text="打开目录", state="disabled",
                                       command=self._open_last_dir)
        self.open_dir_btn.pack(side="left", padx=(6, 0))

        log_card = ttk.LabelFrame(page, text="日志", padding=8)
        log_card.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        log_ops = ttk.Frame(log_card)
        log_ops.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Button(log_ops, text="清空", command=self._clear_log).pack(side="left")
        ttk.Button(log_ops, text="全屏", command=self._open_log_window).pack(
            side="left", padx=6)
        self.log_text = tk.Text(log_card, height=8, wrap="none", font=gui.mono_font(9),
                                bg=gui.LOG_BG, fg=gui.LOG_FG, relief="flat",
                                insertbackground=gui.LOG_FG)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        log_y = ttk.Scrollbar(log_card, orient="vertical", command=self.log_text.yview)
        log_y.grid(row=1, column=1, sticky="ns")
        log_x = ttk.Scrollbar(log_card, orient="horizontal", command=self.log_text.xview)
        log_x.grid(row=2, column=0, sticky="ew")
        self.log_text.configure(yscrollcommand=log_y.set, xscrollcommand=log_x.set)
        for tag, color in (("error", "#c0392b"), ("warn", "#b7791f"), ("ok", "#1e8449"),
                           ("gray", "#6b7280"), ("info", gui.LOG_FG)):
            self.log_text.tag_configure(tag, foreground=color)
        self._refresh_setup_hint()
        return page

    # ------------------------------------------------------------ 设置页
    def _build_settings_page(self, parent):
        page = ttk.Frame(parent, padding=(4, 4))
        head = ttk.Frame(page)
        head.pack(fill="x")
        ttk.Label(head, text="设置", style="Title.TLabel").pack(side="left")
        ttk.Button(head, text="打开设置窗口…",
                   command=self.open_settings).pack(side="right")
        ttk.Label(page, style="Sub.TLabel", wraplength=880, justify="left",
                  text="接口、模型、快速/精确档、并发、自动补跑、扫描目录都在设置窗口里。"
                       "这里随时能看当前生效的是哪一套；改了设置点「保存」后立即生效。").pack(
            anchor="w", pady=(4, 14))

        card = ttk.LabelFrame(page, text="当前生效", padding=12)
        card.pack(fill="x")
        card.columnconfigure(1, weight=1)
        self.settings_summary = {}
        rows = (
            ("接口", "service"),
            ("识别模型", "ocr"),
            ("摘要模型", "summary"),
            ("质量档", "tier"),
            ("并发", "conc"),
            ("自动补跑", "auto"),
            ("扫描目录", "scan"),
        )
        for row, (label, key) in enumerate(rows):
            ttk.Label(card, text=label).grid(row=row, column=0, sticky="w", pady=3)
            variable = tk.StringVar(value="—")
            entry = ttk.Entry(card, textvariable=variable, state="readonly")
            entry.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=3)
            self.settings_summary[key] = variable
        buttons = ttk.Frame(card)
        buttons.grid(row=len(rows), column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Button(buttons, text="打开设置窗口…", command=self.open_settings).pack(side="left")
        ttk.Button(buttons, text="打开设置文件",
                   command=lambda: self.open_path(self.settings_path)).pack(
            side="left", padx=8)

        tier_card = ttk.LabelFrame(page, text="快速档 / 精确档", padding=12)
        tier_card.pack(fill="x", pady=(14, 0))
        ttk.Label(tier_card, style="Sub.TLabel", wraplength=880, justify="left",
                  text="快速档（M）：约 1 秒/页，模型 24 MB，日常够用。\n"
                       "精确档（plus-L）：更准，但约慢 5–8 倍，模型 130 MB，适合版面复杂的书。\n"
                       "两个档都能在「设置 → 质量与速度」里切换；缺模型时启动会先问你要不要下载。").pack(
            anchor="w")

        safe_card = ttk.LabelFrame(page, text="地址与 key", padding=12)
        safe_card.pack(fill="x", pady=(14, 0))
        ttk.Label(safe_card, style="Sub.TLabel", wraplength=880, justify="left",
                  text="地址和 key 只存在本机；地址会自动补 https://、去掉结尾的 /chat/completions，"
                       "key 会自动去掉 Bearer 前缀。保存前会检查地址、key、模型名，缺了不让保存。").pack(
            anchor="w")
        self._refresh_settings_summary()
        return page

    def _refresh_settings_summary(self):
        variables = getattr(self, "settings_summary", None)
        if not variables:
            return
        settings = self.settings
        service = gui_settings.active_service(settings)
        base = str(service.get("地址") or "").strip()
        variables["service"].set(base or "—（还没配）")
        variables["ocr"].set(str(service.get("识别模型") or "—"))
        variables["summary"].set(str(service.get("摘要模型") or "—"))
        variables["tier"].set(gui_settings.tier_label(settings.get("版面档位", "M")))
        variables["conc"].set(str(settings.get("并发", gui.DEFAULT_CONCURRENCY)))
        variables["auto"].set(f"{settings.get('自动补跑轮数', 2)} 轮")
        variables["scan"].set(str(settings.get("扫描目录") or "—"))

    def _refresh_setup_hint(self):
        """转换页顶部的"还没配接口"提示：配好后自动收起。"""
        frame = getattr(self, "setup_banner", None)
        if frame is None:
            return
        service = gui_settings.active_service(self.settings)
        base = str(service.get("地址") or "").strip()
        key = str(service.get("key") or "").strip()
        env = str(service.get("key_环境变量") or "").strip()
        if not key and env:
            key = os.environ.get(env, "")
        if base and key:
            frame.pack_forget()
            return
        missing = "接口地址" if not base else "key"
        try:
            self.setup_banner_label.configure(
                text=f"还没配置{missing}：现在点「开始」会失败。先填好再转换。")
            frame.pack(fill="x", pady=(0, 6), before=getattr(self, "scan_frame", None))
        except (tk.TclError, AttributeError):
            pass

    # ------------------------------------------------------------ 提示词页
    def _build_prompt_page(self, parent):
        page = ttk.Frame(parent)
        page.columnconfigure(0, weight=0)
        page.columnconfigure(1, weight=1)
        page.rowconfigure(0, weight=1)
        left = ttk.LabelFrame(page, text="提示词文件", padding=8)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 10))
        self.prompt_tree = ttk.Treeview(
            left, columns=("title", "state", "time"), show="headings", height=8,
            selectmode="browse")
        for column, title, width in (("title", "名称", 110), ("state", "来源", 70),
                                     ("time", "修改时间", 130)):
            self.prompt_tree.heading(column, text=title)
            self.prompt_tree.column(column, width=width, anchor="w")
        self.prompt_tree.pack(fill="both", expand=True)
        self.prompt_tree.bind("<<TreeviewSelect>>", self._on_prompt_select)
        ttk.Label(left, style="Sub.TLabel",
                  text="程序目录\\提示词 里存在同名文件时优先用它。").pack(
            anchor="w", pady=(8, 0))
        ttk.Button(left, text="打开提示词目录",
                   command=self._open_prompt_dir).pack(anchor="w", pady=(6, 0))

        right = ttk.LabelFrame(page, text="正文", padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        self.prompt_title_var = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.prompt_title_var, style="Sub.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4))
        box = ttk.Frame(right)
        box.grid(row=1, column=0, sticky="nsew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.prompt_text = tk.Text(box, wrap="word", font=gui.ui_font(10),
                                   bg=gui.LOG_BG, fg=gui.LOG_FG, relief="flat")
        self.prompt_text.grid(row=0, column=0, sticky="nsew")
        prompt_scroll = ttk.Scrollbar(box, orient="vertical",
                                      command=self.prompt_text.yview)
        prompt_scroll.grid(row=0, column=1, sticky="ns")
        self.prompt_text.configure(yscrollcommand=prompt_scroll.set)
        ops = ttk.Frame(right)
        ops.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(ops, text="保存", command=self._save_prompt).pack(side="left")
        ttk.Button(ops, text="恢复默认", command=self._reset_prompt).pack(
            side="left", padx=6)
        self.prompt_status_var = tk.StringVar(value="")
        ttk.Label(ops, textvariable=self.prompt_status_var, style="Sub.TLabel").pack(
            side="left", padx=8)
        self._current_prompt = "ocr.md"
        self._prompt_by_iid = {}
        self._reload_prompts()
        return page

    def _reload_prompts(self, select=None):
        if not hasattr(self, "prompt_tree"):
            return
        self.prompt_tree.delete(*self.prompt_tree.get_children())
        self._prompt_by_iid = {}
        for item in promptlib.list_prompts():
            iid = item["name"]
            self._prompt_by_iid[iid] = item
            self.prompt_tree.insert(
                "", "end", iid=iid,
                values=(item["标题"], "已覆盖" if item["覆盖"] else "默认",
                        item["修改时间"]))
        children = self.prompt_tree.get_children()
        target = select if select in self._prompt_by_iid else self._current_prompt
        if target not in self._prompt_by_iid and children:
            target = children[0]
        if target in self._prompt_by_iid:
            self.prompt_tree.selection_set(target)
            self.prompt_tree.see(target)
            self._on_prompt_select()

    def _on_prompt_select(self, _event=None):
        selection = self.prompt_tree.selection()
        if not selection:
            return
        name = selection[0]
        self._current_prompt = name
        item = self._prompt_by_iid.get(name, {})
        self.prompt_title_var.set(
            f'{item.get("标题", name)} · {item.get("路径", "")}')
        try:
            self.prompt_text.delete("1.0", "end")
            self.prompt_text.insert("1.0", promptlib.load_prompt(name))
        except (OSError, ValueError) as exc:
            self.prompt_status_var.set("读取失败：" + str(exc))

    def _save_prompt(self):
        name = getattr(self, "_current_prompt", "")
        text = self.prompt_text.get("1.0", "end-1c")
        try:
            path = promptlib.save_prompt(name, text)
        except Exception as exc:  # noqa: BLE001 - 保存失败要告诉用户
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self.prompt_status_var.set("已保存：" + str(path))
        self._reload_prompts(select=name)

    def _reset_prompt(self):
        name = getattr(self, "_current_prompt", "")
        title = promptlib.TITLES.get(name, name)
        if not messagebox.askyesno("恢复默认", f"确定把「{title}」恢复成程序自带版本？",
                                   parent=self.root):
            return
        removed = promptlib.reset_prompt(name)
        self.prompt_status_var.set("已恢复默认" if removed else "本来就是默认版本")
        self._reload_prompts(select=name)

    def _open_prompt_dir(self):
        self.open_path(promptlib.override_dir())

    # ------------------------------------------------------------ 关于页
    def _build_about_page(self, parent):
        page = ttk.Frame(parent, padding=(4, 4))
        ttk.Label(page, text=f"pdf转 {__version__}", style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text="把扫描 PDF / Markdown 做成单文件 .why 电子书。", style="Sub.TLabel").pack(
            anchor="w", pady=(4, 14))
        box = ttk.LabelFrame(page, text="位置", padding=12)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        for row, (label, value) in enumerate((
            ("版本", __version__),
            ("程序目录", str(self.program_dir())),
            ("模型目录", self.model_dir_text()),
            ("提示词目录", str(promptlib.override_dir())),
            ("设置文件", str(self.settings_path)),
        )):
            ttk.Label(box, text=label).grid(row=row, column=0, sticky="w", pady=3)
            entry = ttk.Entry(box)
            entry.insert(0, value)
            entry.configure(state="readonly")
            entry.grid(row=row, column=1, sticky="ew", padx=(8, 0), pady=3)
        license_box = ttk.LabelFrame(page, text="许可证", padding=12)
        license_box.pack(fill="x", pady=(14, 0))
        ttk.Label(license_box, wraplength=760, justify="left",
                  text="本程序以 AGPL-3.0 发布，完整条款见程序目录的 LICENSE；"
                       "分发修改版时请同样开放源代码。").pack(anchor="w")
        note = ttk.LabelFrame(page, text="当前开发版", padding=12)
        note.pack(fill="x", pady=(14, 0))
        ttk.Label(note, wraplength=760, justify="left", style="Hint.TLabel",
                  text="当前开发版需要本机 Python 3.12；模型放在 模型\\ 目录，"
                       "缺模型时启动会先询问，不会静默下载。").pack(
            anchor="w")
        return page

    def _open_log_window(self):
        existing = getattr(self, "log_window", None)
        if existing is not None:
            try:
                existing.win.lift()
                return
            except tk.TclError:
                self.log_window = None
        win = tk.Toplevel(self.root)
        win.title("日志 · pdf转")
        win.geometry("980x620")
        win.configure(bg=gui.BG)
        win.transient(self.root)
        box = ttk.Frame(win, padding=10)
        box.pack(fill="both", expand=True)
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        text = tk.Text(box, wrap="none", font=gui.mono_font(9), bg=gui.LOG_BG,
                       fg=gui.LOG_FG, relief="flat")
        text.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(box, orient="vertical", command=text.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(box, orient="horizontal", command=text.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        text.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        try:
            text.insert("1.0", self.log_text.get("1.0", "end-1c"))
        except tk.TclError:
            pass
        close = ttk.Button(box, text="关闭", command=win.destroy)
        close.grid(row=2, column=0, columnspan=2, sticky="e", pady=(8, 0))

        class _LogWindow:
            pass

        wrapper = _LogWindow()
        wrapper.win = win
        self.log_window = wrapper