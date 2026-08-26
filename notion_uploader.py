"""Notion 上传：把文章写成用户指定父页面下的子页面。

每用户走 Notion **OAuth**（public integration）：授权时由用户勾选可访问的
页面，access token 存在 ``jobs.db`` 的 ``notion_tokens`` 表。不会申请整个
工作区权限。
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlsplit, urlunsplit

import requests

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"
TEXT_LIMIT = 2000
CHILDREN_LIMIT = 100
TITLE_LIMIT = 2000

# Notion code block 允许的 language；未知值回退 plain text
_CODE_LANGS = {
    "abap", "abc", "agda", "arduino", "ascii art", "assembly", "bash", "basic",
    "bnf", "c", "c#", "c++", "clojure", "coffeescript", "coq", "css", "dart",
    "dhall", "diff", "docker", "ebnf", "elixir", "elm", "erlang", "f#", "flow",
    "fortran", "gherkin", "glsl", "go", "graphql", "groovy", "haskell", "hcl",
    "html", "idris", "java", "javascript", "json", "julia", "kotlin", "latex",
    "less", "lisp", "livescript", "llvm ir", "lua", "makefile", "markdown",
    "markup", "matlab", "mathematica", "mermaid", "nix", "objective-c", "ocaml",
    "pascal", "perl", "php", "plain text", "powershell", "prolog", "protobuf",
    "purescript", "python", "r", "racket", "reason", "ruby", "rust", "sass",
    "scala", "scheme", "scss", "shell", "smalltalk", "solidity", "sql", "swift",
    "toml", "typescript", "vb.net", "verilog", "vhdl", "visual basic",
    "webassembly", "xml", "yaml",
}
_CODE_ALIASES = {
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "sh": "shell",
    "zsh": "shell",
    "bash": "bash",
    "yml": "yaml",
    "md": "markdown",
    "txt": "plain text",
    "text": "plain text",
    "c++": "c++",
    "cpp": "c++",
    "cs": "c#",
}

_PAGE_ID_DASHED = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.I,
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_UL_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")


# ---------------------------------------------------------------------------
# OAuth client (server-wide) + per-user access token
# ---------------------------------------------------------------------------

def oauth_client_config() -> tuple[str, str] | None:
    """Return (client_id, client_secret) if Notion OAuth is configured."""
    cid = os.environ.get("NOTION_CLIENT_ID", "").strip()
    secret = os.environ.get("NOTION_CLIENT_SECRET", "").strip()
    if cid and secret:
        return cid, secret
    return None


def oauth_configured() -> bool:
    return oauth_client_config() is not None


def _load_token_record(user_id: str | None) -> dict[str, Any] | None:
    if not user_id:
        return None
    try:
        import db as _db
        raw = str(_db.get_notion_token(user_id) or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict) and data.get("access_token"):
            return data
        return None
    # 兼容上一版粘贴的 Internal Integration Secret
    return {"access_token": raw}


def is_configured(user_id: str | None) -> bool:
    """Return True if this user has completed Notion OAuth (or has a stored token)."""
    rec = _load_token_record(user_id)
    return bool(rec and rec.get("access_token"))


def auth_status(user_id: str | None) -> dict[str, Any]:
    rec = _load_token_record(user_id)
    workspace = ""
    if rec:
        workspace = str(rec.get("workspace_name") or "").strip()
    return {
        "authenticated": bool(rec and rec.get("access_token")),
        "workspace": workspace,
        "oauth_ready": oauth_configured(),
    }


def get_auth_url(redirect_uri: str) -> tuple[str, str]:
    """Return (authorization_url, state) for the Notion OAuth popup."""
    cfg = oauth_client_config()
    if cfg is None:
        raise FileNotFoundError(
            "未配置 Notion OAuth。请在 .env.local 设置 NOTION_CLIENT_ID 和 NOTION_CLIENT_SECRET，"
            "并在 Notion Integration 的 Redirect URIs 中添加：\n" + redirect_uri
        )
    client_id, _secret = cfg
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": client_id,
        "response_type": "code",
        "owner": "user",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    return f"{NOTION_API}/oauth/authorize?{urlencode(params)}", state


def exchange_code(code: str, redirect_uri: str, user_id: str) -> tuple[bool, str]:
    """Exchange the OAuth code for an access token and store it per-user."""
    cfg = oauth_client_config()
    if cfg is None:
        return False, "服务端未配置 Notion OAuth 客户端。"
    client_id, client_secret = cfg
    code = str(code or "").strip()
    if not code:
        return False, "未收到授权码。"
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    try:
        resp = requests.post(
            f"{NOTION_API}/oauth/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/json",
                "Notion-Version": NOTION_VERSION,
            },
            json={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        return False, f"交换 Notion token 失败：{exc}"
    if not resp.ok:
        return False, f"交换 Notion token 失败：{_error_message(resp)}"
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        return False, "Notion 返回了无法解析的 token 响应"
    access = str(payload.get("access_token") or "").strip()
    if not access:
        return False, "Notion 未返回 access_token"
    record = {
        "access_token": access,
        "refresh_token": payload.get("refresh_token") or "",
        "bot_id": payload.get("bot_id") or "",
        "workspace_id": payload.get("workspace_id") or "",
        "workspace_name": payload.get("workspace_name") or "",
    }
    try:
        import db as _db
        _db.save_notion_token(user_id, json.dumps(record, ensure_ascii=False))
    except Exception:
        return False, "授权成功，但 token 写入数据库失败，请检查 jobs.db 权限后重新授权。"
    workspace = str(record.get("workspace_name") or "").strip()
    if workspace:
        return True, f"授权成功！已连接 Notion 工作区「{workspace}」。"
    return True, "授权成功！Notion 已连接。"


def parse_page_id(value: str) -> str:
    """Extract a Notion page UUID from a URL, dashed id, or 32-hex id.

    Returns dashed lowercase UUID, or empty string if nothing recognizable.
    """
    raw = unquote(str(value or "")).strip()
    if not raw:
        return ""
    dashed = _PAGE_ID_DASHED.search(raw)
    if dashed:
        return dashed.group(1).lower()
    # Notion 分享链接把 32 位 hex 放在路径最后一段末尾（前面可能是标题）
    segment = raw.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    compact = re.sub(r"[^0-9a-fA-F]", "", segment)
    if len(compact) < 32:
        return ""
    hex32 = compact[-32:].lower()
    return f"{hex32[:8]}-{hex32[8:12]}-{hex32[12:16]}-{hex32[16:20]}-{hex32[20:]}"


def _token_for(user_id: str | None) -> str:
    rec = _load_token_record(user_id)
    if not rec:
        return ""
    return str(rec.get("access_token") or "").strip()


def _page_title(page: dict[str, Any]) -> str:
    props = page.get("properties") or {}
    if not isinstance(props, dict):
        return "无标题页面"
    for prop in props.values():
        if not isinstance(prop, dict) or prop.get("type") != "title":
            continue
        parts = prop.get("title") or []
        text = "".join(
            str(p.get("plain_text") or "") for p in parts if isinstance(p, dict)
        ).strip()
        if text:
            return text[:200]
    return "无标题页面"


def _parent_ref_id(page: dict[str, Any]) -> str:
    parent = page.get("parent") or {}
    if not isinstance(parent, dict):
        return ""
    ptype = str(parent.get("type") or "").strip()
    if ptype in ("page_id", "database_id", "block_id"):
        return str(parent.get(ptype) or "").strip().lower()
    return ""


def list_accessible_pages(user_id: str) -> dict[str, Any]:
    """List pages shared with this user's Notion connection.

    Notion OAuth does not return the IDs the user ticked. After the token
    exists, ``POST /v1/search`` is the supported way to discover them.

    ``pages`` is every accessible page. ``roots`` are the likely OAuth
    selections: workspace-level pages, or pages whose parent is not itself
    in the accessible set (so created article children are filtered out).
    """
    token = _token_for(user_id)
    if not token:
        return {"pages": [], "roots": [], "error": None}

    raw: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(5):
        body: dict[str, Any] = {
            "filter": {"property": "object", "value": "page"},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        data, err = _request("POST", "/search", token, json_body=body)
        if err:
            return {"pages": [], "roots": [], "error": err.get("message")}
        for item in (data or {}).get("results") or []:
            if item.get("object") != "page":
                continue
            pid = str(item.get("id") or "").strip().lower()
            if not pid:
                continue
            raw.append({
                "id": pid,
                "title": _page_title(item),
                "url": str(item.get("url") or "").strip(),
                "parent_id": _parent_ref_id(item),
                "parent_type": str((item.get("parent") or {}).get("type") or ""),
            })
        if not (data or {}).get("has_more"):
            break
        cursor = (data or {}).get("next_cursor")
        if not cursor:
            break

    ids = {p["id"] for p in raw}
    roots = [
        p for p in raw
        if p["parent_type"] == "workspace"
        or not p["parent_id"]
        or p["parent_id"] not in ids
    ]
    if not roots:
        roots = list(raw)
    return {"pages": raw, "roots": roots, "error": None}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _error_message(resp: requests.Response) -> str:
    try:
        payload = resp.json()
        msg = str(payload.get("message") or "").strip()
        code = str(payload.get("code") or "").strip()
        if msg and code:
            return f"{code}: {msg}"
        return msg or resp.text[:300]
    except Exception:
        return resp.text[:300] or f"HTTP {resp.status_code}"


def _request(method: str, path: str, token: str,
             json_body: dict[str, Any] | None = None,
             params: dict[str, Any] | None = None,
             timeout: int = 30) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """HTTP helper. Returns (json, None) or (None, {error, message})."""
    url = path if path.startswith("http") else f"{NOTION_API}{path}"
    for attempt in range(4):
        try:
            resp = requests.request(
                method, url, headers=_headers(token),
                json=json_body, params=params, timeout=timeout,
            )
        except requests.Timeout:
            return None, {"error": "timeout", "message": "请求 Notion API 超时"}
        except requests.RequestException as exc:
            return None, {"error": "network", "message": f"请求 Notion API 失败：{exc}"}
        if resp.status_code == 429 and attempt < 3:
            wait = 1.0
            try:
                wait = max(wait, float(resp.headers.get("Retry-After") or 1))
            except ValueError:
                pass
            time.sleep(wait)
            continue
        if not resp.ok:
            status = resp.status_code
            hint = _error_message(resp)
            if status in (401, 403):
                hint = (
                    f"{hint}。请到设置页重新授权 Notion，"
                    "并确认授权时勾选了目标父页面。"
                )
            elif status == 404:
                hint = (
                    f"{hint}。常见原因：父页面 ID 不对，"
                    "或授权时没有勾选该页面。"
                )
            return None, {"error": "api_error", "message": hint, "status": status}
        try:
            return resp.json(), None
        except json.JSONDecodeError:
            return None, {"error": "api_error", "message": "Notion 返回了无法解析的响应"}
    return None, {"error": "rate_limited", "message": "Notion API 请求过于频繁，请稍后重试"}


def verify_token(token: str) -> tuple[bool, str]:
    """Call GET /v1/users/me. Returns (ok, message)."""
    token = str(token or "").strip()
    if not token:
        return False, "Token 为空"
    data, err = _request("GET", "/users/me", token)
    if err:
        return False, err["message"]
    name = ""
    if isinstance(data, dict):
        name = str(data.get("name") or "").strip()
    return True, name or "ok"


# ---------------------------------------------------------------------------
# Markdown → Notion blocks
# ---------------------------------------------------------------------------

def _chunks(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    if not text:
        return [""]
    return [text[i:i + limit] for i in range(0, len(text), limit)]


_ALLOWED_LINK_SCHEMES = {"http", "https", "mailto"}
_MARKDOWN_TITLE_RE = re.compile(r"""^(\S+)(?:\s+["'(].*)?$""")
_BARE_HOST_RE = re.compile(
    r"^(?:www\.|[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,}(?::\d+)?(?:/|\?|#|$)"
)


def _normalize_notion_url(raw: str) -> str | None:
    """Return a URL Notion will accept, or None to keep the text unlinked.

    Article TOCs are ``[章节](#锚点)``; Notion rejects fragment-only and
    other non-absolute links with ``Invalid URL for link``.
    """
    url = str(raw or "").strip()
    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1].strip()
    titled = _MARKDOWN_TITLE_RE.match(url)
    if titled:
        url = titled.group(1)
    url = url.strip(" \t\"'")
    if not url or url.startswith("#"):
        return None
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlsplit(url)
    if not parsed.scheme:
        if _BARE_HOST_RE.match(url):
            url = "https://" + url
            parsed = urlsplit(url)
        else:
            return None
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_LINK_SCHEMES:
        return None
    if scheme in ("http", "https") and not parsed.netloc:
        return None
    try:
        path = quote(parsed.path, safe="/:@-._~!$&'()*+,;=")
        query = quote(parsed.query, safe="=&:@-._~!$'()*+,;/?")
        fragment = quote(parsed.fragment, safe=":@-._~!$&'()*+,;=")
    except Exception:
        return None
    rebuilt = urlunsplit((scheme, parsed.netloc, path, query, fragment))
    if " " in rebuilt or len(rebuilt) > 2000:
        return None
    return rebuilt


