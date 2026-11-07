"""PP-DocLayout 推理封装（模型按需下载到程序目录的 ``模型/<档位>/``）。

对外契约（与 pipeline/s2_layout.py 对接）：
    load(tier="plus-L" | "M", model_dir=None, cpu_threads=4, device="自动") -> 模型实例（单例复用）
    detect(image_path, tier=None, device="自动") -> [{"label", "score", "bbox"}]
    detect_batch(image_paths, tier=None) -> [[...], ...]

要点：
* ``paddleocr`` 只在 ``load()`` 里延迟导入，没装依赖时给出明确中文错误；
* 两个档位靠 ``model_name`` 区分（PaddleOCR 会按 inference.yml 反推模型名，
  只传 model_dir 会把 M 当 plus-L 加载，必须显式给 model_name）；
* ``device="自动"`` 优先用 CUDA 显卡（paddle 必须是 GPU 版且检测到卡），否则回落到 CPU；
  显式指定 gpu:0 / cpu 时同样带失败回落，不因为显卡问题炸掉整条流水线；
* Windows Paddle 原生接口不认中文路径：内部用 ``subst`` 映射 ASCII 盘符，
  失败才把权重复制到 %TEMP% 下的纯 ASCII 目录——**只复制一次**，同一模型目录在进程内
  和跨运行都复用同一份副本（文件大小变了才重拷），不再每次 load 都抄一遍百兆权重；
* 模型未安装时抛 ``models.ModelNotInstalled``，调用方据此降级。
"""
from __future__ import annotations

import atexit
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from . import models

_ALIAS_DRIVE = "Y:"
_alias_created = False
_lock = threading.Lock()          # 模型实例缓存
_ascii_lock = threading.Lock()    # ASCII 路径解析/复制
_ascii_cache: dict[str, str] = {}  # 原始模型目录 -> 可直接喂给 Paddle 的 ASCII 目录
_cache: dict[str, tuple[object, tuple[str, int, str]]] = {}


def _ensure_home_environment() -> None:
    """导入 paddleocr 之前，先保证 HOME / USERPROFILE 存在。

    为什么：paddleocr 的间接依赖 modelscope 在 **import 期** 就要解析用户主目录，
    HOME 与 USERPROFILE 都缺失时直接抛
    ``RuntimeError: Could not determine home directory``。
    真实用户机器一般都有，CI、沙箱、精简镜像没有；这不是 paddle 的错，纯粹是环境缺变量。
    这里只在两个变量都缺失时才兜底：把 HOME / USERPROFILE 指向临时目录，并把相关缓存
    （MODELSCOPE_CACHE / HF_HOME / HUGGINGFACE_HUB_CACHE）一起收进去，免得它们再去别处找主目录。
    用 setdefault 语义，用户已有的值一律不覆盖。
    """
    if os.environ.get("HOME") or os.environ.get("USERPROFILE"):
        return
    fallback = Path(tempfile.gettempdir()) / "why-harness-home"
    try:
        fallback.mkdir(parents=True, exist_ok=True)
    except OSError:
        return  # 临时目录都建不出来就保持原样，让下游报它自己的错
    os.environ.setdefault("HOME", str(fallback))
    os.environ.setdefault("USERPROFILE", str(fallback))
    for name in ("MODELSCOPE_CACHE", "HF_HOME", "HUGGINGFACE_HUB_CACHE"):
        os.environ.setdefault(name, str(fallback / name.lower()))
    print(f"提示：HOME/USERPROFILE 缺失，已临时指向 {fallback}（paddleocr 的 modelscope 需要主目录）")


def _subst_output() -> str:
    # errors="replace"：subst 会回显中文路径，UTF-8 模式下严格解码会让读取线程崩、stdout 变 None。
    proc = subprocess.run(["subst"], check=False, capture_output=True, text=True, errors="replace")
    return proc.stdout + proc.stderr


def _cleanup() -> None:
    global _alias_created
    if _alias_created:
        subprocess.run(["subst", _ALIAS_DRIVE, "/d"], check=False, capture_output=True)
        _alias_created = False


atexit.register(_cleanup)


def _same_weights(source_dir: Path, target_dir: Path) -> bool:
    """目标目录里三个权重是否与源目录一一对应（只比大小，够用且便宜）。"""
    try:
        return all(
            (target_dir / name).is_file()
            and (target_dir / name).stat().st_size == (source_dir / name).stat().st_size
            for name in models.REQUIRED_FILES
        )
    except OSError:
        return False


def _ascii_model_dir(model_dir: Path) -> str:
    """返回 ASCII 路径；中文路径用映射盘符或一次性临时副本绕过 Paddle 的限制。"""
    global _alias_created
    resolved = str(Path(model_dir).resolve())
    if all(ord(ch) < 128 for ch in resolved):
        return resolved
    with _ascii_lock:
        cached = _ascii_cache.get(resolved)
        if cached:
            return cached
        if _ALIAS_DRIVE not in _subst_output():
            result = subprocess.run(["subst", _ALIAS_DRIVE, resolved], check=False, capture_output=True)
            if result.returncode == 0 and _ALIAS_DRIVE in _subst_output():
                _alias_created = True
                _ascii_cache[resolved] = _ALIAS_DRIVE
                return _ALIAS_DRIVE
        # subst 不可用（盘符被占用等）：复制一次到固定目录，之后跨运行复用；权重换了大小才重拷。
        target = Path(tempfile.gettempdir()) / "why-layout-ascii" / hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:12]
        if not _same_weights(Path(resolved), target):
            target.mkdir(parents=True, exist_ok=True)
            for name in models.REQUIRED_FILES:
                shutil.copy2(Path(resolved) / name, target / name)
        _ascii_cache[resolved] = str(target)
        return str(target)


