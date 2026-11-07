"""版面模型下载与管理。

设计原则：**模型权重不进发行物**（不打进 exe / zip / wheel），由用户按需下载，
或用 `--手动` 方式自己把三个权重文件放进 models 目录。

默认位置：程序目录下的 ``模型/<档位>/`` 目录（打包后 = exe 所在目录，开发时 = 仓库根）。
可用环境变量 ``WHY_MODELS_DIR`` 覆盖根目录（测试用）。

用法：
    python -m why_harness.media.models list
    python -m why_harness.media.models download plus-L
    python -m why_harness.media.models verify plus-L
    python -m why_harness.media.models remove plus-L
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import tarfile
import urllib.request
from pathlib import Path

REQUIRED_FILES = ("inference.json", "inference.pdiparams", "inference.yml")

# 下载源候选：官方 bcebos 与 hf-mirror（国内镜像）。华为云镜像站没托管 PaddleX 权重
# （任何路径都回一张网页），所以只作 pip 源，不放进这份清单。
OFFICIAL_ROOT = "https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0"
HF_MIRROR_ROOT = "https://hf-mirror.com/PaddlePaddle"
# 环境变量 WHY_MODEL_MIRROR 指向自建/其它镜像目录时，会排在候选最前面。
MIRROR_ENV = "WHY_MODEL_MIRROR"

# 官方推理包（Paddle 3.0.0 静态图）。sha256 由本地原档实算，改地址必须同时改校验值。
CATALOG = {
    "plus-L": {
        "name": "PP-DocLayout_plus-L",
        "url": f"{OFFICIAL_ROOT}/PP-DocLayout_plus-L_infer.tar",
        "size": 130396160,
        "sha256": "80fff9c2fb598ca27380a48e733ac47ba022aaf06706ea9c39273ae63d7940dc",
        "available": True,
        "note": "精确档：20 类版面检测，CPU 约 16 秒/页",
    },
    "M": {
        "name": "PP-DocLayout-M",
        "url": f"{OFFICIAL_ROOT}/PP-DocLayout-M_infer.tar",
        "size": 23767040,
        "sha256": "f61a1839dac9dfd44fe427df84edd5635f4661e84fae9c2c34cdf9c65e5b7b5e",
        "available": True,
        "note": "快速档：CPU 约 1 秒/页，适合长篇；标题类框比精确档少",
    },
}


def _program_dir() -> Path:
    """程序目录：打包后是 exe 所在目录，开发时是仓库根；被 pip 装进 site/dist-packages 时退回用户数据目录。"""
    override = os.environ.get("WHY_HARNESS_DIR")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    base = Path(__file__).resolve().parents[2]
    if {part.lower() for part in base.parts} & {"site-packages", "dist-packages"}:
        return _user_data_dir()
    return base


def _home() -> Path:
    """主目录：HOME/USERPROFILE 都缺失时（CI、沙箱）退回系统临时目录，绝不抛。"""
    try:
        return Path.home()
    except RuntimeError:
        try:
            return Path(tempfile.gettempdir())
        except Exception:  # noqa: BLE001
            return Path.cwd()


def _user_data_dir() -> Path:
    """用户数据目录：Windows 取 %LOCALAPPDATA% 下的 pdf转，其它平台 ~/.local/share 下的 pdf转。"""
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "pdf转"
        return _home() / ".pdf转"
    return _home() / ".local" / "share" / "pdf转"


def _writable(directory: Path) -> bool:
    """目录可写：真建一个探测文件试写，比 os.access 在 Windows 上更可靠。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".why-write-probe-{os.getpid()}"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _candidates() -> tuple[Path, Path]:
    """两个候选根：程序目录\\模型 与 用户数据目录\\模型。"""
    return _program_dir() / "模型", _user_data_dir() / "模型"


def _find_models_root() -> Path:
    """查找：WHY_MODELS_DIR → 程序目录\\模型 → 用户数据目录\\模型，返回第一个已存在的；都没有返回程序目录\\模型。"""
    override = os.environ.get("WHY_MODELS_DIR")
    if override:
        return Path(override).resolve()
    program, user = _candidates()
    if program.exists():
        return program
    if user.exists():
        return user
    return program


def _write_models_root() -> Path:
    """写入目标：WHY_MODELS_DIR 优先；程序目录\\模型 的父目录可写就用它，否则退回用户数据目录并提示。"""
    override = os.environ.get("WHY_MODELS_DIR")
    if override:
        return Path(override).resolve()
    program, user = _candidates()
    if _writable(program.parent):
        return program
    print(f"程序目录不可写，模型改放到：{user}")
    return user