def _plain_rich(text: str, **annotations: Any) -> list[dict[str, Any]]:
    link = _normalize_notion_url(str(annotations.pop("link", "") or ""))
    items: list[dict[str, Any]] = []
    for part in _chunks(text):
        item: dict[str, Any] = {"type": "text", "text": {"content": part}}
        if link:
            item["text"]["link"] = {"url": link}
        if annotations:
            item["annotations"] = dict(annotations)
        items.append(item)
    return items


def _equation_rich(expr: str) -> list[dict[str, Any]]:
    expr = str(expr or "").strip()
    if not expr:
        return _plain_rich("")
    return [{"type": "equation", "equation": {"expression": expr[:TEXT_LIMIT]}}]


def _equation_block(expr: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "equation",
        "equation": {"expression": (expr or "").strip() or " "},
    }


# 行内 $...$：美元金额（$2,000）不当公式；公式里的 \$ 不当结束符。
_INLINE_TOKEN_RE = re.compile(
    r"!\[(?P<img_alt>[^\]]*)\]\((?P<img_src>[^)]+)\)"
    r"|\*\*(?P<bold>.+?)\*\*"
    r"|__(?P<bold2>.+?)__"
    r"|`(?P<code>[^`]+)`"
    r"|\[(?P<link_text>[^\]]+)\]\((?P<link_href>[^)]+)\)"
    r"|~~(?P<strike>.+?)~~"
    r"|\$\$(?P<display_math>(?:\\.|[^$])+?)\$\$"
    r"|\\\((?P<math_paren>.+?)\\\)"
    r"|\$(?!\d)(?P<math>(?:\\.|[^$\n])+?)\$"
    r"|(?P<url>https?://[^\s<>\]）)，。；、]+)"
    r"|(?<!\*)\*(?P<italic>.+?)(?<!\*)\*(?!\*)",
)

