"""repair_article_markdown 的回归测试。

覆盖修复层当前的全部修复类别：
- 两级标题标记粘连（``### #### 标题`` → ``#### 标题``）；
- 加粗标记首尾空格 / 紧贴引号的规范化；
- 整张表格被挤在同一行时的拆分；
- 多个列表项挤在同一段落时的拆分；
- 列表 / 表格与相邻段落之间的空行补齐；
- 空 HTML 锚点标签（``<a name="..."></a>``）的剥离；
- 代码块保护，以及整体幂等性（修复后不再被二次修改）。

运行方式（项目根目录）：
    python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from markdown_repair import (  # noqa: E402
    _repair_squashed_table,
    _split_inline_list_items,
    repair_article_markdown,
)

rep = repair_article_markdown

# 真实文章路径（回归锚点：该文章曾出现 17 处 "### ####" 标题粘连）
REGRESSION_ARTICLE = (
    REPO_ROOT
    / "outputs"
    / "认识你自己-Lex-Fridman对话AI科学家-人生的7个认知阶段-你在哪一层-20260817-BV1x13Q6XEqq-p1"
    / "article.md"
)


class HeadingGlueTest(unittest.TestCase):
    """两级标题标记粘连：### #### 标题 → #### 标题。"""

    def test_collapse_doubled_marker(self):
        self.assertEqual(
            rep("### #### 阶段1：反应性生存婴儿"), "#### 阶段1：反应性生存婴儿"
        )

    def test_keep_deeper_marker(self):
        self.assertEqual(rep("## ### 嵌套"), "### 嵌套")
        self.assertEqual(rep("#### ### 反向"), "#### 反向")

    def test_indented_line(self):
        self.assertEqual(rep("   ### #### 带缩进"), "   #### 带缩进")

    def test_normal_heading_untouched(self):
        self.assertEqual(rep("## 阶段3：社会自我与群体心智"), "## 阶段3：社会自我与群体心智")

    def test_hash_inside_text_untouched(self):
        self.assertEqual(rep("### 需求 ## 说明"), "### 需求 ## 说明")

    def test_table_and_fence_lines_untouched(self):
        self.assertEqual(rep("| --- | --- |"), "| --- | --- |")
        self.assertEqual(rep("```mermaid"), "```mermaid")


class EmphasisSpacingTest(unittest.TestCase):
    """加粗标记首尾空格与引号紧贴的规范化。"""

    def test_strip_outer_spaces(self):
        self.assertEqual(rep("** 重点 **"), "**重点**")
        self.assertEqual(rep("** 重点**"), "**重点**")
        self.assertEqual(rep("**重点 **"), "**重点**")

    def test_keep_inner_space(self):
        self.assertEqual(rep("**能力 圈**"), "**能力 圈**")

    def test_quote_adjacent_bold(self):
        self.assertEqual(rep("**“X”**"), "“**X**”")
        self.assertEqual(rep("**「Y」**"), "「**Y**」")
        self.assertEqual(rep('**"Z"**'), '"**Z**"')

    def test_triple_star_untouched(self):
        self.assertEqual(rep("***加粗斜体***"), "***加粗斜体***")

    def test_plain_line_untouched(self):
        self.assertEqual(rep("普通文本"), "普通文本")


class SquashedTableTest(unittest.TestCase):
    """整张表格被挤在同一行时的拆分。"""

    def test_split_with_prefix(self):
        self.assertEqual(
            rep("前置说明 | 列A | 列B || --- | --- || 1 | 2 |"),
            "前置说明\n\n| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n",
        )

    def test_split_without_prefix(self):
        self.assertEqual(
            rep("| 列A | 列B || --- | --- || 1 | 2 |"),
            "| 列A | 列B |\n| --- | --- |\n| 1 | 2 |\n",
        )

    def test_non_table_kept(self):
        self.assertEqual(_repair_squashed_table("不是表格的一行"), ["不是表格的一行"])


class InlineListSplitTest(unittest.TestCase):
    """多个列表项挤在同一段落时的拆分。"""

    def test_bullet_split(self):
        self.assertEqual(rep("要点如下： * 一。 * 二。"), "要点如下：\n\n* 一。\n* 二。")

    def test_numbered_split(self):
        self.assertEqual(rep("第一点。 1. 内容。 2. 内容。"), "第一点。\n\n1. 内容。\n2. 内容。")

    def test_no_punct_untouched(self):
        self.assertEqual(_split_inline_list_items("无标点的一行"), ["无标点的一行"])


class BlankLineInsertionTest(unittest.TestCase):
    """列表 / 表格与相邻段落之间缺失空行的补齐。"""

    def test_paragraph_then_list(self):
        self.assertEqual(rep("正文\n- 项目"), "正文\n\n- 项目")

    def test_list_then_paragraph(self):
        self.assertEqual(rep("- 项目\n正文"), "- 项目\n\n正文")


class EmptyHtmlAnchorTest(unittest.TestCase):
    """模型为目录插入的空 <a name/id> 锚点应被剥离。"""

    def test_standalone_name_anchor(self):
        src = '<a name="1"></a>\n## 引言'
        self.assertEqual(rep(src), "## 引言")

    def test_inline_before_heading_text(self):
        src = '<a name="sec2"></a>二、从纺织公司到存储霸主：SK海力士简史'
        self.assertEqual(rep(src), "二、从纺织公司到存储霸主：SK海力士简史")

    def test_id_and_self_closing(self):
        self.assertEqual(rep('<a id="foo"></a>\n正文'), "正文")
        self.assertEqual(rep('<a name="x" />标题'), "标题")

    def test_real_link_kept(self):
        src = '<a href="https://example.com">官网</a>'
        self.assertEqual(rep(src), src)

    def test_code_block_kept(self):
        src = '```html\n<a name="1"></a>\n```'
        self.assertEqual(rep(src), src)


class CodeBlockProtectionTest(unittest.TestCase):
    """代码块（``` 围栏）内的内容必须原样保留。"""

    def test_code_block_untouched(self):
        src = "```mermaid\n### #### 别动我\n| a | b || c |\n```"
        self.assertEqual(rep(src), src)


class EndToEndTest(unittest.TestCase):
    """整体行为：规范文档不被破坏、修复幂等、真实文章无残留。"""

    def test_wellformed_document_unchanged(self):
        doc = """## 标题

正文段落。

- 列表一
- 列表二

| 列A | 列B |
| --- | --- |
| 1 | 2 |

> 引用块

```python
print("hi")
```
"""
        self.assertEqual(rep(doc), doc)

    def test_idempotent(self):
        samples = [
            "### #### 阶段1：反应性生存婴儿",
            "** 重点 **",
            "前置说明 | 列A | 列B || --- | --- || 1 | 2 |",
            "要点如下： * 一。 * 二。",
            "正文\n- 项目",
            "- 项目\n正文",
            '<a name="sec2"></a>\n## 标题',
            "```mermaid\n### #### 别动我\n```",
        ]
        for s in samples:
            once = rep(s)
            self.assertEqual(rep(once), once, f"not idempotent: {s!r}")

    def test_regression_fixed_article(self):
        if not REGRESSION_ARTICLE.exists():
            self.skipTest("outputs 目录中找不到该文章（可能已清理）")
        text = REGRESSION_ARTICLE.read_text(encoding="utf-8")
        self.assertNotIn("### ####", text)
        self.assertIn("#### 阶段1：反应性生存婴儿", text)
        self.assertEqual(rep(text), text)  # 修复层不破坏已修好的文章


if __name__ == "__main__":
    unittest.main()
