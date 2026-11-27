"""应用层 agent：使用态契约展示与跨 Harness Skill 安全安装/升级/认领/卸载编排。

零隐式存储构造与环境目录回退；AgentReceiptStore、cwd 与 user_home 均由调用方显式注入。
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import NoReturn

from .._version import __version__
from ..agent_assets import (
    load_history_hashes,
    read_contract_markdown,
    read_skill_frontmatter,
)
from ..domain.agent_assets import (
    SKILL_DIR_NAME,
    DiskSnapshot,
    ReceiptRecord,
    derive_managed_files,
    hash_managed_files,
    inspect_target_status,
    plan_install,
    plan_uninstall,
    sha256_bytes,
    sha256_text,
)
from ..errors import AgentConflict, BadUsage, NeedsHuman
from ..store.agent_receipts import AgentReceiptStore, atomic_write_managed_file

_SHOW_KINDS = frozenset({"contract", "skill"})
_CLI_TARGETS = frozenset({"agents", "claude"})
_CLI_SCOPES = frozenset({"project", "user"})

_CONFLICT_HINTS: dict[str, str] = {
    "agent_symlink_rejected": (
        "目标路径或内部条目包含符号链接；出于安全边界已拒绝操作，请先人工移除符号链接"
        "（--force 不会顺着符号链接写入或删除）"
    ),
    "agent_receipt_corrupt": (
        "收据文件格式或校验失败；可运行 guiju agent install --force 自动备份损坏收据后重建"
        "（status 与 uninstall 始终拒绝直接覆写）"
    ),
    "agent_unmanaged_conflict": (
        "目标目录已存在未受管文件且不匹配任何官方版本；确认无需保留后可显式传 --force 清理覆盖"
    ),
    "agent_user_modified": (
        "受管技能目录存在本地改动、额外文件或未知版本内容；核对变更后如需重装/清理受管文件请显式传 --force"
    ),
    "agent_not_managed": (
        "目标未在收据中登记且不匹配官方资产哈希；即使传入 --force 也绝不会自动删除未受管内容"
    ),
}


def _raise_conflict(
    reason: str,
    *,
    target_dir: Path,
    receipt_path: Path,
) -> NoReturn:
    messages = {
        "agent_symlink_rejected": f"目标路径命中符号链接，拒绝操作：{target_dir}",
        "agent_receipt_corrupt": f"受管技能收据已损坏：{receipt_path}",
        "agent_unmanaged_conflict": f"目标目录存在未受管冲突，拒绝覆盖：{target_dir}",
        "agent_user_modified": f"受管技能目录已被手工修改或包含额外文件：{target_dir}",
        "agent_not_managed": f"目标不是干净或已登记的受管技能目录，拒绝卸载：{target_dir}",
    }
    raise AgentConflict(
        messages.get(reason, f"受管技能操作冲突（{reason}）：{target_dir}"),
        reason=reason,
        hint=_CONFLICT_HINTS[reason],
        path=str(target_dir),
        receipt_path=str(receipt_path),
    )


def _resolve_parent_checked(raw_parent: Path) -> Path:
    try:
        return raw_parent.resolve()
    except RuntimeError as exc:
        raise AgentConflict(
            f"目标路径命中符号链接环，拒绝操作：{raw_parent}",
            reason="agent_symlink_rejected",
            hint=_CONFLICT_HINTS["agent_symlink_rejected"],
            path=str(raw_parent / SKILL_DIR_NAME),
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise AgentConflict(
                f"目标路径命中符号链接环，拒绝操作：{raw_parent}",
                reason="agent_symlink_rejected",
                hint=_CONFLICT_HINTS["agent_symlink_rejected"],
                path=str(raw_parent / SKILL_DIR_NAME),
            ) from exc
        raise NeedsHuman(
            f"无法解析目标技能父目录：{raw_parent}（{type(exc).__name__}: {exc}）",
            hint="检查目标目录及父目录权限后重试",
        ) from exc


def _resolve_location(
    *,
    target: str | None,
    scope: str | None,
    dir_path: str | None,
    cwd: Path,
    user_home: Path,
) -> tuple[str, str, Path]:
    if dir_path is not None and (target is not None or scope is not None):
        raise BadUsage(
            "--dir 与 --target / --scope 互斥，不能同时指定",
            hint="自定义目录请仅传 --dir PATH；否则改用 --target / --scope",
        )
    if dir_path is not None:
        if not isinstance(dir_path, str) or not dir_path.strip() or "\x00" in dir_path:
            raise BadUsage(
                "--dir 不能为空路径",
                hint="传入有效的父目录路径（将写入 <PATH>/guiju-standards），或省略以使用默认路径",
            )
        parent = _resolve_parent_checked(Path(dir_path).expanduser())
        return "custom", "custom", parent / SKILL_DIR_NAME

    eff_target = "agents" if target is None else target
    eff_scope = "project" if scope is None else scope
    if eff_target not in _CLI_TARGETS:
        raise BadUsage(
            f"不支持的 --target：{eff_target}（可选：agents / claude）",
            hint="使用 --target agents 或 --target claude，或改用 --dir PATH",
        )
    if eff_scope not in _CLI_SCOPES:
        raise BadUsage(
            f"不支持的 --scope：{eff_scope}（可选：project / user）",
            hint="使用 --scope project（当前目录）或 --scope user（用户主目录）",
        )
    base_root = Path(cwd if eff_scope == "project" else user_home).expanduser()
    vendor_dir = ".agents" if eff_target == "agents" else ".claude"
    parent = _resolve_parent_checked(base_root / vendor_dir / "skills")
    return eff_target, eff_scope, parent / SKILL_DIR_NAME


def _read_regular_sha256(entry_path: str | Path, target_dir: Path) -> tuple[str | None, bool]:
    """用 O_NOFOLLOW 安全读取普通文件并算 sha256；返回 (digest, hit_symlink)。"""
    try:
        fd = os.open(entry_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None, True
        raise NeedsHuman(
            f"无法读取技能目录文件：{entry_path}（{type(exc).__name__}: {exc}）",
            hint="检查目标技能目录及文件可读权限后重试",
        ) from exc
    try:
        with os.fdopen(fd, "rb") as stream:
            return sha256_bytes(stream.read()), False
    except OSError as exc:
        raise NeedsHuman(
            f"读取技能目录文件失败：{entry_path}（{type(exc).__name__}: {exc}）",
            hint=f"检查 {target_dir} 内文件权限后重试",
        ) from exc


def _has_resolved_parent_symlink(parent: Path) -> bool:
    try:
        resolved_parent = parent.resolve()
    except RuntimeError:
        return True
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return True
        raise NeedsHuman(
            f"无法解析目标技能父目录：{parent}（{type(exc).__name__}: {exc}）",
            hint="检查目标目录及父目录权限后重试",
        ) from exc
    if resolved_parent != parent:
        return True

    cursor = parent
    while cursor != cursor.parent:
        try:
            st = cursor.lstat()
        except FileNotFoundError:
            cursor = cursor.parent
            continue
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return True
            raise NeedsHuman(
                f"无法检查目标技能父目录：{cursor}（{type(exc).__name__}: {exc}）",
                hint="检查目标目录及父目录权限后重试",
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            return True
        cursor = cursor.parent
    return False


def scan_skill_dir(target_dir: Path, *, expect_resolved_parent: bool = False) -> DiskSnapshot:
    """用 lstat + O_NOFOLLOW 采集目标技能目录快照；发现任何符号链接立即停止并标记。"""
    if expect_resolved_parent and _has_resolved_parent_symlink(target_dir.parent):
        return DiskSnapshot(exists=False, has_symlink=True, files={})

    try:
        root_st = target_dir.lstat()
    except FileNotFoundError:
        return DiskSnapshot(exists=False, has_symlink=False, files={})
    except OSError as exc:
        raise NeedsHuman(
            f"无法检查目标技能目录：{target_dir}（{type(exc).__name__}: {exc}）",
            hint="检查目标目录及父目录权限后重试",
        ) from exc

    if stat.S_ISLNK(root_st.st_mode):
        return DiskSnapshot(exists=False, has_symlink=True, files={})
    if not stat.S_ISDIR(root_st.st_mode):
        return DiskSnapshot(
            exists=True,
            has_symlink=False,
            files={},
            non_regular_entries=(".",),
        )

    files: dict[str, str] = {}
    non_regular: list[str] = []
    empty_dirs: list[str] = []
    stack: list[tuple[Path, str]] = [(target_dir, "")]

    while stack:
        current_dir, prefix = stack.pop()
        try:
            with os.scandir(current_dir) as entries_iter:
                entries = sorted(entries_iter, key=lambda item: item.name)
        except OSError as exc:
            raise NeedsHuman(
                f"无法遍历目标技能目录：{current_dir}（{type(exc).__name__}: {exc}）",
                hint="检查目标目录读/执行权限后重试",
            ) from exc

        if prefix and not entries:
            empty_dirs.append(f"{prefix}/")
            continue

        for entry in entries:
            rel = f"{prefix}/{entry.name}" if prefix else entry.name
            try:
                if entry.is_symlink():
                    return DiskSnapshot(exists=True, has_symlink=True, files={})
                if entry.is_dir(follow_symlinks=False):
                    stack.append((Path(entry.path), rel))
                elif entry.is_file(follow_symlinks=False):
                    digest, hit_symlink = _read_regular_sha256(entry.path, target_dir)
                    if hit_symlink:
                        return DiskSnapshot(exists=True, has_symlink=True, files={})
                    assert digest is not None
                    files[rel] = digest
                else:
                    non_regular.append(rel)
            except OSError as exc:
                raise NeedsHuman(
                    f"无法检查技能目录条目：{entry.path}（{type(exc).__name__}: {exc}）",
                    hint="检查目标目录及条目权限后重试",
                ) from exc

    return DiskSnapshot(
        exists=True,
        has_symlink=False,
        files=dict(sorted(files.items())),
        non_regular_entries=tuple(sorted(non_regular)),
        empty_dirs=tuple(sorted(empty_dirs)),
    )


def _assert_no_symlink_ancestors(target_dir: Path, entry_path: Path) -> None:
    rel_parts = entry_path.relative_to(target_dir).parts
    cursor = target_dir
    try:
        st = cursor.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise NeedsHuman(
            f"无法检查目录状态：{cursor}（{type(exc).__name__}: {exc}）",
            hint="检查目标目录权限后重试",
        ) from exc
    if stat.S_ISLNK(st.st_mode):
        raise AgentConflict(
            f"拒绝通过符号链接操作目录：{cursor}",
            reason="agent_symlink_rejected",
            hint=_CONFLICT_HINTS["agent_symlink_rejected"],
            path=str(target_dir),
        )
    for part in rel_parts:
        cursor = cursor / part
        try:
            st = cursor.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise NeedsHuman(
                f"无法检查条目状态：{cursor}（{type(exc).__name__}: {exc}）",
                hint="检查目标目录权限后重试",
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise AgentConflict(
                f"拒绝通过符号链接操作条目：{cursor}",
                reason="agent_symlink_rejected",
                hint=_CONFLICT_HINTS["agent_symlink_rejected"],
                path=str(target_dir),
            )


def _prune_empty_dirs(target_dir: Path, *, remove_root_if_empty: bool) -> None:
    """自底向上清理目标技能目录内的空子目录（跳过任何符号链接）。"""
    try:
        root_st = target_dir.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise NeedsHuman(
            f"无法检查技能目录：{target_dir}（{type(exc).__name__}: {exc}）",
            hint="检查目标目录权限后重试",
        ) from exc
    if stat.S_ISLNK(root_st.st_mode) or not stat.S_ISDIR(root_st.st_mode):
        return

    subdirs: list[Path] = []
    stack: list[Path] = [target_dir]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries_iter:
                for entry in entries_iter:
                    if entry.is_symlink():
                        return
                    if entry.is_dir(follow_symlinks=False):
                        sub = Path(entry.path)
                        subdirs.append(sub)
                        stack.append(sub)
        except OSError as exc:
            raise NeedsHuman(
                f"无法扫描技能目录以清理空目录：{current}（{type(exc).__name__}: {exc}）",
                hint="检查目标目录权限后重试",
            ) from exc

    for sub in sorted(subdirs, key=lambda p: len(p.parts), reverse=True):
        with suppress(OSError):
            if not sub.is_symlink():
                sub.rmdir()

    if remove_root_if_empty:
        with suppress(OSError):
            if not target_dir.is_symlink():
                target_dir.rmdir()


def _remove_entries(target_dir: Path, relpaths: tuple[str, ...]) -> None:
    if not relpaths:
        return
    if "." in relpaths:
        try:
            st = target_dir.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise NeedsHuman(
                f"无法检查阻塞目标文件：{target_dir}（{type(exc).__name__}: {exc}）",
                hint="检查目标路径写权限后重试",
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise AgentConflict(
                f"拒绝删除符号链接：{target_dir}",
                reason="agent_symlink_rejected",
                hint=_CONFLICT_HINTS["agent_symlink_rejected"],
                path=str(target_dir),
            )
        if not stat.S_ISDIR(st.st_mode):
            try:
                target_dir.unlink()
            except OSError as exc:
                raise NeedsHuman(
                    f"无法移除阻塞目标文件：{target_dir}（{type(exc).__name__}: {exc}）",
                    hint="检查父目录写权限后重试",
                ) from exc
        return

    file_rels = [r for r in relpaths if not r.endswith("/")]
    for rel in file_rels:
        candidate = target_dir / rel
        _assert_no_symlink_ancestors(target_dir, candidate)
        try:
            st = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise NeedsHuman(
                f"无法检查待删除文件：{candidate}（{type(exc).__name__}: {exc}）",
                hint="检查目标目录写权限后重试",
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise AgentConflict(
                f"拒绝删除符号链接：{candidate}",
                reason="agent_symlink_rejected",
                hint=_CONFLICT_HINTS["agent_symlink_rejected"],
                path=str(target_dir),
            )
        if not stat.S_ISDIR(st.st_mode):
            try:
                candidate.unlink()
            except OSError as exc:
                raise NeedsHuman(
                    f"无法删除受管文件：{candidate}（{type(exc).__name__}: {exc}）",
                    hint="检查目标目录写权限后重试",
                ) from exc


def _resolve_assets(
    *,
    package_version: str | None,
    managed_contents: Mapping[str, str] | None,
    history_hashes: Mapping[str, Mapping[str, str]] | None,
) -> tuple[str, dict[str, str], dict[str, str], dict[str, dict[str, str]]]:
    eff_version = __version__ if package_version is None else package_version
    eff_contents = (
        derive_managed_files(read_skill_frontmatter(), read_contract_markdown())
        if managed_contents is None
        else dict(managed_contents)
    )
    eff_hashes = hash_managed_files(eff_contents)
    eff_history = (
        load_history_hashes()
        if history_hashes is None
        else {ver: dict(hashes) for ver, hashes in history_hashes.items()}
    )
    return eff_version, eff_contents, eff_hashes, eff_history


def _files_list_payload(files_map: Mapping[str, str]) -> list[dict[str, str]]:
    return [{"relpath": rel, "sha256": digest} for rel, digest in sorted(files_map.items())]


def show_agent_asset(*, kind: str | None = None) -> dict:
    """只读返回使用态契约原文或完整 SKILL.md，绝不创建目录或写盘。"""
    eff_kind = "contract" if kind is None else kind
    if eff_kind not in _SHOW_KINDS:
        raise BadUsage(
            f"不支持的 --kind：{eff_kind}（可选：contract / skill）",
            hint="使用 --kind contract（默认）或 --kind skill",
        )
    content = (
        read_contract_markdown()
        if eff_kind == "contract"
        else derive_managed_files(read_skill_frontmatter(), read_contract_markdown())["SKILL.md"]
    )
    return {
        "ok": True,
        "kind": eff_kind,
        "package_version": __version__,
        "sha256": sha256_text(content),
        "content": content,
    }


def install_agent_skill(
    receipt_store: AgentReceiptStore,
    *,
    cwd: Path,
    user_home: Path,
    target: str | None = None,
    scope: str | None = None,
    dir_path: str | None = None,
    force: bool = False,
    package_version: str | None = None,
    managed_contents: Mapping[str, str] | None = None,
    history_hashes: Mapping[str, Mapping[str, str]] | None = None,
) -> dict:
    """向指定工具 Skill 目录安装、升级或认领标准使用态 Skill 资产。"""
    eff_target, eff_scope, target_dir = _resolve_location(
        target=target,
        scope=scope,
        dir_path=dir_path,
        cwd=cwd,
        user_home=user_home,
    )
    path_key = str(target_dir)
    eff_version, eff_contents, current_hashes, eff_history = _resolve_assets(
        package_version=package_version,
        managed_contents=managed_contents,
        history_hashes=history_hashes,
    )

    pre_disk = scan_skill_dir(target_dir, expect_resolved_parent=True)
    if pre_disk.has_symlink:
        _raise_conflict("agent_symlink_rejected", target_dir=target_dir, receipt_path=receipt_store.path)

    pre_snap = receipt_store.load()
    pre_plan = plan_install(
        pre_disk,
        receipt=None if pre_snap.corrupt else pre_snap.installs.get(path_key),
        receipt_corrupt=pre_snap.corrupt,
        current_version=eff_version,
        current_hashes=current_hashes,
        history_hashes=eff_history,
        force=force,
        current_target=eff_target,
        current_scope=eff_scope,
    )
    if not pre_plan.ok:
        _raise_conflict(
            pre_plan.conflict_reason,
            target_dir=target_dir,
            receipt_path=receipt_store.path,
        )
    if (
        pre_plan.action == "unchanged"
        and not pre_plan.write_files
        and not pre_plan.write_receipt
        and not pre_plan.backup_corrupt_receipt
    ):
        return {
            "ok": True,
            "action": pre_plan.action,
            "target": eff_target,
            "scope": eff_scope,
            "path": path_key,
            "receipt_path": str(receipt_store.path),
            "package_version": eff_version,
            "files": _files_list_payload(current_hashes),
            "removed_extra_files": [],
            "backed_up_receipt": None,
        }

    with receipt_store.locked() as snap:
        disk = scan_skill_dir(target_dir, expect_resolved_parent=True)
        plan = plan_install(
            disk,
            receipt=None if snap.corrupt else snap.installs.get(path_key),
            receipt_corrupt=snap.corrupt,
            current_version=eff_version,
            current_hashes=current_hashes,
            history_hashes=eff_history,
            force=force,
            current_target=eff_target,
            current_scope=eff_scope,
        )
        if not plan.ok:
            _raise_conflict(
                plan.conflict_reason,
                target_dir=target_dir,
                receipt_path=receipt_store.path,
            )

        backed_up_receipt: str | None = None
        if plan.backup_corrupt_receipt:
            backed_up_receipt = str(receipt_store.backup_corrupt())

        if plan.write_files:
            _remove_entries(target_dir, plan.files_to_remove)
            _prune_empty_dirs(target_dir, remove_root_if_empty=False)
            for relpath in sorted(eff_contents):
                atomic_write_managed_file(
                    target_dir / relpath, eff_contents[relpath].encode("utf-8")
                )

        if plan.write_receipt:
            updated = {} if plan.backup_corrupt_receipt else dict(snap.installs)
            updated[path_key] = ReceiptRecord(
                skill=SKILL_DIR_NAME,
                target=eff_target,
                scope=eff_scope,
                package_version=eff_version,
                installed_at=receipt_store.now_timestamp(),
                files=current_hashes,
            )
            receipt_store.save(updated)

    return {
        "ok": True,
        "action": plan.action,
        "target": eff_target,
        "scope": eff_scope,
        "path": path_key,
        "receipt_path": str(receipt_store.path),
        "package_version": eff_version,
        "files": _files_list_payload(current_hashes),
        "removed_extra_files": list(plan.removed_extra_files),
        "backed_up_receipt": backed_up_receipt,
    }


def _apply_uninstall(
    target_dir: Path,
    plan_files_to_remove: tuple[str, ...],
    *,
    remove_dir_if_empty: bool,
) -> None:
    _remove_entries(target_dir, plan_files_to_remove)
    _prune_empty_dirs(target_dir, remove_root_if_empty=remove_dir_if_empty)


def uninstall_agent_skill(
    receipt_store: AgentReceiptStore,
    *,
    cwd: Path,
    user_home: Path,
    target: str | None = None,
    scope: str | None = None,
    dir_path: str | None = None,
    force: bool = False,
    package_version: str | None = None,
    managed_contents: Mapping[str, str] | None = None,
    history_hashes: Mapping[str, Mapping[str, str]] | None = None,
) -> dict:
    """安全卸载受管或干净官方 Skill 目录，并按边界清理或保留额外文件。"""
    _eff_target, _eff_scope, target_dir = _resolve_location(
        target=target,
        scope=scope,
        dir_path=dir_path,
        cwd=cwd,
        user_home=user_home,
    )
    path_key = str(target_dir)
    _, _, current_hashes, eff_history = _resolve_assets(
        package_version=package_version,
        managed_contents=managed_contents,
        history_hashes=history_hashes,
    )

    pre_disk = scan_skill_dir(target_dir, expect_resolved_parent=True)
    if pre_disk.has_symlink:
        _raise_conflict("agent_symlink_rejected", target_dir=target_dir, receipt_path=receipt_store.path)

    pre_snap = receipt_store.load()
    pre_plan = plan_uninstall(
        pre_disk,
        receipt=None if pre_snap.corrupt else pre_snap.installs.get(path_key),
        receipt_corrupt=pre_snap.corrupt,
        current_hashes=current_hashes,
        history_hashes=eff_history,
        force=force,
    )
    if not pre_plan.ok:
        _raise_conflict(
            pre_plan.conflict_reason,
            target_dir=target_dir,
            receipt_path=receipt_store.path,
        )

    if not pre_snap.exists and not pre_plan.remove_receipt:
        _apply_uninstall(
            target_dir,
            pre_plan.files_to_remove,
            remove_dir_if_empty=pre_plan.remove_dir_if_empty,
        )
        return {
            "ok": True,
            "action": pre_plan.action,
            "path": path_key,
            "receipt_path": str(receipt_store.path),
            "removed_files": list(pre_plan.files_to_remove),
            "preserved_files": list(pre_plan.preserved_files),
        }

    with receipt_store.locked() as snap:
        disk = scan_skill_dir(target_dir, expect_resolved_parent=True)
        plan = plan_uninstall(
            disk,
            receipt=None if snap.corrupt else snap.installs.get(path_key),
            receipt_corrupt=snap.corrupt,
            current_hashes=current_hashes,
            history_hashes=eff_history,
            force=force,
        )
        if not plan.ok:
            _raise_conflict(
                plan.conflict_reason,
                target_dir=target_dir,
                receipt_path=receipt_store.path,
            )

        _apply_uninstall(
            target_dir,
            plan.files_to_remove,
            remove_dir_if_empty=plan.remove_dir_if_empty,
        )
        if plan.remove_receipt:
            updated = dict(snap.installs)
            updated.pop(path_key, None)
            receipt_store.save(updated)

    return {
        "ok": True,
        "action": plan.action,
        "path": path_key,
        "receipt_path": str(receipt_store.path),
        "removed_files": list(plan.files_to_remove),
        "preserved_files": list(plan.preserved_files),
    }


def _status_entry(
    *,
    path_key: str,
    target: str,
    scope: str,
    receipt: ReceiptRecord | None,
    current_hashes: Mapping[str, str],
    history_hashes: Mapping[str, Mapping[str, str]],
) -> dict:
    disk = scan_skill_dir(Path(path_key), expect_resolved_parent=True)
    st = inspect_target_status(
        disk,
        receipt=receipt,
        current_hashes=current_hashes,
        history_hashes=history_hashes,
    )
    return {
        "path": path_key,
        "target": receipt.target if receipt is not None else target,
        "scope": receipt.scope if receipt is not None else scope,
        "receipt_version": receipt.package_version if receipt is not None else None,
        "state": st.state,
        "files": [{"relpath": rel, "sha256": digest} for rel, digest in st.files],
        "extra_files": list(st.extra_files),
    }


def status_agent_skills(
    receipt_store: AgentReceiptStore,
    *,
    cwd: Path,
    user_home: Path,
    target: str | None = None,
    scope: str | None = None,
    dir_path: str | None = None,
    package_version: str | None = None,
    managed_contents: Mapping[str, str] | None = None,
    history_hashes: Mapping[str, Mapping[str, str]] | None = None,
) -> dict:
    """严格只读核验本机全部受管目标或单个指定目标状态，绝不建目录或写盘。"""
    single_mode = target is not None or scope is not None or dir_path is not None
    resolved: tuple[str, str, Path] | None = None
    if single_mode:
        resolved = _resolve_location(
            target=target,
            scope=scope,
            dir_path=dir_path,
            cwd=cwd,
            user_home=user_home,
        )

    eff_version, _, current_hashes, eff_history = _resolve_assets(
        package_version=package_version,
        managed_contents=managed_contents,
        history_hashes=history_hashes,
    )
    snap = receipt_store.load()
    snap.require_valid()

    if single_mode:
        assert resolved is not None
        eff_target, eff_scope, target_dir = resolved
        path_key = str(target_dir)
        entries = [
            _status_entry(
                path_key=path_key,
                target=eff_target,
                scope=eff_scope,
                receipt=snap.installs.get(path_key),
                current_hashes=current_hashes,
                history_hashes=eff_history,
            )
        ]
        mode = "single"
    else:
        entries = [
            _status_entry(
                path_key=path_key,
                target=rec.target,
                scope=rec.scope,
                receipt=rec,
                current_hashes=current_hashes,
                history_hashes=eff_history,
            )
            for path_key, rec in sorted(snap.installs.items())
        ]
        mode = "all"

    return {
        "ok": True,
        "mode": mode,
        "receipt_path": str(receipt_store.path),
        "receipt_exists": snap.exists,
        "package_version": eff_version,
        "entries": entries,
    }
