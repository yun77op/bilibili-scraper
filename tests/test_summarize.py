"""任务摘要：缓存键、参数解析、篇目选取与提示词组装。

不打真实 DeepSeek 接口。运行方式（项目根目录）：
    python -m unittest tests.test_summarize -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from summarize import (  # noqa: E402
    MAX_ARTICLE_CHARS,
    article_for_page,
    build_messages,
    cache_get,
    cache_put,
    parse_format,
    parse_length,
    summarize_job,
)


class ParseTest(unittest.TestCase):
    def test_format_aliases(self):
        self.assertEqual(parse_format("paragraph"), "paragraph")
        self.assertEqual(parse_format("要点"), "bullets")
        self.assertEqual(parse_format("一句话"), "oneliner")
        self.assertEqual(parse_format("LIST"), "bullets")

    def test_length_aliases(self):
        self.assertEqual(parse_length("short"), "short")
        self.assertEqual(parse_length("中"), "medium")
        self.assertEqual(parse_length("长"), "long")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_format("mindmap")
        with self.assertRaises(ValueError):
            parse_length("tiny")


class ArticlePageTest(unittest.TestCase):
    def test_single_page_ignores_index(self):
        job = {"article": "hello article", "page_articles": []}
        self.assertEqual(article_for_page(job, 9), (0, "hello article"))

    def test_multi_page_clamps(self):
        job = {
            "article": "merged",
            "page_articles": ["第一篇内容", "第二篇内容"],
        }
        self.assertEqual(article_for_page(job, 1), (1, "第二篇内容"))
        self.assertEqual(article_for_page(job, 99), (1, "第二篇内容"))
        self.assertEqual(article_for_page(job, "0"), (0, "第一篇内容"))


class CacheTest(unittest.TestCase):
    def test_put_and_get(self):
        cache = cache_put({}, 0, "paragraph", "short", "  一段摘要  ")
        self.assertEqual(cache_get(cache, 0, "paragraph", "short"), "一段摘要")
        self.assertIsNone(cache_get(cache, 0, "paragraph", "long"))
        self.assertIsNone(cache_get(cache, 1, "paragraph", "short"))

    def test_merge_keeps_other_slots(self):
        cache = cache_put({}, 0, "paragraph", "short", "A")
        cache = cache_put(cache, 0, "bullets", "medium", "B")
        cache = cache_put(cache, 1, "oneliner", "long", "C")
        self.assertEqual(cache_get(cache, 0, "paragraph", "short"), "A")
        self.assertEqual(cache_get(cache, 0, "bullets", "medium"), "B")
        self.assertEqual(cache_get(cache, 1, "oneliner", "long"), "C")

    def test_empty_and_garbage(self):
        self.assertIsNone(cache_get(None, 0, "paragraph", "short"))
        self.assertIsNone(cache_get([], 0, "paragraph", "short"))
        self.assertIsNone(cache_get({"pages": "nope"}, 0, "paragraph", "short"))


class PromptTest(unittest.TestCase):
    def test_messages_include_format_and_article(self):
        msgs = build_messages("文章正文XYZ", "bullets", "short")
        self.assertEqual(msgs[0]["role"], "system")
        user = msgs[1]["content"]
        self.assertIn("无序列表", user)
        self.assertIn("3–5 条", user)
        self.assertIn("文章正文XYZ", user)

    def test_clip_long_article(self):
        article = "字" * (MAX_ARTICLE_CHARS + 80)
        user = build_messages(article, "paragraph", "medium")[1]["content"]
        self.assertIn("已截断", user)
        self.assertNotIn("字" * (MAX_ARTICLE_CHARS + 1), user)


class SummarizeJobGuardTest(unittest.TestCase):
    def test_rejects_unfinished_job(self):
        with self.assertRaises(ValueError):
            summarize_job({"status": "running", "article": "x" * 20}, fmt="paragraph", length="short")

    def test_rejects_empty_article(self):
        with self.assertRaises(ValueError):
            summarize_job({"status": "done", "article": "  "}, fmt="paragraph", length="short")

    def test_cache_hit_skips_network(self):
        job = {
            "status": "done",
            "article": "一篇足够长的文章内容",
            "summaries": {
                "pages": {"0": {"paragraph": {"medium": "已缓存的摘要"}}}
            },
        }
        result = summarize_job(job, fmt="paragraph", length="medium")
        self.assertTrue(result["cached"])
        self.assertEqual(result["summary"], "已缓存的摘要")
        self.assertEqual(result["page"], 0)


if __name__ == "__main__":
    unittest.main()
