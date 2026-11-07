# -*- coding: utf-8 -*-
"""让 Tk 顶层窗口接收资源管理器拖入的文件/文件夹。

纯标准库（ctypes）实现，不依赖 tkinterdnd2 / tkdnd：
  * DragAcceptFiles 让窗口成为拖放目标；
  * SetWindowLongPtrW 子类化窗口过程，处理 WM_DROPFILES；
  * 其余消息原样链式转发给原来的 WNDPROC。

对外 API：
  enable_drop(widget, callback) -> bool
  is_enabled(widget) -> bool
  disable_drop(widget) -> None
  _simulate_drop(hwnd, paths) -> bool   # 仅供测试

任何失败路径都只返回 False / 静默返回，绝不抛异常、绝不影响窗口。
"""

from __future__ import annotations

import atexit
import ctypes
import os
import struct
import sys
import weakref

from ctypes import wintypes

__all__ = ["enable_drop", "is_enabled", "disable_drop", "_simulate_drop"]

_IS_WINDOWS = sys.platform == "win32"

WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4
GMEM_MOVEABLE = 0x0042
_DROPFILES_HEADER_SIZE = 20  # DWORD pFiles + POINT pt + BOOL fNC + BOOL fWide
_DRAGQUERY_ALL = 0xFFFFFFFF

# ---------------------------------------------------------------------------
# Win32 绑定（只在 Windows 上初始化；其它平台模块仍可安全 import）
# ---------------------------------------------------------------------------
_API_OK = False

if _IS_WINDOWS:  # pragma: no branch - 平台相关
    try:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
        _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        _LRESULT = ctypes.c_ssize_t
        _WPARAM = ctypes.c_size_t
        _LPARAM = ctypes.c_ssize_t
        _WNDPROC = ctypes.WINFUNCTYPE(
            _LRESULT, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM
        )

        _user32.GetParent.argtypes = [wintypes.HWND]
        _user32.GetParent.restype = wintypes.HWND
        _user32.IsWindow.argtypes = [wintypes.HWND]
        _user32.IsWindow.restype = wintypes.BOOL
        _shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
        _shell32.DragAcceptFiles.restype = None
        _user32.SendMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM
        ]
        _user32.SendMessageW.restype = _LRESULT
        _user32.CallWindowProcW.argtypes = [
            ctypes.c_void_p, wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM
        ]
        _user32.CallWindowProcW.restype = _LRESULT
        _user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, _WPARAM, _LPARAM
        ]
        _user32.DefWindowProcW.restype = _LRESULT

        if ctypes.sizeof(ctypes.c_void_p) == 8:
            _get_wndproc = _user32.GetWindowLongPtrW
            _set_wndproc = _user32.SetWindowLongPtrW
        else:  # 32 位：SetWindowLongPtrW 只是宏，部分系统不导出
            _get_wndproc = getattr(_user32, "GetWindowLongPtrW", None) \
                or _user32.GetWindowLongW
            _set_wndproc = getattr(_user32, "SetWindowLongPtrW", None) \
                or _user32.SetWindowLongW

        _get_wndproc.argtypes = [wintypes.HWND, ctypes.c_int]
        _get_wndproc.restype = ctypes.c_void_p
        _set_wndproc.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        _set_wndproc.restype = ctypes.c_void_p

        _shell32.DragQueryFileW.argtypes = [
            ctypes.c_void_p, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT
        ]
        _shell32.DragQueryFileW.restype = wintypes.UINT
        _shell32.DragFinish.argtypes = [ctypes.c_void_p]
        _shell32.DragFinish.restype = None

        _kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        _kernel32.GlobalAlloc.restype = ctypes.c_void_p
        _kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        _kernel32.GlobalLock.restype = ctypes.c_void_p
        _kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        _kernel32.GlobalUnlock.restype = wintypes.BOOL
        _kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
        _kernel32.GlobalFree.restype = ctypes.c_void_p

        _API_OK = True
    except BaseException:  # pragma: no cover - 极端环境
        _API_OK = False


def _windows() -> bool:
    """当前是否可用 Windows 拖放实现（测试可 patch 此函数）。"""
    return bool(_IS_WINDOWS and _API_OK)


