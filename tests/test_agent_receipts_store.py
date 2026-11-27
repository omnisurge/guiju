"""v1.5.0 S1：AgentReceiptStore 持久化、损坏备份、只读语义与 flock 并发单测。"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from guiju.domain.agent_assets import ReceiptRecord, sha256_text
from guiju.errors import AgentConflict, NeedsHuman
from guiju.store.agent_receipts import AgentReceiptStore

ROOT = Path(__file__).resolve().parent.parent


class AgentReceiptStoreTest(unittest.TestCase):
    def _record(
        self,
        *,
        version: str = "1.5.0",
        target: str = "agents",
        scope: str = "project",
    ) -> ReceiptRecord:
        return ReceiptRecord(
            skill="guiju-standards",
            target=target,
            scope=scope,
            package_version=version,
            installed_at="2026-11-27 14:00:00",
            files={"SKILL.md": sha256_text(f"skill-{version}")},
        )

    def test_load_missing_does_not_create_parent_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home_dir = Path(tmp) / "nonexistent-guiju-home"
            store = AgentReceiptStore(home_dir / "agent-receipts.json")
            snap = store.load()
            self.assertFalse(snap.exists)
            self.assertFalse(snap.corrupt)
            self.assertEqual(dict(snap.installs), {})
            self.assertFalse(home_dir.exists())

    def test_save_and_load_roundtrip_and_immutable_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "agent-receipts.json"
            store = AgentReceiptStore(receipt_path, clock=lambda: 1764223200.0)
            key = "/abs/project/.agents/skills/guiju-standards"
            saved = store.save({key: self._record()})
            self.assertTrue(saved.exists)
            self.assertFalse(saved.corrupt)
            with self.assertRaises(TypeError):
                saved.installs["/other/guiju-standards"] = self._record()  # type: ignore[index]

            reloaded = store.load()
            self.assertTrue(reloaded.exists)
            self.assertFalse(reloaded.corrupt)
            rec = reloaded.installs[key]
            self.assertEqual(rec.package_version, "1.5.0")
            self.assertEqual(rec.files["SKILL.md"], sha256_text("skill-1.5.0"))

    def test_load_detects_corrupt_variants(self):
        valid_hash = sha256_text("ok")
        valid_entry = {
            "skill": "guiju-standards",
            "target": "agents",
            "scope": "project",
            "package_version": "1.5.0",
            "installed_at": "2026-11-27 14:00:00",
            "files": {"SKILL.md": valid_hash},
        }
        key = "/abs/project/.agents/skills/guiju-standards"

        corrupt_cases = {
            "invalid_json": "{not json",
            "duplicate_key": '{"schema": 1, "schema": 1, "installs": {}}',
            "bool_schema": json.dumps({"schema": True, "installs": {}}),
            "wrong_schema": json.dumps({"schema": 2, "installs": {}}),
            "extra_top_key": json.dumps({"schema": 1, "installs": {}, "extra": 1}),
            "relative_key": json.dumps({"schema": 1, "installs": {"rel/guiju-standards": valid_entry}}),
            "non_normalized_key": json.dumps(
                {"schema": 1, "installs": {"/a/../b/guiju-standards": valid_entry}}
            ),
            "wrong_basename": json.dumps({"schema": 1, "installs": {"/a/b/other-skill": valid_entry}}),
            "bad_target": json.dumps(
                {"schema": 1, "installs": {key: {**valid_entry, "target": "evil"}}}
            ),
            "bad_scope": json.dumps(
                {"schema": 1, "installs": {key: {**valid_entry, "scope": "global"}}}
            ),
            "bad_date": json.dumps(
                {"schema": 1, "installs": {key: {**valid_entry, "installed_at": "2026-02-30 10:00:00"}}}
            ),
            "bad_date_space": json.dumps(
                {"schema": 1, "installs": {key: {**valid_entry, "installed_at": "2026-11-27  4:00:00"}}}
            ),
            "bad_version_ws": json.dumps(
                {"schema": 1, "installs": {key: {**valid_entry, "package_version": " 1.5.0"}}}
            ),
            "bad_sha": json.dumps(
                {"schema": 1, "installs": {key: {**valid_entry, "files": {"SKILL.md": "xyz"}}}}
            ),
        }

        with tempfile.TemporaryDirectory() as tmp:
            for name, content in corrupt_cases.items():
                path = Path(tmp) / f"{name}.json"
                path.write_text(content, encoding="utf-8")
                snap = AgentReceiptStore(path).load()
                with self.subTest(case=name):
                    self.assertTrue(snap.corrupt)
                    self.assertTrue(snap.exists)
                    with self.assertRaises(AgentConflict) as ctx:
                        snap.require_valid()
                    self.assertEqual(ctx.exception.reason, "agent_receipt_corrupt")

            # 非法 UTF-8 拒绝为损坏，符号链接（含悬空 symlink）直接以 agent_symlink_rejected 拒绝
            bin_path = Path(tmp) / "binary.json"
            bin_path.write_bytes(b"\xff\xfe\xfd")
            self.assertTrue(AgentReceiptStore(bin_path).load().corrupt)

            dangling = Path(tmp) / "dangling-receipt.json"
            dangling.symlink_to(Path(tmp) / "missing-target.json")
            with self.assertRaises(AgentConflict) as ctx_dangling:
                AgentReceiptStore(dangling).load()
            self.assertEqual(ctx_dangling.exception.reason, "agent_symlink_rejected")

            # GUIJU_HOME 被普通文件占用导致 ENOTDIR 时走 NeedsHuman (50)，不得误报损坏
            blocker = Path(tmp) / "home-as-file"
            blocker.write_text("x", encoding="utf-8")
            with self.assertRaises(NeedsHuman):
                AgentReceiptStore(blocker / "agent-receipts.json").load()

    def test_backup_corrupt_renames_atomically_and_handles_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "agent-receipts.json"
            receipt.write_text("{corrupt-1", encoding="utf-8")
            store = AgentReceiptStore(receipt, clock=lambda: 1764223200.0)
            b1 = store.backup_corrupt()
            self.assertRegex(b1.name, r"^agent-receipts\.json\.corrupt-\d{8}-\d{6}$")
            self.assertFalse(receipt.exists())
            self.assertEqual(b1.read_text(encoding="utf-8"), "{corrupt-1")

            receipt.write_text("{corrupt-2", encoding="utf-8")
            b2 = store.backup_corrupt()
            self.assertEqual(b2.name, f"{b1.name}-1")
            self.assertEqual(b2.read_text(encoding="utf-8"), "{corrupt-2")

    def test_symlink_receipt_and_lock_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            receipt = Path(tmp) / "agent-receipts.json"
            receipt.symlink_to(outside)

            store = AgentReceiptStore(receipt)
            with self.assertRaises(AgentConflict) as ctx_load:
                store.load()
            self.assertEqual(ctx_load.exception.reason, "agent_symlink_rejected")

            with self.assertRaises(AgentConflict) as ctx:
                store.save({"/tmp/p/guiju-standards": self._record()})
            self.assertEqual(ctx.exception.reason, "agent_symlink_rejected")

            with self.assertRaises(AgentConflict) as ctx2:
                store.backup_corrupt()
            self.assertEqual(ctx2.exception.reason, "agent_symlink_rejected")

            lock_file = Path(tmp) / "agent-receipts.lock"
            lock_file.symlink_to(outside)
            with self.assertRaises(AgentConflict) as ctx3, store.locked():
                pass
            self.assertEqual(ctx3.exception.reason, "agent_symlink_rejected")

    def test_save_cleans_temp_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "agent-receipts.json"
            store = AgentReceiptStore(receipt)
            with (
                mock.patch("guiju.store.agent_receipts.os.replace", side_effect=OSError("disk full")),
                self.assertRaises(NeedsHuman),
            ):
                store.save({"/tmp/p/guiju-standards": self._record()})
            leftovers = [p for p in Path(tmp).iterdir() if ".tmp" in p.name]
            self.assertEqual(leftovers, [])

    def test_locked_raises_needs_human_when_home_path_uncreatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file-not-dir"
            blocker.write_text("x", encoding="utf-8")
            store = AgentReceiptStore(blocker / "agent-receipts.json")
            with self.assertRaises(NeedsHuman), store.locked():
                pass

    def test_multiprocess_flock_serializes_concurrent_saves_without_lost_updates(self):
        worker_code = """
