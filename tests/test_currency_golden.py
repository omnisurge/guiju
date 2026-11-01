"""全部八份原始 Go golden 均按原始字节锁定，不依赖旧仓存在。"""

import hashlib
import json
import unittest
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "currency"


class GoldenInventoryTest(unittest.TestCase):
    def test_exact_eight_raw_fixtures_match_original_bytes(self):
        manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
        expected = {"csres_gb50011.gbk", "ccsn_gb50011.json", "ccsn_jgj147.json", "dbba_db31t792.json",
                    "dbba_db31_373.json", "stdsamr_gbt19001.json", "jtst_jtgd60.html", "jtst_empty.html"}
        self.assertEqual(manifest["schema"], 1)
        self.assertEqual(len(manifest["fixtures"]), 8)
        self.assertEqual({row["file"] for row in manifest["fixtures"]}, expected)
        for row in manifest["fixtures"]:
            with self.subTest(fixture=row["file"]):
                raw = (FIXTURES / row["file"]).read_bytes()
                self.assertEqual(len(raw), row["bytes"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), row["sha256"])


if __name__ == "__main__":
    unittest.main()