def _has_cuda_gpu() -> bool:
    """paddle 是不是 GPU 版且真的看得到显卡；任何异常都当"没有"。"""
    try:
        import paddle

        return bool(paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() > 0)
    except Exception:  # noqa: BLE001 - 没装 paddle / 没驱动都算不可用
        return False


def resolve_device(device: str | None = "自动") -> str:
    """把设置里的设备名翻译成 Paddle 认识的值。

    "自动" 只在有 CUDA 显卡时用 GPU；Intel / AMD 集显不被 paddlepaddle 支持
    （XPU 只覆盖部分 Intel 独显/数据中心卡，CUDA 只认 NVIDIA），只能走 CPU。
    """
    value = str(device or "自动").strip()
    if value.lower() in ("", "自动", "auto"):
        if _has_cuda_gpu():
            return "gpu:0"
        print("版面模型：未检测到可用的 CUDA 显卡（集显不被 paddlepaddle 支持），使用 CPU")
        return "cpu"
    return value


def load(tier: str = "plus-L", model_dir: str | Path | None = None, cpu_threads: int = 4,
         device: str | None = "自动"):
    """加载并缓存指定档位的模型。未安装时抛 models.ModelNotInstalled。"""
    # 先把 HOME/USERPROFILE 兜住：后面 _ascii_model_dir 与 paddleocr 导入都可能需要它。
    _ensure_home_environment()
    info = models.CATALOG.get(tier)
    if info is None:
        raise ValueError(f"未知版面档位: {tier}（可选: {', '.join(models.CATALOG)}）")
    if not info.get("available"):
        raise models.ModelNotInstalled(f"{tier} 档暂不支持：{info.get('note', '')}")
    folder = Path(model_dir) if model_dir else models.path(tier)
    if not models.installed(tier) and model_dir is None:
        raise models.ModelNotInstalled(
            f"版面模型未安装（{tier}，约 {info['size'] / 1e6:.0f} MB）。\n"
            f"运行：python -m why_harness 模型 下载 {tier}\n"
            f"或手动把 {', '.join(models.REQUIRED_FILES)} 放到：{folder}"
        )
    ascii_dir = _ascii_model_dir(folder)
    wanted = resolve_device(device)
    key = (ascii_dir, int(cpu_threads), wanted)
    with _lock:
        cached = _cache.get(tier)
        if cached and cached[1] == key:
            return cached[0]
        try:
            from paddleocr import LayoutDetection
        except ImportError as exc:  # noqa: BLE001
            raise models.ModelNotInstalled(
                "缺少 paddleocr/paddlepaddle，无法运行版面检测。\n"
                "安装：pip install paddlepaddle paddleocr"
            ) from exc
        model = None
        for candidate in ((wanted, "cpu") if wanted != "cpu" else ("cpu",)):
            try:
                model = LayoutDetection(
                    model_name=info["name"],
                    model_dir=ascii_dir,
                    engine="paddle_static",
                    device=candidate,
                    cpu_threads=key[1],
                )
                if candidate != wanted:
                    print(f"版面模型：{wanted} 不可用，已回落到 CPU")
                wanted = candidate
                break
            except Exception as exc:  # noqa: BLE001 - 显卡相关的失败一律回落 CPU
                if candidate == "cpu":
                    raise
                print(f"版面模型：{candidate} 初始化失败（{type(exc).__name__}: {exc}），改用 CPU")
        _cache[tier] = (model, (ascii_dir, int(cpu_threads), wanted))
    return model


def _boxes(result) -> list[dict]:
    payload = result.json
    if callable(payload):
        payload = payload()
    if isinstance(payload, str):
        import json

        payload = json.loads(payload)
    if "res" in payload:
        payload = payload["res"]
    boxes = []
    for item in payload.get("boxes", []) if isinstance(payload, dict) else []:
        coords = item.get("coordinate", item.get("bbox"))
        if coords is None or len(coords) != 4:
            continue
        x0, y0, x1, y1 = (float(v) for v in coords)
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0
        if x0 == x1 or y0 == y1:
            continue
        boxes.append({"label": str(item.get("label", "")), "score": float(item.get("score", 0.0)),
                      "bbox": [x0, y0, x1, y1]})
    return boxes


def detect(image_path: str | Path, tier: str = "plus-L", device: str | None = "自动") -> list[dict]:
    model = load(tier, device=device)
    results = model.predict(input=str(image_path), batch_size=1)
    return _boxes(results[0]) if results else []


def detect_batch(image_paths: list[str | Path], tier: str = "plus-L", device: str | None = "自动") -> list[list[dict]]:
    model = load(tier, device=device)
    results = model.predict(input=[str(p) for p in image_paths], batch_size=1)
    return [_boxes(result) for result in results]


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(description="本地 PP-DocLayout 版面检测")
    parser.add_argument("image")
    parser.add_argument("--档位", "--tier", dest="tier", default="M", choices=list(models.CATALOG))
    parser.add_argument("--设备", "--device", dest="device", default="自动")
    parser.add_argument("--json", dest="json_path")
    args = parser.parse_args(argv)
    boxes = detect(args.image, tier=args.tier, device=args.device)
    text = json.dumps(boxes, ensure_ascii=False, indent=2)
    print(text)
    if args.json_path:
        Path(args.json_path).write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
