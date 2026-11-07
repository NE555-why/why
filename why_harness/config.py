"""读取中文键值配置，支持工作区配置覆盖模板。"""
from pathlib import Path
import os

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT = PACKAGE_DIR / '设置.txt'


def load(workspace, explicit=None):
    workspace = Path(workspace).resolve()
    candidates = [Path(explicit).resolve()] if explicit else [workspace / '设置.txt', DEFAULT]
    selected = next((p for p in candidates if p.exists()), DEFAULT)
    target = workspace / '设置.txt'
    if not target.exists() and selected == DEFAULT:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(DEFAULT.read_text(encoding='utf-8'), encoding='utf-8')
    data = {}
    section = ''
    for raw in selected.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or line.startswith(';'):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1].strip()
            continue
        if '=' in line:
            key, value = (x.strip() for x in line.split('=', 1))
            data[f'{section}.{key}'] = value
    return data


def get(data, section, key, default=None):
    value = data.get(f'{section}.{key}', default)
    if isinstance(value, str):
        low = value.lower()
        if low in ('true', '是', 'yes'): return True
        if low in ('false', '否', 'no'): return False
    return value


def float_value(data, section, key, default):
    try: return float(get(data, section, key, default))
    except (TypeError, ValueError): return default


def flag(data, section, key, default=True):
    """开关值：true/是/1 算开，false/否/0 算关；写错或不写都用默认。"""
    value = get(data, section, key, default)
    if isinstance(value, bool):
        return value
    text = str(value or '').strip().lower()
    if text in ('1', 'true', 'yes', 'on', '是', '开', '真'):
        return True
    if text in ('0', 'false', 'no', 'off', '否', '关', '假', ''):
        return False
    return default


def choice(data, section, key, choices, default):
    """枚举值：不在候选里（写错/不写）就用默认，保证不会因为一个错别字跑不通。"""
    value = str(get(data, section, key, default) or '').strip().lower()
    return value if value in choices else default