# ---------------------------------------------------------------------------
# 内部状态
# ---------------------------------------------------------------------------
class _Entry:
    """一个已注册 widget 的回调条目。"""

    __slots__ = ("widget_ref", "callback", "bind_id")

    def __init__(self, widget, callback):
        self.widget_ref = weakref.ref(widget)
        self.callback = callback
        self.bind_id = None


class _DropState:
    """一个 HWND 上的子类化状态。"""

    __slots__ = ("hwnd", "old_proc", "new_proc", "entries")

    def __init__(self, hwnd, old_proc):
        self.hwnd = hwnd
        self.old_proc = old_proc
        self.new_proc = None
        self.entries = {}  # id(widget) -> _Entry


_states = {}          # hwnd -> _DropState
_widget_to_hwnd = {}  # id(widget) -> hwnd

_UPTR_MASK = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1


def _ptr(value):
    """把 ctypes 返回的指针规范化成无符号整数；空指针返回 0。"""
    if not value:
        return 0
    return int(value) & _UPTR_MASK


# ---------------------------------------------------------------------------
# 窗口过程
# ---------------------------------------------------------------------------
def _read_drop_paths(hdrop):
    """从 HDROP 读取路径列表（不负责 DragFinish）。"""
    paths = []
    try:
        count = int(_shell32.DragQueryFileW(hdrop, _DRAGQUERY_ALL, None, 0))
    except BaseException:
        count = 0
    for index in range(count):
        try:
            length = int(_shell32.DragQueryFileW(hdrop, index, None, 0))
            if length <= 0:
                continue
            buf = ctypes.create_unicode_buffer(length + 1)
            got = int(_shell32.DragQueryFileW(hdrop, index, buf, length + 1))
            if got > 0 and buf.value:
                paths.append(buf.value)
        except BaseException:
            continue
    return paths


def _dedupe(paths):
    """去重但保持拖入顺序。"""
    seen = set()
    result = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _dispatch_drop(hwnd, paths):
    state = _states.get(hwnd)
    if state is None:
        return
    for wid, entry in list(state.entries.items()):
        widget = entry.widget_ref()
        if widget is None:
            state.entries.pop(wid, None)
            _widget_to_hwnd.pop(wid, None)
            continue
        callback = entry.callback
        try:
            # 一律投递到 Tk 主线程事件队列
            widget.after(0, callback, list(paths))
        except BaseException:
            # 窗口已销毁等异常：尽量直接调用一次，绝不外抛
            try:
                callback(list(paths))
            except BaseException:
                pass


def _handle_drop(hwnd, hdrop):
    """处理 WM_DROPFILES：读路径、DragFinish、投递回调。"""
    try:
        paths = _read_drop_paths(hdrop)
    finally:
        try:
            _shell32.DragFinish(hdrop)
        except BaseException:
            pass
    paths = _dedupe(paths)
    if paths:
        _dispatch_drop(hwnd, paths)


def _make_wndproc():
    def _proc(hwnd, msg, wparam, lparam):
        try:
            hwnd_value = int(hwnd) if hwnd else 0
        except BaseException:
            hwnd_value = 0

        if msg == WM_DROPFILES:
            try:
                if wparam:
                    _handle_drop(hwnd_value, wparam)
            except BaseException:
                pass
            return 0

        state = _states.get(hwnd_value)
        old_proc = state.old_proc if state is not None else 0
        try:
            if old_proc:
                return _user32.CallWindowProcW(
                    ctypes.c_void_p(old_proc), hwnd, msg, wparam, lparam
                )
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        except BaseException:
            return 0

    return _WNDPROC(_proc)


