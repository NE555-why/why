# -*- coding: utf-8 -*-
"""阶段进度上报：阶段内部 report()，进程内 sink 统一收口。

CLI 的 sink 打印一行 `[进度] s3_ocr 3/12 第3页`——人看着是有意义的一行，
GUI 用 parse_line 解析同一行，按 STAGE_WEIGHTS 折算成总进度条。
没有 sink 时 report() 什么都不做，所以单元测试里调阶段函数不受影响。
"""
from __future__ import annotations

import re
import threading

# 各阶段占总进度的权重（合计 100）：③识别和⑥摘要最费时间，占大头。
STAGE_WEIGHTS = {
    "s1_pages": 6,
    "s2_layout": 14,
    "s3_ocr": 44,
    "s4_tables": 6,
    "s5_outline": 8,
    "s6_summary": 16,
    "s7_pack": 4,
    "s8_deliver": 2,
}
LINE_PREFIX = "[进度]"
_PATTERN = re.compile(r"^\[进度\]\s+([A-Za-z0-9_]+)\s+(\d+)\s*/\s*(\d+)\s*(.*)$")

_lock = threading.Lock()
_sink = None


def set_sink(sink):
    """装/卸进度接收器：sink(stage, done, total, note)；传 None 卸掉。"""
    global _sink
    with _lock:
        _sink = sink


def get_sink():
    return _sink


def report(stage, done, total, note=""):
    """上报一次进度。上报本身绝不能让流水线失败（sink 抛异常就吞掉）。"""
    sink = _sink
    if sink is None:
        return
    try:
        total = int(total)
        done = max(0, min(int(done), total)) if total > 0 else 0
        sink(str(stage), done, total, str(note or ""))
    except Exception:  # noqa: BLE001 - 进度是旁路，出错不能影响产物
        pass


def format_line(stage, done, total, note=""):
    """进度 → 一行文本（CLI 与 GUI 共用同一口径）。"""
    line = f"{LINE_PREFIX} {stage} {int(done)}/{int(total)}"
    note = str(note or "").strip()
    return f"{line} {note}" if note else line


def parse_line(line):
    """解析进度行 → (stage, done, total, note)；不是进度行返回 None。"""
    match = _PATTERN.match(str(line or "").strip())
    if not match:
        return None
    return match.group(1), int(match.group(2)), int(match.group(3)), match.group(4).strip()


def cli_sink(stage, done, total, note=""):
    """CLI 用的 sink：直接打印一行，flush 让 GUI 立刻收到。"""
    print(format_line(stage, done, total, note), flush=True)


def overall_fraction(state):
    """{stage: (done, total)} → 0..1 的总进度；缺的阶段按 0 算。"""
    weight_sum = float(sum(STAGE_WEIGHTS.values())) or 1.0
    done_weight = 0.0
    for stage, weight in STAGE_WEIGHTS.items():
        pair = (state or {}).get(stage)
        if not pair:
            continue
        done, total = pair
        if not total:
            continue
        done_weight += weight * (max(0, min(float(done), float(total))) / float(total))
    return max(0.0, min(1.0, done_weight / weight_sum))
