"""③ 文本页按几何重建段落，扫描页调用视觉模型。

扫描页按页并发（页数多时不必一页一页等网络）：并发数取 [全局] 并发、下限 1；
页与页互不影响，单页失败只记进 failures；页稿和汇总一律按页号落盘，
顺序与串行版完全一致（缓存和幂等靠这个）。
"""
from pathlib import Path
from contextlib import nullcontext
import json
import re
import statistics
import threading
import time
import pymupdf
from why_harness.paths import Paths
from why_harness import progress
from why_harness.llm.client import Client
from why_harness.promptlib import load_prompt
from why_harness.tunable import tunable_pool

# MuPDF 文档对象不能跨线程并发访问：文字层提取走这把锁（视觉页的网络请求不占它）。
_PDF_LOCK = threading.Lock()
# 进度输出加锁：整行一次写出，避免并发时两行从中间交错。
_PRINT_LOCK = threading.Lock()


def prompt(paths, page, figures, tables):
    text = load_prompt('ocr.md')
    values = {'page_no': page, 'figure_count': len(figures), 'figure_keys': ', '.join(f['key'] for f in figures) or '无', 'table_count': len(tables), 'table_keys': ', '.join(t['key'] for t in tables) or '无', 'figure_example': figures[0]['key'] if figures else 'p1-f1'}
    for key, value in values.items():
        text = text.replace('{{' + key + '}}', str(value))
    return text


def text_page(pdf, number, layout):
    page = pdf[number - 1]
    rows = []
    sizes = []
    for block in page.get_text('dict')['blocks']:
        spans = [s for line in block.get('lines', []) for s in line['spans']]
        value = ''.join(s['text'] for s in spans).strip()
        if not value:
            continue
        sizes.extend(s['size'] for s in spans if s['text'].strip())
        rows.append({'text': value, 'bbox': list(block['bbox']), 'size': max(s['size'] for s in spans)})
    median = statistics.median(sizes) if sizes else 10
    text_width = max((r['bbox'][2] for r in rows), default=page.rect.width) - min((r['bbox'][0] for r in rows), default=0)
    any_visual = any(r['size'] >= median * 1.35 and len(r['text']) <= 60 for r in rows)

    def is_heading(row):
        """字号显著更大、整行短、不占满排版宽、且不是句子结尾的，才算标题。"""
        text = row['text'].strip()
        if not text or len(text) > 40:
            return False
        if row['bbox'][2] - row['bbox'][0] > text_width * 0.55:
            return False
        if text[-1] in '，。、；：,.;:!?！？':
            return False
        if text.startswith(('（', '(', '[')):
            return False
        return row['size'] >= median * 1.35

    for row in rows:
        text = row['text']
        short = len(text) <= 60 and row['bbox'][2] - row['bbox'][0] <= text_width * .70
        tag = re.fullmatch(r'【[^】]{1,12}】(.*)', text)
        content = not any_visual and tag and len(tag.group(1)) <= 30 and short and not text.endswith(('，', '。', '、', '；', ';'))
        row['prefix'] = '## ' if content or is_heading(row) else ''
        if content:
            with _PRINT_LOCK:
                print(f'③页{number}: 标题依据=内容型 {text}')
    merged = []
    for row in rows:
        if merged:
            previous = merged[-1]
            gap = row['bbox'][1] - previous['bbox'][3]
            # 不跨标签和标题合并；普通连续排版行拼回段落。
            if not row['prefix'] and not previous['prefix'] and not row['text'].startswith(('【', '《', '——')) and abs(row['bbox'][0] - previous['bbox'][0]) <= 35 and -2 <= gap <= median * .5:
                previous['text'] += row['text']
                previous['bbox'][3] = row['bbox'][3]
                continue
        merged.append(dict(row))
    entries = [(r['bbox'][1], r['prefix'] + r['text']) for r in merged]
    scale = layout.get('height', page.rect.height) / page.rect.height
    for figure in layout.get('figures', []):
        y = figure['bbox'][1] / scale
        entries.append((y, f"![插图](figure:{figure['key']})"))
    return '\n\n'.join(value for _, value in sorted(entries, key=lambda pair: pair[0]))


def usable(text):
    return len(text) >= 30 and '```' not in text and not re.match(r'^\s*(PAGE=|STATUS=)', text)


def _source_context(source):
    """Markdown/文本输入没有 PDF：用一个空上下文占位，避免 pymupdf 打开非 PDF 文件。"""
    from why_harness import is_text_source
    if is_text_source(source):
        return nullcontext(None)
    return pymupdf.open(source)


