"""why_harness 最小书籍处理流水线。"""
__version__ = '2.0.0'

# 支持的输入文件：PDF 走切页+渲染+版面检测；Markdown/文本直接当纯文本稿，不渲染页图也不调视觉模型。
SOURCE_SUFFIXES = ('.pdf', '.md', '.markdown', '.txt')


def is_text_source(path) -> bool:
    """True = Markdown/文本输入（不需要 pymupdf 切页，也不需要版面模型）。"""
    from pathlib import Path
    return Path(path).suffix.lower() in ('.md', '.markdown', '.txt')