def _install(hwnd):
    """在 hwnd 上安装子类化。成功返回 _DropState，失败返回 None。"""
    old_proc = _ptr(_get_wndproc(wintypes.HWND(hwnd), GWLP_WNDPROC))
    if not old_proc:
        return None

    state = _DropState(hwnd, old_proc)
    # 先登记再改窗口过程，避免改完瞬间来消息时找不到原过程
    _states[hwnd] = state
    try:
        proc = _make_wndproc()
        state.new_proc = proc
        new_addr = ctypes.cast(proc, ctypes.c_void_p).value
        if not new_addr:
            raise ctypes.ArgumentError("WNDPROC address is NULL")
        _set_wndproc(wintypes.HWND(hwnd), GWLP_WNDPROC, new_addr)
        current = _ptr(_get_wndproc(wintypes.HWND(hwnd), GWLP_WNDPROC))
        if current != (int(new_addr) & _UPTR_MASK):
            # 被别人抢先/失败：恢复并放弃
            try:
                _set_wndproc(wintypes.HWND(hwnd), GWLP_WNDPROC, old_proc)
            except BaseException:
                pass
            raise RuntimeError("SetWindowLongPtrW did not stick")
        _shell32.DragAcceptFiles(wintypes.HWND(hwnd), True)
        return state
    except BaseException:
        if _states.get(hwnd) is state:
            _states.pop(hwnd, None)
        try:
            if user32_is_window(hwnd):
                _set_wndproc(wintypes.HWND(hwnd), GWLP_WNDPROC, old_proc)
        except BaseException:
            pass
        return None


def user32_is_window(hwnd):
    try:
        return bool(_user32.IsWindow(wintypes.HWND(hwnd)))
    except BaseException:
        return False


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def _get_hwnd(widget):
    """取 widget 所属顶层窗口句柄：GetParent(winfo_id())，失败退回 winfo_id()。"""
    try:
        widget.update_idletasks()
    except BaseException:
        pass
    try:
        win_id = int(widget.winfo_id())
    except BaseException:
        return 0
    if not win_id:
        return 0
    try:
        parent = _user32.GetParent(wintypes.HWND(win_id))
        if parent:
            return int(parent)
    except BaseException:
        pass
    return win_id


def _drop_gesture_binding(widget):
    return lambda _event, w=widget: disable_drop(w)


def enable_drop(widget, callback) -> bool:
    """让 widget 所属顶层窗口接收资源管理器拖放。

    成功返回 True；非 Windows / 句柄不可用 / 任何异常返回 False，绝不抛出。
    同一 widget 重复调用只挂一次，并更新回调。
    """
    try:
        if not _windows() or widget is None or not callable(callback):
            return False
        hwnd = _get_hwnd(widget)
        if not hwnd or not user32_is_window(hwnd):
            return False

        wid = id(widget)
        previous_hwnd = _widget_to_hwnd.get(wid)
        if previous_hwnd is not None:
            previous_state = _states.get(previous_hwnd)
            entry = previous_state.entries.get(wid) if previous_state else None
            if entry is not None and previous_hwnd == hwnd:
                entry.callback = callback
                return True
            # 句柄变了（少见）：先清掉旧登记再重新挂
            _remove_widget(wid)

        state = _states.get(hwnd)
        if state is None:
            state = _install(hwnd)
            if state is None:
                return False

        entry = _Entry(widget, callback)
        try:
            entry.bind_id = widget.bind(
                "<Destroy>", _drop_gesture_binding(widget), add="+"
            )
        except BaseException:
            entry.bind_id = None

        state.entries[wid] = entry
        _widget_to_hwnd[wid] = hwnd
        return True
    except BaseException:
        return False


def is_enabled(widget) -> bool:
    """widget 是否已成功挂上拖放。"""
    try:
        if not _windows() or widget is None:
            return False
        wid = id(widget)
        hwnd = _widget_to_hwnd.get(wid)
        if hwnd is None:
            return False
        state = _states.get(hwnd)
        entry = state.entries.get(wid) if state else None
        if entry is None or entry.widget_ref() is None:
            _widget_to_hwnd.pop(wid, None)
            return False
        return True
    except BaseException:
        return False


