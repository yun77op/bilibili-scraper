"""Markdown 修复层：清理 LLM 生成文章中的常见 Markdown 格式错误。

LLM 把 Markdown 当作纯文本输出，格式正确性没有任何机制保证，常见错误包括：

- 两级标题标记粘连（``### #### 标题``）：按 CommonMark，行首只认最前面
  一组 ``#``，后面的 ``####`` 会作为字面文本显示在标题里；
- 加粗标记前后多打空格（``** 重点 **``）或紧贴引号（``**“X”**``），
  CommonMark 下这些写法都不会被识别为加粗，星号会裸露显示；
- 整张表格被挤在同一行（``| A | B || --- | --- || 1 | 2 |``）；
- 多个列表项被挤在同一段落（``……。 * 要点一。 * 要点二``）；
- 列表项/表格与相邻段落之间缺少空行，导致被解析器吞并；
- 为目录锚点插入的空 HTML 标签（``<a name="sec2"></a>``）：在 Notion
  等不渲染裸 HTML 的目标里会原样显示。

本模块与 app.py 解耦，便于独立测试。app.py 通过
``from markdown_repair import repair_article_markdown`` 使用。
"""

import re

# --- 正则常量 --------------------------------------------------------------

_TABLE_DELIM_RE = re.compile(r"\|\s*:?-{2,}:?\s*\|")
_TABLE_BOUNDARY_RE = re.compile(r"\|\s*\|")
_LIST_ITEM_RE = re.compile(r"\s*([*+-]|\d{1,2}[.、])\s")
_INLINE_BULLET_RE = re.compile(r"(?<=[。！？；：])\s+([*+-])\s+")
_INLINE_NUMBERED_RE = re.compile(r"(?<=[。！？；：])\s+(\d{1,2})\.\s+")
# 成对的 ** 加粗标记（内部不含 *，避免误伤 ***加粗斜体*** 与嵌套强调）
_EMPH_SPACE_RE = re.compile(r"\*\*([^*\n]+?)\*\*")
# 加粗标记紧贴引号（**“X”**）：CommonMark 规定 * 后紧跟标点且前面不是
# 空白/标点时不能作为加粗起始，于是星号按字面输出。把引号移到 ** 外侧：
# **“X”** → “**X**”，两种渲染器都能识别，输出文本不变。
_EMPH_QUOTE_RE = re.compile(r"\*\*([“‘'\u0022\u300c])([^*\n]+?)([”’'\u0022\u300d])\*\*")
# 模型偶发把两级标题标记粘在一起（如 "### #### 标题"）：按 CommonMark，
# 行首标记只认最前面那一组，后面的 "####" 会作为字面文本显示在标题里。
# 折叠为层级最深的那一个标记，只保留最靠右、# 数最多的那组。
_HEADING_GLUE_RE = re.compile(r"^(?P<indent>[ \t]*)(?P<a>#{1,6})[ \t]+(?P<b>#{1,6})(?=[ \t]+)")
# 模型为 Markdown 目录跳转插入的空锚点；有正文的 <a href>…</a> 不匹配。
_EMPTY_HTML_ANCHOR_RE = re.compile(r"<a\b[^>]*>\s*</a>|<a\b[^>]*/>", re.IGNORECASE)


def _fix_emphasis_spacing(line: str) -> str:
    """修复加粗标记格式：剥离内层首尾多余空格，并把紧贴引号的 ** 移到引号外。

    CommonMark 规定 ** 后不能紧跟空格才算加粗开始，模型偶尔会在 ** 与
    文字之间多打空格，导致标记失效、** 裸露显示。只修首尾空白，内部
    空格（如 "**能力 圈**"）保持不变。引号紧贴的情况见 _EMPH_QUOTE_RE。
    """

    def _replace(m: re.Match) -> str:
        inner = m.group(1)
        stripped = inner.strip()
        return f"**{stripped}**" if stripped != inner else m.group(0)

    line = _EMPH_SPACE_RE.sub(_replace, line)
    return _EMPH_QUOTE_RE.sub(
        lambda m: f"{m.group(1)}**{m.group(2)}**{m.group(3)}", line
    )