MODELS_ROOT = _find_models_root()


class ModelNotInstalled(RuntimeError):
    """模型目录缺文件时抛出；错误信息里带下载指引。"""


def catalog() -> list[dict]:
    """返回模型清单（含是否已安装）。"""
    rows = []
    for tier, info in CATALOG.items():
        row = dict(info)
        row["tier"] = tier
        row["installed"] = installed(tier)
        row["path"] = str(path(tier))
        rows.append(row)
    return rows


def path(tier: str) -> Path:
    if tier not in CATALOG:
        raise KeyError(f"未知档位: {tier}（可选: {', '.join(CATALOG)}）")
    return _find_models_root() / tier


def installed(tier: str) -> bool:
    folder = path(tier)
    return all((folder / name).is_file() and (folder / name).stat().st_size > 0 for name in REQUIRED_FILES)


def verify(tier: str) -> tuple[bool, str]:
    """只校验本地文件是否齐全、大小是否合理；不加载模型。"""
    folder = path(tier)
    if not folder.exists():
        return False, f"未安装：{folder}"
    missing = [name for name in REQUIRED_FILES if not (folder / name).is_file()]
    if missing:
        return False, f"缺文件: {', '.join(missing)}"
    empty = [name for name in REQUIRED_FILES if (folder / name).stat().st_size == 0]
    if empty:
        return False, f"空文件: {', '.join(empty)}"
    return True, f"就绪：{folder}（{sum((folder / n).stat().st_size for n in REQUIRED_FILES) / 1e6:.1f} MB）"


