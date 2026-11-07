"""中文 CLI。"""
import argparse,hashlib,json,os,sys,time
from pathlib import Path
from datetime import datetime
from . import progress
from .paths import Paths
from .config import load,get,flag,choice
from .events import EventLog
EXIT_OK=0; EXIT_INPUT=1; EXIT_STAGE=2; EXIT_DEPENDENCY=3; EXIT_GAPS=4; EXIT_INTERRUPTED=130
VERSION='2.0.0'
STAGES=('s1_pages','s2_layout','s3_ocr','s4_tables','s5_outline','s6_summary','s7_pack','s8_deliver')
# ① 切页要读 PDF；③ 文本层重建时会重开 PDF。其余阶段只吃状态文件。
NEEDS_PDF=(1,3)
# [输出] 格式：both = Markdown + .why（默认）；md = 只要 Markdown（.why 只当中间包）；
# why = 只要 .why（不导 Markdown、不导图片）。
OUTPUT_FORMATS=('both','md','why')


def output_format(cfg):
 """交付格式：设置写错/没写都按 both。"""
 return choice(cfg,'输出','格式',OUTPUT_FORMATS,'both')


def keep_runtime(cfg):
 """成功后是否保留 状态/ 页图/ 日志 这些中间数据。"""
 return flag(cfg,'输出','保留中间数据',True)


def drop_runtime(paths):
 """删掉中间数据，只留成品。失败/取消时不调用——补跑还要靠这些文件。"""
 import shutil
 for folder in (paths.state,paths.logs):
  shutil.rmtree(folder,ignore_errors=True)
 print(f'已删除中间数据：{paths.root}（[输出] 保留中间数据 设成 false）')

def parser():
 p=argparse.ArgumentParser(prog='python -m why_harness',description='pdf转：把 PDF / Markdown 做成单文件 .why 电子书'); s=p.add_subparsers(dest='cmd')
 r=s.add_parser('run',help='运行全部八个阶段'); r.add_argument('pdf'); r.add_argument('--输出',dest='输出',choices=OUTPUT_FORMATS,default=None,help='覆盖 [输出] 格式：both=Markdown+.why（默认）、md=只要 Markdown、why=只要 .why'); r.add_argument('--工作区',default=None); r.add_argument('--配置'); r.add_argument('--force',action='store_true',help='重做已有产物：①③④⑤⑥⑦⑧ 忽略旧产物重做'); r.add_argument('--是','--yes',dest='是',action='store_true',help='非交互：不询问，默认复用已有结果（同 stdin 不是终端时的行为）'); r.add_argument('--并发',type=int,default=None,help='覆盖设置文件 [全局] 并发；不给就用设置文件里的值'); r.add_argument('--档位',choices=('M','plus-L'),default=None,help='覆盖 [版面] 档位：M=快速档，plus-L=精确档'); r.add_argument('--不下载',dest='不下载',action='store_true',help='模型缺失时不自动下载，直接提示并退出 3')
 st=s.add_parser('阶段',help='只运行第 <n> 个阶段'); st.add_argument('n',type=int,choices=range(1,9)); st.add_argument('book',help='①③ 传 PDF 路径，其余阶段传书名'); st.add_argument('--工作区',default=None); st.add_argument('--配置'); st.add_argument('--force',action='store_true'); st.add_argument('--档位',choices=('M','plus-L'),default=None); st.add_argument('--不下载',dest='不下载',action='store_true',help='模型缺失时不自动下载，直接提示并退出 3')
 rr=s.add_parser('补跑',help='只补没做完的部分：已完成/成功的页不重做，只重试失败页和后续阶段'); rr.add_argument('pdf'); rr.add_argument('--输出',dest='输出',choices=OUTPUT_FORMATS,default=None,help='覆盖 [输出] 格式（同 run）'); rr.add_argument('--工作区',default=None); rr.add_argument('--配置'); rr.add_argument('--并发',type=int,default=None); rr.add_argument('--档位',choices=('M','plus-L'),default=None); rr.add_argument('--不下载',dest='不下载',action='store_true')
 q=s.add_parser('提示词',help='查看提示词'); q.add_argument('kind',nargs='?',choices=['识别','摘要'])
 m=s.add_parser('模型',help='模型管理'); m.add_argument('action',choices=['list','download','verify','remove']); m.add_argument('tier',nargs='?')
 c=s.add_parser('自检',help='检查目录、依赖和 PATH'); c.add_argument('--连通',action='store_true',help='用 [提供商] 地址发 GET /models 探测接口'); s.add_parser('版本',help='显示版本'); g=s.add_parser('gui',help='打开图形界面'); g.add_argument('pdf',nargs='?',help='可选：拖进来的 PDF 路径'); return p

