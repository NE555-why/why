"""② 版面检测、阅读序裁图和可诊断失败记录。"""
from pathlib import Path
import importlib.util,json,time
from PIL import Image
from why_harness import progress
from why_harness.paths import Paths

def _engine():
    """返回版面引擎模块；拿不到就抛 models.ModelNotInstalled。

    版面是默认必需阶段：引擎不可用时必须让流程停下（CLI 据此映射退出码 3），
    绝不能静默降级成"这本书没有插图也算成功"。"""
    reasons=[]
    try:
        from why_harness.media import layout as layout_engine
        return layout_engine
    except Exception as exc: reasons.append(f'why_harness.media.layout 导入失败: {type(exc).__name__}: {exc}')
    path=Path(__file__).resolve().parents[2]/'lab'/'layout_engine.py'
    if path.exists():
        spec=importlib.util.spec_from_file_location('why_layout_engine',path); module=importlib.util.module_from_spec(spec)
        try: spec.loader.exec_module(module); return module
        except Exception as exc: reasons.append(f'lab/layout_engine.py 加载失败: {type(exc).__name__}: {exc}')
    else: reasons.append('lab/layout_engine.py 不存在')
    from why_harness.media import models
    raise models.ModelNotInstalled('版面引擎不可用：'+'；'.join(reasons)+'\n  运行 python -m why_harness 模型 下载 <档位>，并安装 paddlepaddle paddleocr')

def _skip_text_input(doc,paths,tier,page_index):
    """Markdown/文本输入没有页图：不加载版面模型，直接落一份"零框"版面 JSON，让 ③～⑧ 照常跑。"""
    output={'doc':doc,'model':None,'tier':tier,'pages':[],
            'failures':[],'load_seconds':0.0,'detect_seconds':0.0,'skipped':'文本输入，无页图'}
    for item in page_index['pages']:
        output['pages'].append({'page':item['page'],'width':item.get('width') or 0,'height':item.get('height') or 0,
                                'boxes':[],'figures':[],'tables':[],'detect_seconds':0.0})
    paths.save(paths.state/'版面'/f'{doc}.json',output)
    print(f'②版面: 文本输入，跳过版面检测（{len(output["pages"])} 段，无插图/表格）')
    return output

def run(doc,paths=None,tier=None,config=None,force=False):
    paths=paths or Paths(); config=config or {}; tier=tier or config.get('版面.档位','M'); page_index=json.loads((paths.state/'页图'/f'{doc}.json').read_text(encoding='utf-8'))
    if any(not item.get('page_png') for item in page_index['pages']):
        return _skip_text_input(doc,paths,tier,page_index)
    engine=_engine()
    from why_harness.media import models as model_catalog
    info=model_catalog.CATALOG.get(tier,{})
    size_mb=(info.get('size') or 0)/1e6; hint=f'{tier}，约 {size_mb:.0f} MB' if size_mb else tier
    # 版面是默认必需阶段：引擎拿不到 / 加载失败都要停下报依赖错误，绝不静默产出"零插图"的假成功。
    if engine is None: raise model_catalog.ModelNotInstalled(f'版面引擎不可用（{hint}）。运行 python -m why_harness 模型 下载 {tier}')
    model=info.get('name') or tier
    started=time.perf_counter()
    device=(config or {}).get('版面.设备') or '自动'
    try:
        try: engine.load(tier=tier, device=device)
        except TypeError: engine.load(tier=tier)
    except Exception as exc:
        if isinstance(exc,model_catalog.ModelNotInstalled): raise
        raise model_catalog.ModelNotInstalled(f'版面模型加载失败（{hint}）：{type(exc).__name__}: {exc}\n  运行 python -m why_harness 模型 下载 {tier}') from exc
    load_seconds=time.perf_counter()-started
    crop_root=paths.state/'裁图'/doc; crop_root.mkdir(parents=True,exist_ok=True)
    # force：重做版面检测。检测本来就逐页重跑，这里再清掉上一次的裁图，
    # 免得旧 key 的裁图变成孤儿文件留在 裁图/<书>/ 里。
    if force:
        for stale in crop_root.glob('*.png'): stale.unlink()
    output={'doc':doc,'model':model,'tier':tier,'pages':[],'failures':[],'load_seconds':round(load_seconds,2)}; total=0
    pages_total=len(page_index['pages']); progress.report('s2_layout',0,pages_total,'开始')
    for item in page_index['pages']:
        image=paths.root/item['page_png']
        with Image.open(image) as im: width,height=im.size
        started=time.perf_counter()
        try:
            try: detections=engine.detect(str(image),tier=tier)
            except TypeError: detections=engine.detect(str(image))
        except Exception as exc:
            # 逐页失败必须显式记账：页记录留 failure，全局 failures 留一条，收尾让阶段失败。
            elapsed=time.perf_counter()-started; total+=elapsed; failure={'page':item['page'],'error':f'{type(exc).__name__}: {exc}','detect_seconds':round(elapsed,2)}; output['failures'].append(failure); output['pages'].append({'page':item['page'],'width':width,'height':height,'boxes':[],'figures':[],'tables':[],'failure':failure}); print(f'②页{item["page"]}: detect 失败 {failure["error"]}'); progress.report('s2_layout',len(output['pages']),pages_total,f'第{item["page"]}页'); continue
        elapsed=time.perf_counter()-started; total+=elapsed; boxes=[]; figures_raw=[]; tables_raw=[]
        for detection in detections:
            label=detection.get('label','text')
            if label in ('header','footer','page_number'): continue
            kind='figure' if label in ('image','chart') else 'table' if label=='table' else 'title' if label in ('paragraph_title','doc_title') else 'text'; boxes.append({'label':label,'score':detection.get('score'),'bbox':detection.get('bbox'),'kind':kind})
            if kind=='figure': figures_raw.append(detection)
            elif kind=='table': tables_raw.append(detection)
        def crop(found,letter):
            found.sort(key=lambda row:(float(row['bbox'][1]),float(row['bbox'][0]))); records=[]
            with Image.open(image) as im:
                for idx,detection in enumerate(found,1):
                    x0,y0,x1,y1=map(float,detection['bbox']); coords=(max(0,int(x0)-6),max(0,int(y0)-6),min(im.width,int(x1)+6),min(im.height,int(y1)+6)); key=f'p{item["page"]}-{letter}{idx}'; target=crop_root/f'{key}.png'; im.crop(coords).save(target,format='PNG'); records.append({'key':key,'bbox':list(coords),'path':str(target.relative_to(paths.root))})
            return records
        output['pages'].append({'page':item['page'],'width':width,'height':height,'boxes':boxes,'figures':crop(figures_raw,'f'),'tables':crop(tables_raw,'t'),'detect_seconds':round(elapsed,2)}); print(f'②页{item["page"]}: detect {elapsed:.2f}s'); progress.report('s2_layout',len(output['pages']),pages_total,f'第{item["page"]}页')
    output['detect_seconds']=round(total,2); paths.save(paths.state/'版面'/f'{doc}.json',output); print(f'②版面: {len(output["pages"])}页，boxes {sum(len(p["boxes"]) for p in output["pages"])}，插图 {sum(len(p["figures"]) for p in output["pages"])}，表格 {sum(len(p["tables"]) for p in output["pages"])}，加载 {load_seconds:.2f}s，检测 {total:.2f}s')
    if output['failures']: raise RuntimeError(f'版面检测 {len(output["failures"])} 页失败；详情见版面 JSON')
    return output