import sys, time
from pathlib import Path
from guiju.domain.agent_assets import ReceiptRecord, sha256_text
from guiju.store.agent_receipts import AgentReceiptStore

receipt_path, key_prefix, worker_idx = sys.argv[1], sys.argv[2], int(sys.argv[3])
store = AgentReceiptStore(receipt_path)
rec = ReceiptRecord(
    skill="guiju-standards",
    target="agents",
    scope="project",
    package_version="1.5.0",
    installed_at="2026-11-27 14:00:00",
    files={"SKILL.md": sha256_text(f"w-{worker_idx}")},
)
with store.locked() as snap:
    updated = dict(snap.installs)
    time.sleep(0.02)
    updated[f"{key_prefix}/w{worker_idx}/guiju-standards"] = rec
    store.save(updated)
"""
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "home" / "agent-receipts.json"
            key_prefix = "/abs/work"
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", worker_code, str(receipt), key_prefix, str(i)],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for i in range(6)
            ]
            for proc in procs:
                stdout, stderr = proc.communicate(timeout=30)
                self.assertEqual(proc.returncode, 0, stdout + stderr)

            final_snap = AgentReceiptStore(receipt).load()
            self.assertFalse(final_snap.corrupt)
            self.assertEqual(len(final_snap.installs), 6)
            for i in range(6):
                self.assertIn(f"{key_prefix}/w{i}/guiju-standards", final_snap.installs)


if __name__ == "__main__":
    unittest.main()
