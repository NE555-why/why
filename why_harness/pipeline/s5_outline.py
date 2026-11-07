"""⑤ 切章：把逐页页稿建成一棵章节树。

层级以编号体系（第X编 > 第X章 > 第X节 / 1.1 > 1.1.1 > 一、 > （一） > 1. > （1） > ①）和编号连续性为准；
视觉模型逐页识别，`#` 个数只反映单页内部的大小关系，只作参考。PDF 书签存在时以书签为骨架。
节点编号随机生成，按登记表跨次沿用；输出的 text 就是 .why 的最终 raw（插图、表格已改写成锚点形式）。
"""
from collections import Counter
import difflib
import hashlib
import json
from pathlib import Path
import re
import secrets
import unicodedata

from why_harness.book.why2 import tree_sha256
from why_harness.paths import Paths


# 表格/插图常以占位符或布局标记存在，无法要求章节字符数与页稿逐字 1:1 对齐。
CONSERVATION_THRESHOLD = 0.95
CONSERVATION_UPPER_THRESHOLD = 1.02


class ConservationError(RuntimeError):
    """页稿与章节内容的字符守恒低于审计阈值。"""


# ── 行级规则 ──────────────────────────────────────────────────────

_NOISE_PATTERNS = (
    re.compile(r'^\s*[-—–~·•]*\s*\d+\s*[-—–~·•]*\s*$'),
    re.compile(r'^\s*第\s*\d+\s*页(?:\s*[,，/／]?\s*共\s*\d+\s*页)?\s*$'),
    re.compile(r'^\s*\d+\s*[/／]\s*\d+\s*$'),
    re.compile(r'^\s*(?:page|p\.)\s*\d+(?:\s*(?:of|/)\s*\d+)?\s*$', re.I),
)
_HEADING = re.compile(r'^\s*(#{1,})\s+(.+?)(?:\s+#+)?\s*$')
_IMAGE = re.compile(r'!\[[^\]]*\]\([^)]*\)')
_FIGURE_REF = re.compile(r'!\[(?P<caption>[^\]]*)\]\(figure:(?P<key>p(?P<page>\d+)-f\d+)\)')
_FIGURE_FINAL = re.compile(r'!\[(?P<caption>[^\]]*)\]\(why:fig/(?P<id>[0-9a-f]{16})\)')
_TABLE_BLOCK = re.compile(r'<table\b[^>]*>.*?</table\s*>', re.I | re.S)
_TABLE_FINAL = re.compile(r'<table data-why="tab/(?P<id>[0-9a-f]{16})"')
_TABLE_TAGS = re.compile(r'</?(?:table|thead|tbody|tfoot|tr|td|th|caption|colgroup|col)\b[^>]*>', re.I)
_MATH_BLOCK = re.compile(r'\$\$.+?\$\$', re.S)
# 目录条目的尾巴：引导线或大段空白后接页码（文本已做 NFKC，省略号变成了 "..."）。
_TOC_TAIL = re.compile(r'(?:[.·•_\-—]{3,}|\s{2,})\s*(?:\d{1,4}|[ivxlc]{1,6})\s*$', re.I)


def _nfkc(text):
    return unicodedata.normalize('NFKC', text)


def _is_noise(line):
    return any(pattern.fullmatch(line) for pattern in _NOISE_PATTERNS)


def _strip(text):
    """守恒口径的文本归一：去表格标签、插图引用、标题标记、空白和强调星号。"""
    text = _TABLE_TAGS.sub('', _IMAGE.sub('', text))
    text = re.sub(r'(?m)^\s*#+\s+', '', text)
    return re.sub(r'[\s*]+', '', text)


def _count(text):
    """守恒口径：去掉标题标记、强调星号、插图引用和表格标签后的非空白字符数。"""
    return len(_strip(text))


def _strip_title(title):
    """标题已经去掉过自己那一层 #，归一化时不能再把标题里的 # 当标记删掉。

    页首写成 "## ## 4" 时，_HEADING 只剥掉第一层，剩下的 "## 4" 是正文内容；
    旧口径按 _strip 又会把这两个 # 当标记删掉，守恒比因此少算 2 字/页。"""
    text = _TABLE_TAGS.sub('', _IMAGE.sub('', title))
    return re.sub(r'[\s*]+', '', text)


def _count_title(title):
    """守恒口径的标题字数：标题不再剥 # 标记。"""
    return len(_strip_title(title))


def _title_key(text):
    """比较标题、页眉用的归一文本：去 #、目录引导线与页码、首尾页码、（续）、空白和标点。"""
    text = re.sub(r'^#+\s*', '', _nfkc(text).lower().strip())
    text = _TOC_TAIL.sub('', text)
    text = re.sub(r'^\d+\s+|\s+\d+$', '', text)
    text = re.sub(r'\((?:续|續)\)$', '', text)
    return re.sub(r'[\W_]+', '', text)


def _name_key(title):
    """按名称识别（前言、目录、附录……）用：NFKC、小写、去空白、去强调和末尾括注。"""
    text = re.sub(r'\s+', '', _nfkc(title).lower()).strip('*')
    return re.sub(r'\(.*\)$', '', text) or text


def _edge_key(line):
    """页眉页脚候选的归一文本；题号、选项、标签、插图、HTML 这类行不参与，返回 None。"""
    text = re.sub(r'^#+\s*', '', _nfkc(line).strip())
    if not text or _IMAGE.search(text) or text.startswith('<') or text.endswith(':'):
        return None
    if re.match(r'(?:\d+\s*[.、](?!\d)|\(\d+\)|[a-d]\s*[.、])', text, re.I):
        return None
    key = _title_key(text)
    return key if 2 <= len(key) <= 40 else None


# ── 编号体系 ──────────────────────────────────────────────────────

