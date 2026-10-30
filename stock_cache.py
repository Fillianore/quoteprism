# -*- coding: utf-8 -*-
"""长期运行友好的取数基础设施。

设计目标：**尽量少请求、失败不硬刚、永远有东西可看**。

三层防护：
  1. 磁盘缓存（DiskCache）：K线/资金流/分时这类"短时间不会变"的数据，
     在 TTL 内直接用缓存，跨进程重启也有效。
  2. 限速器（RateLimiter）：每个数据源强制最小请求间隔，避免固定节奏"敲门"。
  3. 自适应退避（Backoff）：连续失败后指数级拉长间隔，成功一次立刻恢复。

只依赖标准库。
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
from typing import Any, Dict, Optional


class DiskCache:
    """落地到磁盘的 KV 缓存，带 TTL 与容量上限。

    目录结构：<base>/cache/<hash>.json，内容 {"at": 时间戳, "data": ...}
    """

    def __init__(self, base_dir: str, max_entries: int = 400) -> None:
        self.dir = os.path.join(base_dir, "cache")
        self.max_entries = max_entries
        self._lock = threading.Lock()
        try:
            os.makedirs(self.dir, exist_ok=True)
        except Exception:
            pass

    def __bool__(self) -> bool:
        return True

    def _path(self, key: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in key)
        return os.path.join(self.dir, f"{safe[:80]}.json")

    def get(self, key: str, ttl: Optional[float] = None) -> Optional[Any]:
        """取缓存；ttl 内才算有效。返回 (data, age_seconds) 或 None。"""
        path = self._path(key)
        try:
            with self._lock, open(path, encoding="utf-8") as fh:
                blob = json.load(fh)
            at = float(blob.get("at") or 0)
            age = time.time() - at
            if ttl is not None and age > ttl:
                return None
            return blob.get("data"), age
        except Exception:
            return None

    def get_stale(self, key: str) -> Optional[Any]:
        """不管 TTL，取最近一次成功的数据（用于降级展示）。"""
        return self.get(key, ttl=None)

    def put(self, key: str, data: Any) -> None:
        path = self._path(key)
        blob = {"at": time.time(), "data": data}
        try:
            with self._lock:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(blob, fh, ensure_ascii=False)
                os.replace(tmp, path)
            self._prune()
        except Exception:
            pass

    def _prune(self) -> None:
        """超过上限时删掉最旧的文件。"""
        try:
            files = [(os.path.getmtime(os.path.join(self.dir, n)), n)
                     for n in os.listdir(self.dir) if n.endswith(".json")]
            if len(files) <= self.max_entries:
                return
            files.sort()
            for _mtime, name in files[: len(files) - self.max_entries]:
                try:
                    os.remove(os.path.join(self.dir, name))
                except Exception:
                    pass
        except Exception:
            pass


class RateLimiter:
    """按数据源限制请求频率 + 连续失败后的指数退避。

    用法：
        if not limiter.allow("eastmoney"):  # 还没到下次允许时间
            ...
        try: ...; limiter.success("eastmoney")
        except: limiter.failure("eastmoney")
    """

    def __init__(self, min_interval: float = 0.8, max_backoff: float = 600.0) -> None:
        self.min_interval = min_interval
        self.max_backoff = max_backoff
        self._next: Dict[str, float] = {}
        self._fails: Dict[str, int] = {}
        self._lock = threading.Lock()

    def allow(self, source: str) -> bool:
        with self._lock:
            return time.time() >= self._next.get(source, 0.0)

    def wait_seconds(self, source: str) -> float:
        with self._lock:
            return max(0.0, self._next.get(source, 0.0) - time.time())

    def success(self, source: str) -> None:
        with self._lock:
            self._fails[source] = 0
            self._next[source] = time.time() + self.min_interval

    def failure(self, source: str, factor: float = 2.0) -> float:
        """记一次失败并返回本次退避秒数。"""
        with self._lock:
            fails = self._fails.get(source, 0) + 1
            self._fails[source] = fails
            backoff = min(self.max_backoff, self.min_interval * (factor ** fails))
            backoff *= 0.85 + random.random() * 0.3      # 抖动，避免多实例同频
            self._next[source] = time.time() + backoff
            return backoff

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "fails": dict(self._fails),
                "wait": {k: round(max(0.0, v - time.time()), 1)
                         for k, v in self._next.items() if v > time.time()},
            }


def jittered(interval: float, ratio: float = 0.15) -> float:
    """给刷新间隔加抖动，避免每秒固定时刻请求（更像人、更少被判定为爬虫）。"""
    delta = interval * ratio
    return max(0.5, interval + random.uniform(-delta, delta))