def prepare(workspace,config_path=None):
 """run 与 阶段 共用：解析工作区、读配置、查版面模型。返回 (paths, cfg, 档位, 模型是否已装)。"""
 paths=Paths(workspace); cfg=load(paths.root,config_path)
 from .media import models
 tier=cfg.get('版面.档位','M')
 return paths,cfg,tier,models.installed(tier)

def ensure_model(tier):
 """模型缺失时自动下载，返回是否就绪。

 打印「模型缺失，开始自动下载：<档位>」→ 逐百分比进度（models.line_progress()，
 一行一个百分比并 flush，终端可见、GUI 子进程也能按行读到）→「模型就绪」。
 下载/校验/解包失败由 models.ensure_available 抛 ModelNotInstalled，
 这里把中文指引（手动命令 + 模型目录）打到 stderr 并返回 False。"""
 from .media import models
 print('模型缺失，开始自动下载：'+tier,flush=True)
 try:
  ok=models.ensure_available(tier,allow_download=True,on_progress=models.line_progress())
 except models.ModelNotInstalled as exc:
  print(str(exc),file=sys.stderr,flush=True); return False
 except Exception as exc:  # noqa: BLE001 - 下载/解包的任何失败都按缺依赖处理
  print(f'模型下载失败（{tier}）：{exc}',file=sys.stderr,flush=True); return False
 if ok: print('模型就绪',flush=True)
 return bool(ok)

def missing_model_message(tier,no_download):
 """模型仍缺失时的 stderr 提示；--不下载 时多一句说明不自动下载。"""
 return '模型缺失：'+tier+('（--不下载：已跳过自动下载）' if no_download else '')

# 每个阶段真正会用到的模型：[阶段.<节>] 模型。① ② ④ ⑤ ⑦ ⑧ 不调模型。
STAGE_MODEL_SECTIONS={3:('识别',),6:('摘要',)}
RUN_MODEL_SECTIONS=('识别','摘要')

def settings_file(paths,config_path=None):
 """当前生效的设置文件绝对路径：显式 --配置 优先，否则工作区 设置.txt。"""
 return Path(config_path).resolve() if config_path else (paths.root/'设置.txt')

def check_required_inputs(paths,cfg,model_sections=(),config_path=None):
 """进阶段之前查"该填没填"：先 [提供商] 地址，再本次真正会用到的模型。

 model_sections 为空表示这次不调模型（① ② ④ ⑤ ⑦ ⑧），直接放行。
 缺项时打印带设置文件绝对路径的中文指引并返回 EXIT_INPUT，不再拖到 ⑥ 才抛
 "未配置模型"；齐全时返回 EXIT_OK，原有行为不变。"""
 sections=tuple(s for s in RUN_MODEL_SECTIONS if s in tuple(model_sections))
 if not sections: return EXIT_OK
 settings=settings_file(paths,config_path)
 if not str(get(cfg,'提供商','地址','') or '').strip():
  print(f'还没填接口地址。请编辑：{settings} 的 [提供商] 地址（任何 OpenAI 兼容端点，示例见 说明书\\设置说明.md）',file=sys.stderr)
  return EXIT_INPUT
 missing=['[阶段.'+s+'] 模型' for s in sections if not str(get(cfg,'阶段.'+s,'模型','') or '').strip()]
 if missing:
  print(f"还没填模型名。请在 {settings} 填：{'、'.join(missing)}（例如 [阶段.识别] 模型 / [阶段.摘要] 模型；名字只填最后一个 / 之后的部分）",file=sys.stderr)
  return EXIT_INPUT
 return EXIT_OK