_ONLY_IMAGE_RE = re.compile(r"^!\[(?P<alt>[^\]]*)\]\((?P<src>[^)]+)\)\s*$")
_TODO_RE = re.compile(r"^\[([ xX])\]\s+(.*)$")
_CURRENCY_RE = re.compile(r"^[\d,.\s\-–—]+$")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+")


def _should_emit_equation(expr: str) -> bool:
    """Return False for currency / 中文片段，避免 Notion 显示 Invalid equation。"""
    s = (expr or "").strip()
    if not s:
        return False
    if _CURRENCY_RE.match(s):
        return False
    if _CJK_RE.search(s) and not _LATEX_CMD_RE.search(s):
        return False
    if s.endswith("\\") and not s.endswith("\\\\"):
        return False
    return True


def _parse_inline(text: str) -> list[dict[str, Any]]:
    """Split a line into Notion rich_text (bold / italic / code / link / math)."""
    if not text:
        return _plain_rich("")
    out: list[dict[str, Any]] = []
    pos = 0
    for m in _INLINE_TOKEN_RE.finditer(text):
        if m.start() > pos:
            out.extend(_plain_rich(text[pos:m.start()]))
        gd = m.groupdict()
        if gd.get("img_alt") is not None:
            label = gd["img_alt"] or gd["img_src"]
            out.extend(_plain_rich(label, link=gd["img_src"]))
        elif gd.get("bold") is not None:
            out.extend(_plain_rich(gd["bold"], bold=True))
        elif gd.get("bold2") is not None:
            out.extend(_plain_rich(gd["bold2"], bold=True))
        elif gd.get("code") is not None:
            out.extend(_plain_rich(gd["code"], code=True))
        elif gd.get("link_text") is not None:
            out.extend(_plain_rich(gd["link_text"], link=gd["link_href"]))
        elif gd.get("strike") is not None:
            out.extend(_plain_rich(gd["strike"], strikethrough=True))
        elif gd.get("display_math") is not None:
            inner = gd["display_math"]
            if _should_emit_equation(inner):
                out.extend(_equation_rich(inner))
            else:
                out.extend(_plain_rich(f"$${inner}$$"))
        elif gd.get("math_paren") is not None:
            inner = gd["math_paren"]
            if _should_emit_equation(inner):
                out.extend(_equation_rich(inner))
            else:
                out.extend(_plain_rich(f"\\({inner}\\)"))
        elif gd.get("math") is not None:
            inner = gd["math"]
            if _should_emit_equation(inner):
                out.extend(_equation_rich(inner))
            else:
                out.extend(_plain_rich(f"${inner}$"))
        elif gd.get("url") is not None:
            raw_url = gd["url"].rstrip(".,;:)")
            out.extend(_plain_rich(raw_url, link=raw_url))
        elif gd.get("italic") is not None:
            out.extend(_plain_rich(gd["italic"], italic=True))
        else:
            out.extend(_plain_rich(m.group(0)))
        pos = m.end()
    if pos < len(text):
        out.extend(_plain_rich(text[pos:]))
    return out or _plain_rich("")


