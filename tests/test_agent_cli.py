"""v1.5.0 S2：guiju/application/agent.py 与 CLI `guiju agent` 全场景端到端单测。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from guiju._version import __version__
from guiju.agent_assets import (
    load_current_hashes,
    load_skill_markdown,
    read_contract_markdown,
)
from guiju.application.agent import (
    install_agent_skill,
    status_agent_skills,
    uninstall_agent_skill,
)
from guiju.cli import main
from guiju.domain.agent_assets import sha256_text
from guiju.errors import EXIT_CONFLICT, EXIT_OK, EXIT_USAGE
from guiju.store.agent_receipts import AgentReceiptStore


def _run_agent_cli(
    argv: list[str],
    *,
    home: Path,
    cwd: Path,
    user_home: Path,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    env = {"GUIJU_HOME": str(home), "HOME": str(user_home), "GUIJU_NO_AUTO_SYNC": "1"}
    with (
        mock.patch.dict(os.environ, env),
        mock.patch("pathlib.Path.cwd", return_value=cwd),
        mock.patch("pathlib.Path.home", return_value=user_home),
        redirect_stdout(out),
        redirect_stderr(err),
    ):
        rc = main(argv)
    return rc, out.getvalue(), err.getvalue()


class AgentCliShowAndValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "guiju-home"
        self.cwd = self.root / "project"
        self.user_home = self.root / "user-home"
        self.cwd.mkdir()
        self.user_home.mkdir()

    def test_root_subcommands_and_show_readonly_parity(self):
        rc, out, err = _run_agent_cli(["agent", "--json"], home=self.home, cwd=self.cwd, user_home=self.user_home)
        self.assertEqual(rc, EXIT_OK, err)
        root_payload = json.loads(out)
        self.assertTrue(root_payload["ok"])
        self.assertEqual(root_payload["subcommands"], ["show", "install", "uninstall", "status"])
        self.assertNotIn("catalog", root_payload)

        # show 默认 --kind contract（纯文本直出原文，绝不建 GUIJU_HOME）
        rc_c, out_c, err_c = _run_agent_cli(["agent", "show"], home=self.home, cwd=self.cwd, user_home=self.user_home)
        self.assertEqual(rc_c, EXIT_OK, err_c)
        self.assertEqual(out_c, read_contract_markdown())
        self.assertFalse(self.home.exists())

        # show --kind skill --json
        rc_s, out_s, _ = _run_agent_cli(
            ["agent", "show", "--kind", "skill", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_s, EXIT_OK)
        skill_payload = json.loads(out_s)
        self.assertEqual(skill_payload["kind"], "skill")
        self.assertEqual(skill_payload["package_version"], __version__)
        self.assertEqual(skill_payload["content"], load_skill_markdown())
        self.assertEqual(skill_payload["sha256"], sha256_text(load_skill_markdown()))
        self.assertNotIn("catalog", skill_payload)
        self.assertFalse(self.home.exists())

        # 非法 --kind -> exit 2 bad_usage
        rc_bad, out_bad, _ = _run_agent_cli(
            ["agent", "show", "--kind", "unknown", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_bad, EXIT_USAGE)
        self.assertEqual(json.loads(out_bad)["reason"], "bad_usage")

    def test_option_mutual_exclusion_and_bad_usage_across_commands(self):
        custom_dir = str(self.root / "custom")
        bad_invocations = [
            ["agent", "install", "--dir", custom_dir, "--target", "agents", "--json"],
            ["agent", "install", "--dir", custom_dir, "--scope", "user", "--json"],
            ["agent", "install", "--dir", "", "--json"],
            ["agent", "install", "--dir", "   ", "--json"],
            ["agent", "install", "--target", "cursor", "--json"],
            ["agent", "install", "--scope", "global", "--json"],
            ["agent", "uninstall", "--dir", custom_dir, "--target", "claude", "--json"],
            ["agent", "uninstall", "--dir", custom_dir, "--scope", "project", "--json"],
            ["agent", "uninstall", "--dir", "", "--json"],
            ["agent", "status", "--dir", custom_dir, "--target", "agents", "--json"],
            ["agent", "status", "--dir", custom_dir, "--scope", "user", "--json"],
            ["agent", "status", "--dir", "  ", "--json"],
            ["agent", "status", "--target", "bad", "--json"],
        ]
        for argv in bad_invocations:
            with self.subTest(argv=argv):
                rc, out, _ = _run_agent_cli(argv, home=self.home, cwd=self.cwd, user_home=self.user_home)
                self.assertEqual(rc, EXIT_USAGE)
                payload = json.loads(out)
                self.assertFalse(payload["ok"])
                self.assertEqual(payload["exit"], EXIT_USAGE)
                self.assertEqual(payload["reason"], "bad_usage")


class AgentCliLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.home = self.root / "guiju-home"
        self.cwd = self.root / "project"
        self.user_home = self.root / "user-home"
        self.cwd.mkdir()
        self.user_home.mkdir()

    def test_install_defaults_targets_scopes_pure_dir_and_no_catalog_db(self):
        # 1) 默认：--target agents --scope project
        rc, out, err = _run_agent_cli(
            ["agent", "install", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc, EXIT_OK, err)
        p1 = json.loads(out)
        expected_p1 = self.cwd / ".agents/skills/guiju-standards"
        self.assertTrue(p1["ok"])
        self.assertEqual(p1["action"], "installed")
        self.assertEqual(p1["target"], "agents")
        self.assertEqual(p1["scope"], "project")
        self.assertEqual(p1["path"], str(expected_p1))
        self.assertEqual(p1["receipt_path"], str(self.home / "agent-receipts.json"))
        self.assertEqual(p1["package_version"], __version__)
        self.assertEqual(p1["files"], [{"relpath": "SKILL.md", "sha256": load_current_hashes()["SKILL.md"]}])
        self.assertEqual(p1["removed_extra_files"], [])
        self.assertIsNone(p1["backed_up_receipt"])
        self.assertNotIn("catalog", p1)
        # 目标目录绝无 .manifest.json 等私有文件，GUIJU_HOME 绝无 catalog.db
        self.assertEqual(sorted(p.name for p in expected_p1.iterdir()), ["SKILL.md"])
        self.assertFalse((self.home / "catalog.db").exists())

        # 幂等重跑 -> unchanged 且零写盘
        skill_file = expected_p1 / "SKILL.md"
        inode_before = skill_file.stat().st_ino
        rc2, out2, _ = _run_agent_cli(
            ["agent", "install", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc2, EXIT_OK)
        self.assertEqual(json.loads(out2)["action"], "unchanged")
        self.assertEqual(skill_file.stat().st_ino, inode_before)

        # 2) --target claude --scope user
        rc_cu, out_cu, _ = _run_agent_cli(
            ["agent", "install", "--target", "claude", "--scope", "user", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_cu, EXIT_OK)
        p_cu = json.loads(out_cu)
        self.assertEqual(p_cu["target"], "claude")
        self.assertEqual(p_cu["scope"], "user")
        self.assertEqual(p_cu["path"], str(self.user_home / ".claude/skills/guiju-standards"))

        # 3) --dir custom
        custom_parent = self.root / "custom-skills"
        rc_d, out_d, _ = _run_agent_cli(
            ["agent", "install", "--dir", str(custom_parent), "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_d, EXIT_OK)
        p_d = json.loads(out_d)
        self.assertEqual(p_d["target"], "custom")
        self.assertEqual(p_d["scope"], "custom")
        self.assertEqual(p_d["path"], str(custom_parent / "guiju-standards"))

        # status 全览列出 3 条 clean 记录
        rc_st, out_st, _ = _run_agent_cli(
            ["agent", "status", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_st, EXIT_OK)
        st = json.loads(out_st)
        self.assertEqual(st["mode"], "all")
        self.assertTrue(st["receipt_exists"])
        self.assertEqual(len(st["entries"]), 3)
        self.assertTrue(all(item["state"] == "clean" for item in st["entries"]))

    def test_status_readonly_and_single_states(self):
        # 收据缺失：mode=all 返回空且不建 GUIJU_HOME
        rc, out, _ = _run_agent_cli(
            ["agent", "status", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc, EXIT_OK)
        st0 = json.loads(out)
        self.assertEqual(st0["mode"], "all")
        self.assertFalse(st0["receipt_exists"])
        self.assertEqual(st0["entries"], [])
        self.assertFalse(self.home.exists())

        # 单目标未安装
        rc1, out1, _ = _run_agent_cli(
            ["agent", "status", "--target", "agents", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc1, EXIT_OK)
        s1 = json.loads(out1)
        self.assertEqual(s1["mode"], "single")
        self.assertEqual(s1["entries"][0]["state"], "not_installed")
        self.assertIsNone(s1["entries"][0]["receipt_version"])

        # 未登记目录但精确匹配官方当前版本 -> adoptable_current
        skill_dir = self.cwd / ".agents/skills/guiju-standards"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(load_skill_markdown(), encoding="utf-8")
        rc2, out2, _ = _run_agent_cli(
            ["agent", "status", "--target", "agents", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc2, EXIT_OK)
        self.assertEqual(json.loads(out2)["entries"][0]["state"], "adoptable_current")

        # 收据损坏 -> status 只读拒返 11 且不备份不修改收据
        self.home.mkdir(parents=True)
        receipt = self.home / "agent-receipts.json"
        receipt.write_text("{corrupt", encoding="utf-8")
        rc3, out3, _ = _run_agent_cli(
            ["agent", "status", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc3, EXIT_CONFLICT)
        self.assertEqual(json.loads(out3)["reason"], "agent_receipt_corrupt")
        self.assertEqual(receipt.read_text(encoding="utf-8"), "{corrupt")
        self.assertEqual([p.name for p in self.home.iterdir()], ["agent-receipts.json"])

    def test_smooth_upgrade_cross_machine_adopt_and_r1_team_sync(self):
        store = AgentReceiptStore(self.home / "agent-receipts.json", clock=lambda: 1764223200.0)
        assets_150 = {"SKILL.md": "skill-v1.5.0\n"}
        assets_151 = {"SKILL.md": "skill-v1.5.1\n"}
        assets_152 = {"SKILL.md": "skill-v1.5.2\n"}
        h150 = {"SKILL.md": sha256_text(assets_150["SKILL.md"])}
        h151 = {"SKILL.md": sha256_text(assets_151["SKILL.md"])}
        history = {"1.5.0": h150, "1.5.1": h151}

        # 先在 1.5.0 安装到 project/agents
        res_150 = install_agent_skill(
            store,
            cwd=self.cwd,
            user_home=self.user_home,
            package_version="1.5.0",
            managed_contents=assets_150,
            history_hashes={"1.5.0": h150},
        )
        self.assertEqual(res_150["action"], "installed")
        skill_dir = self.cwd / ".agents/skills/guiju-standards"

        # 1) 本机平滑升级 1.5.0 -> 1.5.1（无需 --force）
        res_up = install_agent_skill(
            store,
            cwd=self.cwd,
            user_home=self.user_home,
            package_version="1.5.1",
            managed_contents=assets_151,
            history_hashes=history,
        )
        self.assertEqual(res_up["action"], "upgraded")
        self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), assets_151["SKILL.md"])

        # 2) R1：团队成员提交 1.5.1，成员 A 收据仍记录 1.5.0，A git pull 并升级包到 1.5.1 后重跑 install
        with store.locked() as snap:
            old_rec = snap.installs[str(skill_dir)]
            from dataclasses import replace

            store.save({str(skill_dir): replace(old_rec, package_version="1.5.0", files=h150)})
        inode_before = (skill_dir / "SKILL.md").stat().st_ino
        res_pull = install_agent_skill(
            store,
            cwd=self.cwd,
            user_home=self.user_home,
            package_version="1.5.1",
            managed_contents=assets_151,
            history_hashes=history,
        )
        self.assertEqual(res_pull["action"], "unchanged")
        self.assertEqual((skill_dir / "SKILL.md").stat().st_ino, inode_before)
        self.assertEqual(store.load().installs[str(skill_dir)].package_version, "1.5.1")

        # 3) R1：git checkout 切回旧版 1.5.0 磁盘内容（本机收据 1.5.1、当前包 1.5.2） -> 无需 --force 平滑恢复为 1.5.2
        (skill_dir / "SKILL.md").write_text(assets_150["SKILL.md"], encoding="utf-8")
        st_co = status_agent_skills(
            store,
            cwd=self.cwd,
            user_home=self.user_home,
            target="agents",
            package_version="1.5.2",
            managed_contents=assets_152,
            history_hashes=history,
        )
        self.assertEqual(st_co["entries"][0]["state"], "upgradable")
        res_co = install_agent_skill(
            store,
            cwd=self.cwd,
            user_home=self.user_home,
            package_version="1.5.2",
            managed_contents=assets_152,
            history_hashes=history,
        )
        self.assertEqual(res_co["action"], "upgraded")
        self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), assets_152["SKILL.md"])

        # 4) B3：切换全新本机收据环境，分别认领当前版（adopted）与历史版（adopted_and_upgraded）
        fresh_store = AgentReceiptStore(self.root / "fresh-home/agent-receipts.json")
        res_ad_cur = install_agent_skill(
            fresh_store,
            cwd=self.cwd,
            user_home=self.user_home,
            package_version="1.5.2",
            managed_contents=assets_152,
            history_hashes=history,
        )
        self.assertEqual(res_ad_cur["action"], "adopted")

        fresh_store_2 = AgentReceiptStore(self.root / "fresh-home-2/agent-receipts.json")
        (skill_dir / "SKILL.md").write_text(assets_150["SKILL.md"], encoding="utf-8")
        st_old = status_agent_skills(
            fresh_store_2,
            cwd=self.cwd,
            user_home=self.user_home,
            target="agents",
            package_version="1.5.2",
            managed_contents=assets_152,
            history_hashes=history,
        )
        self.assertEqual(st_old["entries"][0]["state"], "adoptable_upgrade")
        res_ad_old = install_agent_skill(
            fresh_store_2,
            cwd=self.cwd,
            user_home=self.user_home,
            package_version="1.5.2",
            managed_contents=assets_152,
            history_hashes=history,
        )
        self.assertEqual(res_ad_old["action"], "adopted_and_upgraded")
        self.assertEqual((skill_dir / "SKILL.md").read_text(encoding="utf-8"), assets_152["SKILL.md"])

    def test_force_install_cleans_extra_files_and_blocking_file_then_idempotent(self):
        skills_parent = self.cwd / ".agents/skills"
        sibling_dir = skills_parent / "other-skill"
        sibling_dir.mkdir(parents=True)
        (sibling_dir / "SKILL.md").write_text("do not touch", encoding="utf-8")
        sibling_link = skills_parent / "other-link"
        sibling_link.symlink_to(sibling_dir)

        # 未受管阻塞文件占住 guiju-standards 路径
        target_dir = skills_parent / "guiju-standards"
        target_dir.write_text("blocking regular file", encoding="utf-8")
        rc_blk, out_blk, _ = _run_agent_cli(
            ["agent", "install", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_blk, EXIT_CONFLICT)
        self.assertEqual(json.loads(out_blk)["reason"], "agent_unmanaged_conflict")

        rc_f1, out_f1, _ = _run_agent_cli(
            ["agent", "install", "--force", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_f1, EXIT_OK)
        self.assertEqual(json.loads(out_f1)["action"], "installed")
        self.assertTrue(target_dir.is_dir())

        # 再写入额外脏文件、嵌套文件与空目录
        (target_dir / "notes.md").write_text("extra", encoding="utf-8")
        (target_dir / "sub/deep").mkdir(parents=True)
        (target_dir / "sub/deep/x.txt").write_text("nested extra", encoding="utf-8")
        (target_dir / "empty_dir").mkdir()

        rc_mod, out_mod, _ = _run_agent_cli(
            ["agent", "install", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_mod, EXIT_CONFLICT)
        self.assertEqual(json.loads(out_mod)["reason"], "agent_user_modified")

        rc_f2, out_f2, _ = _run_agent_cli(
            ["agent", "install", "--force", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_f2, EXIT_OK)
        p_f2 = json.loads(out_f2)
        self.assertEqual(p_f2["removed_extra_files"], ["empty_dir/", "notes.md", "sub/deep/x.txt"])
        self.assertEqual(sorted(p.name for p in target_dir.iterdir()), ["SKILL.md"])

        # 立即再跑一次不带 --force 必得 unchanged
        rc_again, out_again, _ = _run_agent_cli(
            ["agent", "install", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_again, EXIT_OK)
        self.assertEqual(json.loads(out_again)["action"], "unchanged")

        # 同级第三方 skill 目录与符号链接零触碰
        self.assertEqual((sibling_dir / "SKILL.md").read_text(encoding="utf-8"), "do not touch")
        self.assertTrue(sibling_link.is_symlink())

    def test_uninstall_boundaries_missing_dir_force_preserve_and_unmanaged_refusal(self):
        target_dir = self.cwd / ".agents/skills/guiju-standards"

        # 1) 目录不存在且无收据 -> even --force 拒绝 (agent_not_managed)
        for flags in ([], ["--force"]):
            rc, out, _ = _run_agent_cli(
                ["agent", "uninstall", *flags, "--json"],
                home=self.home,
                cwd=self.cwd,
                user_home=self.user_home,
            )
            self.assertEqual(rc, EXIT_CONFLICT)
            self.assertEqual(json.loads(out)["reason"], "agent_not_managed")

        # 2) 未受管未知目录 -> even --force 绝不强删
        target_dir.mkdir(parents=True)
        (target_dir / "important.txt").write_text("keep me", encoding="utf-8")
        for flags in ([], ["--force"]):
            rc, out, _ = _run_agent_cli(
                ["agent", "uninstall", *flags, "--json"],
                home=self.home,
                cwd=self.cwd,
                user_home=self.user_home,
            )
            self.assertEqual(rc, EXIT_CONFLICT)
            self.assertEqual(json.loads(out)["reason"], "agent_not_managed")
            self.assertEqual((target_dir / "important.txt").read_text(encoding="utf-8"), "keep me")

        # 3) 干净未登记官方目录（如队友提交的 SKILL.md）-> 直接安全卸载且不建 GUIJU_HOME
        (target_dir / "important.txt").unlink()
        (target_dir / "SKILL.md").write_text(load_skill_markdown(), encoding="utf-8")
        rc_cu, out_cu, _ = _run_agent_cli(
            ["agent", "uninstall", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_cu, EXIT_OK)
        self.assertEqual(json.loads(out_cu)["removed_files"], ["SKILL.md"])
        self.assertFalse(target_dir.exists())
        self.assertFalse(self.home.exists())

        # 4) R4：已登记安装后用户手工 rm -rf 目标目录 -> status 报 missing，uninstall 清陈旧收据返 0
        rc_i, _, _ = _run_agent_cli(
            ["agent", "install", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_i, EXIT_OK)
        (target_dir / "SKILL.md").unlink()
        target_dir.rmdir()
        rc_sm, out_sm, _ = _run_agent_cli(
            ["agent", "status", "--target", "agents", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_sm, EXIT_OK)
        self.assertEqual(json.loads(out_sm)["entries"][0]["state"], "missing")

        rc_rm, out_rm, _ = _run_agent_cli(
            ["agent", "uninstall", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_rm, EXIT_OK)
        p_rm = json.loads(out_rm)
        self.assertEqual(p_rm["removed_files"], [])
        self.assertEqual(p_rm["preserved_files"], [])
        self.assertEqual(AgentReceiptStore(self.home / "agent-receipts.json").load().installs, {})

        # 5) 已受管但含额外文件：uninstall 拒绝，uninstall --force 仅删受管文件并保留 notes.md
        _run_agent_cli(["agent", "install", "--json"], home=self.home, cwd=self.cwd, user_home=self.user_home)
        (target_dir / "notes.md").write_text("user notes", encoding="utf-8")
        (target_dir / "sub").mkdir()
        (target_dir / "sub/keep.md").write_text("keep nested", encoding="utf-8")

        rc_ref, out_ref, _ = _run_agent_cli(
            ["agent", "uninstall", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_ref, EXIT_CONFLICT)
        self.assertEqual(json.loads(out_ref)["reason"], "agent_user_modified")

        rc_uf, out_uf, _ = _run_agent_cli(
            ["agent", "uninstall", "--force", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_uf, EXIT_OK)
        p_uf = json.loads(out_uf)
        self.assertEqual(p_uf["removed_files"], ["SKILL.md"])
        self.assertEqual(p_uf["preserved_files"], ["notes.md", "sub/keep.md"])
        self.assertFalse((target_dir / "SKILL.md").exists())
        self.assertEqual((target_dir / "notes.md").read_text(encoding="utf-8"), "user notes")
        self.assertEqual((target_dir / "sub/keep.md").read_text(encoding="utf-8"), "keep nested")
        self.assertEqual(AgentReceiptStore(self.home / "agent-receipts.json").load().installs, {})

    def test_corrupt_receipt_recovery_and_uninstall_refusal(self):
        self.home.mkdir(parents=True)
        receipt = self.home / "agent-receipts.json"
        receipt.write_text("{broken receipt", encoding="utf-8")

        rc_no, out_no, _ = _run_agent_cli(
            ["agent", "install", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_no, EXIT_CONFLICT)
        self.assertEqual(json.loads(out_no)["reason"], "agent_receipt_corrupt")

        for flags in ([], ["--force"]):
            rc_u, out_u, _ = _run_agent_cli(
                ["agent", "uninstall", *flags, "--json"],
                home=self.home,
                cwd=self.cwd,
                user_home=self.user_home,
            )
            self.assertEqual(rc_u, EXIT_CONFLICT)
            self.assertEqual(json.loads(out_u)["reason"], "agent_receipt_corrupt")

        rc_f, out_f, _ = _run_agent_cli(
            ["agent", "install", "--force", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_f, EXIT_OK)
        p_f = json.loads(out_f)
        self.assertIsNotNone(p_f["backed_up_receipt"])
        backup = Path(p_f["backed_up_receipt"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), "{broken receipt")
        self.assertFalse(AgentReceiptStore(receipt).load().corrupt)

    def test_symlink_safety_matrix_across_live_dangling_and_nested_symlinks(self):
        skills_parent = self.cwd / ".agents/skills"
        skills_parent.mkdir(parents=True)
        target_dir = skills_parent / "guiju-standards"
        outside = self.root / "outside-victim"
        outside.mkdir()
        (outside / "secret.txt").write_text("untouched", encoding="utf-8")

        # 先在收据中模拟登记 target_dir（测 R4 悬空 symlink 与已登记 symlink）
        store = AgentReceiptStore(self.home / "agent-receipts.json")
        install_agent_skill(store, cwd=self.cwd, user_home=self.user_home)

        # 场景 A：target_dir 被替换为指向外部目录的 symlink（即使 --force 也拒删/拒写）
        (target_dir / "SKILL.md").unlink()
        target_dir.rmdir()
        target_dir.symlink_to(outside)
        for subcmd in ("install", "uninstall"):
            for flags in ([], ["--force"]):
                rc, out, _ = _run_agent_cli(
                    ["agent", subcmd, *flags, "--json"],
                    home=self.home,
                    cwd=self.cwd,
                    user_home=self.user_home,
                )
                self.assertEqual(rc, EXIT_CONFLICT)
                self.assertEqual(json.loads(out)["reason"], "agent_symlink_rejected")
                self.assertEqual((outside / "secret.txt").read_text(encoding="utf-8"), "untouched")
        rc_s, out_s, _ = _run_agent_cli(
            ["agent", "status", "--target", "agents", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_s, EXIT_OK)
        self.assertEqual(json.loads(out_s)["entries"][0]["state"], "symlink")

        # 场景 B：R4 关键边界——悬空 symlink（exists=False 但 is_symlink=True）不得当成 missing 放行卸载！
        target_dir.unlink()
        target_dir.symlink_to(self.root / "does-not-exist")
        rc_d, out_d, _ = _run_agent_cli(
            ["agent", "uninstall", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_d, EXIT_CONFLICT)
        self.assertEqual(json.loads(out_d)["reason"], "agent_symlink_rejected")
        rc_sd, out_sd, _ = _run_agent_cli(
            ["agent", "status", "--target", "agents", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_sd, EXIT_OK)
        self.assertEqual(json.loads(out_sd)["entries"][0]["state"], "symlink")

        # 场景 C：target_dir 为普通目录，但内部文件是 symlink（含 SKILL.md 自身）
        target_dir.unlink()
        target_dir.mkdir()
        (target_dir / "SKILL.md").symlink_to(outside / "secret.txt")
        for subcmd in ("install", "uninstall"):
            rc_in, out_in, _ = _run_agent_cli(
                ["agent", subcmd, "--force", "--json"],
                home=self.home,
                cwd=self.cwd,
                user_home=self.user_home,
            )
            self.assertEqual(rc_in, EXIT_CONFLICT)
            self.assertEqual(json.loads(out_in)["reason"], "agent_symlink_rejected")
            self.assertEqual((outside / "secret.txt").read_text(encoding="utf-8"), "untouched")

        # 清理 target_dir 内测试文件验证收据层 uninstall_agent_skill 不顺着坏 symlink
        (target_dir / "SKILL.md").unlink()
        res_ok = uninstall_agent_skill(store, cwd=self.cwd, user_home=self.user_home, force=True)
        self.assertEqual(res_ok["action"], "uninstalled")
        self.assertFalse(target_dir.exists())

    def test_human_mode_cli_outputs_and_no_catalog_db(self):
        rc_help, out_help, _ = _run_agent_cli(
            ["agent"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_help, EXIT_OK)
        self.assertIn("guiju agent", out_help)

        rc_st0, out_st0, _ = _run_agent_cli(
            ["agent", "status"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_st0, EXIT_OK)
        self.assertIn("尚无受管技能安装记录", out_st0)

        rc_i, out_i, _ = _run_agent_cli(
            ["agent", "install"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_i, EXIT_OK)
        self.assertIn("installed:", out_i)
        self.assertFalse((self.home / "catalog.db").exists())

        rc_st1, out_st1, _ = _run_agent_cli(
            ["agent", "status"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_st1, EXIT_OK)
        self.assertIn("[clean]", out_st1)

        rc_u, out_u, _ = _run_agent_cli(
            ["agent", "uninstall"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_u, EXIT_OK)
        self.assertIn("uninstalled:", out_u)

    def test_real_subprocess_agent_cli_roundtrip(self):
        import subprocess
        import sys

        repo_root = Path(__file__).resolve().parent.parent
        env = {
            **os.environ,
            "PYTHONPATH": str(repo_root),
            "GUIJU_HOME": str(self.home),
            "HOME": str(self.user_home),
            "GUIJU_NO_AUTO_SYNC": "1",
        }
        res_show = subprocess.run(
            [sys.executable, "-m", "guiju", "agent", "show"],
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_show.returncode, EXIT_OK, res_show.stderr)
        self.assertEqual(res_show.stdout, read_contract_markdown())
        self.assertFalse(self.home.exists())

        res_inst = subprocess.run(
            [sys.executable, "-m", "guiju", "agent", "install", "--json"],
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_inst.returncode, EXIT_OK, res_inst.stderr)
        inst_payload = json.loads(res_inst.stdout)
        self.assertEqual(inst_payload["action"], "installed")
        self.assertFalse((self.home / "catalog.db").exists())

        res_uninst = subprocess.run(
            [sys.executable, "-m", "guiju", "agent", "uninstall", "--json"],
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res_uninst.returncode, EXIT_OK, res_uninst.stderr)
        self.assertEqual(json.loads(res_uninst.stdout)["action"], "uninstalled")
        self.assertFalse((self.cwd / ".agents/skills/guiju-standards").exists())

    def test_red_team_receipt_symlink_order_ancestor_swap_and_meta_refresh(self):
        from guiju.errors import EXIT_NEEDS_HUMAN

        # 1) Defect 1a：收据为 symlink 且目标含未受管 notes.md 时，install --force 必须在改动磁盘前以 11 拒绝
        self.home.mkdir(parents=True)
        outside = self.root / "outside-receipt.json"
        outside.write_text("{}", encoding="utf-8")
        receipt = self.home / "agent-receipts.json"
        receipt.symlink_to(outside)

        target_dir = self.cwd / ".agents/skills/guiju-standards"
        target_dir.mkdir(parents=True)
        (target_dir / "notes.md").write_text("must survive", encoding="utf-8")

        rc_sym, out_sym, _ = _run_agent_cli(
            ["agent", "install", "--force", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_sym, EXIT_CONFLICT)
        self.assertEqual(json.loads(out_sym)["reason"], "agent_symlink_rejected")
        self.assertEqual((target_dir / "notes.md").read_text(encoding="utf-8"), "must survive")
        self.assertFalse((target_dir / "SKILL.md").exists())
        receipt.unlink()

        # 2) Defect 1b：GUIJU_HOME 本身被普通文件占用时走 exit 50 NeedsHuman，不误报坏收据 11
        home_file = self.root / "home-is-file"
        home_file.write_text("not a dir", encoding="utf-8")
        rc_nh, out_nh, _ = _run_agent_cli(
            ["agent", "status", "--json"],
            home=home_file,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_nh, EXIT_NEEDS_HUMAN)
        self.assertEqual(json.loads(out_nh)["reason"], "needs_human")

        # 3) Defect 1c：同物理路径切换 --target agents -> --dir <same_parent> 时刷新收据 target/scope
        (target_dir / "notes.md").unlink()
        target_dir.rmdir()
        rc_i1, out_i1, _ = _run_agent_cli(
            ["agent", "install", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_i1, EXIT_OK)
        self.assertEqual(json.loads(out_i1)["target"], "agents")

        rc_i2, out_i2, _ = _run_agent_cli(
            ["agent", "install", "--dir", str(self.cwd / ".agents/skills"), "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_i2, EXIT_OK)
        self.assertEqual(json.loads(out_i2)["action"], "unchanged")
        rec_refreshed = AgentReceiptStore(receipt).load().installs[str(target_dir)]
        self.assertEqual(rec_refreshed.target, "custom")
        self.assertEqual(rec_refreshed.scope, "custom")

        # 4) Defect 2：安装后父祖先目录 .agents/skills 被掉包为外部 symlink，status 全量巡检识别为 symlink 且拒绝卸载
        outside_skills = self.root / "outside-skills"
        (outside_skills / "guiju-standards").mkdir(parents=True)
        (outside_skills / "guiju-standards/SKILL.md").write_text("hijacked", encoding="utf-8")

        skills_parent = self.cwd / ".agents/skills"
        (target_dir / "SKILL.md").unlink()
        target_dir.rmdir()
        skills_parent.rmdir()
        skills_parent.symlink_to(outside_skills)

        rc_st, out_st, _ = _run_agent_cli(
            ["agent", "status", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_st, EXIT_OK)
        self.assertEqual(json.loads(out_st)["entries"][0]["state"], "symlink")

        from guiju.application.agent import scan_skill_dir

        self.assertTrue(scan_skill_dir(target_dir, expect_resolved_parent=True).has_symlink)

        rc_un, out_un, _ = _run_agent_cli(
            ["agent", "uninstall", "--force", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_un, EXIT_CONFLICT)
        self.assertIn(
            json.loads(out_un)["reason"],
            {"agent_not_managed", "agent_symlink_rejected"},
        )
        self.assertEqual(
            (outside_skills / "guiju-standards/SKILL.md").read_text(encoding="utf-8"),
            "hijacked",
        )

        # 5) 祖先路径变成互指 symlink 环（Python 3.10 resolve 抛 RuntimeError，3.13 resolve(strict=False) 原样返回）
        skills_parent.unlink()
        loop_a = self.cwd / ".agents/loop_a"
        loop_b = self.cwd / ".agents/loop_b"
        loop_a.symlink_to(loop_b)
        loop_b.symlink_to(loop_a)
        skills_parent.symlink_to(loop_a)
        self.assertTrue(scan_skill_dir(target_dir, expect_resolved_parent=True).has_symlink)
        rc_lp, out_lp, _ = _run_agent_cli(
            ["agent", "install", "--force", "--json"],
            home=self.home,
            cwd=self.cwd,
            user_home=self.user_home,
        )
        self.assertEqual(rc_lp, EXIT_CONFLICT)
        self.assertEqual(json.loads(out_lp)["reason"], "agent_symlink_rejected")


if __name__ == "__main__":
    unittest.main()
