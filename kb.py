"""知识库（RAG）模块 —— 基于已完成文章的检索增强问答。

数据源：``jobs`` 表中所有已完成任务的文章 + ``outputs/*/article.md`` 归档文件
        （按规范化内容哈希去重，数据库优先；归档覆盖已删除任务的文章）。
检索  ：纯 Python BM25（k1=1.2, b=0.75），中文按「单字 + 相邻二元组」分词，
        英文/数字按单词切分。零第三方依赖、完全离线。
生成  ：调用 DeepSeek Chat 流式接口，把检索到的片段 + 对话历史交给模型，
        以 SSE 事件流的形式逐字返回（区分思考过程 reasoning 与正文 content）。

索引持久化在项目根目录 ``kb_index.json``，用语料指纹判断是否需要重建：
新任务完成文章后，第一次查询 / 状态检查会自动触发重建。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator

import db as _db

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "kb_index.json"
OUTPUT_DIR = ROOT / "outputs"

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"

BM25_K1 = 1.2
BM25_B = 0.75

CHUNK_CHARS = 600      # 目标片段长度
CHUNK_OVERLAP = 120    # 相邻片段重叠（保证跨段信息不丢）

TOP_K = 6              # 检索返回的片段数
MAX_HISTORY = 8        # 送入模型的最近对话消息条数（不含本次提问）
MAX_PROMPT_CHARS = 26000  # 资料区最大字符数（防止超长）

# 弱匹配过滤门槛：顶部片段分数低于该值，或候选里所有片段的覆盖度都低于
# 该比例（且标题也无重叠）时，视为「没有相关内容」，不把无关片段喂给模型。
MIN_TOP_SCORE = 10.0
MIN_COVERAGE = 0.25
TAIL_RATIO = 0.5      # 相对顶部分数，低于该比例的长尾片段丢弃
TITLE_BOOST = 40.0    # 文档标题与查询有效词重叠时，给该文档所有片段加的分数加成

_INDEX_VERSION = 1

# 索引构建/访问锁（server 多线程共享）
_lock = threading.RLock()
_building = threading.Event()          # 重建中置位，避免并发重建
_cached: dict[str, Any] | None = None  # 内存缓存的最新索引快照


# ---------------------------------------------------------------------------
# 语料收集
# ---------------------------------------------------------------------------

def _norm_hash(text: str) -> str:
    """规范化内容哈希（去掉所有空白）用于跨来源去重。"""
    return hashlib.sha256("".join(text.split()).encode("utf-8")).hexdigest()


def _first_heading(text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, flags=re.MULTILINE)
    return m.group(1).strip() if m else ""


def _video_url(text: str) -> str:
    m = re.search(r"原视频链接[：:]\s*(https?://\S+)", text)
    return m.group(1).strip() if m else ""


def _collect_docs() -> list[dict[str, Any]]:
    """收集知识库文档：jobs 表 + outputs 归档，按内容哈希去重（DB 优先）。

    普通任务：article 即一篇文档；合集任务（page_articles 多于 1 篇）：
    每篇独立成文档，避免与合并稿重复索引。
    """
    docs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(text: str, *, title: str, url: str, created_at: float | None,
            job_id: str) -> None:
        text = (text or "").strip()
        if len(text) < 80:
            return
        key = _norm_hash(text)
        if key in seen:
            return
        seen.add(key)
        docs.append({
            "job_id": job_id,
            "title": title or _first_heading(text) or "未命名文章",
            "url": url or _video_url(text),
            "created_at": created_at or 0.0,
            "text": text,
        })

    # 1) jobs 表（优先，含标题/URL 元信息）
    conn = _db._connect()
    try:
        rows = conn.execute(
            """SELECT id, title, url, article, page_articles, created_at
               FROM jobs
               WHERE article != '' AND id != '__worker_heartbeat__'"""
        ).fetchall()
        for row in rows:
            base_title = (row["title"] or "").strip()
            try:
                pages = json.loads(row["page_articles"]) if row["page_articles"] else []
            except (json.JSONDecodeError, TypeError):
                pages = []
            texts = pages if len(pages) > 1 else ([row["article"]] if row["article"] else [])
            for idx, text in enumerate(texts):
                title = f"{base_title}（第 {idx + 1} 篇）" if (len(pages) > 1 and base_title) else base_title
                add(text, title=title, url=row["url"] or "",
                    created_at=row["created_at"], job_id=row["id"])
    finally:
        conn.close()

    # 2) outputs/*/article.md 归档（覆盖已删除任务的文章；与 DB 内容重复的自动去重）
    if OUTPUT_DIR.is_dir():
        try:
            for md in sorted(OUTPUT_DIR.glob("*/article.md")):
                try:
                    text = md.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                add(text, title="", url="",
                    created_at=md.stat().st_mtime, job_id=md.parent.name)
        except OSError:
            pass

    return docs


# ---------------------------------------------------------------------------
# 分词
# ---------------------------------------------------------------------------

_ASCII_RE = re.compile(r"[a-z0-9_]+")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """中文按单字 + 相邻二元组切分；英文/数字按单词切分（统一小写）。"""
    t = text.lower()
    tokens: list[str] = _ASCII_RE.findall(t)
    for run in _CJK_RUN_RE.findall(t):
        if not run:
            continue
        for ch in run:
            tokens.append(ch)
        prev = run[0]
        for ch in run[1:]:
            tokens.append(prev + ch)
            prev = ch
    return tokens


def _doc_fingerprint(docs: list[dict[str, Any]]) -> str:
    """语料指纹：任一文章内容变化都会改变，用于判断是否需要重建索引。"""
    h = hashlib.sha256()
    for d in sorted(docs, key=lambda x: x["job_id"]):
        h.update(d["job_id"].encode("utf-8"))
        h.update(str(len(d["text"])).encode("utf-8"))
        h.update(d["title"].encode("utf-8", "replace"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------

_TOC_LINE_RE = re.compile(r"^\s*[-*]\s*\[")
_HEADING_RE = re.compile(r"^#{1,6}\s")


def chunk_text(text: str, target: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把一篇 markdown 文章切成片段。

    规则：去掉目录锚点行（``- [xxx](#xxx)``）；标题行单独成块单元；其余按空行
    分段的自然块聚合；达到目标长度即切出，下一块带上一块尾部重叠。
    """
    lines = [ln.rstrip() for ln in text.splitlines() if not _TOC_LINE_RE.match(ln)]

    units: list[str] = []
    cur: list[str] = []

    def flush() -> None:
        if cur:
            units.append("\n".join(cur))
            cur.clear()

    for ln in lines:
        if not ln.strip():
            flush()
            continue
        if _HEADING_RE.match(ln):
            flush()
            units.append(ln)
        else:
            cur.append(ln)
    flush()

    chunks: list[str] = []
    buf = ""
    for u in units:
        if buf and len(buf) + len(u) + 1 > target:
            chunks.append(buf)
            buf = (buf[-overlap:] + "\n" + u) if overlap > 0 else u
        else:
            buf = f"{buf}\n{u}" if buf else u
    if buf:
        chunks.append(buf)

    return [c.strip() for c in chunks if len(c.strip()) >= 30]