_CN = '零〇一二两三四五六七八九十百千'
_ORD = rf'\d+|[{_CN}]+'
# （样式, 层级秩, 正则）：秩越小层级越高；正则作用于 NFKC 后的标题。
_WORD_STYLES = (
    ('part', 10, re.compile(rf'^第\s*({_ORD})\s*(?:编|篇|部分|卷)')),
    ('part', 10, re.compile(r'^part\s+(\d+|[ivxlc]+)\b', re.I)),
    ('unit', 15, re.compile(rf'^第\s*({_ORD})\s*(?:单元|模块|专题)')),
    ('unit', 15, re.compile(rf'^(?:单元|模块|专题)\s*({_ORD})(?![\d.])')),
    ('unit', 15, re.compile(r'^(?:unit|module)\s+(\d+)\b', re.I)),
    ('chapter', 20, re.compile(rf'^第\s*({_ORD})\s*(?:章|讲|课|回)')),
    ('chapter', 20, re.compile(r'^(?:chapter|lesson|lecture)\s+(\d+|[ivxlc]+)\b', re.I)),
    ('section', 30, re.compile(rf'^第\s*({_ORD})\s*节')),
    ('section', 30, re.compile(r'^§\s*(\d+)(?![\d.])')),
)
_DECIMAL = re.compile(r'^§?\s*((?:[A-Z]|\d+)(?:[.\-]\d+)+)\.?(?=\s|[^\x00-\x7f]|$)')
_LIST_STYLES = (
    ('bare', 25, re.compile(r'^(\d{1,2})\s+(?=[^\s\d])')),
    ('cn', 50, re.compile(rf'^([{_CN}]{{1,3}})(?:\s*[、.]|\s+(?=\S))')),
    ('cn_paren', 60, re.compile(rf'^\(\s*([{_CN}]{{1,3}})\s*\)')),
    ('arabic', 70, re.compile(r'^(\d{1,3})\s*[、.](?!\d)')),
    ('ar_paren', 80, re.compile(r'^\(?\s*(\d{1,3})\s*\)')),
)
_PROMOTE = re.compile(rf'^(?:第\s*(?:{_ORD})\s*(?:编|篇|部分|卷|单元|章|节|讲|课|回)'
                      r'|(?:chapter|part|unit|lesson)\s+(?:\d+|[ivxlc]+)\b)', re.I)
_CN_DIGITS = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
              '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
_CN_UNITS = {'十': 10, '百': 100, '千': 1000}


def _ordinal(text):
    """阿拉伯、中文、罗马数字序号转整数；无法解析返回 None。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if re.fullmatch(r'[ivxlc]+', text, re.I):
        values, text = {'i': 1, 'v': 5, 'x': 10, 'l': 50, 'c': 100}, text.lower()
        return sum(-values[c] if i + 1 < len(text) and values[text[i + 1]] > values[c] else values[c]
                   for i, c in enumerate(text))
    total, digit = 0, None
    for char in text:
        if char in _CN_DIGITS:
            digit = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            total += (1 if digit is None else digit) * _CN_UNITS[char]
            digit = None
        else:
            return None
    return total + (digit or 0)


def _classify(title):
    """标题的编号样式、序号、层级秩和小节标签；无编号时 number 为 None。"""
    text = _nfkc(title).strip().strip('*').strip()
    for style, rank, pattern in _WORD_STYLES:
        match = pattern.match(text)
        if match and _ordinal(match.group(1)):
            return {'style': style, 'rank': rank, 'number': _ordinal(match.group(1)), 'label': None}
    match = _DECIMAL.match(text)
    if match:
        parts = re.split(r'[.\-]', match.group(1))
        style, rank = {2: ('dec2', 30), 3: ('dec3', 40)}.get(len(parts), ('dec4', 45))
        label = '.'.join(str(int(p)) if p.isdigit() else p for p in parts)
        return {'style': style, 'rank': rank, 'number': int(parts[-1]), 'label': label}
    for style, rank, pattern in _LIST_STYLES:
        match = pattern.match(text)
        if match and _ordinal(match.group(1)):
            return {'style': style, 'rank': rank, 'number': _ordinal(match.group(1)), 'label': None}
    first = title.strip().lstrip('*')[:1]
    if first and '①' <= first <= '⑳':
        return {'style': 'circled', 'rank': 90, 'number': ord(first) - ord('①') + 1, 'label': None}
    return {'style': 'bracket' if text.startswith('【') else 'plain', 'rank': None, 'number': None, 'label': None}


# ── 名称与角色 ────────────────────────────────────────────────────

_TOC_NAME = re.compile(r'目录|目次|contents|总目录|简目|详目')
_FRONT_NAME = re.compile(r'前言|序|序言|自序|代序|再版前言|第.版前言|修订版前言|出版说明|编者的话|写在前面'
                         r'|内容简介|内容提要|作者简介|致读者|编写说明|使用说明|本书导读|导读|献词')
_INTRO_NAME = re.compile(r'绪论|引言|导言|导论|引论|序论|概论|introduction')
_BACK_NAME = re.compile(r'(?:附录|附表|附图).*|参考文献|参考书目|主要参考文献|参考资料|后记|跋|编后记|编后语|致谢')
_INDEX_NAME = re.compile(r'索引|名词索引|人名索引|术语索引|主题索引|index')
_ANSWER = re.compile(r'答案|参考解答|解答与提示|提示与解答')
_EXERCISE = re.compile(r'习题|练习|课后作业|课时作业|家庭作业|作业题|思考题|复习题|自测题?|测验|测试题|检测题|单元检测'
                       r'|达标检测|达标训练|强化训练|专项训练|课时训练|选择题|填空题|判断题|解答题|计算题|应用题'
                       r'|证明题|简答题|论述题|材料题|实验题|阅读理解|完形填空|作图题|探究题|综合题')
_SUMMARY = re.compile(r'小结|总结|要点|复习|回顾|知识结构|知识网络|知识梳理|学习目标|阅读材料|拓展阅读|课外阅读')
_SECTION_REF = re.compile(r'(?:习题|练习|作业|答案)\s*((?:[A-Z]|\d+)(?:[.\-]\d+)+)')
# 可以容纳"重新从 1 编号"的一整套内容的节点（参考答案、附录、习题、索引）。
_CONTAINERS = {'answer', 'appendix', 'exercise', 'index'}


def _role(title):
    key = _name_key(title)
    if _TOC_NAME.fullmatch(key):
        return 'toc'
    if _INDEX_NAME.fullmatch(key):
        return 'index'
    if _ANSWER.search(key):
        return 'answer'
    if _BACK_NAME.fullmatch(key):
        return 'appendix'
    if _FRONT_NAME.fullmatch(key):
        return 'front'
    if _EXERCISE.search(key):
        return 'exercise'
    return None


def _named_kind(h, first, last):
    """无编号标题按名称决定挂法：top=挂根；chapter=挂到最近的章级节点下；ref=挂到对应小节下。

    first/last 是全书最高一级编号标题第一次、最后一次出现的位置：之前是书前部分，之后是书后部分。
    """
    ref = _SECTION_REF.search(_nfkc(h['title']))
    if ref:
        return 'ref', '.'.join(str(int(p)) if p.isdigit() else p for p in re.split(r'[.\-]', ref.group(1)))
    key, role = _name_key(h['title']), h['role']
    if role in ('front', 'toc') or _INTRO_NAME.fullmatch(key):
        return ('top' if first is None or h['index'] < first else None), None
    if role in ('appendix', 'index'):
        return ('top' if last is None or h['index'] > last else None), None
    if role == 'answer':
        return ('top' if last is None or h['index'] > last else 'chapter'), None
    if role == 'exercise' or _SUMMARY.search(key):
        return 'chapter', None
    return None, None


# ── 标题候选的取舍 ────────────────────────────────────────────────

_RUN_IN = re.compile(r'^(?:【例题?\s*[\d一二三四五六七八九十]+】|例题?\s*[\d一二三四五六七八九十]+(?=[\s:.()]|$)'
                     r'|(?:解|证|证明|注|分析|点评|提示|思路|答)(?=[\s:.]|$))')
_CAPTION = re.compile(r'^(?:图|表|figure|fig\.?|table)\s*[\d一二三四五六七八九十]+(?:\s*[-–—.:]\s*\d+)*(?:[\s:.]|$)', re.I)


def _promotable(line):
    """没有 # 但句式明确的章节标题行（第X章、Chapter N 等），整行短且不以标点收尾。"""
    text = _nfkc(line).strip()
    return (len(text) <= 30 and len(text.split()) <= 8 and _PROMOTE.match(text) is not None
            and not re.search(r'[,.;:!?。、…]$', text) and not _TOC_TAIL.search(text))


