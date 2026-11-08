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


def _cooldown_left(store: Store) -> int:
    raw = store.get_meta("auto_sync_failed_at")
    if not raw:
        return 0
    try:
        last = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    left = RETRY_AFTER_SECONDS - (datetime.now() - last).total_seconds()
    return max(0, int(left))


def _snap(
    store: Store,
    *,
    state: str,
    message: str = "",
    new: int | None = None,
    retry_after_seconds: int | None = None,
) -> dict:
    age = store.catalog_age_days()
    return {
        "state": state,
        "message": message,
        "age_days": None if age is None else round(age, 2),
        "synced_at": store.get_meta("synced_at") or None,
        "announcements": store.count_announcements(),
        "new": new,
        "retry_after_seconds": retry_after_seconds,
    }


def evaluate(
    store: Store,
    *,
    command: str,
    offline: bool = False,
    notify: Notify | None = None,
) -> dict:
    """判断目录是否需要刷新，需要就刷。

    返回值会**注入到每份 JSON 结果**（见 `render.set_catalog`），所以 agent 永远能知道：
    这次结果基于哪个时点的目录、有没有在刷新、失败后多久可以再试。
    状态：fresh / refreshed / refreshing / refresh_failed / stale。
    """
    age = store.catalog_age_days()
    empty = store.count_announcements() == 0
    needs = empty or age is None or age > MAX_AGE_DAYS

    if not needs:
        return _snap(store, state="fresh")

    if offline or os.environ.get("GUIJU_NO_AUTO_SYNC") == "1" or command not in AUTO_SYNC_COMMANDS:
        if offline:
            why = "离线模式"
        elif os.environ.get("GUIJU_NO_AUTO_SYNC") == "1":
            why = "自动刷新已禁用（GUIJU_NO_AUTO_SYNC=1）"
        else:
            why = "该命令不触发表更新"
        if empty:
            what = "目录为空"
        elif age is None:
            what = "目录从未记录同步时间"
        else:
            what = f"目录已 {age:.1f} 天未更新"
        return _snap(
            store,
            state="stale",
            message=f"{why}；{what}，本次结果可能不含最新公告（需要时跑 guiju sync）",
        )

    left = _cooldown_left(store)
    if left > 0:
        return _snap(
            store,
            state="refresh_failed",
            message="上次目录刷新失败，冷却中；本次结果基于现有目录",
            retry_after_seconds=left,
        )

    with _try_lock(store) as locked:
        if not locked:
            return _snap(
                store,
                state="refreshing",
                message="另一个进程正在更新目录；本次结果基于现有目录，建议 30 秒后重试",
                retry_after_seconds=30,
            )
        # 等锁期间别人可能已经同步好了 —— 复查一次再决定要不要花请求
        age = store.catalog_age_days()
        if store.count_announcements() and age is not None and age <= MAX_AGE_DAYS:
            return _snap(store, state="fresh")

        from .syncengine import crawl

        if notify is not None:
            if empty:
                notify("目录为空，正在首次同步（33 页，约 60 秒）")
            elif age is not None:
                notify(f"目录已 {age:.1f} 天未更新，正在增量同步（约 3 秒）")
            else:
                notify("目录上次同步时间未知，正在增量同步（约 3 秒）")
        try:
            result = crawl(store)
        except Exception as exc:
            store.set_meta("auto_sync_failed_at", now())
            store.commit()
            return _snap(
                store,
                state="refresh_failed",
                message=(
                    f"目录刷新失败（{type(exc).__name__}）；本次结果基于现有目录，"
                    f"{RETRY_AFTER_SECONDS // 60} 分钟后会自动再试"
                ),
                retry_after_seconds=RETRY_AFTER_SECONDS,
            )
        store.set_meta("auto_sync_failed_at", "")
        store.commit()
        return _snap(
            store,
            state="refreshed",
            message=f"目录已更新（新增 {result['new']} 条），本次结果基于新目录",
            new=result["new"],
        )
