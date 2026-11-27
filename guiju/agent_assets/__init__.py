"""包内使用态 Agent 资产加载器 —— 仅读取随包分发的单一真源资源。"""

from __future__ import annotations

from importlib.resources import files

from ..domain.agent_assets import (
    derive_managed_files,
    extract_mcp_instructions,
    hash_managed_files,
    parse_history_hashes,
    render_skill_markdown,
)


def read_contract_markdown() -> str:
    return files(__package__).joinpath("contract.md").read_text(encoding="utf-8")


def read_skill_frontmatter() -> str:
    return files(__package__).joinpath("skill_frontmatter.yaml").read_text(encoding="utf-8")


def read_history_hashes_json() -> str:
    return files(__package__).joinpath("history_hashes.json").read_text(encoding="utf-8")


def load_skill_markdown() -> str:
    return render_skill_markdown(read_skill_frontmatter(), read_contract_markdown())


def load_mcp_instructions() -> str:
    return extract_mcp_instructions(read_contract_markdown())


def load_current_hashes() -> dict[str, str]:
    managed = derive_managed_files(read_skill_frontmatter(), read_contract_markdown())
    return hash_managed_files(managed)


def load_history_hashes() -> dict[str, dict[str, str]]:
    return parse_history_hashes(read_history_hashes_json())
