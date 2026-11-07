"""可调上限的工作池：运行中实时调整并发数，只用标准库。

TunablePool(limit) 用 submit / map 提交任务（结果用 concurrent.futures.Future 返回），
set_limit(n) 可在任意线程调用：调大逐步补 worker，调小让多出来的 worker 在
当前任务做完后退出（不打断正在跑的任务、不丢任务、不重复执行）。with 退出时等所有任务完成。

控制通道：tunable_pool 每秒读一次 <工作区>/状态/并发.txt；内容是 >=1 的整数就作为
新上限并打印"并发 A → B（来自 状态\\并发.txt）"；不存在或内容非法就忽略（不报错）。
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import queue
import threading
import time
from pathlib import Path

# 队列里的退出哨兵：worker 拿到它就去顶部重新判断是否退出（shutdown 或 shrink）。
_EXIT = object()


class TunablePool:
    """可调并发上限的工作池（线程安全，内部 queue.Queue + 一把锁）。"""

    def __init__(self, limit: int = 4):
        self._cond = threading.Condition()      # 一把锁（RLock）+ 条件变量
        self._limit = max(1, int(limit))
        self._queue = queue.Queue()
        self._workers = []                       # 当前存活的 worker 线程
        self._pending = 0                        # 已提交未完成的任务数
        self._closed = False                     # close() 后不再接受 submit
        self._shutdown = False                   # 任务清空后通知 worker 全部退出
        self._next_worker_id = 0
        for _ in range(self._limit):
            self._spawn_worker()

    @property
    def limit(self):
        with self._cond:
            return self._limit

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        with self._cond:
            if self._closed:
                raise RuntimeError('TunablePool 已关闭，不能再提交任务')
            self._pending += 1
            self._queue.put((fn, args, kwargs, future))
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        """与 ThreadPoolExecutor.map 同语义：按输入顺序返回结果，出错时在迭代处抛出。"""
        if chunksize < 1:
            raise ValueError('chunksize 必须 >= 1')
        futures = [self.submit(fn, *args) for args in zip(*iterables)]
        if timeout is None:
            return (f.result() for f in futures)
        deadline = time.monotonic() + timeout
        return (f.result(max(0.0, deadline - time.monotonic())) for f in futures)

    def set_limit(self, n):
        """任意线程可调。调大逐步补 worker；调小发退出哨兵让多余 worker 收尾后退出。"""
        n = max(1, int(n))
        with self._cond:
            if self._shutdown or n == self._limit:
                return
            self._limit = n
            deficit = n - len(self._workers)
        if deficit > 0:
            for _ in range(deficit):
                self._spawn_worker()
        elif deficit < 0:
            for _ in range(-deficit):
                self._queue.put(_EXIT)

    def _spawn_worker(self):
        with self._cond:
            if self._shutdown or self._closed or len(self._workers) >= self._limit:
                return
            self._next_worker_id += 1
            worker = threading.Thread(target=self._worker_loop,
                                      name=f'TunablePool-{self._next_worker_id}',
                                      daemon=True)
            self._workers.append(worker)
        worker.start()

    def _worker_loop(self):
        while True:
            # 取任务前先判断：shutdown 或超员（shrink）就退出。超员退出发生在
            # "当前任务做完后"——只有做完一个任务才会回到这里。
            with self._cond:
                if self._shutdown or self._limit < len(self._workers):
                    try:
                        self._workers.remove(threading.current_thread())
                    except ValueError:
                        pass
                    return
            item = self._queue.get()
            if item is _EXIT:            # 退出哨兵：回到顶部重新判断
                continue
            fn, args, kwargs, future = item
            if not future.set_running_or_notify_cancel():
                with self._cond:
                    self._pending -= 1
                    self._cond.notify_all()
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)
            finally:
                with self._cond:
                    self._pending -= 1
                    self._cond.notify_all()

    def close(self):
        """停止接受新任务，等已提交任务全部完成，再让 worker 退出。幂等。"""
        with self._cond:
            if self._closed:
                return
            self._closed = True
        with self._cond:
            while self._pending > 0:
                self._cond.wait()
            self._shutdown = True
            n = len(self._workers)
        for _ in range(n):
            self._queue.put(_EXIT)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def read_concurrency(path):
    """读 状态/并发.txt：返回 >=1 的整数；文件不存在或内容非法返回 None（不报错）。"""
    try:
        text = Path(path).read_text(encoding='utf-8').strip()
    except OSError:
        return None
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None
    return value if value >= 1 else None


def _watch(pool, path, stop, interval):
    current = pool.limit
    while not stop.is_set():
        value = read_concurrency(path)
        if value is not None and value != current:
            old, current = current, value
            pool.set_limit(value)
            print(f'并发 {old} → {value}（来自 状态\\并发.txt）')
        stop.wait(interval)


@contextlib.contextmanager
def tunable_pool(limit, control_path=None, interval=1.0):
    """可调并发池：给了 control_path 就每秒读一次控制文件。退出时等任务完成。"""
    pool = TunablePool(limit)
    stop = threading.Event()
    if control_path is not None:
        threading.Thread(target=_watch, args=(pool, control_path, stop, interval),
                         daemon=True).start()
    try:
        yield pool
    finally:
        stop.set()
        pool.close()