def _is_toc_name(line):
    return len(line.strip()) <= 8 and _TOC_NAME.fullmatch(_name_key(line)) is not None


_EXAM_NOTE = re.compile(r'本题|本大题|小题|每题|满分|共\s*\d+\s*分|\d+\s*分')


def _split_title(title):
    """带说明的编号标题（试卷大题常见）在第一个说明符号处拆开：前段作标题，后段回到正文。
    只拆超长标题或后段像题量/分值说明的，"第三章 微分中值定理：应用"这类正常标题不动。"""
    match = re.search(r'[（(:：，,。]|\s[—-]', title[2:25])
    if not match:
        return title, ''
    position = match.start() + 2
    head, rest = title[:position].strip(), title[position:].strip()
    if len(title) > 30 or _EXAM_NOTE.search(rest):
        return head, rest
    return title, ''


def _demote_reason(title, numbered):
    text = _nfkc(title).strip().strip('*')
    if _RUN_IN.match(text):
        return '例题/解答标签'
    if _CAPTION.match(text):
        return '图表题注'
    if _TOC_TAIL.search(text):
        return '目录条目'
    if len(text) > 60:
        return '过长'
    if not numbered and re.search(r'[。;,]$', text):
        return '句末标点'
    return None


# ── 页稿读取 ──────────────────────────────────────────────────────

def _page_files(folder):
    found = []
    for path in folder.glob('*.md'):
        match = re.search(r'-(\d+)$', path.stem)
        if match:
            found.append((int(match.group(1)), path))
    return sorted(found)


def _read(path):
    return path.read_text(encoding='utf-8').replace('\r\n', '\n').replace('\r', '\n')


