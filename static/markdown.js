// 共享 Markdown 渲染管线（ESM）：
// 1. 修复模型常见的加粗标记空格/引号问题
// 2. 保护 LaTeX 数学段与围栏代码块，避免 Markdown 转义破坏公式
// 3. marked 转 HTML → DOMPurify 消毒
// 4. 还原数学段 / 代码块
// 5. 懒加载 KaTeX（公式）与 Mermaid（图表）并按需渲染
// 供任务文章（components/job-item.js）与知识库回答（kb.js）复用

import { marked } from "/static/vendor/marked/marked.esm.min.js";
import DOMPurify from "/static/vendor/dompurify/purify.es.mjs";

// markdown 渲染后的链接默认新窗口打开
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A") {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer");
  }
});

// ── Mermaid 图表懒加载 ─────────────────────────────
let mermaidPromise = null;
export function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/static/vendor/mermaid/mermaid.min.js";
      s.onload = () => {
        try {
          window.mermaid.initialize({ startOnLoad: false, securityLevel: "strict", suppressErrorRendering: true });
          resolve(window.mermaid);
        } catch (err) {
          mermaidPromise = null;
          reject(err);
        }
      };
      s.onerror = () => {
        mermaidPromise = null;
        reject(new Error("Mermaid 加载失败"));
      };
      document.head.appendChild(s);
    });
  }
  return mermaidPromise;
}

// ── KaTeX 数学公式懒加载 ───────────────────────────
let katexPromise = null;
export function loadKatex() {
  if (!katexPromise) {
    katexPromise = new Promise((resolve, reject) => {
      const link = document.createElement("link");
      link.rel = "stylesheet";
      link.href = "/static/vendor/katex/katex.min.css";
      document.head.appendChild(link);
      const s1 = document.createElement("script");
      s1.src = "/static/vendor/katex/katex.min.js";
      s1.onload = () => {
        const s2 = document.createElement("script");
        s2.src = "/static/vendor/katex/auto-render.min.js";
        s2.onload = () => resolve(window.renderMathInElement);
        s2.onerror = () => {
          katexPromise = null;
          reject(new Error("KaTeX auto-render 加载失败"));
        };
        document.head.appendChild(s2);
      };
      s1.onerror = () => {
        katexPromise = null;
        reject(new Error("KaTeX 加载失败"));
      };
      document.head.appendChild(s1);
    });
  }
  return katexPromise;
}

