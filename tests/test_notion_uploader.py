"""notion_uploader：页面 ID 解析与 markdown → Notion blocks。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import db  # noqa: E402
from notion_uploader import (  # noqa: E402
    _token_for,
    auth_status,
    create_article_page,
    exchange_code,
    extract_title,
    get_auth_url,
    is_configured,
    markdown_to_blocks,
    oauth_configured,
    parse_page_id,
)


class ParsePageIdTest(unittest.TestCase):
    def test_dashed_uuid(self):
        self.assertEqual(
            parse_page_id("59833787-2cf9-4fdf-8782-e53db20768a5"),
            "59833787-2cf9-4fdf-8782-e53db20768a5",
        )

    def test_compact_hex(self):
        self.assertEqual(
            parse_page_id("598337872cf94fdf8782e53db20768a5"),
            "59833787-2cf9-4fdf-8782-e53db20768a5",
        )

    def test_share_url(self):
        url = "https://www.notion.so/My-Title-598337872cf94fdf8782e53db20768a5"
        self.assertEqual(parse_page_id(url), "59833787-2cf9-4fdf-8782-e53db20768a5")

    def test_url_with_query(self):
        url = (
            "https://www.notion.so/workspace/"
            "598337872cf94fdf8782e53db20768a5?v=abcdef"
        )
        self.assertEqual(parse_page_id(url), "59833787-2cf9-4fdf-8782-e53db20768a5")

    def test_empty(self):
        self.assertEqual(parse_page_id(""), "")
        self.assertEqual(parse_page_id("not-an-id"), "")


class ExtractTitleTest(unittest.TestCase):
    def test_first_heading(self):
        md = "> 原视频链接：https://x\n\n# 真正的标题\n\n正文"
        self.assertEqual(extract_title(md, "fallback"), "真正的标题")

    def test_fallback(self):
        self.assertEqual(extract_title("没有标题的正文", "视频名"), "视频名")

    def test_generic(self):
        self.assertEqual(extract_title("", ""), "未命名文章")


class MarkdownToBlocksTest(unittest.TestCase):
    def _types(self, md: str) -> list[str]:
        return [b["type"] for b in markdown_to_blocks(md)]

    def test_headings_quote_paragraph(self):
        md = "> 原视频链接：https://example.com\n\n# 标题\n\n## 小节\n\n一段正文"
        types = self._types(md)
        self.assertEqual(types, ["quote", "heading_1", "heading_2", "paragraph"])
        blocks = markdown_to_blocks(md)
        self.assertIn("example.com", blocks[0]["quote"]["rich_text"][0]["text"]["content"])

    def test_lists_and_code(self):
        md = "- a\n- b\n\n1. one\n2. two\n\n```python\nprint(1)\n```"
        types = self._types(md)
        self.assertEqual(
            types,
            [
                "bulleted_list_item",
                "bulleted_list_item",
                "numbered_list_item",
                "numbered_list_item",
                "code",
            ],
        )
        code = markdown_to_blocks(md)[-1]
        self.assertEqual(code["code"]["language"], "python")
        self.assertEqual(code["code"]["rich_text"][0]["text"]["content"], "print(1)")

    def test_unknown_code_lang_falls_back(self):
        md = "```not-a-lang\nx\n```"
        block = markdown_to_blocks(md)[0]
        self.assertEqual(block["code"]["language"], "plain text")

    def test_divider_and_table_as_code(self):
        md = "---\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
        types = self._types(md)
        self.assertEqual(types, ["divider", "code"])

    def test_inline_bold_and_link(self):
        blocks = markdown_to_blocks("这是 **重点** 和 [链接](https://ex.com)")
        texts = blocks[0]["paragraph"]["rich_text"]
        kinds = [(t["text"]["content"], t.get("annotations", {}), t["text"].get("link")) for t in texts]
        self.assertTrue(any(c == "重点" and a.get("bold") for c, a, _ in kinds))
        self.assertTrue(any(c == "链接" and link == {"url": "https://ex.com"} for c, _, link in kinds))

    def _links(self, md: str) -> list[str | None]:
        blocks = markdown_to_blocks(md)
        urls: list[str | None] = []
        for block in blocks:
            payload = block.get(block.get("type")) or {}
            for t in payload.get("rich_text") or []:
                urls.append((t.get("text") or {}).get("link", {}).get("url") if (t.get("text") or {}).get("link") else None)
        return urls

    def test_toc_anchor_links_are_unlinked(self):
        md = "- [引言](#引言)\n- [总结](#总结)"
        blocks = markdown_to_blocks(md)
        self.assertEqual([b["type"] for b in blocks], ["bulleted_list_item", "bulleted_list_item"])
        for block in blocks:
            for t in block["bulleted_list_item"]["rich_text"]:
                self.assertIsNone((t.get("text") or {}).get("link"))
        self.assertEqual(blocks[0]["bulleted_list_item"]["rich_text"][0]["text"]["content"], "引言")

    def test_markdown_title_and_bare_host(self):
        md = '[a](https://example.com "官网") 和 [b](www.bilibili.com/video/BV1xx)'
        urls = [u for u in self._links(md) if u]
        self.assertEqual(urls, ["https://example.com", "https://www.bilibili.com/video/BV1xx"])

    def test_relative_and_empty_links_dropped(self):
        md = "[空]() [相对](./foo.md) [BV](BV1xx411c7mY)"
        self.assertFalse(any(self._links(md)))

    def test_heading_deeper_than_3_becomes_h3(self):
        self.assertEqual(self._types("#### 四级"), ["heading_3"])


class NotionTokenDbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        db.init_db()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_save_get_delete(self):
        self.assertIsNone(db.get_notion_token("u1"))
        self.assertFalse(is_configured("u1"))
        db.save_notion_token("u1", "secret-token")
        self.assertEqual(db.get_notion_token("u1"), "secret-token")
        self.assertTrue(is_configured("u1"))
        db.delete_notion_token("u1")
        self.assertIsNone(db.get_notion_token("u1"))
        self.assertFalse(is_configured("u1"))


class CreateArticlePageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        db.init_db()
        db.save_notion_token("u1", "secret")

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_missing_parent(self):
        result = create_article_page(
            markdown="# 标题\n\n正文",
            parent_page_id="",
            user_id="u1",
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], "missing_parent")

    def test_creates_page(self):
        parent = "59833787-2cf9-4fdf-8782-e53db20768a5"

        def fake_request(method, path, token, json_body=None, params=None, timeout=30):
            if method == "POST" and path == "/pages":
                self.assertEqual(json_body["parent"]["page_id"], parent)
                self.assertEqual(
                    json_body["properties"]["title"]["title"][0]["text"]["content"],
                    "标题",
                )
                children = json_body.get("children") or []
                self.assertTrue(any(c["type"] == "heading_1" for c in children))
                return {"id": "new-page-id", "url": "https://notion.so/new-page-id"}, None
            self.fail(f"unexpected request {method} {path}")

        with mock.patch("notion_uploader._request", side_effect=fake_request):
            result = create_article_page(
                markdown="# 标题\n\n正文",
                parent_page_id=parent,
                user_id="u1",
            )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["data"]["url"], "https://notion.so/new-page-id")
        self.assertEqual(result["data"]["title"], "标题")


class NotionOAuthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        db.init_db()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_oauth_not_configured_without_env(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("NOTION_CLIENT_ID", "NOTION_CLIENT_SECRET")
        }
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(oauth_configured())
            with self.assertRaises(FileNotFoundError):
                get_auth_url("http://127.0.0.1:8085/api/notion/callback")

    def test_get_auth_url(self):
        redirect = "https://example.com/api/notion/callback"
        with mock.patch.dict(
            os.environ,
            {"NOTION_CLIENT_ID": "cid", "NOTION_CLIENT_SECRET": "csecret"},
        ):
            url, state = get_auth_url(redirect)
        self.assertIn("client_id=cid", url)
        self.assertIn("response_type=code", url)
        self.assertIn("owner=user", url)
        self.assertIn(state, url)
        self.assertTrue(state)

    def test_exchange_code_stores_json(self):
        class _Resp:
            ok = True

            def json(self):
                return {
                    "access_token": "ntn_abc",
                    "refresh_token": "rt_1",
                    "workspace_name": "Demo",
                    "bot_id": "bot-1",
                    "workspace_id": "ws-1",
                }

        with mock.patch.dict(
            os.environ,
            {"NOTION_CLIENT_ID": "cid", "NOTION_CLIENT_SECRET": "csecret"},
        ), mock.patch("notion_uploader.requests.post", return_value=_Resp()):
            ok, msg = exchange_code("auth-code", "https://ex/callback", "u1")
        self.assertTrue(ok, msg)
        self.assertIn("Demo", msg)
        rec = json.loads(db.get_notion_token("u1"))
        self.assertEqual(rec["access_token"], "ntn_abc")
        self.assertEqual(_token_for("u1"), "ntn_abc")
        status = auth_status("u1")
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["workspace"], "Demo")

    def test_legacy_plain_token_still_works(self):
        db.save_notion_token("u1", "secret-legacy")
        self.assertEqual(_token_for("u1"), "secret-legacy")
        self.assertTrue(is_configured("u1"))


class ListAccessiblePagesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.db"
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        db.init_db()
        db.save_notion_token("u1", "secret")

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_roots_exclude_children_of_accessible_pages(self):
        parent_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        child_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        other_id = "cccccccc-cccc-cccc-cccc-cccccccccccc"

        def fake_request(method, path, token, json_body=None, params=None, timeout=30):
            self.assertEqual(method, "POST")
            self.assertEqual(path, "/search")
            return {
                "results": [
                    {
                        "object": "page",
                        "id": parent_id,
                        "url": "https://notion.so/" + parent_id,
                        "parent": {"type": "workspace", "workspace": True},
                        "properties": {
                            "title": {
                                "type": "title",
                                "title": [{"plain_text": "知识库"}],
                            }
                        },
                    },
                    {
                        "object": "page",
                        "id": child_id,
                        "parent": {"type": "page_id", "page_id": parent_id},
                        "properties": {
                            "Name": {
                                "type": "title",
                                "title": [{"plain_text": "已生成的文章"}],
                            }
                        },
                    },
                    {
                        "object": "page",
                        "id": other_id,
                        "parent": {
                            "type": "page_id",
                            "page_id": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                        },
                        "properties": {
                            "title": {
                                "type": "title",
                                "title": [{"plain_text": "另一个勾选页"}],
                            }
                        },
                    },
                ],
                "has_more": False,
            }, None

        from notion_uploader import list_accessible_pages

        with mock.patch("notion_uploader._request", side_effect=fake_request):
            listed = list_accessible_pages("u1")
        self.assertIsNone(listed["error"])
        self.assertEqual(len(listed["pages"]), 3)
        root_ids = [p["id"] for p in listed["roots"]]
        self.assertEqual(root_ids, [parent_id, other_id])
        self.assertEqual(listed["roots"][0]["title"], "知识库")
        self.assertEqual(listed["roots"][1]["title"], "另一个勾选页")

    def test_no_token(self):
        from notion_uploader import list_accessible_pages

        listed = list_accessible_pages("nobody")
        self.assertEqual(listed["pages"], [])
        self.assertEqual(listed["roots"], [])
        self.assertIsNone(listed["error"])


if __name__ == "__main__":
    unittest.main()
