"""⑥ 摘要：自下而上为每个节点生成 summary / brief / keywords，按内容哈希缓存。

叶子依据自身 raw；中间节点（含根）依据导语 + 各子节点的 summary，所以同一层的节点并发、层与层串行。
缓存按内容寻址而不是按编号：叶子的键是 tree_sha256，中间节点的键再叠加它实际用到的导语与子摘要——
子树没变就命中（哪怕编号变了），子摘要重做了上层也跟着重做。写进 .why 的 src 一律是生成时的 tree_sha256。
键里还挂着影响输出的生成参数：提示词模板内容、模型名、摘要字数上限、一句话字数上限——
换了提示词/模型/上限，哪怕书一字未改也必须重做，不能 0 次调用直接复用旧结果。
"""
import hashlib
import json
import math
from pathlib import Path
import re
import time

from why_harness import progress
from why_harness.paths import Paths
from why_harness.llm.client import Client
from why_harness.promptlib import load_prompt
from why_harness.tunable import tunable_pool

MIN_BODY = 20                    # 正文不足 20 字不做摘要：原文本身就比摘要短，阅读器直接用 raw
SUMMARY_ELASTIC_NUM = 13         # 摘要上限的 1.3 倍弹性，写成整数比避免 float 误差：ceil(limit*13/10)
SUMMARY_ELASTIC_DEN = 10
SKIP_ROLES = ('toc', 'index')    # 书里自带的目录、索引，摘要没有信息量


def _parse(raw):
    text = str(raw).strip()
    text = re.sub(r'^\s*```(?:json)?\s*', '', text, flags=re.I)
    text = re.sub(r'\s*```\s*$', '', text)
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end < start:
        raise ValueError('未找到 JSON 对象')
    value = json.loads(text[start:end + 1])
    if not isinstance(value, dict) or not isinstance(value.get('summary'), str) or not isinstance(value.get('keywords'), list):
        raise ValueError('JSON 必须包含字符串 summary 和数组 keywords')
    return value


def _sentence(text, limit):
    """首句（到第一个句末标点为止），截到 limit 字；用作回退摘要和回退的一句话。"""
    text = re.sub(r'\s+', ' ', text).strip()
    match = re.search(r'[。？！?!]', text)
    return (text[:match.end()] if match else text)[:limit]


def _body(node, by_id, results):
    """叶子用 raw；中间节点用"导语 + 各子节点摘要"（子节点没有摘要就用其 raw 首句）。"""
    if not node['children']:
        return node['text']
    parts = [f'【导语】\n{node["text"].strip()}'] if node['text'].strip() else []
    lines = []
    for cid in node['children']:
        child = by_id[cid]
        text = ((results.get(cid) or {}).get('summary') or {}).get('text') or _sentence(child['text'], 120)
        lines.append(f"- {child['title']}：{text}" if text else f"- {child['title']}")
    parts.append('【下属各部分摘要】\n' + '\n'.join(lines))
    return '\n\n'.join(parts)


def _fallback(node, by_id):
    """模型失败时的回退摘要：叶子取 raw 首句；中间节点取导语首句，没有导语就列出下属标题。"""
    if node['text'].strip():
        return _sentence(node['text'], 120)
    return ('包含：' + '；'.join(by_id[c]['title'] for c in node['children']))[:120] if node['children'] else ''


def _cache_key(node, body, template_sha256, model, limit, brief_limit):
    """内容哈希叠加生成参数。

    内容部分：叶子是 tree_sha256；中间节点再叠加它实际用到的导语与子摘要。
    生成参数：提示词模板内容的 sha256、模型名、摘要字数上限、一句话字数上限。
    模板/模型/上限变一个，键就变，旧缓存不再命中；同一次运行内它们恒定，
    所以"同一工作区重跑→0 次调用"不受影响。
    """
    content = node['tree_sha256'] if not node['children'] else node['tree_sha256'] + '\n' + body
    params = json.dumps([template_sha256, model, limit, brief_limit], ensure_ascii=False)
    return hashlib.sha256((content + '\n' + params).encode('utf-8')).hexdigest()


def _load_cache(path):
    try:
        cache = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return cache if isinstance(cache, dict) else {}


