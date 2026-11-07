"""统一解析工作区目录：一本书一个目录，成品和中间数据都放在里面。"""
from pathlib import Path
import json
import os
import sys

ROOT = Path(__file__).resolve().parent.parent
# 工作区大目录名：旧版本叫「运行时」，会和里面的成品混在一起看不懂，现在统一叫「产物」。
ROOT_NAME = '产物'
LEGACY_ROOT_NAME = '运行时'


def migrate_legacy(base):
    """旧版工作区大目录叫「运行时」：第一次用新版时改名成「产物」，旧稿不丢。

    改名失败（被占用/没权限）就先用旧目录继续跑，不能因为改名把程序卡住。"""
    base = Path(base)
    target = base / ROOT_NAME
    legacy = base / LEGACY_ROOT_NAME
    try:
        if not target.exists() and legacy.exists():
            legacy.rename(target)
    except OSError:
        return legacy
    return target


def program_dir():
    """程序目录：打包后 = exe 所在目录，开发时 = 仓库根；WHY_HARNESS_DIR 可覆盖。"""
    override = os.environ.get('WHY_HARNESS_DIR')
    if override:
        return Path(override).resolve()
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return ROOT


class Paths:
    def __init__(self, root=None):
        self.root = Path(root).resolve() if root else migrate_legacy(ROOT)
        self.state = self.root / '状态'
        # 成品直接落在书目录里（产物/<书>/<书>.why），不再多套一层 产物。
        self.output = self.root
        self.logs = self.root / '日志'

    def why_file(self, doc):
        """正式成品 .why 的位置（没生成时也返回约定路径）。"""
        return self.output / f'{doc}.why'

    def pack_file(self, doc):
        """只交付 Markdown 时，.why 只当中间包放在 状态/包/ 里，不算成品。"""
        return self.state / '包' / f'{doc}.why'

    def find_why(self, doc):
        """找 .why：新位置 → 中间包 → 旧版多套一层 产物/ 的老位置。"""
        for path in (self.why_file(doc), self.pack_file(doc),
                     self.output / '产物' / doc / f'{doc}.why'):
            if path.is_file():
                return path
        return self.why_file(doc)

    def state_file(self, name):
        return self.state / name

    def save(self, path, value):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')

    def load(self, name):
        return json.loads(self.state_file(name).read_text(encoding='utf-8'))
