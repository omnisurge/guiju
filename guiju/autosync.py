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
import errno
import fcntl
import os
from collections.abc import Callable
from datetime import datetime

from .errors import NeedsHuman
from .store.sqlite import Store, now

MAX_AGE_DAYS = 7
RETRY_AFTER_SECONDS = 3600
AUTO_SYNC_COMMANDS = frozenset({"search", "which", "pull", "cite"})

Notify = Callable[[str], None]





@contextlib.contextmanager
def _try_lock(store: Store, *, strict: bool = False):
    """非阻塞抢占同步锁；被别的进程占用时 yield False。

    strict=True（显式 sync）把“锁文件无法创建”升级为 needs_human(50)，不再
    冒充“另一个进程正在刷新”；进程内 autosync 默认降级为 False，避免阻断查询。
    """
    path = str(store.path) + ".lock"
    try:
        # 锁文件必须跨 yield 存活，不能用 with 打开（SIM115 在此处是误报）
        fh = open(path, "a+")  # noqa: SIM115
    except OSError as exc:
        if strict:
            raise NeedsHuman(
                f"无法创建同步锁文件：{path}（{type(exc).__name__}: {exc}）",
                hint="检查 GUIJU_HOME 或数据库所在目录是否可写；只读目录无法执行 sync",
            ) from exc
        # None 区分“锁文件无法创建”（权限/只读）与 False（已被占用）。
        yield None
        return
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if strict and exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                raise NeedsHuman(
                    f"无法锁定同步锁文件：{path}（{exc}）",
                    hint="检查锁文件权限；锁被其他进程占用时会返回 refreshing",
                ) from exc
            yield False
            return
        try:
            yield True
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# 显式 sync 与进程内 autosync 共用同一把文件锁；strict 让只读目录得到 50。
def safe_sync(store: Store):
    return _try_lock(store, strict=True)


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


def _age(store: Store, source: str) -> float | None:
    """mohurd 走无参路径，保留现有 mock/兼容；其它源按源查询。"""
    return store.catalog_age_days() if source == "mohurd" else store.catalog_age_days(source)


def _snap(
    store: Store,
    *,
    state: str,
    message: str = "",
    new: int | None = None,
    retry_after_seconds: int | None = None,
    source: str = "mohurd",
) -> dict:
    age = _age(store, source)
    meta = store.source_meta(source)
    return {
        "state": state,
        "message": message,
        "source": source,
        "complete": meta["complete"],
        "complete_reason": meta["reason"],
        "age_days": None if age is None else round(age, 2),
        "synced_at": meta["synced_at"] or None,
        "announcements": store.count_announcements(),
        "new": new,
        "retry_after_seconds": retry_after_seconds,
        "sources": [meta],
    }


def evaluate(
    store: Store,
    *,
    command: str,
    offline: bool = False,
    notify: Notify | None = None,
    source: str = "mohurd",
) -> dict:
    """判断目录是否需要刷新，需要就刷。

    返回值会**注入到每份 JSON 结果**（见 `render.set_catalog`），所以 agent 永远能知道：
    这次结果基于哪个时点的目录、有没有在刷新、失败后多久可以再试。
    状态：fresh / refreshed / refreshing / refresh_failed / stale。
    """
    age = _age(store, source)
    empty = store.count_announcements() == 0
    meta = store.source_meta(source)
    needs = empty or age is None or age > MAX_AGE_DAYS
    # 按源水位判断：别的源完整不能把本源授权成 fresh；显式不完整（0）必须重试。
    raw_complete = store.get_meta(f"sync:{source}:complete")
    legacy_complete = store.get_meta("catalog_complete") if source == "mohurd" else ""
    if ((source != "mohurd" and raw_complete != "1")
            or (source == "mohurd" and (raw_complete == "0" or (not raw_complete and legacy_complete == "0")))):
        needs = True

    if not needs:
        return _snap(store, state="fresh", source=source)

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
            source=source,
        )

    left = _cooldown_left(store)
    if left > 0:
        return _snap(
            store,
            state="refresh_failed",
            message="上次目录刷新失败，冷却中；本次结果基于现有目录",
            retry_after_seconds=left,
            source=source,
        )

    with _try_lock(store) as locked:
        if locked is None:
            return _snap(
                store,
                state="refresh_failed",
                message="无法创建同步锁文件；本次结果基于现有目录",
                retry_after_seconds=RETRY_AFTER_SECONDS,
                source=source,
            )
        if not locked:
            return _snap(
                store,
                state="refreshing",
                message="另一个进程正在更新目录；本次结果基于现有目录，建议 30 秒后重试",
                retry_after_seconds=30,
                source=source,
            )
        # 等锁期间别人可能已经同步好了 —— 复查一次再决定要不要花请求
        age = _age(store, source)
        meta = store.source_meta(source)
        raw_complete = store.get_meta(f"sync:{source}:complete")
        legacy_complete = store.get_meta("catalog_complete") if source == "mohurd" else ""
        complete_ok = meta["complete"] if source != "mohurd" else (
            raw_complete != "0" and not (not raw_complete and legacy_complete == "0")
        )
        if store.count_announcements() and age is not None and age <= MAX_AGE_DAYS and complete_ok:
            return _snap(store, state="fresh", source=source)

        from .syncengine import crawl

        if notify is not None:
            # 提示文案要用锁内的最新状态：等锁期间别的进程可能已经补了目录，
            # 这里再引用锁外的 empty 快照会把"增量同步"误报成"首次同步"。
            now_empty = store.count_announcements() == 0
            if now_empty:
                notify("目录为空，正在首次同步（33 页，约 60 秒）")
            elif age is not None:
                notify(f"目录已 {age:.1f} 天未更新，正在增量同步（约 3 秒）")
            else:
                notify("目录上次同步时间未知，正在增量同步（约 3 秒）")
        try:
            result = crawl(store, source=source)
        except Exception as exc:
            # 冷却时间戳写失败（只读库/磁盘满/并发锁）不能反过来阻断命令：
            # 刷新失败本来就要降级为"用旧目录继续"，记录失败只是锦上添花。
            with contextlib.suppress(Exception):
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
                source=source,
            )
        with contextlib.suppress(Exception):
            store.set_meta("auto_sync_failed_at", "")
            store.commit()
        return _snap(
            store,
            state="refreshed",
            message=f"目录已更新（新增 {result['new']} 条），本次结果基于新目录",
            new=result["new"],
            source=source,
        )