def _block(kind: str, text: str, *, inline: bool = True, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = extra
    payload["rich_text"] = _parse_inline(text) if inline else _plain_rich(text)
    return {"object": "block", "type": kind, kind: payload}


def _split_table_cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


_TABLE_DIVIDER_CELL_RE = re.compile(r"^:?-{3,}:?$")


def _is_table_divider(line: str) -> bool:
    cells = _split_table_cells(line)
    return bool(cells) and all(_TABLE_DIVIDER_CELL_RE.match(c.replace(" ", "")) for c in cells)


def _table_block(header: list[str], rows: list[list[str]]) -> dict[str, Any]:
    """Markdown table → Notion table with nested table_row children."""
    width = max([len(header)] + [len(r) for r in rows] + [1])
    width = min(max(width, 1), 100)
    children: list[dict[str, Any]] = []
    for row in [header, *rows]:
        cells = (list(row) + [""] * width)[:width]
        children.append({
            "object": "block",
            "type": "table_row",
            "table_row": {"cells": [_parse_inline(c) for c in cells]},
        })
    return {
        "object": "block",
        "type": "table",
        "table": {
            "table_width": width,
            "has_column_header": True,
            "has_row_header": False,
            "children": children,
        },
    }


_TOC_ITEM_RE = re.compile(r"^\[[^\]]+\]\(#[^)]+\)$")


def _code_language(lang: str) -> str:
    key = (lang or "").strip().lower()
    if key in _CODE_ALIASES:
        key = _CODE_ALIASES[key]
    if key in _CODE_LANGS:
        return key
    return "plain text"


def _code_block(code: str, lang: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "code",
        "code": {
            "rich_text": _plain_rich(code or " "),
            "language": _code_language(lang),
        },
    }


def _image_block(url: str, caption: str = "") -> dict[str, Any] | None:
    href = _normalize_notion_url(url)
    if not href:
        return None
    image: dict[str, Any] = {
        "type": "external",
        "external": {"url": href},
    }
    if caption.strip():
        image["caption"] = _plain_rich(caption.strip())
    return {"object": "block", "type": "image", "image": image}


def _fence_lang(header: str) -> str:
    return (header or "").strip().split(None, 1)[0].lower() if header.strip() else ""


def _mermaid_or_code_blocks(source: str) -> list[dict[str, Any]]:
    # Notion 原生支持 language=mermaid 的 code block，比 mermaid.ink 外链图更稳定。
    return [_code_block(source, "mermaid")]


def _list_item_block(kind: str, text: str, checked: bool = False) -> dict[str, Any]:
    if kind == "to_do":
        return {
            "object": "block",
            "type": "to_do",
            "to_do": {"rich_text": _parse_inline(text), "checked": checked},
        }
    return _block(kind, text)


def _fold_list_items(items: list[tuple[int, str, str, bool]]) -> list[dict[str, Any]]:
    """Fold (indent, text, kind, checked) into Notion list blocks with one nesting level."""
    roots: list[dict[str, Any]] = []
    stack: list[tuple[int, dict[str, Any]]] = []
    for indent, text, kind, checked in items:
        node = _list_item_block(kind, text, checked)
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            parent = stack[-1][1]
            payload = parent[parent["type"]]
            payload.setdefault("children", []).append(node)
        else:
            roots.append(node)
        stack.append((indent, node))
    return roots


def extract_title(markdown: str, fallback: str = "") -> str:
    """First ATX heading, else fallback, else a generic name."""
    for line in (markdown or "").splitlines():
        m = _HEADING_RE.match(line.strip())
        if m:
            title = m.group(2).strip()
            if title:
                return title[:TITLE_LIMIT]
    title = str(fallback or "").strip()
    return (title or "未命名文章")[:TITLE_LIMIT]


def markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    """Convert a markdown article into Notion block objects."""
    lines = (markdown or "").replace("\r\n", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    i = 0
    n = len(lines)

    def paragraph_from(buf: list[str]) -> None:
        text = "\n".join(buf).strip()
        if not text:
            return
        img = _ONLY_IMAGE_RE.match(text)
        if img:
            image = _image_block(img.group("src"), img.group("alt") or "")
            if image is not None:
                blocks.append(image)
                return
            blocks.append(_block("paragraph", img.group("alt") or img.group("src")))
            return
        blocks.append(_block("paragraph", text))

    def is_display_math_open(raw: str) -> bool:
        s = raw.strip()
        return s.startswith("$$") or s == "\\[" or s.startswith("\\[")

    while i < n:
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            lang = _fence_lang(stripped[3:])
            i += 1
            body: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            if i < n:
                i += 1
            source = "\n".join(body)
            if lang == "mermaid":
                blocks.extend(_mermaid_or_code_blocks(source))
            else:
                blocks.append(_code_block(source, lang))
            continue

        if is_display_math_open(line):
            if stripped.startswith("$$") and stripped.endswith("$$") and len(stripped) > 4:
                blocks.append(_equation_block(stripped[2:-2]))
                i += 1
                continue
            closer = "$$" if stripped.startswith("$$") else "\\]"
            expr_parts: list[str] = []
            if stripped.startswith("$$") and stripped != "$$":
                expr_parts.append(stripped[2:])
            elif stripped.startswith("\\[") and stripped != "\\[":
                expr_parts.append(stripped[2:])
            i += 1
            while i < n:
                cur = lines[i]
                i += 1
                cs = cur.strip()
                if closer == "$$" and cs.endswith("$$"):
                    piece = cs[:-2].strip()
                    if piece:
                        expr_parts.append(piece)
                    break
                if closer == "\\]" and (cs == "\\]" or cs.endswith("\\]")):
                    piece = cs[:-2].strip() if cs.endswith("\\]") else ""
                    if piece:
                        expr_parts.append(piece)
                    break
                expr_parts.append(cur)
            blocks.append(_equation_block("\n".join(expr_parts)))
            continue

        if _HR_RE.match(line):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = min(len(heading.group(1)), 3)
            blocks.append(_block(f"heading_{level}", heading.group(2).strip()))
            i += 1
            continue

        if line.lstrip().startswith(">"):
            quotes: list[str] = []
            while i < n and lines[i].lstrip().startswith(">"):
                quotes.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            text = "\n".join(quotes).strip() or " "
            url_m = re.search(r"https?://\S+", text)
            if text.startswith("原视频链接") and url_m:
                href = _normalize_notion_url(url_m.group(0))
                if href:
                    blocks.append({
                        "object": "block",
                        "type": "bookmark",
                        "bookmark": {
                            "url": href,
                            "caption": _plain_rich("原视频链接"),
                        },
                    })
                    continue
            if "核心要点" in text:
                blocks.append({
                    "object": "block",
                    "type": "callout",
                    "callout": {
                        "rich_text": _parse_inline(text),
                        "icon": {"type": "emoji", "emoji": "💡"},
                        "color": "gray_background",
                    },
                })
            else:
                blocks.append(_block("quote", text))
            continue

        ul = _UL_RE.match(line)
        ol = _OL_RE.match(line)
        if ul or ol:
            collected: list[tuple[int, str, str, bool]] = []
            while i < n:
                um = _UL_RE.match(lines[i])
                om = _OL_RE.match(lines[i])
                if not um and not om:
                    break
                if um:
                    indent = len(um.group(1).expandtabs(4))
                    raw_text = um.group(3)
                    todo = _TODO_RE.match(raw_text)
                    if todo:
                        collected.append((indent, todo.group(2), "to_do", todo.group(1) != " "))
                    else:
                        collected.append((indent, raw_text, "bulleted_list_item", False))
                else:
                    indent = len(om.group(1).expandtabs(4))
                    collected.append((indent, om.group(3), "numbered_list_item", False))
                i += 1
            toc_texts = [t for _, t, k, _ in collected if k == "bulleted_list_item"]
            if (
                toc_texts
                and len(toc_texts) == len(collected)
                and all(_TOC_ITEM_RE.match(t.strip()) for t in toc_texts)
            ):
                blocks.append({
                    "object": "block",
                    "type": "table_of_contents",
                    "table_of_contents": {"color": "default"},
                })
            else:
                blocks.extend(_fold_list_items(collected))
            continue

        if "|" in line and i + 1 < n and _is_table_divider(lines[i + 1]):
            header = _split_table_cells(line)
            i += 2
            body_rows: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip() and not _HEADING_RE.match(lines[i]):
                if _is_table_divider(lines[i]):
                    i += 1
                    continue
                body_rows.append(_split_table_cells(lines[i]))
                i += 1
            blocks.append(_table_block(header, body_rows))
            continue

        if not stripped:
            i += 1
            continue

        para: list[str] = []
        while i < n:
            nxt = lines[i]
            if (
                not nxt.strip()
                or nxt.strip().startswith("```")
                or is_display_math_open(nxt)
                or _HEADING_RE.match(nxt)
                or _HR_RE.match(nxt)
                or nxt.lstrip().startswith(">")
                or _UL_RE.match(nxt)
                or _OL_RE.match(nxt)
                or (i + 1 < n and "|" in nxt and _is_table_divider(lines[i + 1]))
            ):
                break
            para.append(nxt)
            i += 1
        paragraph_from(para)

    return blocks


# ---------------------------------------------------------------------------
# Notion write
# ---------------------------------------------------------------------------

def find_or_create_child_page(
    parent_id: str, title: str, token: str,
) -> tuple[str | None, dict[str, Any] | None]:
    """Find a direct child page with this title, or create one."""
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        data, err = _request("GET", f"/blocks/{parent_id}/children", token, params=params)
        if err:
            return None, err
        for block in (data or {}).get("results") or []:
            if block.get("type") == "child_page" and (
                (block.get("child_page") or {}).get("title") == title
            ):
                return block.get("id"), None
        if not (data or {}).get("has_more"):
            break
        cursor = (data or {}).get("next_cursor")
        if not cursor:
            break

    created, err = _create_page(parent_id, title, [], token)
    if err:
        return None, err
    return (created or {}).get("id"), None


def _create_page(
    parent_id: str, title: str, children: list[dict[str, Any]], token: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    body: dict[str, Any] = {
        "parent": {"page_id": parent_id},
        "properties": {
            "title": {
                "title": _plain_rich((title or "未命名文章")[:TITLE_LIMIT]),
            }
        },
    }
    first, rest = children[:CHILDREN_LIMIT], children[CHILDREN_LIMIT:]
    if first:
        body["children"] = first
    data, err = _request("POST", "/pages", token, json_body=body)
    if err:
        return None, err
    page_id = (data or {}).get("id")
    if rest and page_id:
        append_err = _append_children(page_id, rest, token)
        if append_err:
            return data, append_err
    return data, None


def _append_children(page_id: str, children: list[dict[str, Any]], token: str) -> dict[str, Any] | None:
    for offset in range(0, len(children), CHILDREN_LIMIT):
        chunk = children[offset:offset + CHILDREN_LIMIT]
        _, err = _request(
            "PATCH", f"/blocks/{page_id}/children", token,
            json_body={"children": chunk},
        )
        if err:
            return err
    return None


def create_article_page(
    *,
    markdown: str,
    parent_page_id: str,
    user_id: str,
    title: str = "",
    date_subdir: bool = False,
) -> dict[str, Any]:
    """Create a Notion child page for one article.

    Returns ``{"status": "success", "data": {"id", "url", "title"}}``
    or ``{"status": "error", "error", "message"}``.
    """
    token = _token_for(user_id)
    if not token:
        return {
            "status": "error",
            "error": "not_configured",
            "message": "尚未授权 Notion，请先到设置页完成 OAuth 授权。",
        }
    parent_id = parse_page_id(parent_page_id)
    if not parent_id:
        return {
            "status": "error",
            "error": "missing_parent",
            "message": "请在设置页选择 Notion 写入页面（授权时勾选的那个）。",
        }

    page_title = extract_title(markdown, title)
    target_parent = parent_id
    if date_subdir:
        date_name = time.strftime("%Y%m%d")
        found, err = find_or_create_child_page(parent_id, date_name, token)
        if err:
            return {"status": "error", **err}
        if not found:
            return {
                "status": "error",
                "error": "api_error",
                "message": "无法创建日期子页面",
            }
        target_parent = found

    blocks = markdown_to_blocks(markdown)
    data, err = _create_page(target_parent, page_title, blocks, token)
    if err:
        return {"status": "error", **err}
    return {
        "status": "success",
        "data": {
            "id": (data or {}).get("id", ""),
            "url": (data or {}).get("url", ""),
            "title": page_title,
        },
    }