def _strip_empty_html_anchors(line: str) -> str:
    """去掉空的 ``<a name/id=...>`` 锚点标签，保留同行其余文本。"""
    return _EMPTY_HTML_ANCHOR_RE.sub("", line)


def _repair_squashed_table(line: str) -> list[str]:
    """把被模型挤到同一行的表格拆成标准的 Markdown 表格行。"""
    first_pipe = line.find("|")
    prefix = line[:first_pipe].strip()
    rows: list[str] = []
    for part in _TABLE_BOUNDARY_RE.split(line[first_pipe:]):
        row = part.strip()
        if not row:
            continue
        if not row.startswith("|"):
            row = "| " + row
        if not row.endswith("|"):
            row = row + " |"
        rows.append(row)
    # 至少要有表头 + 分隔行才认为是表格，否则保持原样
    if len(rows) < 2 or not _TABLE_DELIM_RE.search(rows[1]):
        return [line]
    out: list[str] = []
    if prefix:
        out.extend([prefix, ""])
    out.extend(rows)
    out.append("")
    return out


def _split_inline_list_items(line: str) -> list[str]:
    """把"……。 * 要点一。 * 要点二"这类挤在一行的列表拆成逐项一行。"""
    if "。" not in line and "：" not in line and "；" not in line:
        return [line]
    new = _INLINE_BULLET_RE.sub(lambda m: "\n" + m.group(1) + " ", line)
    new = _INLINE_NUMBERED_RE.sub(lambda m: "\n" + m.group(1) + ". ", new)
    return [part for part in new.split("\n") if part.strip()]


def repair_article_markdown(article: str) -> str:
    """尽力修复 LLM 常见的 Markdown 格式错误，保证后续 HTML / PDF 渲染器能正常解析。

    逐行处理，修复类别：
    1. 两级标题标记粘连（"### #### 标题" → "#### 标题"）；
    2. 整张表格被挤在同一行 → 拆成标准表格行；
    3. 多个列表项挤在同一段落 → 拆成逐项一行；
    4. 列表项/表格与相邻段落之间缺失空行 → 自动补空行；
    5. 加粗标记首尾多余空格、紧贴引号 → 规范化为标准写法；
    6. 空的 HTML 锚点标签（``<a name="..."></a>``）→ 删除。

    代码块（``` 围栏）内的内容原样保留，不做任何修改。
    """
    out: list[str] = []
    in_code = False
    for line in article.split("\n"):
        if line.strip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        m = _HEADING_GLUE_RE.match(line)
        if m:
            a, b = m.group("a"), m.group("b")
            line = m.group("indent") + (a if len(a) >= len(b) else b) + line[m.end():]
        if _TABLE_DELIM_RE.search(line) and line.count("|") >= 6:
            pieces = _repair_squashed_table(line)
        else:
            pieces = _split_inline_list_items(line)
        for piece in pieces:
            stripped = _strip_empty_html_anchors(piece)
            # 整行只是空锚点时丢掉；原本空行（piece 已是 ""）照常保留
            if not stripped.strip() and piece.strip():
                continue
            piece = stripped
            # 列表项紧跟在普通段落后面时，前面补空行，否则解析器不认；
            # 普通段落紧跟在列表项/表格行后面时同样补空行，避免被吞进上一块
            if out and out[-1].strip() and piece.strip():
                prev_is_block = bool(
                    _LIST_ITEM_RE.match(out[-1]) or out[-1].lstrip().startswith("|")
                )
                cur_is_block = bool(
                    _LIST_ITEM_RE.match(piece) or piece.lstrip().startswith("|")
                )
                if prev_is_block != cur_is_block:
                    out.append("")
            out.append(_fix_emphasis_spacing(piece))
    return "\n".join(out)
