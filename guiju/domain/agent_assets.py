"""使用态 Agent 资产的纯领域逻辑与状态机（零磁盘、零环境路径、零网络）。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from ..errors import AGENT_CONFLICT_REASONS

SKILL_DIR_NAME = "guiju-standards"
SKILL_FILENAME = "SKILL.md"
MCP_SUMMARY_BEGIN = "<!-- guiju:mcp-summary:begin -->"
MCP_SUMMARY_END = "<!-- guiju:mcp-summary:end -->"
HISTORY_SCHEMA = 1
VALID_TARGETS = frozenset({"agents", "claude", "custom"})
VALID_SCOPES = frozenset({"project", "user", "custom"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?$")
_FM_NAME_RE = re.compile(r"^name:\s*guiju-standards\s*$", flags=re.MULTILINE)
_FM_DESC_RE = re.compile(r"^description:\s*\S+", flags=re.MULTILINE)


@dataclass(frozen=True)
class ReceiptRecord:
    """单条带外安装收据记录（对应 GUIJU_HOME/agent-receipts.json 中一条 installs）。"""

    skill: str
    target: str
    scope: str
    package_version: str
    installed_at: str
    files: Mapping[str, str]


@dataclass(frozen=True)
class DiskSnapshot:
    """目标技能目录的纯值快照（由应用层采自磁盘）。"""

    exists: bool
    has_symlink: bool
    files: Mapping[str, str]
    non_regular_entries: tuple[str, ...] = ()
    empty_dirs: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstallPlan:
    ok: bool
    action: str
    conflict_reason: str = ""
    write_files: bool = False
    write_receipt: bool = False
    backup_corrupt_receipt: bool = False
    files_to_remove: tuple[str, ...] = ()
    removed_extra_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class UninstallPlan:
    ok: bool
    action: str
    conflict_reason: str = ""
    remove_receipt: bool = False
    files_to_remove: tuple[str, ...] = ()
    preserved_files: tuple[str, ...] = ()
    remove_dir_if_empty: bool = False


@dataclass(frozen=True)
class TargetStatus:
    state: str
    files: tuple[tuple[str, str], ...] = ()
    extra_files: tuple[str, ...] = ()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(content: str) -> str:
    return sha256_bytes(content.encode("utf-8"))


def validate_relpath(relpath: object) -> str:
    """校验技能目录内受管相对路径，拒绝绝对路径、反斜杠与路径穿越。"""
    if not isinstance(relpath, str) or not relpath:
        raise ValueError(f"受管相对路径必须为非空字符串：{relpath!r}")
    if "\x00" in relpath or "\\" in relpath or relpath.startswith("/") or relpath.endswith("/"):
        raise ValueError(f"非法的受管相对路径：{relpath!r}")
    parts = relpath.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"受管相对路径包含非法片段：{relpath!r}")
    return relpath


def validate_file_hashes(files: object) -> dict[str, str]:
    """校验 `{relpath: sha256}` 映射并返回按键排序后的规范化字典。"""
    if not isinstance(files, Mapping) or not files:
        raise ValueError("受管文件哈希映射必须为非空字典")
    normalized: dict[str, str] = {}
    for relpath, digest in sorted(files.items(), key=lambda item: str(item[0])):
        clean_rel = validate_relpath(relpath)
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"受管文件 {clean_rel!r} 的 sha256 格式非法：{digest!r}")
        normalized[clean_rel] = digest
    return normalized


def validate_skill_frontmatter(frontmatter_yaml: str) -> str:
    """校验 SKILL.md 头部 YAML frontmatter 格式并返回去首尾换行的纯净头部文本。"""
    if not isinstance(frontmatter_yaml, str) or "\r" in frontmatter_yaml or "\x00" in frontmatter_yaml:
        raise ValueError("skill_frontmatter.yaml 必须为 LF 编码字符串")
    normalized = frontmatter_yaml.strip("\n")
    if not normalized:
        raise ValueError("skill_frontmatter.yaml 不能为空")
    if any(line.strip() == "---" for line in normalized.splitlines()):
        raise ValueError("skill_frontmatter.yaml 不得包含 `---` 分隔线")
    if not _FM_NAME_RE.search(normalized):
        raise ValueError(f"skill_frontmatter.yaml 缺少 `name: {SKILL_DIR_NAME}`")
    if not _FM_DESC_RE.search(normalized):
        raise ValueError("skill_frontmatter.yaml 缺少 `description`")
    return normalized


def render_skill_markdown(frontmatter_yaml: str, contract_markdown: str) -> str:
    """由 frontmatter 模板与唯一正文 contract.md 确定性拼接出完整 SKILL.md。"""
    frontmatter = validate_skill_frontmatter(frontmatter_yaml)
    if (
        not isinstance(contract_markdown, str)
        or not contract_markdown.strip()
        or "\r" in contract_markdown
        or "\x00" in contract_markdown
    ):
        raise ValueError("contract.md 必须为非空 LF 字符串")
    return f"---\n{frontmatter}\n---\n\n{contract_markdown}"


def strip_skill_frontmatter(skill_markdown: str) -> str:
    """剥除 SKILL.md 顶部 frontmatter，还原逐字节一致的正文。"""
    if not isinstance(skill_markdown, str) or not skill_markdown.startswith("---\n"):
        raise ValueError("SKILL.md 缺少起始 YAML frontmatter 分隔线")
    delimiter = "\n---\n\n"
    end_idx = skill_markdown.find(delimiter, 4)
    if end_idx < 0:
        raise ValueError("SKILL.md 缺少结束 YAML frontmatter 分隔线")
    validate_skill_frontmatter(skill_markdown[4:end_idx])
    body = skill_markdown[end_idx + len(delimiter) :]
    if not body.strip():
        raise ValueError("SKILL.md 剥除 frontmatter 后正文为空")
    return body


def extract_mcp_instructions(contract_markdown: str) -> str:
    """从 contract.md 的 MCP 摘要标记区间确定性截取 instructions 文本。"""
    if not isinstance(contract_markdown, str):
        raise ValueError("contract.md 必须为字符串")
    begin_count = contract_markdown.count(MCP_SUMMARY_BEGIN)
    end_count = contract_markdown.count(MCP_SUMMARY_END)
    if begin_count != 1 or end_count != 1:
        raise ValueError(
            f"contract.md 必须恰好包含 1 对 MCP 摘要标记（begin={begin_count}, end={end_count}）"
        )
    begin_idx = contract_markdown.index(MCP_SUMMARY_BEGIN) + len(MCP_SUMMARY_BEGIN)
    end_idx = contract_markdown.index(MCP_SUMMARY_END)
    if end_idx <= begin_idx:
        raise ValueError("contract.md 中 MCP 摘要结束标记不得位于开始标记之前")
    snippet = contract_markdown[begin_idx:end_idx].strip()
    if not snippet:
        raise ValueError("contract.md 中 MCP 摘要标记区间不能为空")
    return snippet


def derive_managed_files(frontmatter_yaml: str, contract_markdown: str) -> dict[str, str]:
    """返回 `{relpath: file_content}` 受管文件映射。"""
    return {SKILL_FILENAME: render_skill_markdown(frontmatter_yaml, contract_markdown)}


def hash_managed_files(managed_contents: Mapping[str, str]) -> dict[str, str]:
    """对受管文件内容字典计算规范化 `{relpath: sha256}` 映射。"""
    hashed = {
        validate_relpath(relpath): sha256_text(content)
        for relpath, content in managed_contents.items()
    }
    return validate_file_hashes(hashed)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 存在重复字段：{key}")
        result[key] = value
    return result


def parse_history_hashes(raw_json: str) -> dict[str, dict[str, str]]:
    """严格解析包内 history_hashes.json，返回 `{version: {relpath: sha256}}`。"""
    if not isinstance(raw_json, str):
        raise ValueError("history_hashes.json 内容必须为字符串")
    data = json.loads(raw_json, object_pairs_hook=_unique_json_object)
    if not isinstance(data, dict) or data.keys() != {"schema", "versions"}:
        raise ValueError("history_hashes.json 顶层字段必须精确为 schema 与 versions")
    if type(data["schema"]) is not int or data["schema"] != HISTORY_SCHEMA:
        raise ValueError(f"history_hashes.json schema 必须为 {HISTORY_SCHEMA}")
    versions = data["versions"]
    if not isinstance(versions, dict):
        raise ValueError("history_hashes.json versions 必须为对象")
    parsed: dict[str, dict[str, str]] = {}
    for version, mapping in versions.items():
        if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
            raise ValueError(f"history_hashes.json 包含非法版本号键：{version!r}")
        parsed[version] = validate_file_hashes(mapping)
    return parsed


def _snapshot_is_clean_match(disk: DiskSnapshot, expected_hashes: Mapping[str, str]) -> bool:
    if not disk.exists or disk.has_symlink or disk.non_regular_entries or disk.empty_dirs:
        return False
    return dict(disk.files) == dict(expected_hashes)


def _matches_any_history(
    disk: DiskSnapshot,
    history_hashes: Mapping[str, Mapping[str, str]],
) -> bool:
    return any(_snapshot_is_clean_match(disk, hashes) for hashes in history_hashes.values())


def _conflict_install(reason: str) -> InstallPlan:
    if reason not in AGENT_CONFLICT_REASONS:
        raise ValueError(f"非法的冲突 reason：{reason!r}")
    return InstallPlan(ok=False, action="conflict", conflict_reason=reason)


def _conflict_uninstall(reason: str) -> UninstallPlan:
    if reason not in AGENT_CONFLICT_REASONS:
        raise ValueError(f"非法的冲突 reason：{reason!r}")
    return UninstallPlan(ok=False, action="conflict", conflict_reason=reason)


def plan_install(
    disk: DiskSnapshot,
    *,
    receipt: ReceiptRecord | None,
    receipt_corrupt: bool,
    current_version: str,
    current_hashes: Mapping[str, str],
    history_hashes: Mapping[str, Mapping[str, str]],
    force: bool = False,
    current_target: str | None = None,
    current_scope: str | None = None,
) -> InstallPlan:
    """根据磁盘快照、本机收据与官方当前/历史哈希，输出纯粹的安装决策计划。"""
    normalized_current = validate_file_hashes(current_hashes)
    if disk.has_symlink:
        return _conflict_install("agent_symlink_rejected")
    if receipt_corrupt and not force:
        return _conflict_install("agent_receipt_corrupt")

    backup_corrupt = bool(receipt_corrupt and force)
    effective_receipt = None if receipt_corrupt else receipt

    if not disk.exists:
        return InstallPlan(
            ok=True,
            action="installed",
            write_files=True,
            write_receipt=True,
            backup_corrupt_receipt=backup_corrupt,
        )

    if effective_receipt is not None:
        if _snapshot_is_clean_match(disk, normalized_current):
            needs_receipt_refresh = (
                effective_receipt.package_version != current_version
                or dict(effective_receipt.files) != normalized_current
                or (current_target is not None and effective_receipt.target != current_target)
                or (current_scope is not None and effective_receipt.scope != current_scope)
            )
            return InstallPlan(
                ok=True,
                action="unchanged",
                write_files=False,
                write_receipt=needs_receipt_refresh,
                backup_corrupt_receipt=backup_corrupt,
            )

        stale_or_extra = tuple(
            sorted(
                (set(disk.files) - set(normalized_current))
                | set(disk.non_regular_entries)
                | set(disk.empty_dirs)
            )
        )
        if _snapshot_is_clean_match(disk, effective_receipt.files) or _matches_any_history(
            disk, history_hashes
        ):
            return InstallPlan(
                ok=True,
                action="upgraded",
                write_files=True,
                write_receipt=True,
                backup_corrupt_receipt=backup_corrupt,
                files_to_remove=stale_or_extra,
            )

        if force:
            removed_extra = tuple(
                sorted(
                    (set(disk.files) - set(normalized_current) - set(effective_receipt.files))
                    | set(disk.non_regular_entries)
                    | set(disk.empty_dirs)
                )
            )
            return InstallPlan(
                ok=True,
                action="upgraded",
                write_files=True,
                write_receipt=True,
                backup_corrupt_receipt=backup_corrupt,
                files_to_remove=stale_or_extra,
                removed_extra_files=removed_extra,
            )

        return _conflict_install("agent_user_modified")

    # 本机无登记：先看是否可直接认领干净官方文件（B3），否则仅在 --force 下清脏文件重装（B4）
    if _snapshot_is_clean_match(disk, normalized_current):
        return InstallPlan(
            ok=True,
            action="adopted",
            write_files=False,
            write_receipt=True,
            backup_corrupt_receipt=backup_corrupt,
        )

    stale_or_extra = tuple(
        sorted(
            (set(disk.files) - set(normalized_current))
            | set(disk.non_regular_entries)
            | set(disk.empty_dirs)
        )
    )
    if _matches_any_history(disk, history_hashes):
        return InstallPlan(
            ok=True,
            action="adopted_and_upgraded",
            write_files=True,
            write_receipt=True,
            backup_corrupt_receipt=backup_corrupt,
            files_to_remove=stale_or_extra,
        )

    if force:
        return InstallPlan(
            ok=True,
            action="installed",
            write_files=True,
            write_receipt=True,
            backup_corrupt_receipt=backup_corrupt,
            files_to_remove=stale_or_extra,
            removed_extra_files=stale_or_extra,
        )

    return _conflict_install("agent_unmanaged_conflict")


def plan_uninstall(
    disk: DiskSnapshot,
    *,
    receipt: ReceiptRecord | None,
    receipt_corrupt: bool,
    current_hashes: Mapping[str, str],
    history_hashes: Mapping[str, Mapping[str, str]],
    force: bool = False,
) -> UninstallPlan:
    """根据磁盘快照、本机收据与官方当前/历史哈希，输出纯粹的卸载决策计划。"""
    normalized_current = validate_file_hashes(current_hashes)
    if disk.has_symlink:
        return _conflict_uninstall("agent_symlink_rejected")
    if receipt_corrupt:
        return _conflict_uninstall("agent_receipt_corrupt")

    if receipt is not None:
        if not disk.exists:
            return UninstallPlan(
                ok=True,
                action="uninstalled",
                remove_receipt=True,
                files_to_remove=(),
                preserved_files=(),
                remove_dir_if_empty=False,
            )

        is_clean_managed = (
            _snapshot_is_clean_match(disk, normalized_current)
            or _snapshot_is_clean_match(disk, receipt.files)
            or _matches_any_history(disk, history_hashes)
        )
        if is_clean_managed:
            return UninstallPlan(
                ok=True,
                action="uninstalled",
                remove_receipt=True,
                files_to_remove=tuple(sorted(disk.files)),
                preserved_files=(),
                remove_dir_if_empty=True,
            )

        if not force:
            return _conflict_uninstall("agent_user_modified")

        managed_present = tuple(sorted(k for k in receipt.files if k in disk.files))
        preserved = tuple(
            sorted((set(disk.files) - set(receipt.files)) | set(disk.non_regular_entries))
        )
        return UninstallPlan(
            ok=True,
            action="uninstalled",
            remove_receipt=True,
            files_to_remove=managed_present,
            preserved_files=preserved,
            remove_dir_if_empty=not preserved,
        )

    # 未在本机收据中登记：仅干净匹配官方版本允许直接卸载，否则即使 --force 也绝不删除（B4）
    if _snapshot_is_clean_match(disk, normalized_current) or _matches_any_history(disk, history_hashes):
        return UninstallPlan(
            ok=True,
            action="uninstalled",
            remove_receipt=False,
            files_to_remove=tuple(sorted(disk.files)),
            preserved_files=(),
            remove_dir_if_empty=True,
        )

    return _conflict_uninstall("agent_not_managed")


def inspect_target_status(
    disk: DiskSnapshot,
    *,
    receipt: ReceiptRecord | None,
    current_hashes: Mapping[str, str],
    history_hashes: Mapping[str, Mapping[str, str]],
) -> TargetStatus:
    """计算单个目标路径面向 status 命令的实时状态及文件摘要。"""
    normalized_current = validate_file_hashes(current_hashes)
    file_items = tuple(sorted(disk.files.items()))
    expected_keys = set(normalized_current) | (set(receipt.files) if receipt is not None else set())
    extra_files = tuple(
        sorted(
            (set(disk.files) - expected_keys)
            | set(disk.non_regular_entries)
            | set(disk.empty_dirs)
        )
    )

    if disk.has_symlink:
        return TargetStatus(state="symlink", files=file_items, extra_files=extra_files)

    if receipt is not None:
        if not disk.exists:
            return TargetStatus(state="missing", files=(), extra_files=())
        if _snapshot_is_clean_match(disk, normalized_current):
            return TargetStatus(state="clean", files=file_items, extra_files=())
        if _snapshot_is_clean_match(disk, receipt.files) or _matches_any_history(disk, history_hashes):
            return TargetStatus(state="upgradable", files=file_items, extra_files=())
        return TargetStatus(state="modified", files=file_items, extra_files=extra_files)

    if not disk.exists:
        return TargetStatus(state="not_installed", files=(), extra_files=())
    if _snapshot_is_clean_match(disk, normalized_current):
        return TargetStatus(state="adoptable_current", files=file_items, extra_files=())
    if _matches_any_history(disk, history_hashes):
        return TargetStatus(state="adoptable_upgrade", files=file_items, extra_files=())
    return TargetStatus(state="unmanaged_conflict", files=file_items, extra_files=extra_files)
