""".why 2.0 读写库（只用标准库）。"""
from __future__ import annotations
import hashlib, json, re, struct, zlib
from pathlib import Path
from typing import Any, Iterable

MAGIC=b"WHY\x02"
MAJOR,MINOR=2,0
GENERATOR="why-harness/2.0.0"
HEADER=struct.Struct("<4sHHQQI4s")
KINDS=("raw","summary","brief")
ROLES=("front","toc","body","exercise","answer","appendix","index")
ORIGINS=("text-layer","vision","mixed","unknown")
ID_PATTERN=re.compile(r"[0-9a-f]{16}")
FIGURE_REF=re.compile(r"!\[[^\]]*\]\(why:fig/([0-9a-f]{16})\)")
TABLE_REF=re.compile(r'data-why="tab/([0-9a-f]{16})"')

class WhyFormatError(ValueError): pass

def dumps(obj: Any) -> bytes:
    return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")

def tree_sha256(title: str, raw_sha256: str | None, child_hashes: Iterable[str]) -> str:
    s="why2\n"+title+"\n"+(raw_sha256 or "")+"\n"+"\n".join(child_hashes)
    return hashlib.sha256(s.encode()).hexdigest()

def image_type(data: bytes) -> tuple[str,str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n"): return "image/png",".png"
    if data.startswith(b"\xff\xd8\xff"): return "image/jpeg",".jpg"
    if data.startswith((b"GIF87a",b"GIF89a")): return "image/gif",".gif"
    if data.startswith(b"RIFF") and data[8:12]==b"WEBP": return "image/webp",".webp"
    raise WhyFormatError("图片魔数无法识别")

def _decode_blob(raw):
    if not raw: raise WhyFormatError("blob 为空")
    if raw[0]==0:return raw[1:]
    if raw[0]==1:return zlib.decompress(raw[1:])
    raise WhyFormatError("未知 blob codec")

def write_book(path: str|Path, *, book:dict, source:dict, root:str, nodes:dict, blocks:dict, figures=None, tables=None, keywords=None, trailer=None)->dict:
    path=Path(path); figures=figures or {}; tables=tables or {}; blocks=blocks or {}
    order=[]; seen=set()
    def visit(i):
        if i in seen: raise WhyFormatError(f"树重复/成环: {i}")
        if i not in nodes: raise WhyFormatError(f"节点不存在: {i}")
        seen.add(i); order.append(i)
        for c in nodes[i].get('children',[]):visit(c)
    visit(root)
    if len(seen)!=len(nodes): raise WhyFormatError("存在不可达节点")
    trees={}; rawhash={}
    def calc(i):
        for c in nodes[i].get('children',[]):calc(c)
        raw=(blocks.get(i,{}) or {}).get('raw')
        rawhash[i]=hashlib.sha256(raw.encode()).hexdigest() if raw else None
        trees[i]=tree_sha256(nodes[i]['title'],rawhash[i],[trees[c] for c in nodes[i].get('children',[])])
    calc(root)
    text_parts=[]; outblocks={}; cursor=0
    for i in order:
        bd={}
        for k in KINDS:
            val=(blocks.get(i,{}) or {}).get(k)
            if val is None:continue
            if k=='raw': txt=val
            else:
                if not isinstance(val,dict) or 'text' not in val: raise WhyFormatError(f"{i}/{k} 格式错误")
                txt=val['text']
            if not txt:continue
            b=txt.encode('utf-8'); item={'off':cursor,'len':len(b),'chars':len(txt),'sha256':hashlib.sha256(b).hexdigest()}
            if k!='raw': item.update({x:val.get(x) for x in ('src','by','model')})
            bd[k]=item;text_parts.append(b);cursor+=len(b)
        if bd:outblocks[i]=bd
    text=b''.join(text_parts); blobdata=bytearray(); outfig={}; outtab={}; dedup={}
    def putasset(fid,f):
        data=f['data']; med,ext=image_type(data); sha=hashlib.sha256(data).hexdigest()
        if sha not in dedup:
            packed=zlib.compress(data,9)
            entry=(b'\x01'+packed) if len(packed)<len(data) else b'\x00'+data
            off=len(blobdata);blobdata.extend(entry);dedup[sha]=(off,len(entry))
        off,n=dedup[sha]
        outfig[fid]={k:v for k,v in f.items() if k!='data'}
        outfig[fid].update(media=med,ext=ext,blob={'off':off,'len':n,'sha256':sha})
    assets=sorted([(order.index(v['node']),v.get('anchor',0),fid,v) for fid,v in figures.items()])
    for _,__,fid,f in assets:putasset(fid,f)
    for tid,t in tables.items():outtab[tid]=dict(t)
    kw={}
    rank={i:n for n,i in enumerate(order)}
    for key,ids in (keywords or {}).items():kw[key]=sorted(set(ids),key=lambda i:rank.get(i,10**9))
    tr=zlib.compress(dumps(trailer),9) if trailer is not None else b''
    manifest={'why':{'version':'2.0','generator':GENERATOR},'book':book,'source':source,'regions':{'text_len':len(text),'blob_len':len(blobdata),'trailer_len':len(tr),'trailer_codec':'zlib' if tr else None},'root':root,'nodes':{},'blocks':outblocks,'assets':{'figures':outfig,'tables':outtab},'keywords':kw}
    for i in order:
        n=dict(nodes[i]);n['tree_sha256']=trees[i];manifest['nodes'][i]=n
    # Anchor offsets refer to raw codepoints; asset ordering is node/anchor, independent of input dictionary order.
    mb=dumps(manifest); hdr=HEADER.pack(MAGIC,MAJOR,MINOR,len(mb),len(text),zlib.crc32(mb),b'\0'*4)
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        path.write_bytes(hdr+mb+text+blobdata+tr)
        with WhyBook(path) as wb:
            result=wb.verify()
        if not result['ok']:raise WhyFormatError('写入自检失败: '+repr(result['problems']))
    except Exception:
        path.unlink(missing_ok=True);raise
    return manifest

class WhyBook:
    def __init__(self,path):
        self.path=Path(path);self._fh=self.path.open('rb')
        try:
            raw=self._fh.read(32)
            if len(raw)!=32:raise WhyFormatError('HEADER 不完整')
            magic,major,minor,ml,tl,crc,_=HEADER.unpack(raw)
            if magic!=MAGIC or major!=2:raise WhyFormatError('magic/major 不兼容')
            mb=self._fh.read(ml)
            if zlib.crc32(mb)!=crc:raise WhyFormatError('manifest CRC32 不符')
            try:self.manifest=json.loads(mb.decode('utf8'))
            except Exception as e:raise WhyFormatError(str(e))
            self.header={'manifest_len':ml,'text_len':tl,'minor':minor};self.root=self.manifest['root'];self._text_start=32+ml;self._text_len=tl;self._blob_start=self._text_start+tl;self._size=self.path.stat().st_size
        except Exception:
            self._fh.close()
            raise
    def close(self):self._fh.close()
    def __enter__(self):return self
    def __exit__(self,*a):self.close()
    @property
    def has_trailer(self):
        r=self.manifest['regions']; core=self._blob_start+r['blob_len'];return r['trailer_len']>0 and self._size==core+r['trailer_len']
    def trailer(self):
        if not self.has_trailer:return None
        self._fh.seek(self._blob_start+self.manifest['regions']['blob_len']);return json.loads(zlib.decompress(self._fh.read()).decode())
    def node(self,i):return dict(self.manifest['nodes'][i],id=i)
    def children(self,i):return list(self.manifest['nodes'][i]['children'])
    def parent(self,i):
        for p,n in self.manifest['nodes'].items():
            if i in n['children']:return p
        return None
    def depth(self,i):
        d=0
        while i!=self.root:
            i=self.parent(i)
            if i is None:raise KeyError(i)
            d+=1
        return d
    def walk(self):
        out=[]
        def v(i,d):out.append((i,d));[v(c,d+1) for c in self.children(i)]
        v(self.root,0);return out
    def text(self,i,kind='raw'):
        ref=self.manifest['blocks'].get(i,{}).get(kind)
        if ref is None:return None
        self._fh.seek(self._text_start+ref['off']);return self._fh.read(ref['len']).decode('utf8')
    def kinds(self,i):return [k for k in KINDS if k in self.manifest['blocks'].get(i,{})]
    def stale(self,i,kind):
        b=self.manifest['blocks'].get(i,{}).get(kind)
        return None if b is None else kind!='raw' and b.get('src')!=self.manifest['nodes'][i]['tree_sha256']
    def page(self,n):
        out=[]
        for i,_ in self.walk():
            raw=self.text(i)
            if raw is None:continue
            pm=self.manifest['nodes'][i].get('page_map',[])
            for j,(p,start) in enumerate(pm):
                if p==n:out.append((i,start,pm[j+1][1] if j+1<len(pm) else len(raw)))
        return out
    def figure(self,fid):
        b=self.manifest['assets']['figures'][fid]['blob'];self._fh.seek(self._blob_start+b['off']);return _decode_blob(self._fh.read(b['len']))
    def table(self,tid):return dict(self.manifest['assets']['tables'][tid],id=tid)
    def search(self,keyword):return list(self.manifest['keywords'].get(keyword,[]))
    def export_markdown(self,fold=None,figure_link=None):
        fold=fold or {};links=figure_link or {};out=[]
        def emit(i,d):
            n=self.manifest['nodes'][i];title=n['title'];head='#'*min(d+1,6)+' '+title
            k=fold.get(i);body=self.text(i,k) if k else self.text(i)
            if body is None and k:body=self.text(i)
            part=head+(('\n\n'+body) if body else '')
            for fid,url in links.items():part=part.replace('(why:fig/'+fid+')','('+str(url)+')')
            out.append(part)
            if not k or self.text(i,k) is None:
                for c in n['children']:emit(c,d+1)
        emit(self.root,0);return '\n\n'.join(out)
    def verify(self):
        problems=[];warnings=[];m=self.manifest;nodes=m.get('nodes',{});blocks=m.get('blocks',{});regions=m.get('regions',{})
        if regions.get('text_len')!=self._text_len:problems.append('regions.text_len 与 HEADER 不一致')
        bl=regions.get('blob_len',-1);core=self._blob_start+bl;decl=regions.get('trailer_len',0)
        if self._size not in {core,core+decl}:problems.append('文件大小不符合核心区/TRAILER')
        root=m.get('root');order=[];parents={};visiting=set();visited=set()
        def dfs(i):
            if i in visiting:problems.append('树有环');return
            if i in visited:return
            if i not in nodes:problems.append(f'children 指向不存在节点: {i}');return
            visiting.add(i);visited.add(i);order.append(i)
            if not ID_PATTERN.fullmatch(i):problems.append(f'编号非法: {i}')
            n=nodes[i]
            if n.get('role') not in ROLES or n.get('origin') not in ORIGINS:problems.append(f'节点字段非法: {i}')
            c=n.get('confidence');
            if not isinstance(c,(int,float)) or not 0<=c<=1:problems.append(f'confidence 非法: {i}')
            p=n.get('pages')
            if p is not None and (not isinstance(p,list) or len(p)!=2 or p[0]>p[1]):problems.append(f'pages 非法: {i}')
            for child in n.get('children',[]):
                parents[child]=parents.get(child,0)+1;dfs(child)
            visiting.remove(i)
        if root not in nodes:problems.append('root 不存在')
        else:dfs(root)
        if len(visited)!=len(nodes):problems.append('存在不可达节点')
        if any(v>1 for k,v in parents.items() if k!=root):problems.append('节点有多个父节点')
        # §6.4 validate every block owner and kind.
        for owner, kinds in blocks.items():
            if owner not in nodes: problems.append(f'blocks 指向不存在节点: {owner}')
            if not isinstance(kinds, dict):
                problems.append(f'blocks 格式非法: {owner}'); continue
            for kind in kinds:
                if kind not in KINDS: problems.append(f'BLOCK kind 非法: {owner}/{kind}')
        # §6.6 validate page_map against raw codepoint offsets.
        for nid, node in nodes.items():
            raw=self.text(nid)
            page_map=node.get('page_map')
            if not isinstance(page_map,list):
                problems.append(f'page_map 非法: {nid}'); continue
            if raw is not None and not page_map: problems.append(f'有 raw 的节点 page_map 为空: {nid}')
            try:
                offsets=[entry[1] for entry in page_map]
                if page_map and offsets[0]!=0: problems.append(f'page_map 首项偏移非 0: {nid}')
                if offsets!=sorted(offsets): problems.append(f'page_map 偏移未升序: {nid}')
                if raw is not None and any(not isinstance(x,int) or x<0 or x>len(raw) for x in offsets): problems.append(f'page_map 偏移越界: {nid}')
            except (IndexError,TypeError): problems.append(f'page_map 条目非法: {nid}')
        refs=[];text_bytes=b''
        for i in order:
            for k in KINDS:
                b=blocks.get(i,{}).get(k)
                if b:
                    refs.append((b.get('off'),i,k,b))
                    try:
                        self._fh.seek(self._text_start+b['off']);raw=self._fh.read(b['len']);s=raw.decode('utf8')
                        if not b['len'] or hashlib.sha256(raw).hexdigest()!=b['sha256'] or len(s)!=b['chars']:problems.append(f'BLOCK 校验失败: {i}/{k}')
                        if k!='raw' and ('src' not in b or 'by' not in b):problems.append(f'摘要元数据缺失: {i}/{k}')
                    except Exception:problems.append(f'BLOCK 范围/UTF-8 错误: {i}/{k}')
                    if k!='raw' and b.get('src')!=nodes[i].get('tree_sha256'):warnings.append(f'过期摘要: {i}/{k}')
        refs.sort(key=lambda x:x[0]);cursor=0
        if [(x[1],x[2]) for x in refs]!=[(i,k) for i in order for k in KINDS if blocks.get(i,{}).get(k)]:problems.append('BLOCK 顺序错误')
        for off,i,k,b in refs:
            if off!=cursor:problems.append('TEXT 未连续铺满')
            cursor=off+b.get('len',0)
        if cursor!=self._text_len:problems.append('TEXT 未铺满')
        def rawhash(i):
            raw=self.text(i);return hashlib.sha256(raw.encode()).hexdigest() if raw else None
        hashes={}
        def calc(i):
            for c in nodes[i]['children']:calc(c)
            hashes[i]=tree_sha256(nodes[i]['title'],rawhash(i),[hashes[c] for c in nodes[i]['children']])
            if nodes[i].get('tree_sha256')!=hashes[i]:problems.append(f'tree_sha256 错误: {i}')
        if root in nodes:
            try:calc(root)
            except Exception:problems.append('Merkle 校验失败')
        assets=m.get('assets',{});figs=assets.get('figures',{});tabs=assets.get('tables',{})
        for i in order:
            raw=self.text(i) or ''
            for fid in FIGURE_REF.findall(raw):
                if fid not in figs or figs[fid].get('node')!=i:problems.append(f'raw 图片引用无资产/节点不符: {fid}')
            for tid in TABLE_REF.findall(raw):
                if tid not in tabs or tabs[tid].get('node')!=i:problems.append(f'raw 表格引用无资产/节点不符: {tid}')
        for fid,f in figs.items():
            try:
                if f['node'] not in nodes:raise ValueError('node missing')
                data=self.figure(fid);media,ext=image_type(data)
                if hashlib.sha256(data).hexdigest()!=f['blob']['sha256'] or media!=f['media']:raise ValueError('blob/media')
                raw=self.text(f['node']) or '';a=f['anchor'];match=re.match(r'!\[[^\]]*\]\(why:fig/'+re.escape(fid)+r'\)',raw[a:])
                if not match:raise ValueError('anchor')
            except Exception:problems.append(f'FIGURE anchor/blob 不存在或无效: {fid}')
        for tid,t in tabs.items():
            raw=self.text(t.get('node','')) or '';a=t.get('anchor',-1)
            if t.get('node') not in nodes or not raw[a:].startswith('<table data-why="tab/'+tid+'"'):problems.append(f'TABLE 错误: {tid}')
        rank={i:j for j,i in enumerate(order)}
        for kw,ids in m.get('keywords',{}).items():
            if ids!=sorted(set(ids),key=lambda x:rank.get(x,10**9)) or any(x not in nodes or kw not in (self.text(x) or '') for x in ids):problems.append(f'keywords 错误: {kw}')
        return {'ok':not problems,'problems':problems,'warnings':warnings}

def strip_trailer(src: str|Path,dst: str|Path)->None:
    with WhyBook(src) as b: keep=32+b.header['manifest_len']+b.header['text_len']+b.manifest['regions']['blob_len']
    with Path(src).open('rb') as a,Path(dst).open('wb') as z:z.write(a.read(keep))