def _remove_widget(wid):
    hwnd = _widget_to_hwnd.pop(wid, None)
    if hwnd is None:
        return
    state = _states.get(hwnd)
    if state is None:
        return
    entry = state.entries.pop(wid, None)
    if entry is not None and entry.bind_id:
        widget = entry.widget_ref()
        if widget is not None:
            try:
                widget.unbind("<Destroy>", entry.bind_id)
            except BaseException:
                pass
    if state.entries:
        return
    # 该窗口没有别的注册者了：恢复原窗口过程并取消拖放接受
    try:
        current = _ptr(_get_wndproc(wintypes.HWND(hwnd), GWLP_WNDPROC))
        own_addr = _ptr(ctypes.cast(state.new_proc, ctypes.c_void_p).value) \
            if state.new_proc is not None else 0
        if current and own_addr and current == own_addr:
            _set_wndproc(wintypes.HWND(hwnd), GWLP_WNDPROC, state.old_proc)
        if user32_is_window(hwnd):
            _shell32.DragAcceptFiles(wintypes.HWND(hwnd), False)
    except BaseException:
        pass
    finally:
        _states.pop(hwnd, None)


def disable_drop(widget) -> None:
    """撤销 widget 的拖放注册（若有）。绝不抛出。"""
    try:
        if widget is None:
            return
        _remove_widget(id(widget))
    except BaseException:
        pass


# ---------------------------------------------------------------------------
# 测试辅助：构造 DROPFILES 内存块并投递 WM_DROPFILES
# ---------------------------------------------------------------------------
def _build_dropfiles(paths):
    """构造 DROPFILES 结构 + 宽字符双 NUL 结尾文件列表。"""
    chunks = [struct.pack("<IiiII", _DROPFILES_HEADER_SIZE, 0, 0, 0, 1)]
    for path in paths:
        chunks.append(str(path).encode("utf-16-le", "surrogatepass") + b"\x00\x00")
    chunks.append(b"\x00\x00")
    return b"".join(chunks)


def _alloc_dropfiles(paths):
    payload = _build_dropfiles(paths)
    size = len(payload)
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        return 0
    locked = _kernel32.GlobalLock(handle)
    if not locked:
        try:
            _kernel32.GlobalFree(handle)
        except BaseException:
            pass
        return 0
    try:
        ctypes.memmove(locked, payload, size)
    finally:
        try:
            _kernel32.GlobalUnlock(handle)
        except BaseException:
            pass
    return int(handle)


def _simulate_drop(hwnd, paths) -> bool:
    """仅供测试：向 hwnd 发送一条 WM_DROPFILES 消息。

    返回是否成功发送；句柄不可用/参数非法/任何异常返回 False。
    内存块由接收方通过 DragFinish 释放，这里不重复释放。
    """
    try:
        if not _windows():
            return False
        try:
            hwnd_value = int(hwnd)
        except BaseException:
            return False
        if hwnd_value <= 0 or not user32_is_window(hwnd_value):
            return False
        if isinstance(paths, (str, bytes, os.PathLike)):
            paths = [paths]
        try:
            raw = [os.fspath(p) for p in paths]
        except BaseException:
            return False
        if not raw:
            return False
        normalized = []
        for item in raw:
            if isinstance(item, bytes):
                item = os.fsdecode(item)
            elif not isinstance(item, str):
                item = str(item)
            if item:
                normalized.append(item)
        if not normalized:
            return False
        hdrop = _alloc_dropfiles(normalized)
        if not hdrop:
            return False
        _user32.SendMessageW(
            wintypes.HWND(hwnd_value), WM_DROPFILES,
            _WPARAM(hdrop), _LPARAM(0),
        )
        return True
    except BaseException:
        return False


# ---------------------------------------------------------------------------
# 退出时尽量恢复（窗口可能已经销毁，失败静默）
# ---------------------------------------------------------------------------
def _restore_all():
    for hwnd, state in list(_states.items()):
        try:
            if not _windows() or not user32_is_window(hwnd):
                continue
            current = _ptr(_get_wndproc(wintypes.HWND(hwnd), GWLP_WNDPROC))
            own_addr = _ptr(ctypes.cast(state.new_proc, ctypes.c_void_p).value) \
                if state.new_proc is not None else 0
            if current and own_addr and current == own_addr:
                _set_wndproc(wintypes.HWND(hwnd), GWLP_WNDPROC, state.old_proc)
        except BaseException:
            continue


if _windows():
    try:
        atexit.register(_restore_all)
    except BaseException:
        pass
