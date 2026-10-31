"""进程内目录保鲜 —— 不依赖 cron / launchd / 常驻进程。

所有者要求：定时任务不能挂在设备上，必须由程序自己维持目录新鲜。

三条规则：
  1. 只在**需要目录**的命令上触发（search / which / pull / cite），其它命令零副作用
  2. 目录新鲜（非空且 ≤7 天）→ 0 请求直接返回
  3. 刷新失败 → 记录时间戳，1 小时内不再尝试（否则每条命令都去撞墙）

并发：用非阻塞 flock。抢不到锁说明有别的进程正在同步，本次直接跳过并用现有目录继续。
失败永不阻断命令：目录旧一点，也好过让一次"查规范"变成网络等待。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Callable
from datetime import datetime

from .store.sqlite import Store, now

MAX_AGE_DAYS = 7
RETRY_AFTER_SECONDS = 3600
AUTO_SYNC_COMMANDS = frozenset({"search", "which", "pull", "cite"})

Notify = Callable[[str], None]


def _cooldown_active(store: Store) -> bool:
    raw = store.get_meta("auto_sync_failed_at")
    if not raw:
        return False
    try:
        last = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (datetime.now() - last).total_seconds() < RETRY_AFTER_SECONDS


@contextlib.contextmanager
def _try_lock(store: Store):
    """非阻塞抢占同步锁；被别的进程占用时 yield False。"""
    path = str(store.path) + ".lock"
    try:
        # 锁文件必须跨 yield 存活，不能用 with 打开（SIM115 在此处是误报）
        fh = open(path, "a+")  # noqa: SIM115
    except OSError:
        yield False
        return
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def maybe_refresh(
    store: Store,
    *,
    command: str,
    offline: bool = False,
    notify: Notify | None = None,
) -> str | None:
    """需要在命令执行前刷新就刷新。返回一句给人看的说明（或 None）。"""
    if offline or os.environ.get("GUIJU_NO_AUTO_SYNC") == "1":
        return None
    if command not in AUTO_SYNC_COMMANDS:
        return None

    age = store.catalog_age_days()
    empty = store.count_announcements() == 0
    if not empty and age is not None and age <= MAX_AGE_DAYS:
        return None
    if _cooldown_active(store):
        return None

    with _try_lock(store) as locked:
        if not locked:
            return "另一个进程正在同步目录，本次使用现有目录继续"
        # 等锁期间别人可能已经同步好了 —— 复查一次再决定要不要花请求
        age = store.catalog_age_days()
        if store.count_announcements() and age is not None and age <= MAX_AGE_DAYS:
            return None

        from .syncengine import crawl

        if notify is not None:
            if empty:
                notify("目录为空，正在首次同步（33 页，约 60 秒）")
            else:
                notify(f"目录已 {age:.1f} 天未更新，正在增量同步（约 3 秒）")
        try:
            result = crawl(store)
        except Exception as exc:
            store.set_meta("auto_sync_failed_at", now())
            store.commit()
            return f"目录刷新失败（{type(exc).__name__}），按现有目录继续；{RETRY_AFTER_SECONDS // 60} 分钟内不再重试"
        store.set_meta("auto_sync_failed_at", "")
        store.commit()
        return f"目录已更新：新增 {result['new']} 条（库中 {result['total_in_db']} 条）"