def run(doc, paths=None, force=False, config=None):
    paths = paths or Paths(); config = config or {}
    chapter_path = paths.state / f'章节-{doc}.json'
    if not chapter_path.exists():
        return {'doc': doc, 'error': f'缺少章节文件：{chapter_path}'}
    nodes = json.loads(chapter_path.read_text(encoding='utf-8')).get('nodes', [])
    by_id = {node['id']: node for node in nodes}
    template = load_prompt('summary.md')
    limit = int(config.get('阶段.摘要.摘要最大字数', 150))
    brief_limit = int(config.get('阶段.摘要.一句话最大字数', 30))
    body_limit = int(config.get('阶段.摘要.正文上限', 6000))
    try: concurrency = max(1, int(config.get('全局.并发', 4)))
    except (TypeError, ValueError): concurrency = 4
    client = Client(config)
    try: model = client.config.get('阶段.摘要.模型')
    except AttributeError: model = config.get('阶段.摘要.模型')
    template_sha256 = hashlib.sha256(template.encode('utf-8')).hexdigest()
    cache_dir = paths.state / '摘要'; cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f'{doc}.json'
    cache = _load_cache(cache_path)

    def summarize(node, body):
        prompt = (template.replace('{{limit}}', str(limit)).replace('{{brief_limit}}', str(brief_limit))
                  .replace('{{title}}', node['title']).replace('{{body}}', body[:body_limit]))
        parsed, error = None, None
        for _ in range(2):
            try:
                parsed = _parse(client.chat(prompt, stage='摘要'))
                break
            except Exception as exc:
                error = exc
        if parsed is None:
            text = _fallback(node, by_id)
            return {'status': 'unparsed', 'error': str(error), 'model': None, 'keywords': [],
                    'summary': {'text': text, 'by': 'heuristic'} if text else None,
                    'brief': {'text': _sentence(text, brief_limit), 'by': 'heuristic'} if text else None}
        # 上限 = ceil(limit * 1.3)（150 → 195）：模型略超字数不硬砍，超过弹性上限才截。
        summary_limit = -(-limit * SUMMARY_ELASTIC_NUM // SUMMARY_ELASTIC_DEN)
        summary = parsed['summary'].strip()
        if len(summary) > summary_limit:
            summary = summary[:summary_limit]
        if not summary:
            return {'status': 'empty', 'model': model, 'keywords': [], 'summary': None, 'brief': None}
        brief = parsed.get('brief').strip() if isinstance(parsed.get('brief'), str) else ''
        # 模型给的一句话略超长度可以接受；没给或明显超长就从摘要首句截取。
        brief_record = ({'text': brief, 'by': 'model'} if brief and len(brief) <= brief_limit * 1.5
                        else {'text': _sentence(summary, brief_limit), 'by': 'heuristic'})
        keywords = [k for k in parsed['keywords'] if isinstance(k, str) and 2 <= len(k) <= 12 and k in node['text']]
        return {'status': 'ok' if keywords else 'keywords-dropped', 'model': model, 'keywords': keywords,
                'summary': {'text': summary, 'by': 'model'}, 'brief': brief_record}

    height = {}
    for node in reversed(nodes):  # nodes 是前序：倒过来走，孩子一定先于父亲
        height[node['id']] = 1 + max((height[c] for c in node['children']), default=-1)
    started, results, hits = time.time(), {}, 0
    for level in range(max(height.values(), default=-1) + 1):
        batch = []
        for node in nodes:
            if height[node['id']] != level:
                continue
            body = _body(node, by_id, results)
            if node['role'] in SKIP_ROLES or len(re.sub(r'\s+', '', body)) < MIN_BODY:
                results[node['id']] = {'status': 'skipped', 'model': None, 'keywords': [], 'summary': None, 'brief': None}
                continue
            key = _cache_key(node, body, template_sha256, model, limit, brief_limit)
            hit = cache.get(key)
            if not force and isinstance(hit, dict) and hit.get('status') in ('ok', 'keywords-dropped', 'empty'):
                results[node['id']] = dict(hit, cached=True)
                hits += 1
                continue
            batch.append((node, body, key))
        progress.report('s6_summary', len(results), len(nodes), f'第 {level + 1} 层')
        if batch and not client.key:
            raise RuntimeError(client.key_error_message())
        with tunable_pool(concurrency, paths.state / '并发.txt') as pool:
            for (node, _, key), record in zip(batch, pool.map(lambda item: summarize(item[0], item[1]), batch)):
                results[node['id']] = record
                cache[key] = record
                progress.report('s6_summary', len(results), len(nodes), node['title'][:14])
    paths.save(cache_path, cache)

    out = {}
    for node in nodes:
        record = results[node['id']]
        entry = {'src': node['tree_sha256'], 'status': record['status'], 'cached': bool(record.get('cached')),
                 'keywords': record.get('keywords') or []}
        for kind in ('summary', 'brief'):
            item = record.get(kind)
            entry[kind] = ({'text': item['text'], 'by': item['by'], 'model': record.get('model') if item['by'] == 'model' else None}
                           if item and item.get('text') else None)
        out[node['id']] = entry
    stats = {
        'nodes': len(nodes),
        'model_summaries': sum(1 for e in out.values() if (e['summary'] or {}).get('by') == 'model'),
        'cached': hits,
        'heuristic': sum(1 for e in out.values() if (e['summary'] or {}).get('by') == 'heuristic'),
        'skipped': sum(1 for e in out.values() if e['status'] in ('skipped', 'empty')),
        'failed': [nid for nid, e in out.items() if e['status'] == 'unparsed'],
    }
    payload = {'doc': doc, 'nodes': out, 'stats': stats, 'elapsed_sec': round(time.time() - started, 3),
               'tokens': {'calls': client.calls, 'prompt_tokens': client.prompt_tokens,
                          'completion_tokens': client.completion_tokens}, 'model': model,
               'prompt_sha256': template_sha256}
    paths.save(paths.state / f'摘要-{doc}.json', payload)
    print(f"⑥摘要: {len(nodes)} 个节点，模型摘要 {stats['model_summaries']}（缓存命中 {hits}），"
          f"回退 {stats['heuristic']}，跳过 {stats['skipped']}，调用 {client.calls} 次")
    return payload
