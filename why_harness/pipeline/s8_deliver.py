"""⑧ 交付：只读取 .why 2.0，按需导出 Markdown、去重图片和说明。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from why_harness.paths import Paths
from why_harness.book.why2 import WhyBook, FIGURE_REF


def _convert_png(data: bytes, enabled: bool) -> tuple[bytes, str, bool]:
    if not enabled or len(data) <= 300 * 1024:
        if data.startswith(b"\x89PNG"):
            ext = ".png"
        elif data.startswith(b"\xff\xd8\xff"):
            ext = ".jpg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            ext = ".gif"
        elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            ext = ".webp"
        else:
            ext = ".bin"
        return data, ext, False
    try:
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            output = BytesIO()
            image.convert("RGB").save(output, format="JPEG", quality=85, optimize=True)
            payload = output.getvalue()
            # 转 JPEG 的目的是变小：线稿类 PNG 转出来反而更大，那就保留原图。
            if len(payload) >= len(data):
                return data, ".png", False
            return payload, ".jpg", True
    except Exception:
        return data, ".png", False


def _export_figures(book, figures, figures_dir: Path, convert: bool):
    """导出图片，返回 (figure_links, 去重后的图片数, 转 JPEG 的张数)。"""
    output_names: dict[str, str] = {}
    digest_names: dict[str, str] = {}
    pixels_converted = 0
    for fid in figures:
        data = book.figure(fid)
        digest = hashlib.sha256(data).hexdigest()
        if digest in digest_names:
            output_names[fid] = digest_names[digest]
            continue
        payload, ext, converted = _convert_png(data, convert)
        if not converted:
            try:
                from why_harness.book.why2 import image_type
                _, ext = image_type(data)
            except Exception:
                ext = ".bin"
        name = f"img-{digest[:8]}{ext}"
        (figures_dir / name).write_bytes(payload)
        digest_names[digest] = name
        output_names[fid] = name
        pixels_converted += int(converted)
    figure_links = {fid: f"figures/{name}" for fid, name in output_names.items()}
    return figure_links, len(digest_names), pixels_converted


def run(doc, paths=None, force=False, markdown=True, why=None) -> dict[str, object]:
    """交付一本书。

    markdown=True：导出 <书>.md 和 figures/（默认）。
    markdown=False：只要 .why，只补一份 说明.txt，不导 md、不导图片。
    why 显式指定用哪个包（只交付 Markdown 时是 状态/包/ 里的中间包）。"""
    paths = paths or Paths()
    out = Path(paths.output)
    why = Path(why) if why else Path(paths.find_why(doc))
    if not why.is_file():
        raise FileNotFoundError(f"找不到 .why：{why}（先跑 ⑦ 打包）")
    figures_dir = out / "figures"
    if markdown:
        figures_dir.mkdir(parents=True, exist_ok=True)
        # force：重交付。先清掉旧 figures 里的文件，免得换了规则/模型后上一轮的图变成孤儿。
        if force:
            for stale in figures_dir.iterdir():
                if stale.is_file():
                    stale.unlink()
    convert = os.environ.get("WHY_CONVERT_PNG", "").lower() in {"1", "true", "yes", "是"}
    markdown_text = ""
    missing_refs: list[str] = []
    pixels_converted = 0
    image_count = 0
    with WhyBook(why) as book:
        verification = book.verify()
        if not verification["ok"]:
            raise ValueError(".why 校验失败: " + repr(verification["problems"]))
        nodes = book.manifest.get("nodes", {})
        figures = book.manifest.get("assets", {}).get("figures", {})
        tables = book.manifest.get("assets", {}).get("tables", {})
        if markdown:
            figure_links, image_count, pixels_converted = _export_figures(
                book, figures, figures_dir, convert)
            raw_by_node = {nid: book.text(nid) or "" for nid in nodes}
            for nid, raw in raw_by_node.items():
                for fid in FIGURE_REF.findall(raw):
                    if fid not in figure_links:
                        missing_refs.append(fid)
            for fid in figures:
                if fid not in figure_links:
                    missing_refs.append(fid)
            markdown_text = book.export_markdown(figure_link=figure_links).rstrip() + "\n"
        walk = book.walk()
        deepest = max((depth for _, depth in walk), default=0)
        node_count = len(walk)
        stale_count = len(verification["warnings"])
    md_path = None
    if markdown:
        md_path = out / f"{doc}.md"
        md_path.write_text(markdown_text, encoding="utf-8")
    lines = [f"交付项：{doc}"]
    if markdown:
        lines.extend([f"正文：{doc}.md", f"节点数：{node_count}", f"最深层数：{deepest}"])
        if image_count:
            lines.append(f"图片：figures/ 目录，共 {image_count} 张（其中 {pixels_converted} 张由 PNG 转 JPEG）")
    else:
        lines.extend([f"电子书：{why.name}", f"节点数：{node_count}", f"最深层数：{deepest}"])
    lines.extend([f"表格数：{len(tables)}", f"过期摘要数：{stale_count}"])
    (out / "说明.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = {"why": str(why), "md": str(md_path) if md_path else None,
              "figures": image_count, "pixels_converted": pixels_converted,
              "missing_refs": missing_refs}
    if markdown:
        print(f"⑧交付: {md_path.name}，图片 {image_count}，缺失 {len(missing_refs)}")
    else:
        print(f"⑧交付: 只留 {why.name}，不导 Markdown")
    return result