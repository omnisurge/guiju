#!/usr/bin/env python3
"""发版一致性检查 —— 两个互斥档位。

用法：
    python3 scripts/check-release.py --docs-only
    python3 scripts/check-release.py --artifacts [--dist-dir dist]

--docs-only  只查版本/文档/平台/CHANGELOG/发布工作流是否一致；不需要 dist 或 tag。
             这是 PR 与本地 CI 的默认门（scripts/ci.sh、.github/workflows/ci.yml）。
--artifacts  在 --docs-only 之外，再查 dist/SHA256SUMS、ACCEPTANCE 记录的产物哈希与
             tag 是否指向最终 HEAD；只在发版流程（.github/workflows/release-check.yml）
             或 Lead 冻结后的本地发布门运行。

退出码：0 全过；1 有失败；2 用法错误。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# docs/audit 下的发版记录固定为 release-<pep440>-<yyyymmdd>.md；
# 这里只认与当前版本同前缀的唯一记录。
AUDIT_GLOB = "release-{version}-*.md"
RELEASE_NOTES_DIR = Path("docs/releases")


class Checker:
    def __init__(self, label: str) -> None:
        self.label = label
        self.failures: list[str] = []
        self.checks = 0

    def check(self, condition: bool, message: str) -> bool:
        self.checks += 1
        if condition:
            return True
        self.failures.append(message)
        print(f"[FAIL] {message}")
        return False

    def finish(self) -> int:
        if self.failures:
            print()
            print(f"{self.label}：FAIL（{len(self.failures)} 项失败 / 共 {self.checks} 项检查）")
            return 1
        print()
        print(f"{self.label}：PASS（{self.checks} 项检查）")
        return 0


def _read(rel: str | Path) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _source_version() -> str:
    text = _read("guiju/_version.py")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
    if not match:
        raise SystemExit("guiju/_version.py 里找不到 __version__")
    return match.group(1)


def _pretty_version(version: str) -> str:
    """PEP 440 预发布版本 → 文档/标签用的连字符形式。"""
    match = re.fullmatch(r"(\d+\.\d+\.\d+)(?:(a|b|rc)(\d+))?", version)
    if not match:
        raise SystemExit(f"无法解析版本号：{version}")
    base, pre, number = match.groups()
    if not pre:
        return base
    labels = {"a": "alpha", "b": "beta", "rc": "rc"}
    return f"{base}-{labels[pre]}.{number}"


def _first_heading(text: str) -> str:
    match = re.search(r"^##\s+(.+)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _section(text: str, title: str) -> str:
    """取 '## title' 到下一个二级标题之间的正文；找不到返回空串。"""
    pattern = re.compile(rf"^##\s+{re.escape(title)}\s*$", flags=re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    tail = text[match.end():]
    end = re.search(r"^##\s+", tail, flags=re.MULTILINE)
    return tail[: end.start()] if end else tail


def _release_section(text: str, pretty: str) -> str:
    """按 '## v<pretty>（<date>[，预发布]）' 取当前版本段，不绑定日期/状态。"""
    pattern = re.compile(rf"^##\s+v{re.escape(pretty)}（[^）]*）\s*$", flags=re.MULTILINE)
    match = pattern.search(text)
    if not match:
        return ""
    tail = text[match.end():]
    end = re.search(r"^##\s+", tail, flags=re.MULTILINE)
    return tail[: end.start()] if end else tail


def _discovered_test_count() -> int:
    """当前工作树的 unittest 发现数量 —— 用实际数核对文档里的计数。"""
    import unittest

    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    return suite.countTestCases()


def _extract_count(pattern: str, text: str, label: str, checker: Checker) -> int | None:
    match = re.search(pattern, text)
    if not match:
        checker.check(False, f"{label}：找不到当前单测计数（期望模式 {pattern!r}）")
        return None
    return int(match.group(1))


def check_docs(checker: Checker) -> tuple[str, str, int | None]:
    version = _source_version()
    pretty = _pretty_version(version)
    print(f"== 发版检查 · docs-only ==  version={version}  tag=v{pretty}")

    # 1) 唯一版本源与打包配置
    pyproject = _read("pyproject.toml")
    checker.check(
        'version = { attr = "guiju._version.__version__" }' in pyproject,
        "pyproject.toml 的 dynamic version 没有指向 guiju._version.__version__",
    )

    # 2) CHANGELOG 顶部必须就是当前版本
    changelog = _read("CHANGELOG.md")
    top = _first_heading(changelog)
    checker.check(
        top.startswith(version),
        f"CHANGELOG 最新版本条目是 {top!r}，不是当前的 {version}",
    )
    changelog_body = _section(changelog, top)
    checker.check(
        pretty in top or pretty in changelog_body or version in changelog_body,
        f"CHANGELOG 的 {top!r} 条目没有写明 {pretty}",
    )

    # 3) README 当前状态
    readme = _read("README.md")
    match = re.search(r"\|\s*版本\s*\|\s*`([^`]+)`（schema v2）\s*\|", readme)
    checker.check(bool(match), "README 状态表找不到「版本」行")
    if match:
        checker.check(match.group(1) == pretty, f"README 当前版本是 {match.group(1)!r}，应为 {pretty!r}")
    checker.check(
        f"> v{pretty} 发布包" in readme,
        f"README 缺少「> v{pretty} 发布包与完整自部署步骤」",
    )
    readme_count = _extract_count(r"\|\s*单测 / 端到端\s*\|\s*(\d+) 项单测", readme, "README", checker)
    checker.check("39/39" in readme, "README 没有写当前完整联网 e2e 39/39")

    # 4) HANDOFF 首段
    handoff = _read("HANDOFF.md")
    head = "\n".join(handoff.splitlines()[:10])
    checker.check(
        f"状态：**v{pretty} 已发布**" in head,
        f"HANDOFF 首段没有「状态：**v{pretty} 已发布**」",
    )
    handoff_count = _extract_count(r"单测 \*\*(\d+) 项\*\*", head, "HANDOFF", checker)
    checker.check("39/39" in head, "HANDOFF 首段没有写完整联网 e2e 39/39")
    checker.check(
        f"https://github.com/omnisurge/guiju/releases/tag/v{pretty}" in handoff,
        f"HANDOFF 缺少发布页 https://github.com/omnisurge/guiju/releases/tag/v{pretty}",
    )

    # 5) INSTALL 标题与下载名
    install = _read("INSTALL.md")
    checker.check(
        f"# guiju v{pretty} 安装与自部署" in install,
        f"INSTALL 标题不是「# guiju v{pretty} 安装与自部署」",
    )
    wheel_name = f"guiju-{version}-py3-none-any.whl"
    checker.check(wheel_name in install, f"INSTALL 没有引用 {wheel_name}")
    checker.check(
        f"https://github.com/omnisurge/guiju/releases/tag/v{pretty}" in install,
        f"INSTALL 缺少发布页 v{pretty} 链接",
    )
    checker.check(
        "Linux / macOS" in install or "Linux/macOS" in install,
        "INSTALL 没有明确 Linux / macOS 平台",
    )

    # 6) ACCEPTANCE 顶部与当前版本段
    acceptance = _read("docs/ACCEPTANCE.md")
    acc_head = "\n".join(acceptance.splitlines()[:8])
    checker.check(
        f"最新发布验收：**v{pretty}" in acc_head,
        f"ACCEPTANCE 顶部不是「最新发布验收：**v{pretty}」",
    )
    acc_count = _extract_count(r"\*\*(\d+) 项单测\*\*", acc_head, "ACCEPTANCE 顶部", checker)
    checker.check("39/39" in acc_head, "ACCEPTANCE 顶部没有写完整联网 e2e 39/39")
    section = _release_section(acceptance, pretty)
    checker.check(bool(section), f"ACCEPTANCE 缺少 v{pretty} 的发布记录段")
    if section:
        matrix = re.search(r"各\s*(\d+)/\d+\s*通过", section)
        checker.check(bool(matrix), "ACCEPTANCE 当前版本段缺少「各 N/N 通过」单测矩阵")
        if matrix and acc_count is not None:
            checker.check(
                int(matrix.group(1)) == acc_count,
                f"ACCEPTANCE 顶部 {acc_count} 项与版本段 {matrix.group(1)} 项不一致",
            )
        checker.check("39/39" in section, "ACCEPTANCE 当前版本段没有写完整联网 e2e 39/39")

    # 7) 文档计数一致（README / HANDOFF / ACCEPTANCE）
    counts = [count for count in (readme_count, handoff_count, acc_count) if count is not None]
    if len(counts) == 3:
        checker.check(
            len(set(counts)) == 1,
            f"README/HANDOFF/ACCEPTANCE 的当前单测计数不一致：{counts}",
        )
        current_count = counts[0]
        checker.check(current_count > 0, "当前单测计数必须大于 0")
        actual_count = _discovered_test_count()
        checker.check(
            actual_count == current_count,
            f"文档写 {current_count} 项单测，但当前工作树实际发现 {actual_count} 项",
        )
    else:
        current_count = None

    # 8) POSIX-only 平台声明
    checker.check(
        "Operating System :: OS Independent" not in pyproject,
        "pyproject.toml 仍声明 Operating System :: OS Independent（本版本起 POSIX-only）",
    )
    for classifier in ("Operating System :: POSIX :: Linux", "Operating System :: MacOS"):
        checker.check(classifier in pyproject, f"pyproject.toml 缺少平台声明 {classifier!r}")

    # 9) 发版记录与 release notes
    audit_matches = sorted((ROOT / "docs/audit").glob(AUDIT_GLOB.format(version=version)))
    checker.check(bool(audit_matches), f"缺少 docs/audit/{AUDIT_GLOB.format(version=version)} 发版记录")
    if audit_matches:
        audit_name = audit_matches[0].name
        audit_text = audit_matches[0].read_text(encoding="utf-8")
        checker.check(f"v{pretty}" in audit_text, f"{audit_name} 没有写明 v{pretty}")
        checker.check("39/39" in audit_text, f"{audit_name} 没有记录联网 e2e 39/39")
        if current_count is not None:
            checker.check(str(current_count) in audit_text, f"{audit_name} 没有记录当前单测计数 {current_count}")
        docs_readme = _read("docs/README.md")
        checker.check(audit_name in docs_readme, f"docs/README.md 索引没有链接 {audit_name}")
        remediation = _read("docs/remediation-2026-11-18.md")
        remediation_head = "\n".join(remediation.splitlines()[:15])
        checker.check("后续状态" in remediation_head, "docs/remediation 顶部没有「后续状态见新记录」按语")
        checker.check(audit_name in remediation_head, f"docs/remediation 顶部没有链接 {audit_name}")

    notes = ROOT / RELEASE_NOTES_DIR / f"v{pretty}.md"
    checker.check(notes.is_file(), f"缺少 release notes：{notes.relative_to(ROOT)}")
    if notes.is_file():
        notes_text = notes.read_text(encoding="utf-8")
        checker.check(wheel_name in notes_text, f"release notes 没有列出 {wheel_name}")
        checker.check(f"guiju-{version}.tar.gz" in notes_text, f"release notes 没有列出 guiju-{version}.tar.gz")

    # 10) 发布工作流接线：docs-only 进默认门，artifacts 只在发版 workflow
    ci_sh = _read("scripts/ci.sh")
    checker.check("check-release.py --docs-only" in ci_sh, "scripts/ci.sh 没有接入 check-release.py --docs-only")
    ci_yml = _read(".github/workflows/ci.yml")
    checker.check("check-release.py --docs-only" in ci_yml, ".github/workflows/ci.yml 的 test job 没有接入 --docs-only")
    checker.check("--artifacts" not in ci_yml, ".github/workflows/ci.yml 不应运行 --artifacts")
    release_yml_path = ROOT / ".github/workflows/release-check.yml"
    checker.check(release_yml_path.is_file(), "缺少 .github/workflows/release-check.yml")
    if release_yml_path.is_file():
        release_yml = release_yml_path.read_text(encoding="utf-8")
        checker.check("workflow_dispatch" in release_yml, "release-check.yml 缺少 workflow_dispatch")
        checker.check("tags:" in release_yml and "v*" in release_yml, "release-check.yml 缺少 v* tag 触发")
        checker.check("pull_request" not in release_yml, "release-check.yml 不应在 pull_request 上运行")
        checker.check("check-release.py --artifacts" in release_yml, "release-check.yml 没有运行 --artifacts")

    return version, pretty, current_count


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)


def _recorded_hash(text: str, filename: str) -> str | None:
    index = text.find(filename)
    if index < 0:
        return None
    window = text[index : index + 260]
    match = re.search(r"SHA-256\s*`([0-9a-f]{64})`", window)
    return match.group(1) if match else None


def check_artifacts(checker: Checker, version: str, pretty: str, dist_dir: Path, skip_acceptance: bool, skip_tag: bool) -> None:
    print()
    print(f"== 发版检查 · artifacts ==  dist={dist_dir}")
    wheel = dist_dir / f"guiju-{version}-py3-none-any.whl"
    sdist = dist_dir / f"guiju-{version}.tar.gz"
    checker.check(wheel.is_file(), f"缺少 wheel：{wheel}")
    checker.check(sdist.is_file(), f"缺少 sdist：{sdist}")
    sums = dist_dir / "SHA256SUMS"
    checker.check(sums.is_file(), f"缺少 {sums}")

    if sums.is_file():
        lines = [line.strip() for line in sums.read_text(encoding="utf-8").splitlines() if line.strip()]
        listed: dict[str, str] = {}
        for line in lines:
            match = re.fullmatch(r"([0-9a-f]{64})\s+[*]?(.+)", line)
            if not match:
                checker.check(False, f"SHA256SUMS 行格式不合法：{line!r}")
                continue
            digest, name = match.groups()
            listed[name] = digest
            path = dist_dir / name
            checker.check(path.is_file(), f"SHA256SUMS 列出的文件不存在：{name}")
            if path.is_file():
                actual = _sha256(path)
                checker.check(actual == digest, f"{name} 的 SHA-256 与 SHA256SUMS 不一致")
        for artifact in (wheel.name, sdist.name):
            checker.check(artifact in listed, f"SHA256SUMS 没有列出 {artifact}")

    if not skip_acceptance:
        acceptance = _read("docs/ACCEPTANCE.md")
        section = _release_section(acceptance, pretty)
        checker.check(bool(section), f"ACCEPTANCE 缺少 v{pretty} 的发布记录段，无法核对产物哈希")
        if section:
            for artifact, path in ((wheel.name, wheel), (sdist.name, sdist)):
                recorded = _recorded_hash(section, artifact)
                checker.check(recorded is not None, f"ACCEPTANCE 没有记录 {artifact} 的 SHA-256")
                if recorded is not None and path.is_file():
                    actual = _sha256(path)
                    checker.check(
                        recorded == actual,
                        f"ACCEPTANCE 记录的 {artifact} SHA-256 与 dist 实际文件不一致",
                    )

    if not skip_tag:
        tag = f"v{pretty}"
        tag_result = _git("rev-parse", "--verify", f"{tag}^{{commit}}")
        checker.check(tag_result.returncode == 0, f"git tag {tag} 不存在")
        head_result = _git("rev-parse", "HEAD")
        checker.check(head_result.returncode == 0, "无法读取 git HEAD")
        if tag_result.returncode == 0 and head_result.returncode == 0:
            checker.check(
                tag_result.stdout.strip() == head_result.stdout.strip(),
                f"tag {tag} 没有指向当前 HEAD（{tag_result.stdout.strip()[:12]} != {head_result.stdout.strip()[:12]}）",
            )
        status = _git("status", "--porcelain")
        checker.check(status.returncode == 0, "无法读取 git status")
        checker.check(not status.stdout.strip(), "git status --porcelain 不为空（发布前必须干净）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="guiju 发版一致性检查")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--docs-only", action="store_true", help="只查版本/文档/平台/CHANGELOG（默认）")
    mode.add_argument("--artifacts", action="store_true", help="再查 dist/SHA256SUMS 与 tag-HEAD")
    parser.add_argument("--dist-dir", default="dist", help="产物目录（默认 dist）")
    parser.add_argument(
        "--skip-acceptance-hashes",
        action="store_true",
        help="供 release workflow 重建产物时使用：不比对 ACCEPTANCE 记录的哈希",
    )
    parser.add_argument(
        "--skip-tag",
        action="store_true",
        help="供 release workflow 在 tag 尚未创建的演练场景使用：不检查 tag-HEAD",
    )
    args = parser.parse_args(argv)

    checker = Checker("check-release")
    version, pretty, _ = check_docs(checker)
    if args.artifacts:
        check_artifacts(
            checker,
            version,
            pretty,
            (ROOT / args.dist_dir).resolve(),
            skip_acceptance=args.skip_acceptance_hashes,
            skip_tag=args.skip_tag,
        )
    return checker.finish()


if __name__ == "__main__":
    sys.exit(main())