// ── Markdown 强调标记修复 ─────────────────────────
const EMPH_SPACE_RE = /\*\*([^*\n]+?)\*\*/g;
const EMPH_QUOTE_RE = /\*\*([“‘'"「])([^*\n]+?)([”’'"」])\*\*/g;
// CommonMark flanking 规则：闭合 ** 前是标点且后接非标点字符（如中文）时
// 无法闭合（**90%**的股份 → 星号裸露）；开启 ** 前是中文、后接标点时同样
// 无法开启（的**“星际之门”项目** → 星号裸露）。这两种写法都等价改写为
// <strong>…</strong>，marked 与 DOMPurify 均可正确处理。
const EMPH_TRAILING_PUNCT_RE = /\*\*([^*\n]+?[\p{P}\p{S}])\*\*(?![\s\p{P}\p{S}])/gu;
const EMPH_LEADING_PUNCT_RE = /(?<![\s\p{P}\p{S}])\*\*([\p{P}\p{S}][^*\n]*?)\*\*/gu;

function fixEmphasisSpacing(md) {
  let out = md.replace(EMPH_SPACE_RE, (whole, inner) => {
    const stripped = inner.trim();
    return stripped !== inner ? `**${stripped}**` : whole;
  });
  out = out.replace(
    EMPH_QUOTE_RE,
    (whole, open, inner, close) => `${open}**${inner}**${close}`,
  );
  // flanking 失败时改写为等价 HTML（内容做 HTML 转义，防注入）
  out = out.replace(
    EMPH_TRAILING_PUNCT_RE,
    (whole, inner) => `<strong>${escapeHtmlText(inner)}</strong>`,
  );
  out = out.replace(
    EMPH_LEADING_PUNCT_RE,
    (whole, inner) => `<strong>${escapeHtmlText(inner)}</strong>`,
  );
  return out;
}

// ── LaTeX 数学公式保护 ─────────────────────────────
const MATH_PLACEHOLDER = "KATEXMATHSEG{}Z";
const MATH_SEGMENT_RE = /```[\s\S]*?```|\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$[^$\n]+?\$/g;

function protectMathSegments(md) {
  const segments = [];
  const protectedMd = md.replace(MATH_SEGMENT_RE, (m) => {
    segments.push(m);
    return MATH_PLACEHOLDER.replace("{}", segments.length - 1);
  });
  return { protectedMd, segments };
}

function escapeHtmlText(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function restoreMathSegments(html, segments) {
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    const placeholder = MATH_PLACEHOLDER.replace("{}", i);
    if (seg.startsWith("```")) {
      let body = seg.slice(3);
      const firstNl = body.indexOf("\n");
      const info = firstNl !== -1 ? body.slice(0, firstNl).trim() : "";
      body = firstNl !== -1 ? body.slice(firstNl + 1) : "";
      if (body.replace(/\n+$/, "").endsWith("```")) {
        body = body.replace(/\n+$/, "").slice(0, -3);
      }
      const langCls = info ? ` class="language-${info}"` : "";
      const codeHtml = `<pre><code${langCls}>${escapeHtmlText(body.trim())}</code></pre>`;
      const wrapped = `<p>${placeholder}</p>`;
      html = html.includes(wrapped) ? html.replace(wrapped, codeHtml) : html.split(placeholder).join(codeHtml);
    } else {
      html = html.split(placeholder).join(escapeHtmlText(seg));
    }
  }
  return html;
}

/**
 * Markdown → 消毒后的 HTML。
 * 先保护 LaTeX 数学段（及围栏代码块），再交给 marked，转换后还原，
 * 避免 Markdown 把公式里的 _ 当成强调语法破坏 $$...$$ 等数学标记。
 */
export function sanitizeMarkdown(md) {
  if (!md) return "";
  const { protectedMd, segments } = protectMathSegments(fixEmphasisSpacing(md));
  const sanitized = DOMPurify.sanitize(marked.parse(protectedMd));
  return restoreMathSegments(sanitized, segments);
}

/** 渲染容器内的 Mermaid 代码块为图表；懒加载脚本，失败时保留原文代码块 */
async function renderMermaid(container) {
  if (!container) return;
  const blocks = container.querySelectorAll("pre > code.language-mermaid");
  if (!blocks.length) return;
  let mermaid;
  try {
    mermaid = await loadMermaid();
  } catch {
    return; // 脚本加载失败：保留代码块原文
  }
  for (const code of blocks) {
    const pre = code.parentElement;
    const text = code.textContent.trim();
    if (!pre || !text) continue;
    const id = "mermaid-" + Math.random().toString(36).slice(2, 10);
    try {
      const { svg } = await mermaid.render(id, text);
      const wrap = document.createElement("div");
      wrap.className = "mermaid-block";
      wrap.innerHTML = svg;
      pre.replaceWith(wrap);
    } catch {
      // 语法错误等：保留原文代码块
    } finally {
      const tmp = document.getElementById("d" + id);
      if (tmp) tmp.remove();
    }
  }
}

/** 渲染容器内的 LaTeX 数学公式（$...$ 行内、$$...$$ 独立行）；懒加载脚本，失败保留原文 */
async function renderKatex(container) {
  if (!container) return;
  const html = container.innerHTML;
  if (!html || !html.includes("$")) return;
  let renderMathInElement;
  try {
    renderMathInElement = await loadKatex();
  } catch {
    return; // 脚本加载失败：保留原文
  }
  try {
    renderMathInElement(container, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "$", right: "$", display: false },
        { left: "\\(", right: "\\)", display: false },
      ],
      ignoredTags: ["script", "noscript", "style", "textarea", "pre", "code"],
    });
  } catch {
    // 个别公式语法错误：保留原文
  }
}

/** DOM 更新后，对容器依次渲染 KaTeX 与 Mermaid（任一失败都保留原文） */
export async function renderRich(container) {
  if (!container) return;
  await renderKatex(container);
  await renderMermaid(container);
}
