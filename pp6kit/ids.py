"""19 位雪花 ID 生成器，与目标系统的 id 风格保持一致。"""
from __future__ import annotations

import threading
import time

EPOCH = 1288834974657  # 2010-11-04，与系统内既有 id 数量级一致


class IdGen:
    def __init__(self, worker: int = 1) -> None:
        self.worker = worker & 0x3FF
        self._seq = 0
        self._last = -1
        self._lock = threading.Lock()

    def next(self) -> str:
        with self._lock:
            ts = int(time.time() * 1000)
            if ts == self._last:
                self._seq = (self._seq + 1) & 0xFFF
                if self._seq == 0:
                    while ts <= self._last:
                        ts = int(time.time() * 1000)
            else:
                self._seq = 0
            self._last = ts
            return str(((ts - EPOCH) << 22) | (self.worker << 12) | self._seq)


_default = IdGen()


def new_id() -> str:
    return _default.next()


def is_snowflake(value: str) -> bool:
    return bool(value) and value.isdigit() and len(value) >= 15


_SHORT_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789"


def short_id(length: int = 7) -> str:
    """7 位小写短 id。

    系统的表单项（formRules 的 id / rule_id）用这种短 id，不是雪花 id。
    照着真实导出包生成，避免和目标系统的写法分叉。
    """
    n = int(new_id())
    # 直接把雪花 id 拆成 base36 会得到 3vpem1z / 4vpem1z 这种连号，
    # 肉眼一看就是机器批量生成的。先做一次 64 位混淆再转，看起来才像手工配的。
    n = ((n ^ (n >> 21)) * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFF
    out = []
    for _ in range(length):
        n, r = divmod(n, len(_SHORT_ALPHABET))
        out.append(_SHORT_ALPHABET[r])
    return "".join(out)
