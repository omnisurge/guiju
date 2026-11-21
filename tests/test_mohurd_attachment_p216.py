"""P2-16 attachment kind/magic tests."""

from __future__ import annotations

import unittest

from guiju.sources.mohurd import attachments, magic_of


class MohurdAttachmentTest(unittest.TestCase):
    def test_api_gateway_is_pdf_candidate(self):
        html = (
            '<a href="/api-gateway/a">某标准</a>'
            '<a href="/files/a.pdf">另一标准</a>'
            '<a href="/files/a.docx">旧条文</a>'
            '<a href="/files/a.rar">打包</a>'
        )
        got = {a.label: a.kind for a in attachments(html)}
        self.assertEqual(got["某标准"], "pdf_candidate")
        self.assertEqual(got["另一标准"], "pdf")
        self.assertEqual(got["旧条文"], "doc")
        self.assertEqual(got["打包"], "zip")

    def test_doc_magic_uses_real_bytes(self):
        self.assertEqual(magic_of(bytes.fromhex("d0cf11e0a1b2")), "doc")
        self.assertEqual(magic_of(b"D0CF11E0"), "unknown")


if __name__ == "__main__":
    unittest.main()