def default_workspace(name):
 """未指定 --工作区 时：按书名自动分目录，工作区 = 产物/<书>，并打印一行说明。"""
 workspace=str(Path('产物')/name)
 print(f'工作区：{Path(workspace).resolve()}（未指定 --工作区，按书名自动分目录）')
 return workspace

def stage_calls(pdf,doc,paths,cfg):
 """八个阶段的调用表（下标 0..7 对应 ①..⑧）。

 [输出] 决定 ⑦⑧ 干什么：格式 = md 时 .why 只当中间包（状态/包/）不落到成品目录；
 格式 = why 时 ⑧ 不导 Markdown；溯源段 = false 时 ⑦ 不写 TRAILER。

 force 传给签名里真有这个形参的阶段：① s1_pages(...,force=False)、③ s3_ocr(...,force=False)、
 ④ s4_tables(doc,paths,force=False)、⑤ s5_outline(doc,paths,force=False)、
 ⑥ s6_summary(doc,paths,force=False,config=None)、⑦ s7_pack(doc,paths,force=False)、
 ⑧ s8_deliver(doc,paths,force=False)。② s2_layout 还没有这个形参，保持原样。"""
 fmt=output_format(cfg)
 keep_why=fmt in ('both','why')
 markdown=fmt in ('both','md')
 provenance=flag(cfg,'输出','溯源段',True)
 return [
  ('s1_pages',lambda m,force:m.run(pdf,doc,paths,cfg,force=force)),
  ('s2_layout',lambda m,force:m.run(doc,paths,config=cfg)),
  ('s3_ocr',lambda m,force:m.run(pdf,doc,paths,cfg,force=force)),
  ('s4_tables',lambda m,force:m.run(doc,paths,force=force)),
  ('s5_outline',lambda m,force:m.run(doc,paths,force=force)),
  ('s6_summary',lambda m,force:m.run(doc,paths,force=force,config=cfg)),
  ('s7_pack',lambda m,force:m.run(doc,paths,force=force,provenance=provenance,keep_why=keep_why)),
  ('s8_deliver',lambda m,force:m.run(doc,paths,force=force,markdown=markdown,
                                   why=(None if keep_why else paths.pack_file(doc)))),
 ]

def run_stage(name,module,call,force,log):
 """跑一个阶段并记事件日志。返回 (结果, 退出码)；KeyboardInterrupt 留给调用方处理。

 依赖类异常（如版面模型没装/加载不了，models.ModelNotInstalled）映射退出码 3：
 缺依赖不是"书本身跑挂了"，别让用户照着 2 去查内容。"""
 started=time.perf_counter(); print('开始 '+name); progress.report(name,0,1,'开始')
 try: result=call(module,force)
 except Exception as e:
  from .media.models import ModelNotInstalled
  code=EXIT_DEPENDENCY if isinstance(e,ModelNotInstalled) else EXIT_STAGE
  log.stage(name,'failed',elapsed=time.perf_counter()-started); log.error(e,stage=name)
  # 失败的阶段不算跑完：报 0/1，界面就不会把它算进总进度、更不会推到 100%。
  progress.report(name,0,1,'失败'); print(name+' 失败：'+str(e),file=sys.stderr); return None,code
 log.stage(name,'success',elapsed=time.perf_counter()-started); print('完成 '+name); progress.report(name,1,1,'完成'); return result,EXIT_OK

