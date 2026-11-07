"""⑦ 打包：把章节树、摘要、插图、表格写成 .why 2.0 单文件（写入器自检通过才原子替换）。"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from why_harness.paths import Paths
from why_harness.book.why2 import write_book
from why_harness.promptlib import prompt_sha256

_NODE_FIELDS = ("title", "role", "children", "pages", "page_map", "origin", "confidence", "split_rule")


def _drop_table_anchor(raw: str, tid: str) -> str:
    """去掉 raw 里指向 tid 的表格锚点（data-why 属性），保留 <table> 本体；避免空表引用悬空。"""
    return re.sub(r'(<table\b[^>]*?)\s+data-why="tab/' + re.escape(tid) + r'"', r'\1', raw, flags=re.I)


def _load(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default
    except (OSError, ValueError):
        return default


def _resolve(root: Path, value: Any) -> Path | None:
    if not value:
        return None
    p = Path(str(value))
    candidates = [p] if p.is_absolute() else [root / p, root.parent / p]
    return next((x for x in candidates if x.exists() and x.is_file()), None)


def _page_image(root: Path, doc: str, page: int) -> Path | None:
    folder = root / "状态" / "页图" / doc
    return next(iter(sorted(folder.glob(f"{doc}-{page:03d}-page.*"))), None) if folder.exists() else None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _provenance(root: Path, state: Path, doc: str, chapters: dict, index: dict, layout: dict) -> dict:
    """TRAILER 里的溯源事实：只放与输入有关、重跑不变的内容（不放耗时、token 数）。"""
    ocr = _load(state / f"ocr-{doc}.json", {})
    summary = _load(state / f"摘要-{doc}.json", {})

    methods = ocr.get("methods") or {}
    return {
        "pages": {"count": index.get("page_count"),
                  "text_layer": sum(1 for p in index.get("pages", []) if p.get("has_text_layer")),
                  "bookmarks": len(index.get("outline") or [])},
        "layout": {"model": layout.get("model"), "tier": layout.get("tier")},
        "ocr": {"text_layer_pages": sum(1 for m in methods.values() if m == "text-layer"),
                "vision_pages": sum(1 for m in methods.values() if m == "vision"),
                "prompt_sha256": prompt_sha256("ocr.md")},
        "outline": {"conservation": chapters.get("conservation"), "warnings": chapters.get("warnings", [])},
        "summary": {"model": summary.get("model"),
                    "prompt_sha256": prompt_sha256("summary.md")},
    }


def run(doc, paths=None, force=False, provenance=True, keep_why=True) -> dict[str, Any]:
    """⑦ 打包。

    keep_why=False 表示只要 Markdown：.why 只当中间包放进 状态/包/，不落到成品目录。
    provenance=False 表示成品末尾不写溯源段（TRAILER 省掉）。"""
    paths = paths or Paths()
    root, state = Path(paths.root), paths.state
    chapters = _load(state / f"章节-{doc}.json", {})
    if not chapters.get("nodes"):
        raise FileNotFoundError(f"缺少章节文件或章节为空：{state / f'章节-{doc}.json'}")
    summaries = _load(state / f"摘要-{doc}.json", {}).get("nodes") or {}
    layout = _load(state / "版面" / f"{doc}.json", {})
    index = _load(state / "页图" / f"{doc}.json", {})
    crops = {str(f.get("key")): f.get("path") for p in layout.get("pages", []) or [] for f in p.get("figures", []) or []}
    parsed_tables = {str(t.get("id")): t for t in _load(state / f"表格-{doc}.json", {}).get("tables", []) or []}
    table_assets = chapters.get("assets", {}).get("tables", [])
    # 兜底：cells 缺失或为空的表跳过，不写进 assets；锚点一并从 raw 里摘掉，避免悬空引用。
    skipped_tids = {item["id"] for item in table_assets
                    if not (parsed_tables.get(item.get("source_ref")) or {}).get("cells")}

    nodes, blocks, keywords, stale = {}, {}, {}, 0
    for node in chapters["nodes"]:
        nid = node["id"]
        nodes[nid] = {field: node[field] for field in _NODE_FIELDS}
        raw = node["text"]
        if raw and skipped_tids:
            for tid in skipped_tids:
                raw = _drop_table_anchor(raw, tid)
        block: dict[str, Any] = {"raw": raw} if raw else {}
        done = summaries.get(nid) or {}
        for kind in ("summary", "brief"):
            item = done.get(kind)
            if item and item.get("text") and done.get("src"):
                block[kind] = {"text": item["text"], "src": done["src"], "by": item.get("by", "heuristic"),
                               "model": item.get("model")}
                stale += done["src"] != node["tree_sha256"]
        if block:
            blocks[nid] = block
        # ⑥ 已校验过关键词是 raw 的子串；这里再校验一遍，因为 ⑥ 可能是针对旧的章节结果跑的。
        for word in done.get("keywords") or []:
            if isinstance(word, str) and word and word in node["text"]:
                keywords.setdefault(word, []).append(nid)

    figures, fallbacks = {}, []
    for item in chapters.get("assets", {}).get("figures", []):
        path = _resolve(root, crops.get(item["source_ref"]))
        fallback = path is None
        if fallback:
            path = _page_image(root, doc, int(item["page"]))
        if path is None:
            raise FileNotFoundError(f"插图 {item['source_ref']} 找不到裁图，也没有整页图可退回")
        if fallback:
            fallbacks.append(item["source_ref"])
        figures[item["id"]] = {"node": item["node"], "anchor": item["anchor"], "caption": item.get("caption", ""),
                               "page": item["page"], "source_ref": item["source_ref"], "fallback": fallback,
                               "data": path.read_bytes()}
    tables = {}
    for item in table_assets:
        if item["id"] in skipped_tids:
            continue
        parsed = parsed_tables.get(item["source_ref"]) or {}
        tables[item["id"]] = {"node": item["node"], "anchor": item["anchor"], "page": item["page"],
                              "source_ref": item["source_ref"], "continued": bool(parsed.get("continued")),
                              "cells": parsed.get("cells") or None}

    pdf = Path(str(index.get("source_pdf") or ""))
    source = {"file": pdf.name if index.get("source_pdf") else None,
              "sha256": _sha256_file(pdf) if index.get("source_pdf") and pdf.is_file() else None}
    target = paths.why_file(doc) if keep_why else paths.pack_file(doc)
    out = target.parent
    out.mkdir(parents=True, exist_ok=True)
    # force：重打包。打包本来就每次重写（临时包 + 原子替换），这里再清掉上次中断留下的临时包；
    # 正式 .why 仍等新包通过 write_book 自检后才替换，重做失败不毁旧产物。
    if force:
        for stale in out.glob(f".{doc}.*.why.tmp"):
            stale.unlink()
    with tempfile.NamedTemporaryFile(prefix=f".{doc}.", suffix=".why.tmp", dir=out, delete=False) as handle:
        temp = Path(handle.name)
    try:
        # write_book 写完会按规范 §6 自检，不合格直接抛错；通过后才替换正式文件。
        write_book(temp, book={"title": doc, "language": "zh-CN", "page_count": chapters.get("pages")},
                   source=source, root=chapters["root"], nodes=nodes, blocks=blocks, figures=figures,
                   tables=tables, keywords=keywords,
                   trailer={"provenance": _provenance(root, state, doc, chapters, index, layout)}
                   if provenance else None)
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    result = {"doc": doc, "why": str(target), "provenance": bool(provenance),
              "nodes": len(nodes), "figures": len(figures),
              "figure_fallbacks": fallbacks, "tables": len(tables), "stale_summaries": stale,
              "bytes": target.stat().st_size}
    print(f"⑦打包: {target.name}，{len(nodes)} 个节点，插图 {len(figures)}（整页图退回 {len(fallbacks)}），"
          f"表格 {len(tables)}，过期摘要 {stale}，溯源段 {'有' if provenance else '无'}，大小 {result['bytes']} 字节")
    return result