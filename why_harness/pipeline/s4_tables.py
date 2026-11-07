"""④ 从逐页 OCR 页稿提取 HTML 表格并写入状态索引。"""
from html import escape
from html.parser import HTMLParser
import json
import re

from why_harness.paths import Paths


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.in_table = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag.lower() == 'table':
            self.in_table = True
        elif self.in_table and tag.lower() == 'tr':
            self.row = []
        elif self.in_table and tag.lower() in ('td', 'th'):
            try:
                rs = int(attrs.get('rowspan', 1)); cs = int(attrs.get('colspan', 1))
                if rs < 1 or cs < 1: raise ValueError('span 必须为正整数')
            except (TypeError, ValueError) as exc:
                raise ValueError(f'非法 rowspan/colspan: {exc}') from exc
            self.cell = {'text': '', 'rowspan': rs, 'colspan': cs}

    def handle_data(self, data):
        if self.cell is not None: self.cell['text'] += data

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ('td', 'th') and self.cell is not None:
            self.cell['text'] = re.sub(r'\s+', ' ', self.cell['text']).strip()
            if self.row is not None: self.row.append(self.cell)
            self.cell = None
        elif tag == 'tr' and self.row is not None:
            self.rows.append(self.row); self.row = None
        elif tag == 'table':
            self.in_table = False


def _matrix(raw):
    parser = _TableParser()
    parser.feed(raw); parser.close()
    if parser.in_table or parser.cell is not None or not parser.rows:
        raise ValueError('HTML table 结构不完整或没有行')
    return parser.rows


def _columns(rows):
    # 以 rowspan 占位、colspan 展开的逻辑网格宽度比较续表列数。
    pending = {}
    widths = []
    for row in rows:
        col = 0
        for cell in row:
            while pending.get(col, 0) > 0: col += 1
            width = cell['colspan']; widths.append(col + width)
            for c in range(col, col + width):
                if cell['rowspan'] > 1: pending[c] = cell['rowspan'] - 1
            col += width
        for c in list(pending):
            if pending[c] > 0: pending[c] -= 1
    return max(widths, default=0)


# 页脚/页码类内容：前表之后到页尾只剩这些，仍算“紧贴页尾”，可以判续表。
# ③ 的提示词要求“页眉页脚、页码照抄”，所以页尾真正的障碍通常就是这些短标记。
_FOOTER_RES = (
    re.compile(r'^\d{1,4}$'),                                              # 3
    re.compile(r'^[-—–_·…\s]+$'),                                          # ---  ···  —
    re.compile(r'^[-—–~～·]\s*\d{1,4}\s*[-—–~～·]$'),                      # - 3 -  · 3 ·
    re.compile(r'^[（(]\s*\d{1,4}\s*[）)]$'),                              # (3)
    re.compile(r'^\d{1,4}\s*[/／]\s*\d{1,4}$'),                            # 3/10
    re.compile(r'^第\s*\d{1,4}\s*页(\s*[/／]?\s*共?\s*\d{1,4}\s*页)?$'),   # 第 3 页 / 第3页共10页
    re.compile(r'^共\s*\d{1,4}\s*页$'),                                  # 共 10 页
    re.compile(r'^page\s*\d{1,4}(\s*(?:of|/)\s*\d{1,4})?$', re.I),      # page 3 of 10
)
_FOOTER_MAX = 24  # 页脚是短标记；超过这个长度的行一律按正文处理，宁可不合


def _footer_line(line):
    """单行是否是空白/页码/页脚标记。"""
    text = re.sub(r'\s+', ' ', line).strip()
    if not text:
        return True
    if len(text) > _FOOTER_MAX:
        return False
    return any(pattern.match(text) for pattern in _FOOTER_RES)


def _footer_only(text):
    """表格之后到页尾是否只剩空白和页码/页脚；出现正文行就返回 False。"""
    return all(_footer_line(line) for line in text.splitlines())


def _cells_key(row):
    return [re.sub(r'\s+', '', cell['text']) for cell in row]


def _same_row(left, right):
    """逐格文本相等（忽略空白）。整行都没有字的不算重复表头，避免吞掉空数据行。"""
    if not left or len(left) != len(right):
        return False
    key = _cells_key(left)
    return key == _cells_key(right) and any(key)