def run(pdf_path, doc, paths=None, config=None, force=False):
    paths = paths or Paths()
    config = config or {}
    started = time.perf_counter()
    index = json.loads((paths.state / '页图' / f'{doc}.json').read_text(encoding='utf-8'))
    layout = json.loads((paths.state / '版面' / f'{doc}.json').read_text(encoding='utf-8'))
    folder = paths.state / '页稿' / doc
    folder.mkdir(parents=True, exist_ok=True)
    try:
        concurrency = max(1, int(config.get('全局.并发', 4)))
    except (TypeError, ValueError):
        concurrency = 4          # 并发写错不能把整阶段拖死：回退默认值
    client = Client(config)
    # 自动补跑：首轮失败（多为服务端 503 / 返回空）的页按轮重试，只重试失败页，不重做已成功的页。
    try:
        retry_rounds = max(0, min(10, int(config.get('全局.自动补跑轮数', 2))))
    except (TypeError, ValueError):
        retry_rounds = 2
    # 每页实际的识别方式（text-layer / vision）交给 ⑤ 填节点的 origin；跳过的页沿用上次记录。
    try:
        previous = json.loads((paths.state / f'ocr-{doc}.json').read_text(encoding='utf-8')).get('methods') or {}
    except (OSError, ValueError, AttributeError):
        previous = {}
    methods = {}
    records = {}
    with _source_context(pdf_path) as pdf:
        def worker(meta, lp, method, target):
            """单页一个任务：页与页之间完全独立，失败只记进自己这条记录，不打断其它页。"""
            page = meta['page']
            try:
                if method == 'markdown':
                    # md/文本输入：正文已经由 ① 切好，直接落页稿，不过质量筛（短段也合法）。
                    text = meta.get('text') or ''
                    if not text.strip():
                        raise ValueError('Markdown 段落为空')
                    target.write_text(text.strip() + '\n', encoding='utf-8')
                    return {'page': page, 'method': method, 'ok': True, 'skipped': False}
                if method == 'text-layer':
                    # MuPDF 文档对象不能跨线程并发访问：文字层提取在 _PDF_LOCK 里串行（本地毫秒级），
                    # 视觉页的网络请求不占这把锁，所以并发度不受影响。
                    with _PDF_LOCK:
                        text = text_page(pdf, page, lp)
                else:
                    text = client.chat(prompt(paths, page, lp['figures'], lp['tables']), paths.root / meta['page_png'])
                if method != 'markdown' and not usable(text):
                    raise ValueError('输出未通过正文质量筛选')
                # 版面已切好的插图如果没被正文引用（模型漏写占位符），补到页尾，避免整张图丢失。
                missing = [fig for fig in lp.get('figures', []) if fig['key'] not in text]
                if missing:
                    text = text.rstrip() + '\n\n' + '\n\n'.join(f"![插图](figure:{fig['key']})" for fig in missing)
                    with _PRINT_LOCK:      # 进度行整行输出，避免多线程 print 从中间切开
                        print(f'③页{page}: 补引用 {len(missing)} 张版面插图')
                target.write_text(text.strip() + '\n', encoding='utf-8')
                return {'page': page, 'method': method, 'ok': True, 'skipped': False}
            except Exception as exc:
                return {'page': page, 'method': method, 'ok': False, 'skipped': False, 'error': str(exc)}

        pending = []
        for meta in index['pages']:
            target = folder / f'{doc}-{meta["page"]:03d}.md'
            text_layer = meta['has_text_layer'] and config.get('阶段.识别.文本层', '自动') != '总是识别'
            method = 'markdown' if meta.get('text') is not None else ('text-layer' if text_layer else 'vision')
            if target.exists() and not force and usable(target.read_text(encoding='utf-8')):
                records[meta['page']] = {'page': meta['page'], 'ok': True, 'skipped': True,
                                         'method': previous.get(str(meta['page'])) or method}
                continue
            lp = {} if method == 'markdown' else next(p for p in layout['pages'] if p['page'] == meta['page'])
            pending.append((meta, lp, method, target))
        if pending:
            total = len(index['pages'])
            progress.report('s3_ocr', len(records), total, f'待识别 {len(pending)} 页')
            with tunable_pool(concurrency, paths.state / '并发.txt') as pool:
                for record in pool.map(lambda task: worker(*task), pending):
                    records[record['page']] = record
                    progress.report('s3_ocr', len(records), total, f"第{record['page']}页")
                # 自动补跑：只把失败的页放回池子，最多 retry_rounds 轮。
                for round_no in range(1, retry_rounds + 1):
                    failed_pages = {page for page, rec in records.items() if not rec['ok']}
                    if not failed_pages:
                        break
                    retry = [task for task in pending if task[0]['page'] in failed_pages]
                    if not retry:
                        break
                    delay = min(8, 2 ** (round_no - 1))
                    print(f'③OCR: 自动补跑第 {round_no}/{retry_rounds} 轮（{len(retry)} 页，等 {delay}s）' +
                          ''.join(f' 第{task[0]["page"]}页' for task in retry), flush=True)
                    progress.report('s3_ocr', len(records), total, f'补跑第{round_no}轮 {len(retry)}页')
                    time.sleep(delay)
                    for record in pool.map(lambda task: worker(*task), retry):
                        records[record['page']] = record
                        progress.report('s3_ocr', len(records), total, f"第{record['page']}页")

    # 无论完成先后，一律按页号汇总：methods / failures 的顺序与串行版完全一致（缓存和幂等靠这个）。
    ok = failed = 0
    failures = []
    for page in sorted(records):
        record = records[page]
        methods[str(page)] = record['method']
        if record['ok']:
            ok += 1
        else:
            failed += 1
            failures.append({'page': page, 'error': record['error']})
    result = {'doc': doc, 'pages': len(index['pages']), 'ok': ok, 'failed': failed, 'failures': failures, 'elapsed': round(time.perf_counter() - started, 2), 'calls': client.calls, 'prompt_tokens': client.prompt_tokens, 'completion_tokens': client.completion_tokens, 'methods': methods}
    paths.save(paths.state / f'ocr-{doc}.json', result)
    print(f'③OCR: {ok}/{len(index["pages"])}，失败 {failed}，模型调用 {client.calls} 次')
    if failed:
        pages_text = '、'.join(str(item['page']) for item in failures[:20])
        raise RuntimeError(
            f'OCR {failed} 页失败（已完成 {ok} 页，自动补跑 {retry_rounds} 轮）：{failures}；'
            f'失败的页：第 {pages_text} 页。再点「补跑」只会重试这些页，已完成的不重做。')
    return result