def resume_skip(name, paths, doc):
 """补跑时判断哪些阶段已经完整，可以整段跳过（只跳过产物齐全的 ①②④）。"""
 if name=='s1_pages':
  data=_json_object(paths.state/'页图'/f'{doc}.json')
  pages=data.get('pages') if isinstance(data.get('pages'),list) else []
  return bool(pages) and all((paths.root/str(p.get('page_png') or '')).is_file() for p in pages if isinstance(p,dict))
 if name=='s2_layout':
  expected=len(_json_object(paths.state/'页图'/f'{doc}.json').get('pages') or [])
  data=_json_object(paths.state/'版面'/f'{doc}.json')
  pages=data.get('pages') if isinstance(data.get('pages'),list) else []
  return bool(expected) and len(pages)==expected and not data.get('failures')
 if name=='s4_tables':
  return (paths.state/f'表格-{doc}.json').is_file()
 return False

def gap_reasons(results):
 """退出码 4 的判据：⑤ 守恒 warnings、⑦ 打包的 stale_summaries > 0，或 ⑧ 交付的 missing_refs 非空。"""
 reasons=[]
 outline=results.get('s5_outline')
 if isinstance(outline,dict):
  conservation=outline.get('conservation') if isinstance(outline.get('conservation'),dict) else {}
  warnings=list(conservation.get('warnings') or [])
  if warnings: reasons.append(f'守恒警告 {len(warnings)} 条')
 pack=results.get('s7_pack')
 if isinstance(pack,dict):
  try: stale=int(pack.get('stale_summaries') or 0)
  except (TypeError,ValueError): stale=0
  if stale>0: reasons.append(f'过期摘要 {stale} 条')
 deliver=results.get('s8_deliver')
 if isinstance(deliver,dict):
  missing=list(deliver.get('missing_refs') or [])
  if missing: reasons.append(f'缺失引用 {len(missing)} 处')
 return '；'.join(reasons)

def check_connection(cfg):
 """自检 --连通：[提供商] 地址 + key_环境变量（或 key），GET {地址}/models，超时 5 秒。"""
 base=str(get(cfg,'提供商','地址','') or '').strip().rstrip('/')
 env_name=str(get(cfg,'提供商','key_环境变量','WHY_API_KEY') or 'WHY_API_KEY')
 key=str(get(cfg,'提供商','key','') or '') or os.environ.get(env_name,'')
 if not base: print('未配置地址：请在设置文件 [提供商] 地址 填接口地址',file=sys.stderr); return EXIT_DEPENDENCY
 if not key: print('未配置 key：请设置环境变量 '+env_name,file=sys.stderr); return EXIT_DEPENDENCY
 import urllib.error,urllib.request
 request=urllib.request.Request(base+'/models',headers={'Authorization':'Bearer '+key},method='GET')
 try:
  with urllib.request.urlopen(request,timeout=5) as response: response.read(1)
 except urllib.error.HTTPError as e: print(f'接口：异常（HTTP {e.code} {e.reason}）',file=sys.stderr); return EXIT_DEPENDENCY
 except Exception as e: print('接口：异常（'+type(e).__name__+'：'+str(e)+'）',file=sys.stderr); return EXIT_DEPENDENCY
 print('接口：正常'); return EXIT_OK

# ── 已有结果提示：run 开始前把工作区里"已经有了什么"摆出来，让用户选复用还是重做 ──

def _prompt_fingerprint():
 """当前生效的摘要提示词（用户覆盖优先）的 sha256；读不出来返回 None。"""
 from .promptlib import prompt_sha256
 return prompt_sha256('summary.md')

def _mtime_text(path):
 """文件修改时间 → 'YYYY-MM-DD HH:MM'（本地时区）；读不到返回"未知"。"""
 try: return datetime.fromtimestamp(path.stat().st_mtime).strftime('%Y-%m-%d %H:%M')
 except (OSError,ValueError,OverflowError,TypeError): return '未知'