def _load(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def _page(number, text):
    """切行；表格与多行公式内部的行受保护（不做噪声、页眉、标题判断）；去掉页码噪声行。"""
    spans = [m.span() for m in _TABLE_BLOCK.finditer(text)] + [m.span() for m in _MATH_BLOCK.finditer(text)]
    lines, protected, start = text.split('\n'), set(), 0
    for index, line in enumerate(lines):
        end = start + len(line)
        if any(a < max(end, start + 1) and start < b for a, b in spans):
            protected.add(index)
        start = end + 1
    keep = [i for i, line in enumerate(lines) if i in protected or not _is_noise(line)]
    return {'page': number, 'lines': lines, 'protected': protected, 'keep': keep}


def _content(page):
    """参与页眉页脚判断的行：非空、不受保护、不是纯插图行。"""
    return [i for i in page['keep'] if page['lines'][i].strip() and i not in page['protected']
            and not _IMAGE.fullmatch(page['lines'][i].strip())]


def _drop_running(pages):
    """相邻页或隔一页（单双页交替）首/末行相同的短行是页眉页脚，只保留最早一次。"""
    edges = []
    for page in pages:
        content, keys = _content(page), {}
        for index in ({content[0], content[-1]} if content else ()):
            key = _edge_key(page['lines'][index])
            if key:
                keys[index] = key
        edges.append(keys)
    for n in range(1, len(pages)):
        earlier = set(edges[n - 1].values()) | (set(edges[n - 2].values()) if n >= 2 else set())
        gone = {index for index, key in edges[n].items() if key in earlier}
        if gone:
            pages[n]['keep'] = [i for i in pages[n]['keep'] if i not in gone]


def _base_chars(pages):
    _drop_running(pages)
    return {page['page']: sum(_count(page['lines'][i]) for i in page['keep']) for page in pages}


def _figure_checker(paths, doc, layout):
    """插图能否落成资产：版面里有裁图文件，或至少有整页图可退回（与 ⑦ 的取图顺序一致）。"""
    crops = {str(f.get('key')): f.get('path') for p in layout.get('pages', []) or [] for f in p.get('figures', []) or []}
    folder = paths.state / '页图' / doc

    def ok(key):
        value = crops.get(key)
        if value:
            path = Path(str(value))
            if any(c.is_file() for c in ([path] if path.is_absolute() else [paths.root / path, paths.root.parent / path])):
                return True
        page = int(re.match(r'p(\d+)-f', key).group(1))
        return folder.is_dir() and any(folder.glob(f'{doc}-{page:03d}-page.*'))
    return ok


def _merged_table_map(tables_index):
    """④ 表格抽取的 merged_ids：被并掉的续表 id → 主表 id；主表 id → 主表页码。"""
    if not isinstance(tables_index, dict):
        return {}, {}
    merged_refs, table_pages = {}, {}
    for item in tables_index.get('tables') or []:
        if not isinstance(item, dict):
            continue
        tid = item.get('id')
        if isinstance(tid, str):
            page = item.get('page')
            if isinstance(page, int):
                table_pages[tid] = page
        for mid in item.get('merged_ids') or []:
            if isinstance(mid, str):
                merged_refs[mid] = tid
    return merged_refs, table_pages


def _rewrite(text, page, registry, figure_ok, assets, uses, warnings, merged_refs=None, table_pages=None):
    """把表格开标签与插图引用改写成 .why 2.0 的锚点形式；资产编号按登记表沿用。

    merged_refs 把④合并掉的续表 id 映射到主表 id：被并掉的续表不再单独发资产，
    锚点直接重定向到主表资产。
    """
    merged_refs = merged_refs or {}
    table_pages = table_pages or {}
    order = 0

    def table(match):
        nonlocal order
        order += 1
        ref = f'p{page}-t{order}'
        main_ref = merged_refs.get(ref, ref)
        main_page = table_pages.get(main_ref, page)
        tid = registry.assets.get(main_ref)
        if tid is None:
            tid = registry.asset(main_ref)
        assets.setdefault(tid, {'source_ref': main_ref, 'page': main_page})
        return re.sub(r'^<table\b', f'<table data-why="tab/{tid}"', match.group(0), count=1, flags=re.I)

    def figure(match):
        key = match.group('key')
        if not figure_ok(key):
            warnings.append(f'插图引用找不到裁图也没有整页图，保留原文：{key}（第 {page} 页）')
            return match.group(0)
        uses[key] += 1
        fid = registry.asset(key if uses[key] == 1 else f'{key}#{uses[key]}')
        assets[fid] = {'source_ref': key, 'page': int(match.group('page'))}
        return f'![{match.group("caption")}](why:fig/{fid})'

    return _FIGURE_REF.sub(figure, _TABLE_BLOCK.sub(table, text))


def _iter_page_lines(page):
    """切章读取边界：逐行给出（行文本, 是否受保护, 是否页边行），便于对正文损失做故障注入测试。"""
    content = _content(page)
    edges = {content[0], content[-1]} if content else set()
    for index in page['keep']:
        yield page['lines'][index], index in page['protected'], index in edges


# ── 事件流：('h', 标题) / ('l', 页, 行, 是否页边) ─────────────────

def _events(pages):
    events = []
    for page in pages:
        for line, protected, edge in _iter_page_lines(page):
            match = None if protected else _HEADING.fullmatch(line)
            if match:
                events.append(('h', {'md': len(match.group(1)), 'title': match.group(2).strip().strip('*').strip() or match.group(2).strip(),
                                     'page': page['page'], 'line': line, 'edge': edge}))
            elif not protected and (_promotable(line) or _is_toc_name(line)):
                events.append(('h', {'md': None, 'title': line.strip(), 'promoted': True,
                                     'page': page['page'], 'line': line, 'edge': edge}))
            else:
                events.append(('l', page['page'], line, edge))
    return events


def _as_line(h):
    return ('l', h['page'], h['line'], h['edge'])


def _mark_toc(events, counts):
    """书中自带的目录：'目录'标题之后紧跟的'幽灵标题'（后面页面还会再出现、自身几乎没有正文）降为目录正文；
    没有'目录'标题时，连续 3 个以上的幽灵标题也按目录处理，并补一个标题为'目录'的节点。"""
    heads = [j for j, e in enumerate(events) if e[0] == 'h']
    later, ghost, body = {}, {}, {}
    for j in reversed(heads):
        h = events[j][1]
        key = _title_key(h['title'])
        ghost[j] = bool(key) and later.get(key, 0) > h['page']
        if key:
            later[key] = max(later.get(key, 0), h['page'])
    for n, j in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(events)
        body[j] = sum(1 for e in events[j + 1:end] if e[0] == 'l' and e[2].strip() and not _IMAGE.fullmatch(e[2].strip()))
    demoted, inserts, n = set(), {}, 0
    while n < len(heads):
        h = events[heads[n]][1]
        n += 1
        if _TOC_NAME.fullmatch(_name_key(h['title'])):
            h['toc'] = True
            while n < len(heads) and ghost[heads[n]] and body[heads[n]] <= 1:
                demoted.add(heads[n])
                n += 1
    run = []
    for j in heads + [None]:
        if j is not None and j not in demoted and not events[j][1].get('toc') and ghost[j] and body[j] <= 1:
            run.append(j)
            continue
        if len(run) >= 3:
            first = events[run[0]][1]
            inserts[run[0]] = {'md': None, 'title': '目录', 'toc': True, 'synthetic': True,
                               'page': first['page'], 'line': '', 'edge': False}
            demoted.update(run)
        run = []
    counts['目录条目降为正文'] += len(demoted)
    out = []
    for j, e in enumerate(events):
        if j in inserts:
            out.append(('h', inserts[j]))
        out.append(_as_line(e[1]) if j in demoted else e)
    return out


def _refine(events, counts):
    """例题/解答标签、图表题注、过长或以句号收尾的无编号标题降为正文；带说明的长编号标题拆开。"""
    out = []
    for e in events:
        if e[0] != 'h' or e[1].get('toc'):
            out.append(e)
            continue
        h = e[1]
        numbered = _classify(h['title'])['number'] is not None
        title, rest = _split_title(h['title']) if numbered else (h['title'], '')
        reason = _demote_reason(title, numbered)
        if reason:
            counts[f'标题降为正文（{reason}）'] += 1
            out.append(_as_line(h))
            continue
        h['title'] = title
        out.append(e)
        if rest:
            out.append(('l', h['page'], rest, False))
    return out


def _drop_repeated_edges(events, counts, dropped):
    """页边的普通行或补认标题，若与更早页面上的某个标题相同，就是页眉里重复的章节名，删去。"""
    seen, out = {}, []
    for e in events:
        if e[0] == 'h':
            h, key = e[1], _title_key(e[1]['title'])
            if h.get('promoted') and h['edge'] and key and seen.get(key, h['page']) < h['page']:
                counts['页眉里的章节名'] += 1
                dropped[h['page']] += _count(h['line'])
                continue
            if key:
                seen.setdefault(key, h['page'])
        elif e[3]:
            key = _edge_key(e[2])
            if key and seen.get(key, e[1]) < e[1]:
                counts['页眉里的章节名'] += 1
                dropped[e[1]] += _count(e[2])
                continue
        out.append(e)
    return out


def _similar(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if (a in b or b in a) and min(len(a), len(b)) >= 2:
        return 0.9
    return difflib.SequenceMatcher(None, a, b).ratio()


def _apply_bookmarks(events, outline, warnings):
    """PDF 书签：在书签页 ±1 页内找最像的标题（找不到就找短行补认），按书签层级定骨架；对上不到一半则不用。"""
    entries = []
    for item in outline:
        try:
            level, title, page = int(item[0]), str(item[1]).strip(), int(item[2])
        except (TypeError, ValueError, IndexError):
            continue
        if title and page > 0:
            entries.append((level, title, page))
    if len(entries) < 2:
        return
    base = min(level for level, _, _ in entries)
    by_page = {}
    for j, e in enumerate(events):
        by_page.setdefault(e[1]['page'] if e[0] == 'h' else e[1], []).append(j)
    matches, missing, position = [], [], 0
    for level, title, page in entries:
        key, best = _title_key(title), None
        for p in (page - 1, page, page + 1):
            for j in by_page.get(p, ()):
                e = events[j]
                text = e[1]['title'] if e[0] == 'h' else e[2]
                if j < position or (e[0] == 'l' and (len(text.strip()) > 40 or not text.strip())):
                    continue
                score = _similar(key, _title_key(text))
                if score >= (0.75 if e[0] == 'h' else 0.85) and (best is None or score > best[0]):
                    best = (score, j)
        if best is None:
            missing.append(f'{title}（第 {page} 页）')
            continue
        matches.append((best[1], level - base + 1))
        position = best[1] + 1
    if len(matches) * 2 < len(entries):
        warnings.append(f'PDF 书签只对上 {len(matches)}/{len(entries)} 条，不采用书签')
        return
    if missing:
        warnings.append(f'PDF 书签未对上 {len(missing)} 条：' + '；'.join(missing[:5]) + ('……' if len(missing) > 5 else ''))
    for j, level in matches:
        e = events[j]
        if e[0] == 'l':
            events[j] = ('h', {'md': None, 'title': e[2].strip(), 'promoted': True, 'page': e[1], 'line': e[2], 'edge': e[3]})
        events[j][1]['bm'] = level


def _classify_all(events):
    heads = [e[1] for e in events if e[0] == 'h']
    for index, h in enumerate(heads):
        h.update(_classify(h['title']))
        h['role'] = 'toc' if h.get('toc') else _role(h['title'])
        h['index'] = index
    ranks = Counter(h['rank'] for h in heads if h['rank'] is not None)
    top = min((rank for rank, count in ranks.items() if count >= 2), default=None)
    first = next((h['index'] for h in heads if top is not None and h['rank'] == top), None)
    last = next((h['index'] for h in reversed(heads) if top is not None and h['rank'] == top), None)
    for h in heads:
        h['kind'], h['ref'] = (None, None) if h['number'] is not None else _named_kind(h, first, last)


# ── 建树 ──────────────────────────────────────────────────────────

def _new_node(h, rule, confidence):
    return {'title': h['title'], 'role': h.get('role'), 'split_rule': rule, 'confidence': confidence,
            'page': h.get('page'), 'synthetic': h.get('synthetic', False), 'style': h.get('style'),
            'line': h.get('line'), 'children': [], 'lines': []}


class _Builder:
    """用一个栈把标题依次挂到树上：栈底是根，往上是当前所在的各级章节。"""

    def __init__(self, root):
        self.stack = [self._frame(root, {'role': 'body'})]
        self.dropped = 0
        self.dropped_chars = Counter()
        self.gaps = []

    @staticmethod
    def _frame(node, h):
        return {'node': node, 'key': _title_key(node['title']), 'number': h.get('number'), 'rank': h.get('rank'),
                'md': h.get('md'), 'page': h.get('page'), 'bm': h.get('bm'), 'label': h.get('label'),
                'role': h.get('role'), 'seq': {}, 'last': {}}

    def _drop(self, h):
        """页眉里重复的标题：不建节点，字数记入守恒的"按页眉删除"一栏。"""
        self.dropped += 1
        self.dropped_chars[h['page']] += _count(h['line'])
        return None

    def place(self, h):
        """把一个标题挂到树上并返回新节点；判为页眉里重复的标题时返回 None。"""
        key = _title_key(h['title'])
        if h['edge'] and key and any(frame['key'] == key for frame in self.stack[1:]):
            return self._drop(h)
        if h.get('bm') is not None:
            index = len(self.stack) - 1
            while index > 0 and (self.stack[index]['bm'] is None or self.stack[index]['bm'] >= h['bm']):
                index -= 1
            return self._attach(index, h, 'bookmark', 0.95)
        if h['number'] is not None:
            return self._numbered(h)
        kind = h.get('kind')
        if kind == 'top':
            return self._attach(0, h, 'toc' if h.get('toc') else 'named', 0.8)
        if kind == 'chapter':
            index = next((i for i in range(len(self.stack) - 1, 0, -1)
                          if self.stack[i]['rank'] is not None and self.stack[i]['rank'] <= 20), None)
            if index is not None:
                return self._attach(index, h, 'named', 0.7)
        if kind == 'ref':
            index = next((i for i in range(len(self.stack) - 1, 0, -1) if self.stack[i]['label'] == h['ref']), None)
            if index is not None:
                return self._attach(index, h, 'named', 0.8)
        return self._plain(h, 'toc' if h.get('toc') else ('named' if kind else 'markdown'))

    def _numbered(self, h):
        """续号（上一个同样式兄弟的号 +1）挂到那一层；重号在页边就是页眉；否则从 1 起新序列或按跳号处理。"""
        style, number, crossed = h['style'], h['number'], False
        for index in range(len(self.stack) - 1, -1, -1):
            frame = self.stack[index]
            last = frame['seq'].get(style)
            if last is not None and number == last + 1:
                return self._attach(index, h, 'numbered', 0.9)
            if last is not None and number == last and not crossed:
                if h['edge'] and frame['last'][style]['title'] and _title_key(frame['last'][style]['title']) == _title_key(h['title']):
                    return self._drop(h)
                return self._attach(index, h, 'numbered', 0.5)
            crossed = crossed or frame['role'] in _CONTAINERS
        if number > 1:
            for index in range(len(self.stack) - 1, -1, -1):
                last = self.stack[index]['seq'].get(h['style'])
                if last is not None and last < number:
                    self.gaps.append(f"{self.stack[index]['last'][style]['title']} → {h['title']}")
                    return self._attach(index, h, 'numbered', 0.6)
        return self._attach(self._room(h), h, 'numbered', 0.85 if number == 1 else 0.6)

    def _room(self, h):
        """新序列挂在哪：不能挂进秩不高于它的编号节点；被挡时若中间有答案/附录类容器，就把容器提上来接住它。
        章、节这类结构性编号（秩 < 50）不挂在书名、前言、绪论这类无编号普通标题下面。"""
        rank, index = h['rank'], len(self.stack) - 1
        conflict = next((i for i in range(1, len(self.stack))
                         if self.stack[i]['rank'] is not None and self.stack[i]['rank'] >= rank), None)
        if conflict is not None:
            container = next((i for i in range(conflict + 1, len(self.stack))
                              if self.stack[i]['role'] in _CONTAINERS), None)
            if container is not None:
                self._hoist(container, conflict - 1)
                return conflict
            index = conflict - 1
        if rank < 50:
            while (index > 0 and self.stack[index]['number'] is None and self.stack[index]['bm'] is None
                   and self.stack[index]['role'] not in _CONTAINERS):
                index -= 1
        return index

    def _hoist(self, index, parent_index):
        """把栈上的容器节点连同子树提到 parent_index 之下；它必是原父节点的最后一个孩子，阅读顺序不变。"""
        node = self.stack[index]['node']
        siblings = self.stack[index - 1]['node']['children']
        if not siblings or siblings[-1] is not node:
            raise RuntimeError('切章内部错误：要提升的容器不是父节点的最后一个孩子')
        siblings.pop()
        self.stack[parent_index]['node']['children'].append(node)
        del self.stack[parent_index + 1:index]

    def _plain(self, h, rule):
        """无编号标题：同页 # 更多就是上一个标题的孩子；挂在编号标题之后且 # 不更少，也算其孩子；否则做兄弟。"""
        top = len(self.stack) - 1
        frame, md = self.stack[top], h.get('md')
        if top == 0 or frame['bm'] is not None:
            return self._attach(top, h, rule, 0.6)
        if md is not None and frame['md'] is not None and md > frame['md']:
            return self._attach(top, h, rule, 0.7)
        if frame['number'] is not None and (md is None or frame['md'] is None or md == frame['md']):
            return self._attach(top, h, rule, 0.5)
        index = top - 1
        while (index > 0 and md is not None and self.stack[index]['number'] is None
               and self.stack[index]['bm'] is None and self.stack[index]['md'] is not None
               and self.stack[index]['md'] > md and self.stack[index]['page'] == h['page']):
            index -= 1
        return self._attach(index, h, rule, 0.6)

    def _attach(self, index, h, rule, confidence):
        if h.get('promoted') and rule == 'numbered':
            rule, confidence = 'pattern', min(confidence, 0.7)
        del self.stack[index + 1:]
        parent = self.stack[index]
        node = _new_node(h, rule, confidence)
        parent['node']['children'].append(node)
        if h.get('number') is not None:
            parent['seq'][h['style']] = h['number']
            parent['last'][h['style']] = node
        self.stack.append(self._frame(node, h))
        return node


def _merge_tags(node):
    """空的【标签】叶子（如"【人物】……"紧跟"【适用主题】……"）是下一条目的标签行：并到下一个兄弟的正文开头。
    后面没有兄弟可并的照旧保留为节点。"""
    kept, pending = [], []
    for child in node['children']:
        if child['style'] == 'bracket' and not child['children'] and not any(t.strip() for _, t in child['lines']):
            pending.append(child)
            continue
        for tag in reversed(pending):
            child['lines'][:0] = [(tag['page'], tag['line'])] + tag['lines']
        pending = []
        kept.append(child)
    node['children'] = kept + pending
    for child in node['children']:
        _merge_tags(child)


def _split_by_page(root):
    """全书没有任何标题：多于一页时按页切成叶子（标题"第 N 页"），否则全部留在根节点。"""
    pages = sorted({page for page, text in root['lines'] if text.strip()})
    if len(pages) <= 1:
        return
    for page in pages:
        node = _new_node({'title': f'第 {page} 页', 'synthetic': True, 'page': page}, 'page', 0.3)
        node['lines'] = [(p, text) for p, text in root['lines'] if p == page]
        root['children'].append(node)
    root['lines'] = []


def _raw(lines):
    """把节点收到的行拼成 raw，并记录每一页内容在 raw 中的起点（码点偏移）。"""
    joined = '\n'.join(text for _, text in lines)
    lead = len(joined) - len(joined.lstrip())
    raw = joined.strip()
    page_map, offset = [], 0
    for page, text in lines:
        if text.strip() and (not page_map or page_map[-1][0] != page):
            page_map.append([page, min(max(0, offset - lead), len(raw))])
        offset += len(text) + 1
    if page_map:
        page_map[0][1] = 0
    return raw, page_map


def _preorder(root):
    nodes = []

    def walk(node, depth, parent):
        node['depth'], node['_parent'] = depth, parent
        node['path'] = [] if parent is None else parent['path'] + [node['title']]
        nodes.append(node)
        for child in node['children']:
            walk(child, depth + 1, node)
    walk(root, 0, None)
    return nodes


def _methods(index, ocr):
    """每页的识别来源：优先用 ③ 记录的实际方式，没有就按 ① 的文本层判定推断。"""
    methods = {int(p['page']): 'text-layer' if p.get('has_text_layer') else 'vision'
               for p in index.get('pages', []) or [] if 'page' in p}
    for page, method in (ocr.get('methods') or {}).items():
        methods[int(page)] = method
    return methods


def _origin(node, methods):
    pages = [page for page, _ in node['page_map']] or ([node['page']] if node['page'] is not None else [])
    found = {methods.get(page, 'unknown') for page in pages}
    if not found:
        return 'unknown'
    return found.pop() if len(found) == 1 else 'mixed'


def _tree_sha256(node):
    return tree_sha256(node['title'], node['raw_sha256'], [child['tree_sha256'] for child in node['children']])


# ── 编号登记表 ────────────────────────────────────────────────────

def _valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{16}', value) is not None


class _Registry:
    """编号登记表：raw 哈希相同 → 沿用；标题与页区间都相同 → 沿用；否则发新的随机编号。"""

    def __init__(self, path, force=False):
        self.path = path
        data = {} if force else _load(path)  # force：不沿用旧编号，全部重新发号
        self.old_root = data.get('root') if _valid_id(data.get('root')) else None
        self.old_nodes = [e for e in data.get('nodes', []) or [] if isinstance(e, dict) and _valid_id(e.get('id'))]
        self.old_assets = {k: v for k, v in (data.get('assets') or {}).items() if _valid_id(v)}
        self.reserved = ({e['id'] for e in self.old_nodes} | set(self.old_assets.values())
                         | ({self.old_root} if self.old_root else set()))
        self.used, self.assets = set(), {}

    def fresh(self):
        while True:
            value = secrets.token_hex(8)
            if value not in self.reserved and value not in self.used:
                self.used.add(value)
                return value

    def asset(self, ref):
        old = self.old_assets.get(ref)
        value = old if old and old not in self.used else self.fresh()
        self.used.add(value)
        self.assets[ref] = value
        return value

    def assign(self, nodes):
        """nodes 按前序排列、根在首位，每项需有 path / pages / raw_sha256；返回沿用统计。"""
        by_hash, by_title = {}, {}
        for entry in self.old_nodes:
            if entry.get('raw_sha256'):
                by_hash.setdefault(entry['raw_sha256'], []).append(entry)
            if entry.get('path'):
                by_title.setdefault((entry['path'][-1], tuple(entry.get('pages') or ())), []).append(entry)

        def pick(candidates, node):
            free = [e for e in candidates if e['id'] not in self.used]
            free.sort(key=lambda e: (e.get('path') != node['path'], e.get('pages') != node['pages']))
            return free[0]['id'] if free else None

        stats = {'reused_hash': 0, 'reused_title': 0, 'new': 0}
        root = nodes[0]
        root['id'] = self.old_root if self.old_root and self.old_root not in self.used else self.fresh()
        self.used.add(root['id'])
        for node in nodes[1:]:
            found = pick(by_hash.get(node['raw_sha256'], []), node) if node['raw_sha256'] else None
            if found:
                stats['reused_hash'] += 1
            else:
                found = pick(by_title.get((node['path'][-1], tuple(node['pages'] or ())), []), node)
                stats['reused_title' if found else 'new'] += 1
            node['id'] = found or self.fresh()
            self.used.add(node['id'])
        return stats

    def save(self, paths, nodes):
        paths.save(self.path, {
            'root': nodes[0]['id'],
            'nodes': [{'id': n['id'], 'path': n['path'], 'pages': n['pages'], 'raw_sha256': n['raw_sha256']}
                      for n in nodes[1:]],
            'assets': self.assets,
        })


# ── 守恒审计 ──────────────────────────────────────────────────────

def _audit(page_chars, base, nodes, dropped):
    """章节一侧 = 各节点 raw + 来自页稿的标题 + 按页眉删除的行（单列 dropped_chars）；两侧同一口径。
    缺口按页定位到丢字最多的 3 页。"""
    node_chars, per_page = 0, Counter(dropped)
    for node in nodes:
        if not node['synthetic']:
            count = _count_title(node['title'])
            node_chars += count
            if node.get('page') is not None:
                per_page[node['page']] += count
        text, marks = node['text'], node['page_map']
        for k, (page, start) in enumerate(marks):
            count = _count(text[start:marks[k + 1][1] if k + 1 < len(marks) else len(text)])
            node_chars += count
            per_page[page] += count
    counted = node_chars + sum(dropped.values())
    # 0 字的书（全空白/纯插图）没有分母，按恒等处理；不能返回 inf，JSON 会写出 Infinity。
    ratio = counted / page_chars if page_chars else 1.0
    if ratio > CONSERVATION_UPPER_THRESHOLD + 1e-12:
        raise ConservationError(
            f'章节正文字数超过页稿，疑似合成内容：页稿字数={page_chars}，'
            f'章节字数={counted}，多出字数={counted - page_chars}，ratio={ratio}'
        )
    if ratio + 1e-12 < CONSERVATION_THRESHOLD:
        losses = sorted(((page, base[page] - per_page.get(page, 0)) for page in base), key=lambda x: x[1], reverse=True)
        detail = ', '.join(f'第{page}页={count}' for page, count in losses[:3] if count > 0) or '无可定位页'
        raise ConservationError(
            f'章节守恒失败：页稿字数={page_chars}，章节字数={counted}，'
            f'缺口字数={page_chars - counted}，丢字数最多的3页={detail}'
        )
    return node_chars, ratio


# ── 按页对账 ──────────────────────────────────────────────────────

def _match_chars(T, O):
    """按字符多重集对账：返回 (matched, T 中没能在 O 里找到的字符下标)。

    这里只查"这页的字有没有被搬进章节"，不查顺序——顺序由 _coverage_audit 的
    "第 N 页内容出现在第 M 页之后"单独检查。旧的按序贪心会被合法的重排误判：
    比如页眉里的【人物】行被并到下一节正文开头后，节点标题排在正文前，整页覆盖率会掉到 0.04。"""
    from collections import Counter
    counts = Counter(O)
    unmatched = []
    for i, ch in enumerate(T):
        if counts[ch]:
            counts[ch] -= 1
        else:
            unmatched.append(i)
    return len(T) - len(unmatched), unmatched


def _missing_snippet(T, unmatched):
    """未被匹配到的连续字符片段，最多 40 字；没有未匹配字符返回空串。"""
    if not unmatched:
        return ''
    runs = []
    start = prev = unmatched[0]
    for idx in unmatched[1:]:
        if idx == prev + 1:
            prev = idx
        else:
            runs.append((start, prev))
            start = prev = idx
    runs.append((start, prev))
    start, end = max(runs, key=lambda r: r[1] - r[0] + 1)
    return T[start:end + 1][:40]


def _page_body(nodes):
    """{page: 该页最终正文归一字符串}：口径与 _audit 的 per_page 一致（标题按节点页，正文按 page_map）。"""
    per_page = {}
    for node in nodes:
        if not node['synthetic'] and node.get('page') is not None:
            per_page.setdefault(node['page'], []).append(_strip_title(node['title']))
        text, marks = node['text'], node['page_map']
        for k, (page, start) in enumerate(marks):
            frag = text[start:marks[k + 1][1] if k + 1 < len(marks) else len(text)]
            per_page.setdefault(page, []).append(_strip(frag))
    return {page: ''.join(parts) for page, parts in per_page.items()}


def _coverage_audit(page_texts, nodes):
    """按页对账 + 顺序检查；返回 (per_page, coverage_min, min_page, warnings)。"""
    node_texts = _page_body(nodes)
    per_page, warnings = {}, []
    coverage_min, min_page = 1.0, None
    for page, T in page_texts.items():
        O = node_texts.get(page, '')
        matched, unmatched = _match_chars(T, O)
        coverage = matched / len(T) if T else 1.0
        per_page[str(page)] = round(coverage, 2)
        if min_page is None or coverage < coverage_min:
            coverage_min, min_page = coverage, page
        if coverage < 0.90:
            missing = len(T) - matched
            warnings.append(f'第 {page} 页覆盖率 {coverage:.2f}（缺 {missing} 字），疑似丢失：{_missing_snippet(T, unmatched)}')
    prev = None
    for node in nodes:
        marks = node['page_map']
        if not marks:
            continue
        start = marks[0][0]
        if prev is not None and start < prev:
            warnings.append(f'第 {start} 页内容出现在第 {prev} 页之后（疑似乱序）')
        prev = start
    return per_page, round(coverage_min, 2), min_page, warnings


# ── 入口 ──────────────────────────────────────────────────────────

def run(doc, paths=None, force=False):
    """从 页稿 建树并写 章节-<书>.json。

    force=True 时忽略上一次切章的产物：不沿用旧编号登记表（编号-<书>.json 也是它的产物），
    整棵树重新发号、章节文件直接重建；force=False 与旧行为完全一致。"""
    paths = paths or Paths()
    files = _page_files(paths.state / '页稿' / doc)
    if not files:
        raise FileNotFoundError(f'没有页稿：{doc}')
    index = _load(paths.state / '页图' / f'{doc}.json')
    layout = _load(paths.state / '版面' / f'{doc}.json')
    ocr = _load(paths.state / f'ocr-{doc}.json')
    registry = _Registry(paths.state / f'编号-{doc}.json', force=force)
    warnings, counts = [], Counter()

    # 审计基数从原始文件独立读取，不使用切章预处理的结果，防止漏页被分母掩盖。
    raw_pages = [_page(page, _read(path)) for page, path in files]
    base = _base_chars(raw_pages)
    page_chars = sum(base.values())
    # 按页对账用：与基数同一份页稿（已去掉页眉页脚、噪声行），归一后逐字符比对。
    page_texts = {p['page']: _strip('\n'.join(p['lines'][i] for i in p['keep'])) for p in raw_pages}

    tables_index = _load(paths.state / f'表格-{doc}.json')
    merged_refs, table_pages = _merged_table_map(tables_index)
    assets, uses = {}, Counter()
    figure_ok = _figure_checker(paths, doc, layout)
    pages = [_page(page, _rewrite(_read(path), page, registry, figure_ok, assets, uses, warnings,
                                   merged_refs, table_pages))
             for page, path in files]
    _drop_running(pages)
    dropped = Counter()
    events = _drop_repeated_edges(_refine(_mark_toc(_events(pages), counts), counts), counts, dropped)
    _apply_bookmarks(events, index.get('outline') or [], warnings)
    _classify_all(events)

    root = _new_node({'title': doc, 'synthetic': True, 'role': 'body'}, 'root', 1.0)
    builder, current = _Builder(root), root
    for event in events:
        if event[0] == 'h':
            node = builder.place(event[1])
            current = node if node is not None else current
        else:
            current['lines'].append((event[1], event[2]))
    _merge_tags(root)
    if not root['children']:
        _split_by_page(root)

    nodes = _preorder(root)
    methods = _methods(index, ocr)
    for node in nodes:
        node['text'], node['page_map'] = _raw(node.pop('lines'))
        node['raw_sha256'] = hashlib.sha256(node['text'].encode('utf-8')).hexdigest() if node['text'] else None
        node['origin'] = _origin(node, methods)
        if node['_parent'] is not None:
            parent_role = node['_parent']['role']
            node['role'] = parent_role if parent_role in ('answer', 'toc', 'index') else (node['role'] or parent_role)
    for node in reversed(nodes):
        found = [page for page, _ in node['page_map']] + ([node['page']] if node['page'] is not None else [])
        for child in node['children']:
            found += child['pages'] or []
        node['pages'] = [min(found), max(found)] if found else None
    stats = registry.assign(nodes)
    for node in reversed(nodes):
        node['tree_sha256'] = _tree_sha256(node)
    dropped.update(builder.dropped_chars)
    node_chars, ratio = _audit(page_chars, base, nodes, dropped)
    per_page, coverage_min, min_page, coverage_warnings = _coverage_audit(page_texts, nodes)
    # 阈值内的差额也要说清楚：0.995 的 ratio 在旧实现里是静默通过的。
    counted = node_chars + sum(dropped.values())
    if page_chars != counted and not coverage_warnings:
        coverage_warnings.append(
            f'守恒差额 {page_chars - counted} 字：页稿 {page_chars}，章节 {counted}（ratio={ratio:.4f}）')

    figures, tables = [], []
    seen_tables = set()
    for node in nodes:
        for match in _FIGURE_FINAL.finditer(node['text']):
            info = assets.get(match.group('id'))
            if info:
                figures.append({'id': match.group('id'), 'source_ref': info['source_ref'], 'node': node['id'],
                                'anchor': match.start(), 'caption': match.group('caption'), 'page': info['page']})
        for match in _TABLE_FINAL.finditer(node['text']):
            info = assets.get(match.group('id'))
            if info and match.group('id') not in seen_tables:
                seen_tables.add(match.group('id'))
                tables.append({'id': match.group('id'), 'source_ref': info['source_ref'], 'node': node['id'],
                               'anchor': match.start(), 'page': info['page']})
    warnings += [f'{reason}：{count} 处' for reason, count in sorted(counts.items()) if count]
    if builder.dropped:
        warnings.append(f'页边重复的章节标题按页眉删除：{builder.dropped} 处')
    warnings += [f'编号跳跃：{gap}' for gap in builder.gaps[:20]]

    result = {
        'doc': doc, 'pages': len(files), 'root': root['id'],
        'nodes': [{
            'id': n['id'], 'parent': n['_parent']['id'] if n['_parent'] else None, 'depth': n['depth'],
            'title': n['title'], 'role': n['role'], 'children': [c['id'] for c in n['children']],
            'pages': n['pages'], 'text': n['text'], 'page_map': n['page_map'], 'origin': n['origin'],
            'confidence': round(n['confidence'], 2), 'split_rule': n['split_rule'],
            'raw_sha256': n['raw_sha256'], 'chars': len(n['text']), 'tree_sha256': n['tree_sha256'],
        } for n in nodes],
        'assets': {'figures': figures, 'tables': tables},
        'conservation': {'page_chars': page_chars, 'node_chars': node_chars,
                         'dropped_chars': sum(dropped.values()), 'ratio': ratio,
                         'per_page': per_page, 'coverage_min': coverage_min,
                         'warnings': coverage_warnings},
        'registry': stats,
        'warnings': warnings,
    }
    paths.save(paths.state / f'章节-{doc}.json', result)
    registry.save(paths, nodes)

    depth = max(n['depth'] for n in nodes)
    print(f'⑤切章: {len(nodes)} 个节点，最深 {depth} 层；编号沿用 {stats["reused_hash"] + stats["reused_title"]}，'
          f'新编号 {stats["new"]}；守恒 ratio={ratio:.3f}')
    if min_page is not None:
        print(f'  覆盖最低 {coverage_min:.2f}（第 {min_page} 页）')
    else:
        print('  覆盖最低 1.00')
    for node in result['nodes'][:300]:
        first, end = node['pages'] or ('?', '?')
        print(f"  {'  ' * node['depth']}{node['title']}  p{first}-{end}  {node['chars']}字  "
              f"{node['role']}/{node['split_rule']}/{node['confidence']}")
    if len(nodes) > 300:
        print(f'  ……其余 {len(nodes) - 300} 个节点见 章节-{doc}.json')
    for warning in warnings[:20]:
        print(f'  警告：{warning}')
    for warning in coverage_warnings[:20]:
        print(f'  警告：{warning}')
    return result