def _sha256_file(file: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with file.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def sources(tier: str) -> list[str]:
    """该档位的下载源候选；真正用哪个由 _rank_sources() 测速决定。

    - WHY_MODEL_MIRROR 环境变量指向的目录（自建镜像）排第一；
    - hf-mirror：HuggingFace 国内镜像，PaddlePaddle 官方组织的权重也在上面；
    - 官方 bcebos 兜底。
    华为云镜像站没托管 PaddleX 权重（任何路径都回一张网页），所以只作 pip 源，不在这里。
    """
    info = CATALOG[tier]
    name = info["name"]
    candidates = []
    custom = (os.environ.get(MIRROR_ENV) or "").strip()
    if custom:
        candidates.append(custom.rstrip("/") + "/" + name + "_infer.tar")
    candidates.append(f"{HF_MIRROR_ROOT}/{name}/resolve/main/{name}_infer.tar")
    candidates.append(info["url"])
    return candidates


def _looks_like_webpage(block: bytes) -> bool:
    """镜像站没有这个文件时通常回一张 HTML 页（200），要当成下载失败而不是权重。"""
    head = block[:512].lstrip().lower()
    return head.startswith(b"<!doctype") or head.startswith(b"<html")


def _probe_speed(url: str, timeout: float = 6.0, sample: int = 512 * 1024) -> float:
    """抓一小段估速度（字节/秒）；拿不到、或回的是网页，都返回 0 = 这个源不可用。"""
    import time

    request = urllib.request.Request(url, headers={"Range": f"bytes=0-{sample - 1}"})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if "text/html" in str(response.headers.get("Content-Type") or "").lower():
                return 0.0
            block = response.read(sample)
    except Exception:  # noqa: BLE001 - 探测失败等于这个源不可用
        return 0.0
    elapsed = max(time.perf_counter() - started, 0.05)
    if len(block) < 1024 or _looks_like_webpage(block):
        return 0.0
    return len(block) / elapsed


def _rank_sources(tier: str) -> list[str]:
    """给候选源测速，快的排前面；全都不通就保持原顺序，让真正的下载去报错。"""
    ranked = [(speed, url) for url in sources(tier) if (speed := _probe_speed(url)) > 0]
    if not ranked:
        return sources(tier)
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    print(f"下载源测速：选用 {ranked[0][1]}（约 {ranked[0][0] / 1024:.0f} KB/s）")
    return [url for _speed, url in ranked]


# 逐百分比输出的状态（每次下载前在 ensure() 里重置）。
_PROGRESS = {"percent": None}


def _download(url: str, target: Path, expected_size: int | None, progress=None) -> None:
    """下载到 target，支持 Range 续传；已存在的部分会被复用。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    have = target.stat().st_size if target.exists() else 0
    request = urllib.request.Request(url)
    if have:
        request.add_header("Range", f"bytes={have}-")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            mode = "ab" if getattr(response, "status", 200) == 206 else "wb"
            if mode == "wb":
                have = 0
            total = expected_size or have + int(response.headers.get("Content-Length") or 0)
            with target.open(mode) as fh:
                while True:
                    block = response.read(1 << 20)
                    if not block:
                        break
                    fh.write(block)
                    have += len(block)
                    if progress:
                        progress(have, total)
    except Exception as exc:  # noqa: BLE001 - 网络异常统一转中文提示
        raise RuntimeError(
            f"下载失败：{exc}\n可手工下载后用「把三个文件放进目录」的方式安装：\n  {url}\n  放到：{path_for_hint(target)}\n"
        ) from exc


def path_for_hint(target: Path) -> Path:
    """下载失败提示里展示最终模型目录。"""
    return target.parent / target.name.removesuffix(".part")


def _extract(archive: Path, dest: Path) -> Path:
    """解包到 dest，返回含三个权重文件的目录（防路径穿越）。"""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            member_path = (dest / member.name).resolve()
            if not str(member_path).startswith(str(dest.resolve())):
                raise ValueError(f"压缩包内路径非法: {member.name}")
        tar.extractall(dest, filter="data")
    for folder, _dirs, files in os.walk(dest):
        if all(name in files for name in REQUIRED_FILES):
            return Path(folder)
    raise ValueError(f"压缩包里找不到 {' / '.join(REQUIRED_FILES)}")


def _stage(on_stage, name: str) -> None:
    """阶段回调：进度是旁路，回调抛异常不能拖垮下载。"""
    if on_stage is None:
        return
    try:
        on_stage(name)
    except Exception:  # noqa: BLE001 - 进度回调出错不影响产物
        pass


def ensure(tier: str, progress=None, on_stage=None) -> Path:
    """确保模型已安装：没有就下载→校验→解包→原子改名。

    progress(已完成字节, 总字节)：下载过程中连续回调；
    on_stage(阶段)：进入"校验"/"解包"时各回调一次，GUI 用来显示当前在干什么。"""
    if tier not in CATALOG:
        raise KeyError(f"未知档位: {tier}")
    info = CATALOG[tier]
    if not info.get("available"):
        raise NotImplementedError(f"{tier} 档暂不支持下载：{info.get('note', '')}")
    if installed(tier):
        return path(tier)

    final = _write_models_root() / tier
    part = final.with_name(final.name + ".part")
    tmp = final.with_name(final.name + ".tmp")
    _PROGRESS["percent"] = None
    # 多个下载源：按测速从快到慢试，哪个先下完且 sha256 对得上就用哪个。
    # 每换一个源都从零开始（断点续传的前提是同一个文件，跨源不能拼）。
    problem = None
    for url in _rank_sources(tier):
        part.unlink(missing_ok=True)
        _PROGRESS["percent"] = None
        try:
            _download(url, part, info["size"], progress)
            _stage(on_stage, "校验")
            if info.get("sha256"):
                actual = _sha256_file(part)
                if actual != info["sha256"]:
                    raise ValueError(f"sha256 不符（期望 {info['sha256'][:12]}…，实际 {actual[:12]}…）")
            break
        except Exception as exc:  # noqa: BLE001 - 换下一个源继续
            problem = exc
            print(f"下载源不可用，换下一个：{url}\n  原因：{exc}")
    else:
        part.unlink(missing_ok=True)
        raise RuntimeError(f"所有下载源都失败：{problem}")

    if tmp.exists():
        shutil.rmtree(tmp)
    _stage(on_stage, "解包")
    found = _extract(part, tmp)
    final.mkdir(parents=True, exist_ok=True)
    for name in REQUIRED_FILES:
        os.replace(found / name, final / name)
    shutil.rmtree(tmp, ignore_errors=True)
    part.unlink(missing_ok=True)
    if not all((final / name).is_file() and (final / name).stat().st_size > 0 for name in REQUIRED_FILES):
        raise ModelNotInstalled(f"安装后仍缺文件：{final}")
    return final


def line_progress(stream=None):
    """返回 on_progress(阶段, 已完成字节, 总字节) 回调：逐百分比打一行。

    一行一个「下载进度  42%  已下  55.1/130.4 MB」，flush 后终端立刻看得见，
    重定向到管道 / GUI 子进程按行读也不会粘在一起。传 stream 可改输出目标。"""
    state = {"percent": None}

    def report(stage, done, total) -> None:
        out = sys.stdout if stream is None else stream
        try:
            done = int(done or 0)
            total = int(total or 0)
        except (TypeError, ValueError):
            return
        if stage == "下载":
            if total <= 0:
                print(f"已下 {done / 1e6:.1f} MB", file=out, flush=True)
                return
            percent = min(100, int(done * 100 / total))
            if percent == state["percent"]:
                return
            state["percent"] = percent
            print(f"下载进度 {percent:3d}%  已下 {done / 1e6:.1f}/{total / 1e6:.1f} MB", file=out, flush=True)
            return
        text = {"校验": "校验模型文件…", "解包": "解包并安装模型…"}.get(str(stage))
        if text:
            print(text, file=out, flush=True)

    return report


def _emit(on_progress, stage: str, done: int, total: int) -> None:
    """安全回调：on_progress 是旁路，抛异常一律吞掉。"""
    if on_progress is None:
        return
    try:
        on_progress(stage, int(done), int(total))
    except Exception:  # noqa: BLE001 - 进度回调出错不影响下载
        pass


def _retry_hint(tier: str, reason) -> str:
    """下载失败的中文指引：手动下载命令 + 模型该放的目录。"""
    try:
        folder = _write_models_root() / tier
    except OSError:
        folder = path(tier)
    return (
        f"模型下载失败（{tier}）：{reason}\n"
        f"手动安装：\n"
        f"  1) 重新下载：python -m why_harness 模型 download {tier}（会自动挑最快的源）\n"
        f"  2) 或手工下载后，把 {'、'.join(REQUIRED_FILES)} 三个文件放进：{folder}"
    )


def ensure_available(tier: str, allow_download: bool = True, on_progress=None) -> bool:
    """模型就绪返回 True；缺失时按 allow_download 决定是否自动下载。

    - 已装：直接返回 True，不触发任何下载；
    - 未装且 allow_download=True：复用 ensure() 下载，边下边回调
      on_progress(阶段, 已完成字节, 总字节)，阶段取 "开始"/"下载"/"校验"/"解包"/"完成"；
      不传 on_progress 时用 line_progress() 逐百分比打到 stdout；
    - 下完再用 installed() 判一次，仍不齐也算失败；
    - 任一步失败抛 ModelNotInstalled，消息带手动下载命令与模型应放目录；
    - allow_download=False 且未装：返回 False，不抛也不下载。
    """
    if tier not in CATALOG:
        raise KeyError(f"未知档位: {tier}（可选: {', '.join(CATALOG)}）")
    if installed(tier):
        return True
    if not allow_download:
        return False
    report = on_progress if on_progress is not None else line_progress()
    info = CATALOG[tier]
    total = int(info.get("size") or 0)
    _emit(report, "开始", 0, total)
    try:
        ensure(tier, progress=lambda done, size: _emit(report, "下载", done, size),
               on_stage=lambda name: _emit(report, name, total, total))
    except Exception as exc:  # noqa: BLE001 - 统一包装成带手动指引的缺依赖异常
        raise ModelNotInstalled(_retry_hint(tier, exc)) from exc
    if not installed(tier):
        raise ModelNotInstalled(_retry_hint(tier, "下载完成但模型文件仍不齐"))
    _emit(report, "完成", total, total)
    return True


def remove(tier: str) -> None:
    folder = path(tier)
    if folder.exists():
        shutil.rmtree(folder)
        print(f"已删除：{folder}")
    else:
        print(f"本来就未安装：{folder}")


def _progress(done: int, total: int) -> None:
    """`模型 download` 的进度：逐百分比打一行，flush 让终端与子进程都能立刻读到。"""
    if not total:
        return
    percent = min(100, int(done * 100 / total))
    if percent == _PROGRESS["percent"]:
        return
    _PROGRESS["percent"] = percent
    print(f"下载进度 {percent:3d}%  已下 {done / 1e6:.1f}/{total / 1e6:.1f} MB", flush=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd, *rest = argv
    tier = rest[0] if rest else ""
    if cmd == "list":
        for row in catalog():
            mark = "已安装" if row["installed"] else ("可下载" if row["available"] else "不可用")
            print(f"{row['tier']:<7} {row['name']:<20} {mark:<4} {row['note']}")
        print(f"\n程序目录：{_program_dir()}")
        print(f"模型目录：{MODELS_ROOT}")
        return 0
    if cmd == "verify":
        ok, message = verify(tier)
        print(message)
        return 0 if ok else 1
    if cmd in ("download", "install"):
        folder = ensure(tier, progress=_progress)
        print(f"\n完成：{folder}")
        return 0
    if cmd == "remove":
        remove(tier)
        return 0
    print(f"未知子命令：{cmd}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
