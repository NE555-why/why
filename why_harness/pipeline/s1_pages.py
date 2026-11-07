"""① 切页并生成页图索引。

PDF：逐页渲染页图并记录文本层判定，供 ② 版面、③ 视觉识别使用。
Markdown/文本（.md/.markdown/.txt）：按标题和空行切段，每段当一页，正文直接存进索引，
不渲染页图、不做版面检测、不调视觉模型——② 会整段跳过。
"""
from pathlib import Path
import re
import pymupdf
from why_harness import progress, is_text_source
from why_harness.paths import Paths
from why_harness.config import float_value

# 单个"页"最多多少字符；超过就再切，避免 ⑤ 一个节点吞掉整本书。
_CHUNK_LIMIT = 4000


def _split_long(block, limit=_CHUNK_LIMIT):
    """把超长段落按行切开，尽量落在行边界上。"""
    pieces, buffer, size = [], [], 0
    for line in block.splitlines(keepends=True):
        if buffer and size + len(line) > limit:
            pieces.append(''.join(buffer).strip())
            buffer, size = [], 0
        buffer.append(line)
        size += len(line)
    if buffer:
        pieces.append(''.join(buffer).strip())
    return [piece for piece in pieces if piece]


def _text_chunks(text):
    """按空行归类成段；标题（# 开头）另起一段；超长段落再按长度切。"""
    chunks, current = [], []
    for block in re.split(r'\n\s*\n', text):
        block = block.strip()
        if not block:
            continue
        if current and block.startswith('#'):
            chunks.append('\n\n'.join(current))
            current = []
        for piece in _split_long(block):
            current.append(piece)
            if sum(len(item) for item in current) >= _CHUNK_LIMIT:
                chunks.append('\n\n'.join(current))
                current = []
    if current:
        chunks.append('\n\n'.join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def _read_text_source(source):
    """读取 Markdown/文本：先按 UTF-8，失败再按 GB18030 兜一次（老 Windows 记事本）。"""
    data = Path(source).read_bytes()
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def _text_pages(source, doc, paths, out):
    """Markdown/文本输入：切成若干"页"，每页只有正文文本（page_png=None）。"""
    text = _read_text_source(source)
    chunks = _text_chunks(text)
    pages, outline = [], []
    for number, chunk in enumerate(chunks, 1):
        pages.append({'page': number, 'width': 0, 'height': 0, 'text_chars': len(chunk),
                      'has_text_layer': True, 'page_png': None, 'text': chunk})
        # 用 md 标题当书签，交给 ⑤ 做章节骨架（没有标题就是空表，⑤ 会自己按页切）。
        for line in chunk.splitlines():
            match = re.match(r'(#{1,3})\s+(.{1,60})$', line.strip())
            if match:
                outline.append([len(match.group(1)), match.group(2).strip(), number])
        progress.report('s1_pages', number, len(chunks), f'第 {number} 段')
    return pages, outline


def run(pdf, doc=None, paths=None, config=None, force=False):
    pdf = Path(pdf).resolve()
    doc = doc or pdf.stem
    paths = paths or Paths()
    out = paths.state / '页图' / doc
    out.mkdir(parents=True, exist_ok=True)
    if is_text_source(pdf):
        pages, outline = _text_pages(pdf, doc, paths, out)
        result = {'doc': doc, 'source_pdf': str(pdf), 'source_kind': 'text', 'page_count': len(pages),
                  'pages': pages, 'outline': outline}
        paths.save(paths.state / '页图' / f'{doc}.json', result)
        print(f'①读入: {len(pages)}段（Markdown/文本，不需要页图和版面检测）')
        return result
    scale = float_value(config or {}, '渲染', '倍率', 2.0)
    source = pymupdf.open(pdf)
    pages, written = [], set()
    total_pages = int(getattr(source, 'page_count', 0) or 0)
    for i, page in enumerate(source, 1):
        image = out / f'{doc}-{i:03d}-page.png'
        page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False).save(image)
        written.add(image.name)
        text = page.get_text('text').strip()
        pages.append({'page': i, 'width': round(page.rect.width, 2), 'height': round(page.rect.height, 2),
                      'text_chars': len(text), 'has_text_layer': len(text) > 20,
                      'page_png': str(image.relative_to(paths.root))})
        progress.report('s1_pages', i, total_pages or i, f'第 {i} 页')
    # PDF 书签（[层级, 标题, 页号]）交给 ⑤ 作章节骨架；没有书签或读取失败就是空表。
    try:
        outline = [[int(level), str(title), int(page)] for level, title, page, *_ in source.get_toc(simple=True)]
    except Exception:
        outline = []
    # force：重切页图。页图本来就逐页重渲染，这里再清掉上一次多出来、这次没有的页图
    # （PDF 页数变少时），别让旧页图以孤儿文件留在 页图/<书>/ 里。
    if force:
        for stale in out.glob(f'{doc}-*-page.png'):
            if stale.name not in written:
                stale.unlink()
    result = {'doc': doc, 'source_pdf': str(pdf), 'source_kind': 'pdf', 'page_count': len(pages),
              'pages': pages, 'outline': outline}
    paths.save(paths.state / '页图' / f'{doc}.json', result)
    source.close()
    print(f'①切页: {len(pages)}页，页图 {out}')
    return result
