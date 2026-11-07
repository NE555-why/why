# -*- coding: utf-8 -*-
"""提示词：包内默认 + 用户覆盖（程序目录\\提示词\\<name>）。

优先级只有两层：程序目录\\提示词\\<name> 存在就用它，否则用包内的 prompts/<name>。
这样用户在界面里改提示词不用动程序文件，升级程序也不会把改动覆盖掉。
摘要缓存键、打包溯源里的 prompt_sha256 一律按"当前生效的提示词"算，
所以改了提示词，旧摘要不会被当成新的复用。
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .paths import program_dir

PACKAGE_PROMPTS = Path(__file__).resolve().parent / "prompts"
OVERRIDE_DIR_NAME = "提示词"
PROMPT_NAMES = ("ocr.md", "summary.md")
TITLES = {"ocr.md": "识别提示词", "summary.md": "摘要提示词"}


def _check(name) -> str:
    """只认已知的两个提示词名，挡住 ../ 之类的路径穿越。"""
    value = str(name or "").strip()
    if value not in PROMPT_NAMES:
        raise ValueError(f"未知提示词：{name}（只有 {'、'.join(PROMPT_NAMES)}）")
    return value


def override_dir() -> Path:
    """用户提示词目录：程序目录\\提示词。"""
    return program_dir() / OVERRIDE_DIR_NAME


def package_path(name) -> Path:
    return PACKAGE_PROMPTS / _check(name)


def override_path(name) -> Path:
    return override_dir() / _check(name)


def is_overridden(name) -> bool:
    """当前用的是不是用户覆盖的那份。"""
    try:
        return override_path(name).is_file()
    except (OSError, ValueError):
        return False


def prompt_path(name) -> Path:
    """当前生效的提示词文件：有覆盖用覆盖，否则包内。"""
    path = override_path(name)
    return path if path.is_file() else package_path(name)


def load_prompt(name) -> str:
    """读当前生效的提示词正文。"""
    return prompt_path(name).read_text(encoding="utf-8")


def prompt_sha256(name):
    """当前生效提示词的 sha256；读不到返回 None（降级不崩）。"""
    try:
        data = prompt_path(name).read_bytes()
    except (OSError, ValueError):
        return None
    return hashlib.sha256(data).hexdigest()


def save_prompt(name, text) -> Path:
    """把正文写到覆盖目录；返回写入路径。空正文拒绝（免得把提示词改成空）。"""
    name = _check(name)
    if not str(text or "").strip():
        raise ValueError("提示词不能为空")
    path = override_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def reset_prompt(name) -> bool:
    """删掉覆盖、回到包内默认；删到了返回 True，本来就没有返回 False。"""
    try:
        override_path(name).unlink()
        return True
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return False


def list_prompts() -> list:
    """两个提示词的现状：名字、标题、生效路径、是否覆盖、大小、修改时间。"""
    items = []
    for name in PROMPT_NAMES:
        path = prompt_path(name)
        try:
            stat = path.stat()
        except OSError:
            stat = None
        items.append({
            "name": name,
            "标题": TITLES[name],
            "路径": path,
            "覆盖": is_overridden(name),
            "大小": stat.st_size if stat else 0,
            "修改时间": time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)) if stat else "",
        })
    return items