def _json_object(path):
 """读 JSON 对象；文件损坏、权限不足或顶层不是对象都返回 {}——降级不能崩。"""
 try: value=json.loads(path.read_text(encoding='utf-8'))
 except (OSError,ValueError,UnicodeDecodeError): return {}
 return value if isinstance(value,dict) else {}

def _size_text(count):
 """字节数 → 可读文本（B/KB/MB）；拿不到大小返回"未知"。"""
 if count is None: return '未知'
 if count<1024: return f'{count} B'
 if count<1024*1024: return f'{round(count/1024)} KB'
 return f'{count/1048576:.1f} MB'

def _text_field(data,key):
 value=data.get(key)
 return value.strip() if isinstance(value,str) and value.strip() else None

def inspect_existing(paths,doc):
 """看 状态/章节-<书>.json、状态/摘要-<书>.json、产物/<书>/<书>.why 各有什么。

 字段缺失、文件损坏、JSON 顶层结构不对一律降级成未知（count/model/sha 为 None），不抛异常。
 三者都不存在返回 None（不打扰用户）。"""
 chapter=paths.state/f'章节-{doc}.json'
 summary=paths.state/f'摘要-{doc}.json'
 product=paths.find_why(doc)
 found={}
 if chapter.is_file():
  nodes=_json_object(chapter).get('nodes')
  found['chapter']={'count':len(nodes) if isinstance(nodes,list) else None,'time':_mtime_text(chapter)}
 if summary.is_file():
  data=_json_object(summary); nodes=data.get('nodes')
  found['summary']={'count':len(nodes) if isinstance(nodes,(dict,list)) else None,
                    'model':_text_field(data,'model'),'sha':_text_field(data,'prompt_sha256'),
                    'time':_mtime_text(summary)}
 if product.is_file():
  try: size=product.stat().st_size
  except OSError: size=None
  found['product']={'name':product.name,'size':size,'time':_mtime_text(product)}
 return found or None

def _model_name(value):
 """模型名比较口径：与 Client 发请求时一致，丢掉 提供商/ 前缀。"""
 return str(value).strip().split('/')[-1] if value else ''

def _same_fingerprint(a,b):
 """指纹比较：允许一侧是截断的前缀（文件里可能只存了前 8 位）。"""
 a='' if a is None else str(a).strip().lower()
 b='' if b is None else str(b).strip().lower()
 if not a or not b: return True
 return a==b or (len(a)>=8 and b.startswith(a)) or (len(b)>=8 and a.startswith(b))

def print_existing(paths,found,cfg):
 """打印中文清单，并在现有摘要与当前模型/提示词不一致时点破"复用不会重算"。"""
 print('检测到已有结果：'+str(paths.root))
 chapter=found.get('chapter')
 if chapter:
  print(f"  章节：{'未知' if chapter['count'] is None else str(chapter['count'])+' 个节点'}（{chapter['time']}）")
 stale=False; summary=found.get('summary')
 if summary:
  count='未知' if summary['count'] is None else f"{summary['count']} 条"
  print(f"  摘要：{count}，模型 {summary['model'] or '未知'}，提示词指纹 {(summary['sha'] or '未知')[:8]}（{summary['time']}）")
  current_model=_model_name(cfg.get('阶段.摘要.模型'))
  if current_model and summary['model'] and current_model!=_model_name(summary['model']): stale=True
  if not _same_fingerprint(_prompt_fingerprint(),summary['sha']): stale=True
 product=found.get('product')
 if product:
  print(f"  产物：{product['name']}，{_size_text(product['size'])}（{product['time']}）")
 if stale: print('注意：现有摘要用的是旧模型/旧提示词，复用不会重算。')

def _interactive():
 """stdin 是不是终端；判断不出来就当非交互（宁可少问，不要卡住脚本）。"""
 try: return bool(sys.stdin.isatty())
 except (AttributeError,ValueError,OSError): return False