# ---------------------------------------------------------------------------
# 索引构建 / 持久化
# ---------------------------------------------------------------------------

def _build_index(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """构建 BM25 倒排索引（内存结构 + 可 JSON 持久化）。"""
    chunk_docs: list[dict[str, Any]] = []
    postings: dict[str, list[list[int]]] = defaultdict(list)
    df: dict[str, int] = defaultdict(int)
    doc_lens: list[int] = []
    token_seen: set[str] = set()

    for doc in docs:
        for ci, text in enumerate(chunk_text(doc["text"])):
            idx = len(chunk_docs)
            chunk_docs.append({
                "id": f"{doc['job_id']}#{ci}",
                "job_id": doc["job_id"],
                "title": doc["title"],
                "url": doc["url"],
                "created_at": doc["created_at"],
                "chunk_index": ci,
                "text": text,
            })
            tokens = tokenize(text)
            doc_lens.append(len(tokens))
            counts: dict[str, int] = defaultdict(int)
            for tok in tokens:
                counts[tok] += 1
            token_seen.clear()
            for tok, tf in counts.items():
                postings[tok].append([idx, tf])
                if tok not in token_seen:
                    df[tok] += 1
                    token_seen.add(tok)

    num_docs = len(chunk_docs)
    avgdl = (sum(doc_lens) / num_docs) if num_docs else 0.0

    return {
        "version": _INDEX_VERSION,
        "built_at": time.time(),
        "fingerprint": _doc_fingerprint(docs),
        "num_docs": num_docs,
        "avgdl": avgdl,
        "docs": chunk_docs,
        "doc_lens": doc_lens,
        "df": dict(df),
        "postings": dict(postings),
    }


def _save_index(index: dict[str, Any]) -> None:
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    tmp.replace(INDEX_FILE)


def _load_index() -> dict[str, Any] | None:
    if not INDEX_FILE.exists():
        return None
    try:
        data = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if data.get("version") != _INDEX_VERSION or not data.get("docs"):
        return None
    return data


def rebuild_index(force: bool = False) -> dict[str, Any]:
    """（重新）构建索引并持久化。返回索引快照。

    已在重建中时直接返回当前（可能为旧）索引，避免并发重复构建。
    """
    global _cached
    with _lock:
        if _building.is_set():
            return _cached or _load_index() or _build_index(_collect_docs())
        _building.set()
        try:
            docs = _collect_docs()
            fp = _doc_fingerprint(docs)
            if not force:
                old = _load_index()
                if old and old.get("fingerprint") == fp:
                    _cached = old
                    return old
            index = _build_index(docs)
            _save_index(index)
            _cached = index
            return index
        finally:
            _building.clear()


def get_index() -> dict[str, Any]:
    """返回最新索引；语料变化时自动重建（进程内缓存 + 磁盘持久化）。"""
    global _cached
    with _lock:
        if _cached is not None:
            docs = _collect_docs()
            if _cached["fingerprint"] == _doc_fingerprint(docs):
                return _cached
            _cached = None
        index = _load_index()
        if index is not None:
            docs = _collect_docs()
            if index["fingerprint"] == _doc_fingerprint(docs):
                _cached = index
                return index
        return rebuild_index()


def index_status() -> dict[str, Any]:
    """索引状态（用于 UI 展示）：文章数、片段数、构建时间、是否最新。"""
    docs = _collect_docs()
    fp = _doc_fingerprint(docs)
    with _lock:
        cached = _cached if _cached is not None else _load_index()
    return {
        "articles": len(docs),
        "chunks": cached["num_docs"] if cached else 0,
        "built_at": cached["built_at"] if cached else None,
        "up_to_date": bool(cached and cached["fingerprint"] == fp),
        "building": _building.is_set(),
    }


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

def search(query: str, index: dict[str, Any] | None = None, top_k: int = TOP_K) -> list[dict[str, Any]]:
    """BM25 检索，返回按分数降序的片段列表（含元信息）。

    除 BM25 外，给「文档标题与查询有效词重叠」的文档整体加一个标题匹配加成，
    把标题强相关（但 BM25 可能因为片段较短/正文词汇分散而排名不高）的片段顶上来。
    """
    if index is None:
        index = get_index()
    if not index.get("num_docs"):
        return []

    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    q_sig = _sig_tokens(query)

    n = index["num_docs"]
    avgdl = index["avgdl"] or 1.0
    df_map = index["df"]
    postings = index["postings"]
    doc_lens = index["doc_lens"]
    docs = index["docs"]

    scores: dict[int, float] = defaultdict(float)
    seen: set[str] = set()
    for tok in q_tokens:
        if tok in seen:
            continue
        seen.add(tok)
        df = df_map.get(tok, 0)
        if df <= 0:
            continue
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        for doc_idx, tf in postings.get(tok, []):
            dl = doc_lens[doc_idx]
            denom = tf + BM25_K1 * (1.0 - BM25_B + BM25_B * dl / avgdl)
            scores[doc_idx] += idf * (tf * (BM25_K1 + 1.0)) / denom

    # 标题匹配加成（按文档缓存标题有效词集合）
    title_sig_cache: dict[int, set[str]] = {}
    if q_sig:
        for doc_idx in scores:
            ts = title_sig_cache.get(doc_idx)
            if ts is None:
                ts = _sig_tokens(docs[doc_idx]["title"])
                title_sig_cache[doc_idx] = ts
            if ts:
                cov = len(q_sig & ts) / len(q_sig)
                if cov > 0:
                    scores[doc_idx] += TITLE_BOOST * cov

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    results = []
    for doc_idx, score in ranked:
        doc = docs[doc_idx]
        results.append({
            "id": doc["id"],
            "job_id": doc["job_id"],
            "title": doc["title"],
            "url": doc["url"],
            "created_at": doc["created_at"],
            "chunk_index": doc["chunk_index"],
            "text": doc["text"],
            "score": round(score, 4),
        })
    return results


def _dedupe_sources(hits: list[dict[str, Any]]) -> list[dict[str, str]]:
    """按（job_id, 标题）去重，保留每个来源的标题/链接用于引用展示。"""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for h in hits:
        key = (h["job_id"], h["title"])
        if key in seen:
            continue
        seen.add(key)
        out.append({"job_id": h["job_id"], "title": h["title"], "url": h["url"]})
    return out


_SIG_TOK_RE = re.compile(r"[a-z0-9_]+")


def _sig_tokens(text: str) -> set[str]:
    """有效词集合：英文词/数字 + 中文滑动窗口二元组（与 tokenize 一致）。"""
    t = text.lower()
    toks = set(_SIG_TOK_RE.findall(t))
    for run in _CJK_RUN_RE.findall(t):
        for i in range(len(run) - 1):
            toks.add(run[i:i + 2])
    return toks


def _sig_coverage(query: str, text: str) -> float:
    """有效词覆盖率：查询中的英文词/中文二元组有多少出现在文本里。

    单字不计入（单字在中文里几乎处处出现，会稀释指标）。
    """
    q = _sig_tokens(query)
    if not q:
        return 0.0
    t = _sig_tokens(text)
    return len(q & t) / len(q)


def _filter_hits(question: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤弱匹配片段：丢弃相对长尾，再按覆盖度/分数门槛筛选。

    判定有内容的标准：顶部分数达标，且候选里存在覆盖度达标的片段，或顶部
    文档的标题与查询高度重叠（正文没有名字、名字只在标题的情况也视为命中）。
    都不满足时返回空列表（表示「知识库中没有相关内容」）。
    """
    if not hits:
        return []
    top = hits[0]
    if top["score"] < MIN_TOP_SCORE:
        return []
    cutoff = TAIL_RATIO * top["score"]
    kept = [h for h in hits if h["score"] >= cutoff]
    if not kept:
        return []
    best_cov = max(_sig_coverage(question, h["text"]) for h in kept)
    title_cov = _sig_coverage(question, top["title"])
    if best_cov >= MIN_COVERAGE or title_cov >= MIN_COVERAGE:
        return kept
    return []


def _topic_titles(index: dict[str, Any], limit: int = 40) -> list[str]:
    """知识库当前收录的文档标题（去重，用于无命中时提示主题范围）。"""
    seen: set[str] = set()
    out: list[str] = []
    for d in index["docs"]:
        t = d["title"]
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# 示例问题（每次进入知识库随机变化）
# ---------------------------------------------------------------------------

# 固定主题池：覆盖知识库主要内容的优质提问（每次随机抽一部分）
CURATED_SAMPLES = [
    "Agent 的 Skill 命中率怎么保证？",
    "大模型如何稳定地输出 JSON？",
    "RAG 遇到 PDF 应该怎么处理？",
    "斯坦福神经科学家讲了哪些提升大脑潜能的方法？",
    "用 Codex 怎么搭一条 AI 视频生产线？",
    "AI 找球员为什么没能成为大生意？",
    "Agent 的 Checkpoint 机制是什么？",
    "NotebookLM 怎么用来高效学习？",
    "怎么做好时间管理？",
    "Agent 高频面试都考哪些内容？",
]

# 语料标题 → 提问的模板（从当前文章随机抽，让示例随知识库内容变化）
_TITLE_TEMPLATES = [
    "《{title}》这篇文章讲了什么？",
    "帮我总结一下《{title}》的核心要点",
    "《{title}》有哪些关键信息？",
]


def sample_questions(count: int = 6) -> list[str]:
    """随机生成示例问题：主题池抽一部分 + 从当前语料随机抽文章按模板生成。

    每次调用结果不同；文章更新后标题类示例也会跟着变化。
    """
    k = min(count, 4, len(CURATED_SAMPLES))
    curated = random.sample(CURATED_SAMPLES, k=k)
    index = get_index()
    titles = [
        t for t in _topic_titles(index, limit=80)
        if len(t) >= 4 and t != "未命名文章"
    ]
    random.shuffle(titles)
    title_qs = [
        random.choice(_TITLE_TEMPLATES).format(title=t)
        for t in titles[: max(0, count - len(curated))]
    ]
    out = curated + title_qs
    random.shuffle(out)
    return out


# ---------------------------------------------------------------------------
# 对话生成（DeepSeek 流式）
# ---------------------------------------------------------------------------

def _build_prompt(question: str, history: list[dict[str, str]], hits: list[dict[str, Any]], index: dict[str, Any]) -> list[dict[str, str]]:
    """组装 messages：系统提示 + 历史 + 检索资料 + 问题。"""
    system = (
        "你是一名知识库问答助手，负责基于给定的「知识库资料」回答问题。\n"
        "规则：\n"
        "1. 只依据资料中明确提到的内容回答；资料没有的内容，明确说「知识库中没有找到相关内容」，"
        "不要编造事实、人名、机构、数字或案例。\n"
        "2. 回答使用简体中文，结构清晰：优先给出直接结论，再展开要点（可用列表/小标题）。\n"
        "3. 引用来源：涉及某个资料的观点或事实时，在该段末尾用【资料标题】标注来源标题。\n"
        "4. 若多个资料观点冲突，分别列出并说明差异。\n"
        "5. 用通俗的语言解释专业概念，必要时举例。"
    )
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]

    for m in (history or [])[-MAX_HISTORY:]:
        role = m.get("role")
        content = str(m.get("content", "")).strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    if hits:
        blocks = []
        used = 0
        for h in hits:
            block = f"【{h['title']}】\n{h['text']}"
            if used + len(block) > MAX_PROMPT_CHARS:
                break
            blocks.append(block)
            used += len(block)
        materials = "\n\n---\n\n".join(blocks)
        user_content = (
            "以下是知识库中检索到的相关资料（每段用【标题】开头）：\n\n"
            f"{materials}\n\n"
            f"---\n\n请根据以上资料回答下面的问题：\n{question}"
        )
    else:
        titles = "\n".join(f"- {t}" for t in _topic_titles(index))
        user_content = (
            "知识库中没有检索到与该问题直接相关的资料。\n\n"
            f"知识库当前收录的主题（文章标题）包括：\n{titles}\n\n"
            f"请如实告知用户知识库中没有与问题直接相关的内容，同时根据上面的主题列表"
            f"简要提示知识库可能覆盖的方向，并建议换一种问法。\n用户问题：\n{question}"
        )
    messages.append({"role": "user", "content": user_content})
    return messages


def _api_key() -> str:
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法生成回答。请在 .env.local 中设置后重试。")
    return key


def chat_stream(
    question: str,
    history: list[dict[str, str]] | None = None,
    top_k: int = TOP_K,
) -> Iterator[dict[str, Any]]:
    """知识库问答：检索 → DeepSeek 流式生成。

    产出事件（dict）：
      {"type": "status", "payload": {...}}   开始检索状态
      {"type": "sources", "payload": {"sources": [...]}}  参考来源
      {"type": "reasoning", "payload": {"text": "..."}}   思考过程增量
      {"type": "delta", "payload": {"text": "..."}}       回答正文增量
      {"type": "done", "payload": {}}                     结束
      {"type": "error", "payload": {"message": "..."}}    出错（终止）
    """
    index = get_index()
    hits = _filter_hits(question, search(question, index, top_k=TOP_K))
    sources = _dedupe_sources(hits)
    yield {"type": "status", "payload": {
        "chunks": index.get("num_docs", 0),
        "hits": len(hits),
    }}
    yield {"type": "sources", "payload": {"sources": sources}}

    api_key = _api_key()
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    payload = {
        "model": model,
        "messages": _build_prompt(question, history or [], hits, index),
        "stream": True,
        "max_tokens": 8192,
        "temperature": 0.6,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = requests_post_stream(payload, headers)
    except Exception as exc:  # 网络/HTTP 错误
        yield {"type": "error", "payload": {"message": f"调用 DeepSeek 失败：{exc}"}}
        return

    try:
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            reasoning = delta.get("reasoning_content")
            if reasoning:
                yield {"type": "reasoning", "payload": {"text": reasoning}}
            content = delta.get("content")
            if content:
                yield {"type": "delta", "payload": {"text": content}}
    finally:
        resp.close()

    yield {"type": "done", "payload": {}}


def requests_post_stream(payload: dict[str, Any], headers: dict[str, str]):
    """发起 DeepSeek 流式请求（独立函数便于测试替换）。"""
    import requests
    return requests.post(
        DEEPSEEK_API_URL,
        json=payload,
        headers=headers,
        timeout=(15, 300),
        stream=True,
    )
