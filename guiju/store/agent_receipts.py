"""使用态受管 Agent 技能带外收据存储（GUIJU_HOME/agent-receipts.json）。

契约边界（对标 guiju/store/atlas.py）：
- 仅读写显式传入的 agent-receipts.json，绝不触碰 catalog.db；
- 只读查询 load() 绝不创建目录或写入任何文件；
- 写入使用临时文件 + fsync + os.replace 原子替换，读-改-写全程持有基于 POSIX fcntl.flock 的同目录锁；
- 损坏时返回 corrupt=True 快照，仅显式调用 backup_corrupt() 时原子备份为 .corrupt-<ts>。
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import math
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from ..domain.agent_assets import (
    SKILL_DIR_NAME,
    VALID_SCOPES,
    VALID_TARGETS,
    ReceiptRecord,
    validate_file_hashes,
)
from ..errors import AgentConflict, NeedsHuman

_SCHEMA = 1
_MAX_BYTES = 4 * 1024 * 1024
_LOCK_FILENAME = "agent-receipts.lock"
_ENVELOPE_FIELDS = frozenset({"schema", "installs"})
_ENTRY_FIELDS = frozenset(
    {"skill", "target", "scope", "package_version", "installed_at", "files"}
)


def _valid_epoch(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        stamp = float(value)
    except OverflowError:
        return None
    return stamp if math.isfinite(stamp) and stamp > 0 else None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"收据存在重复 JSON 字段：{key}")
        result[key] = value
    return result


def _validate_receipt_key(key: object) -> str:
    if not isinstance(key, str) or not key:
        raise ValueError("收据安装路径键必须为非空字符串")
    if "\x00" in key or not key.startswith("/") or key.startswith("//"):
        raise ValueError(f"收据安装路径键不是规范绝对路径：{key!r}")
    if key != os.path.normpath(key):
        raise ValueError(f"收据安装路径键未规范化：{key!r}")
    if os.path.basename(key) != SKILL_DIR_NAME:
        raise ValueError(f"收据安装路径必须指向 {SKILL_DIR_NAME} 目录：{key!r}")
    return key


def _validate_installed_at(value: object) -> str:
    if not isinstance(value, str) or len(value) != 19:
        raise ValueError(f"installed_at 格式非法：{value!r}")
    parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    if parsed.strftime("%Y-%m-%d %H:%M:%S") != value:
        raise ValueError(f"installed_at 格式非法：{value!r}")
    return value


def _parse_record(raw_key: object, raw_entry: object) -> tuple[str, ReceiptRecord]:
    key = _validate_receipt_key(raw_key)
    if not isinstance(raw_entry, dict) or raw_entry.keys() != _ENTRY_FIELDS:
        raise ValueError(f"收据条目字段集合非法：{key}")
    skill = raw_entry["skill"]
    if skill != SKILL_DIR_NAME:
        raise ValueError(f"收据条目 skill 非法：{skill!r}")
    target = raw_entry["target"]
    if not isinstance(target, str) or target not in VALID_TARGETS:
        raise ValueError(f"收据条目 target 非法：{target!r}")
    scope = raw_entry["scope"]
    if not isinstance(scope, str) or scope not in VALID_SCOPES:
        raise ValueError(f"收据条目 scope 非法：{scope!r}")
    package_version = raw_entry["package_version"]
    if not isinstance(package_version, str) or not package_version or any(ch.isspace() for ch in package_version):
        raise ValueError("收据条目 package_version 格式非法")
    installed_at = _validate_installed_at(raw_entry["installed_at"])
    files = MappingProxyType(validate_file_hashes(raw_entry["files"]))
    return key, ReceiptRecord(
        skill=skill,
        target=target,
        scope=scope,
        package_version=package_version,
        installed_at=installed_at,
        files=files,
    )


def _normalize_record(raw_key: str, record: ReceiptRecord) -> tuple[str, ReceiptRecord]:
    key = _validate_receipt_key(raw_key)
    if not isinstance(record, ReceiptRecord):
        raise ValueError("收据记录类型错误")
    if record.skill != SKILL_DIR_NAME:
        raise ValueError(f"收据条目 skill 非法：{record.skill!r}")
    if record.target not in VALID_TARGETS:
        raise ValueError(f"收据条目 target 非法：{record.target!r}")
    if record.scope not in VALID_SCOPES:
        raise ValueError(f"收据条目 scope 非法：{record.scope!r}")
    if (
        not isinstance(record.package_version, str)
        or not record.package_version
        or any(ch.isspace() for ch in record.package_version)
    ):
        raise ValueError("收据条目 package_version 格式非法")
    installed_at = _validate_installed_at(record.installed_at)
    files = MappingProxyType(validate_file_hashes(record.files))
    return key, ReceiptRecord(
        skill=record.skill,
        target=record.target,
        scope=record.scope,
        package_version=record.package_version,
        installed_at=installed_at,
        files=files,
    )


@dataclass(frozen=True)
class AgentReceiptsSnapshot:
    path: Path
    exists: bool
    installs: Mapping[str, ReceiptRecord]
    corrupt: bool = False
    error: str = ""

    def require_valid(self) -> None:
        if self.corrupt:
            raise AgentConflict(
                f"受管技能收据已损坏：{self.path}（{self.error}）",
                reason="agent_receipt_corrupt",
                hint="可用 guiju agent install --force 自动备份损坏收据后重建（status 与 uninstall 始终拒绝直接覆写）",
                receipt_path=str(self.path),
            )


class AgentReceiptStore:
    """GUIJU_HOME/agent-receipts.json 的严格只读装载、文件锁互斥与原子落盘。"""

    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock

    @property
    def lock_path(self) -> Path:
        return self.path.parent / _LOCK_FILENAME

    def now_timestamp(self) -> str:
        epoch = _valid_epoch(self._clock())
        if epoch is None:
            raise ValueError("收据时钟返回值必须是有限正数")
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")

    def _corrupt(self, message: str) -> AgentReceiptsSnapshot:
        return AgentReceiptsSnapshot(
            path=self.path,
            exists=True,
            installs=MappingProxyType({}),
            corrupt=True,
            error=message,
        )

    def _ensure_not_symlink(self, target: Path, label: str) -> None:
        try:
            st = target.lstat()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise NeedsHuman(
                f"无法检查{label}状态：{target}（{type(exc).__name__}: {exc}）",
                hint="检查目录及文件权限后重试",
            ) from exc
        if stat.S_ISLNK(st.st_mode):
            raise AgentConflict(
                f"拒绝通过符号链接操作{label}：{target}",
                reason="agent_symlink_rejected",
                receipt_path=str(self.path),
            )

    def load(self) -> AgentReceiptsSnapshot:
        """只读装载收据快照；缺失返回 exists=False，绝不创建目录或写盘。"""
        try:
            st = self.path.lstat()
        except FileNotFoundError:
            return AgentReceiptsSnapshot(
                path=self.path,
                exists=False,
                installs=MappingProxyType({}),
            )
        except OSError as exc:
            raise NeedsHuman(
                f"无法检查受管技能收据状态：{self.path}（{type(exc).__name__}: {exc}）",
                hint="检查数据目录路径类型与读写权限后重试",
            ) from exc

        if stat.S_ISLNK(st.st_mode):
            raise AgentConflict(
                f"拒绝通过符号链接读取受管技能收据：{self.path}",
                reason="agent_symlink_rejected",
                hint="收据路径不能是符号链接；请人工核实并移除符号链接后重试",
                receipt_path=str(self.path),
            )
        if not stat.S_ISREG(st.st_mode):
            return self._corrupt("收据路径不是普通文件")

        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise AgentConflict(
                    f"拒绝通过符号链接读取受管技能收据：{self.path}",
                    reason="agent_symlink_rejected",
                    hint="收据路径不能是符号链接；请人工核实并移除符号链接后重试",
                    receipt_path=str(self.path),
                ) from exc
            if exc.errno in (errno.EACCES, errno.EPERM, errno.ENOTDIR):
                raise NeedsHuman(
                    f"无法打开受管技能收据：{self.path}（{type(exc).__name__}: {exc}）",
                    hint="检查数据目录路径类型与读写权限后重试",
                ) from exc
            return self._corrupt(f"{type(exc).__name__}: {exc}")
        try:
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(_MAX_BYTES + 1)
        except OSError as exc:
            return self._corrupt(f"{type(exc).__name__}: {exc}")

        if len(raw) > _MAX_BYTES:
            return self._corrupt("收据文件体积超过 4 MB 上限")

        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            return self._corrupt(f"{type(exc).__name__}: {exc}")

        if not isinstance(data, dict) or data.keys() != _ENVELOPE_FIELDS:
            return self._corrupt("收据顶层字段不符合 schema v1")
        if type(data["schema"]) is not int or data["schema"] != _SCHEMA:
            return self._corrupt(f"不支持的收据 schema 版本：{data.get('schema')!r}")
        raw_installs = data["installs"]
        if not isinstance(raw_installs, dict):
            return self._corrupt("installs 必须为对象")

        parsed: dict[str, ReceiptRecord] = {}
        try:
            for raw_key, raw_entry in raw_installs.items():
                key, record = _parse_record(raw_key, raw_entry)
                parsed[key] = record
        except ValueError as exc:
            return self._corrupt(str(exc))

        return AgentReceiptsSnapshot(
            path=self.path,
            exists=True,
            installs=MappingProxyType(parsed),
        )

    @contextlib.contextmanager
    def locked(self) -> Iterator[AgentReceiptsSnapshot]:
        """在 POSIX fcntl.flock(LOCK_EX) 独占锁保护下读取并执行写删改。"""
        lock_file = self.lock_path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise NeedsHuman(
                f"无法创建收据目录：{self.path.parent}（{type(exc).__name__}: {exc}）",
                hint="检查数据目录写权限；只读目录无法执行写入或卸载收据变更",
            ) from exc

        self._ensure_not_symlink(lock_file, "收据锁文件")
        try:
            flags = os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW
            fd = os.open(lock_file, flags, 0o666)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise AgentConflict(
                    f"拒绝通过符号链接访问收据锁文件：{lock_file}",
                    reason="agent_symlink_rejected",
                    receipt_path=str(self.path),
                ) from exc
            raise NeedsHuman(
                f"无法打开收据锁文件：{lock_file}（{type(exc).__name__}: {exc}）",
                hint="检查数据目录写权限或切换可写数据目录",
            ) from exc

        with os.fdopen(fd, "a+", encoding="utf-8") as fh:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            except OSError as exc:
                raise NeedsHuman(
                    f"无法锁定收据锁文件：{lock_file}（{type(exc).__name__}: {exc}）",
                    hint="检查文件锁权限与文件系统支持情况",
                ) from exc
            try:
                yield self.load()
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def backup_corrupt(self, *, now_epoch: float | None = None) -> Path:
        """原子备份损坏的收据文件为 agent-receipts.json.corrupt-<YYYYMMDD-HHMMSS>。"""
        self._ensure_not_symlink(self.path, "损坏收据文件")
        try:
            self.path.lstat()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"待备份的损坏收据文件不存在：{self.path}") from exc
        except OSError as exc:
            raise NeedsHuman(
                f"无法读取待备份收据文件：{self.path}（{type(exc).__name__}: {exc}）",
                hint="检查文件权限后重试",
            ) from exc

        epoch = _valid_epoch(self._clock() if now_epoch is None else now_epoch)
        if epoch is None:
            raise ValueError("备份时间戳必须是有限正数")
        stamp = datetime.fromtimestamp(epoch).strftime("%Y%m%d-%H%M%S")
        base_name = f"{self.path.name}.corrupt-{stamp}"
        candidate = self.path.parent / base_name
        suffix = 0
        while True:
            try:
                candidate.lstat()
            except FileNotFoundError:
                break
            except OSError as exc:
                raise NeedsHuman(
                    f"无法检查备份目标路径：{candidate}（{type(exc).__name__}: {exc}）",
                    hint="检查数据目录权限后重试",
                ) from exc
            suffix += 1
            candidate = self.path.parent / f"{base_name}-{suffix}"

        try:
            os.replace(self.path, candidate)
        except OSError as exc:
            raise NeedsHuman(
                f"无法备份损坏收据文件：{self.path}（{type(exc).__name__}: {exc}）",
                hint="检查数据目录写权限后重试",
            ) from exc
        return candidate

    def save(self, installs: Mapping[str, ReceiptRecord]) -> AgentReceiptsSnapshot:
        """原子写入 schema=1 的受管安装收据集合。"""
        normalized: dict[str, ReceiptRecord] = {}
        serialized_installs: dict[str, dict[str, object]] = {}
        for raw_key in sorted(installs):
            key, record = _normalize_record(raw_key, installs[raw_key])
            normalized[key] = record
            serialized_installs[key] = {
                "skill": record.skill,
                "target": record.target,
                "scope": record.scope,
                "package_version": record.package_version,
                "installed_at": record.installed_at,
                "files": dict(record.files),
            }

        self._ensure_not_symlink(self.path, "收据文件")
        payload = {"schema": _SCHEMA, "installs": serialized_installs}
        raw = (
            json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        ).encode("utf-8")

        temporary: Path | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise NeedsHuman(
                f"受管技能收据写入失败：{self.path}（{type(exc).__name__}: {exc}）",
                hint="检查数据目录写权限或磁盘空间",
            ) from exc
        finally:
            if temporary is not None:
                with contextlib.suppress(OSError):
                    temporary.unlink()

        return AgentReceiptsSnapshot(
            path=self.path,
            exists=True,
            installs=MappingProxyType(normalized),
        )


def atomic_write_managed_file(target_path: Path, raw: bytes) -> None:
    """以临时文件 + fsync + os.replace 原子写入单个受管技能文件（拒绝符号链接）。"""
    try:
        st = target_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise NeedsHuman(
            f"无法检查受管技能文件状态：{target_path}（{type(exc).__name__}: {exc}）",
            hint="检查目标目录权限后重试",
        ) from exc
    else:
        if stat.S_ISLNK(st.st_mode):
            raise AgentConflict(
                f"拒绝通过符号链接写入受管技能文件：{target_path}",
                reason="agent_symlink_rejected",
                path=str(target_path),
            )

    temporary: Path | None = None
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            parent_st = target_path.parent.lstat()
        except OSError as exc:
            raise NeedsHuman(
                f"无法检查受管技能目录状态：{target_path.parent}（{type(exc).__name__}: {exc}）",
                hint="检查目标技能目录写权限后重试",
            ) from exc
        if stat.S_ISLNK(parent_st.st_mode):
            raise AgentConflict(
                f"拒绝通过符号链接目录写入受管技能文件：{target_path.parent}",
                reason="agent_symlink_rejected",
                path=str(target_path.parent),
            )
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target_path.parent,
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            st = target_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(st.st_mode):
                raise AgentConflict(
                    f"拒绝通过符号链接写入受管技能文件：{target_path}",
                    reason="agent_symlink_rejected",
                    path=str(target_path),
                )
        os.replace(temporary, target_path)
    except AgentConflict:
        raise
    except OSError as exc:
        raise NeedsHuman(
            f"写入受管技能文件失败：{target_path}（{type(exc).__name__}: {exc}）",
            hint="检查目标技能目录写权限或磁盘空间",
        ) from exc
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()