def ask_reuse():
 """[Y] 复用（只补缺） / [r] 全部重做 / [q] 取消；其它输入重新问。"""
 while True:
  try: answer=input('[Y] 复用（只补缺） / [r] 全部重做 / [q] 取消：').strip().lower()
  except EOFError:
   print('非交互模式：默认复用已有结果；要重来请加 --force'); return 'reuse'
  except KeyboardInterrupt:
   print(); return 'cancel'
  if answer in ('','y','yes','是','复用'): return 'reuse'
  if answer in ('r','redo','force','重做','全部重做'): return 'redo'
  if answer in ('q','quit','exit','取消'): return 'cancel'
  print('没听懂：请输入 y（复用）、r（全部重做）或 q（取消）。')

def main(argv=None):
 for stream in (sys.stdout, sys.stderr):
  try:
   stream.reconfigure(encoding='utf-8', errors='replace')
  except (AttributeError, TypeError):
   try: stream.reconfigure(errors='replace')
   except (AttributeError, TypeError): pass
 a=parser().parse_args(argv)
 if a.cmd in ('run','补跑'):
  resume=a.cmd=='补跑'
  if not Path(a.pdf).is_file(): print('文件不存在：'+a.pdf,file=sys.stderr); return EXIT_INPUT
  if a.并发 is not None and a.并发<1: print('--并发 必须 ≥1',file=sys.stderr); return EXIT_INPUT
  if a.工作区 is None: a.工作区=default_workspace(Path(a.pdf).stem)
  try: paths,cfg,tier,ready=prepare(a.工作区,a.配置)
  except Exception as e: print('依赖或配置错误：'+str(e),file=sys.stderr); return EXIT_DEPENDENCY
  # --档位 覆盖 [版面] 档位：快速档 M / 精确档 plus-L，覆盖后重新查模型是否已装。
  if getattr(a,'档位',None):
   from .media import models
   cfg['版面.档位']=a.档位; tier=a.档位; ready=models.installed(tier)
  # 一进门先查该填没填：run 两个模型都要，缺了就打印指引退出 1，不拖到 ⑥ 才炸。
  code=check_required_inputs(paths,cfg,RUN_MODEL_SECTIONS,a.配置)
  if code: return code
  # 缺模型先自动下载（用户不用敲命令）；--不下载 时退回旧行为：提示 + 退出 3。
  no_download=getattr(a,'不下载',False)
  if not ready and not no_download: ready=ensure_model(tier)
  if not ready: print(missing_model_message(tier,no_download),file=sys.stderr); return EXIT_DEPENDENCY
  if getattr(a,'输出',None): cfg['输出.格式']=a.输出
  if a.并发 is not None: cfg['全局.并发']=str(a.并发)
  doc=Path(a.pdf).stem; calls=stage_calls(a.pdf,doc,paths,cfg)
  # 真正进阶段之前：摆出已有结果并让用户选复用还是重做。--force 不问，直接全部重做。
  force=getattr(a,'force',False)
  if resume:
   print(f'补跑：已完成/成功的页不重做，只补失败页和后续阶段。工作区：{paths.root}')
  elif not force:
   found=inspect_existing(paths,doc)
   if found:
    print_existing(paths,found,cfg)
    if getattr(a,'是',False) or not _interactive():
     print('非交互模式：默认复用已有结果；要重来请加 --force'); choice='reuse'
    else: choice=ask_reuse()
    if choice=='cancel': print('已取消'); return EXIT_OK
    if choice=='redo': force=True
  log=EventLog(paths.logs/('运行-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.jsonl'))
  progress.set_sink(progress.cli_sink)
  try:
   results={}
   for name,fn in calls:
    if resume and resume_skip(name,paths,doc):
     print(f'补跑：跳过 {name}（产物已完整）')
     continue
    module=__import__('why_harness.pipeline.'+name,fromlist=[name])
    result,code=run_stage(name,module,fn,force,log); results[name]=result
    if code: return code
   gaps=gap_reasons(results)
   if gaps:
    log.log('gaps',message=gaps); print(f'有缺口：{gaps}（八个阶段仍跑完，退出码 {EXIT_GAPS}）'); return EXIT_GAPS
  except KeyboardInterrupt: log.log('warn',message='用户中断'); return EXIT_INTERRUPTED
  finally:
   progress.set_sink(None); log.close()
  # 只有整本跑完才清中间数据；失败/带缺口都留着，供「补跑」复用。
  if not keep_runtime(cfg): drop_runtime(paths)
  return EXIT_OK
 if a.cmd=='阶段':
  pdf=Path(a.book).resolve()
  if a.n in NEEDS_PDF and not pdf.is_file(): print('文件不存在：'+a.book,file=sys.stderr); return EXIT_INPUT
  if a.工作区 is None:
   name=Path(a.book).stem if a.n in NEEDS_PDF else a.book
   a.工作区=default_workspace(name)
  try: paths,cfg,tier,ready=prepare(a.工作区,a.配置)
  except Exception as e: print('依赖或配置错误：'+str(e),file=sys.stderr); return EXIT_DEPENDENCY
  if getattr(a,'档位',None):
   from .media import models
   cfg['版面.档位']=a.档位; tier=a.档位; ready=models.installed(tier)
  # 阶段 只查它真正要用的模型：③ 看识别，⑥ 看摘要，其余阶段不调模型。
  code=check_required_inputs(paths,cfg,STAGE_MODEL_SECTIONS.get(a.n,()),a.配置)
  if code: return code
  no_download=getattr(a,'不下载',False)
  if not ready and not no_download: ready=ensure_model(tier)
  if not ready: print(missing_model_message(tier,no_download),file=sys.stderr); return EXIT_DEPENDENCY
  name,fn=stage_calls(pdf,Path(a.book).stem,paths,cfg)[a.n-1]
  log=EventLog(paths.logs/(f'阶段-{a.n}-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.jsonl'))
  progress.set_sink(progress.cli_sink)
  try:
   module=__import__('why_harness.pipeline.'+name,fromlist=[name])
   _,code=run_stage(name,module,fn,a.force,log); return code
  except KeyboardInterrupt: log.log('warn',message='用户中断'); return EXIT_INTERRUPTED
  finally:
   progress.set_sink(None); log.close()
 if a.cmd=='模型':
  try: from .media.models import main as mm; return mm([a.action]+([a.tier] if a.tier else []))
  except Exception as e: print(str(e),file=sys.stderr); return EXIT_DEPENDENCY
 if a.cmd=='提示词':
  if not a.kind: parser().error('提示词需要指定 识别 或 摘要')
  from .promptlib import load_prompt, prompt_path
  name='ocr.md' if a.kind=='识别' else 'summary.md'
  try:
   print(f'当前生效：{prompt_path(name)}'); print(load_prompt(name)); return EXIT_OK
  except OSError: return EXIT_INPUT
 if a.cmd=='自检':
  print('工作目录：正常'); print('PATH：'+('正常' if os.environ.get('PATH') else '缺失'))
  from .media import models
  print(f"程序目录：{models._program_dir()}")
  print(f"模型目录：{models.MODELS_ROOT}")
  if not a.连通: return EXIT_OK
  try: cfg=load(Paths().root)
  except Exception as e: print('依赖或配置错误：'+str(e),file=sys.stderr); return EXIT_DEPENDENCY
  return check_connection(cfg)
 if a.cmd=='版本': print('pdf转 '+VERSION); return EXIT_OK
 if a.cmd=='gui':
  from . import gui
  return gui.main([a.pdf] if getattr(a,'pdf',None) else [])
 parser().print_help(sys.stderr); return EXIT_INPUT

if __name__=='__main__': raise SystemExit(main())