def _render(rows, open_tag='<table>'):
    """按 cells 重建一张表：续表合并后只保留主表的外壳，不再拼两个 <table>。"""
    lines = [open_tag]
    for row in rows:
        pieces = []
        for cell in row:
            attrs = ''
            if cell['rowspan'] > 1: attrs += f' rowspan="{cell["rowspan"]}"'
            if cell['colspan'] > 1: attrs += f' colspan="{cell["colspan"]}"'
            pieces.append(f'<td{attrs}>{escape(cell["text"], quote=False)}</td>')
        lines.append('<tr>' + ''.join(pieces) + '</tr>')
    lines.append('</table>')
    return '\n'.join(lines)


def _page_files(folder, doc):
    found = []
    for path in folder.glob(f'{doc}-*.md'):
        match = re.search(r'-(\d+)$', path.stem)
        if match: found.append((int(match.group(1)), path))
    return sorted(found)


def _cached(target, page_paths):
    """已有的 表格-<书>.json：结构完好且不比任何页稿旧才复用，否则返回 None 重算。"""
    try:
        data = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get('tables'), list):
        return None
    try:
        stamp = target.stat().st_mtime
        if any(path.stat().st_mtime > stamp for path in page_paths):
            return None
    except OSError:
        return None
    return data


def run(doc, paths=None, force=False):
    paths = paths or Paths()
    folder = paths.state / '页稿' / doc
    page_files = _page_files(folder, doc)
    target = paths.state / f'表格-{doc}.json'
    if not force:
        cached = _cached(target, [path for _, path in page_files])
        if cached is not None:
            print(f'④表格: 复用已有 {len(cached["tables"])} 张（页稿没变；要重算加 --force）')
            return cached

    pages = []
    for page, path in page_files:
        text = path.read_text(encoding='utf-8')
        found = list(re.finditer(r'<table\b[^>]*>.*?</table\s*>', text, re.I | re.S))
        entries = []
        for order, match in enumerate(found, 1):
            raw = match.group(0)
            shell = re.match(r'<table\b[^>]*>', raw, re.I)
            item = {'id': f'p{page}-t{order}', 'page': page, 'order': order,
                    'cells': [], 'html': raw, 'continued': False,
                    '_start': match.start(), '_end': match.end(), '_col_count': 0,
                    '_open': shell.group(0) if shell else '<table>'}
            try:
                item['cells'] = _matrix(raw); item['_col_count'] = _columns(item['cells'])
            except Exception as exc:
                item['error'] = str(exc)
            entries.append(item)
        pages.append((page, text, entries))

    # 仅当页码相邻、前表紧贴页尾（页尾只允许页脚/页码）、后表紧贴页首、两表均解析成功
    # 且逻辑列数相同，才合并；两表之间有任何正文段落就分开，避免把独立表格误并。
    merged = []
    joins = 0
    for page, text, entries in pages:
        for item in entries:
            if merged:
                prev = merged[-1]
                prior_page = prev['_page_text']
                prior_entry = prev['_last_entry']
                # 已合并的条目 page 是最前一段的页码，相邻判定要看最后一段来自哪页。
                between_pages = page == prior_entry['page'] + 1
                prior_tail = prior_page[prior_entry['_end']:]
                current_head = text[:item['_start']].strip()
                if (between_pages and _footer_only(prior_tail) and not current_head and
                        not prev.get('error') and not item.get('error') and
                        prev['_col_count'] == item['_col_count'] and prev['_col_count'] > 0):
                    rows = item['cells']
                    # 续表常在页首重复表头：首行与主表首行逐格相同就丢掉，表头只留一份。
                    if prev['cells'] and rows and _same_row(prev['cells'][0], rows[0]):
                        rows = rows[1:]
                    prev['cells'] = prev['cells'] + rows
                    prev['html'] = _render(prev['cells'], prev.get('_open') or '<table>')
                    prev['continued'] = True
                    prev['merged_ids'] = prev.get('merged_ids', []) + [item['id']]
                    prev['_last_entry'] = item
                    prev['_page_text'] = text
                    joins += 1
                    continue
            item['_last_entry'] = item; item['_page_text'] = text
            merged.append(item)
    tables = [{k: v for k, v in item.items() if not k.startswith('_')} for item in merged]
    result = {'doc': doc, 'tables': tables, 'count': len(tables)}
    paths.save(target, result)
    tail = f'（续表合并 {joins} 处）' if joins else ''
    print(f'④表格: {len(tables)} 张{tail}')
    return result
