"""任务文章按需摘要。

从已生成的中文文章（而非转写稿）调用 DeepSeek，产出短/中/长三种篇幅，
以及段落 / 要点列表 / 一句话三种格式。结果按「篇目 + 格式 + 篇幅」缓存在
``jobs.summaries``，避免重复消耗 token。
"""

from __future__ import annotations

import os
from typing import Any

import requests

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"

FORMATS = ("paragraph", "bullets", "oneliner")
LENGTHS = ("short", "medium", "long")

_FORMAT_ALIASES = {
    "paragraph": "paragraph",
    "段落": "paragraph",
    "bullets": "bullets",
    "bullet": "bullets",
    "要点": "bullets",
    "list": "bullets",
    "oneliner": "oneliner",
    "one_liner": "oneliner",
    "一句话": "oneliner",
}

_LENGTH_ALIASES = {
    "short": "short",
    "短": "short",
    "medium": "medium",
    "中": "medium",
    "long": "long",
    "长": "long",
}

MAX_ARTICLE_CHARS = 40000

_FORMAT_INSTRUCTIONS = {
    "paragraph": "输出连贯的简体中文段落，不要标题、不要列表、不要开场白或结束语。",
    "bullets": "输出 Markdown 无序列表（每行以 `- ` 开头），一条一个要点，不要开场白、标题或总结段。",
    "oneliner": "只输出一句简体中文（可以稍长，但必须是一句），不要列表、标题或第二句。",
}

_LENGTH_INSTRUCTIONS = {
    "paragraph": {
        "short": "约 80–120 字。",
        "medium": "约 200–350 字。",
        "long": "约 500–800 字。",
    },
    "bullets": {
        "short": "3–5 条。",
        "medium": "6–10 条。",
        "long": "12–18 条。",
    },
    "oneliner": {
        "short": "约 30–50 字。",
        "medium": "约 60–90 字。",
        "long": "约 100–150 字。",
    },
}


def parse_format(value: Any) -> str:
    key = str(value or "").strip().lower()
    # 中文别名不能 lower 掉，再试原始值
    raw = str(value or "").strip()
    fmt = _FORMAT_ALIASES.get(key) or _FORMAT_ALIASES.get(raw)
    if not fmt:
        raise ValueError("不支持的摘要格式，请使用 paragraph / bullets / oneliner")
    return fmt


def parse_length(value: Any) -> str:
    key = str(value or "").strip().lower()
    raw = str(value or "").strip()
    length = _LENGTH_ALIASES.get(key) or _LENGTH_ALIASES.get(raw)
    if not length:
        raise ValueError("不支持的摘要篇幅，请使用 short / medium / long")
    return length


def article_for_page(job: dict[str, Any], page_index: Any) -> tuple[int, str]:
    """返回 (实际篇目下标, 文章正文)。合集按 page_articles，普通任务固定 0。"""
    try:
        idx = int(page_index)
    except (TypeError, ValueError):
        idx = 0
    pages = job.get("page_articles") or []
    if isinstance(pages, list) and len(pages) > 1:
        idx = max(0, min(idx, len(pages) - 1))
        return idx, str(pages[idx] or "").strip()
    return 0, str(job.get("article") or "").strip()


def cache_get(cache: Any, page_index: int, fmt: str, length: str) -> str | None:
    if not isinstance(cache, dict):
        return None
    pages = cache.get("pages")
    if not isinstance(pages, dict):
        return None
    slot = pages.get(str(page_index))
    if not isinstance(slot, dict):
        return None
    fmt_slot = slot.get(fmt)
    if not isinstance(fmt_slot, dict):
        return None
    text = fmt_slot.get(length)
    if isinstance(text, str) and text.strip():
        return text.strip()
    return None


def cache_put(cache: Any, page_index: int, fmt: str, length: str, text: str) -> dict[str, Any]:
    out: dict[str, Any] = dict(cache) if isinstance(cache, dict) else {}
    pages = dict(out.get("pages") or {}) if isinstance(out.get("pages"), dict) else {}
    page_key = str(page_index)
    slot = dict(pages.get(page_key) or {}) if isinstance(pages.get(page_key), dict) else {}
    fmt_slot = dict(slot.get(fmt) or {}) if isinstance(slot.get(fmt), dict) else {}
    fmt_slot[length] = text.strip()
    slot[fmt] = fmt_slot
    pages[page_key] = slot
    out["pages"] = pages
    return out


def _clip_article(article: str) -> str:
    text = article.strip()
    if len(text) <= MAX_ARTICLE_CHARS:
        return text
    return text[:MAX_ARTICLE_CHARS].rstrip() + "\n\n[文章过长，已截断后半部分后再摘要]"


def build_messages(article: str, fmt: str, length: str) -> list[dict[str, str]]:
    system = (
        "你是中文编辑，只根据给定文章做摘要。"
        "不编造原文没有的事实、人名、机构、数字或案例。"
        "忽略目录、Mermaid 图、LaTeX 公式细节，只保留知识要点。"
        "全程使用简体中文。"
    )
    user = (
        f"请把下面的文章整理成摘要。\n"
        f"格式：{_FORMAT_INSTRUCTIONS[fmt]}\n"
        f"篇幅：{_LENGTH_INSTRUCTIONS[fmt][length]}\n\n"
        f"文章：\n{_clip_article(article)}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def request_summary(article: str, fmt: str, length: str) -> str:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY，无法生成摘要。请在设置中配置后重试。")

    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    payload = {
        "model": model,
        "messages": build_messages(article, fmt, length),
        "stream": False,
        "max_tokens": 2048,
        "temperature": 0.3,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(DEEPSEEK_API_URL, json=payload, headers=headers, timeout=90)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.HTTPError as exc:
        body = ""
        try:
            body = exc.response.text[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"DeepSeek 接口返回错误：HTTP {exc.response.status_code} {body}"
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"DeepSeek 接口调用失败：{exc}") from exc

    choices = data.get("choices") or []
    text = ""
    if choices:
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "").strip()
    if not text:
        raise RuntimeError("DeepSeek 返回结果为空。")
    return text


def summarize_job(
    job: dict[str, Any],
    *,
    fmt: Any,
    length: Any,
    page_index: Any = 0,
    regenerate: bool = False,
) -> dict[str, Any]:
    """按需生成或命中缓存。返回 summary / cached / page / format / length。"""
    if (job.get("status") or "") != "done":
        raise ValueError("只有已完成的任务可以生成摘要")

    fmt_n = parse_format(fmt)
    length_n = parse_length(length)
    page, article = article_for_page(job, page_index)
    if not article:
        raise ValueError("当前任务没有可摘要的文章")

    cached = cache_get(job.get("summaries"), page, fmt_n, length_n)
    if cached and not regenerate:
        return {
            "summary": cached,
            "cached": True,
            "page": page,
            "format": fmt_n,
            "length": length_n,
        }

    text = request_summary(article, fmt_n, length_n)
    return {
        "summary": text,
        "cached": False,
        "page": page,
        "format": fmt_n,
        "length": length_n,
    }
