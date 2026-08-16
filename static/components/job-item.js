// 任务条目组件：状态徽章、展开详情（HTML/转写稿/日志 tab）、操作按钮
// 展开状态与 tab 是组件内部状态，列表刷新时 Vue 按 :key 保留实例，不重建
import { api } from "/static/common.js";
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
// 文章含 ```mermaid 代码块时才动态加载脚本，避免每次打开页面都拉取 3MB 文件
let mermaidPromise = null;
function loadMermaid() {
  if (!mermaidPromise) {
    mermaidPromise = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/static/vendor/mermaid/mermaid.min.js";
      s.onload = () => {
        try {
          // 与文档/离线 HTML 输出保持一致的初始化配置
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
// 文章含 $...$ / $$...$$ 数学标记时才加载样式与脚本（auto-render 依赖 katex 全局）
let katexPromise = null;
function loadKatex() {
  if (!katexPromise) {
    katexPromise = new Promise((resolve, reject) => {
      // 样式（含字体）先行，避免公式先以未渲染文本闪现
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

const BADGE_MAP = {
  queued: ["排队中", "badge-queued"],
  running: ["处理中", "badge-running"],
  done: ["完成", "badge-done"],
  error: ["失败", "badge-error"],
  cancelled: ["已取消", "badge-cancelled"],
};

// ── Markdown 强调标记修复 ─────────────────────────
// 模型偶尔会在 ** 与文字之间多打空格（如 "** 文字**"），CommonMark 规定 ** 后
// 不能紧跟空格才算加粗开始，导致标记失效、** 裸露。渲染前剥离内层首尾空白，
// 与后端 app.py 的 _fix_emphasis_spacing 保持一致。只修首尾空白，内部空格不动。
const EMPH_SPACE_RE = /\*\*([^*\n]+?)\*\*/g;

function fixEmphasisSpacing(md) {
  return md.replace(EMPH_SPACE_RE, (whole, inner) => {
    const stripped = inner.trim();
    return stripped !== inner ? `**${stripped}**` : whole;
  });
}

// ── LaTeX 数学公式保护 ─────────────────────────────
// Markdown 会把公式里的下划线（\mathcal{L}_{aux} 的 _）当成强调语法转成 <em>，
// 破坏公式并让 KaTeX 无法匹配 $$...$$ 等分隔符。转换前先把数学段（及围栏代码块）
// 替换为占位符，转换后再还原，与后端 app.py 的 _protect_math_segments 保持一致。
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
      // 围栏代码块：还原为 <pre><code class="language-...">（mermaid 依赖该结构）
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

function truncateUrl(url) {
  if (!url) return "";
  return url.length > 60 ? url.slice(0, 60) + "…" : url;
}

function elapsedStr(createdAt) {
  if (!createdAt) return "";
  const seconds = Math.floor(Date.now() / 1000 - createdAt);
  if (seconds < 60) return `${seconds}秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}分钟前`;
  const hours = Math.floor(minutes / 60);
  return `${hours}小时前`;
}

export default {
  name: "JobItem",
  props: {
    job: { type: Object, required: true },
  },
  emits: ["refresh"],
  data() {
    return {
      expanded: false,
      activeTab: "html", // 默认展示渲染后的 HTML（不展示 markdown 原文）
      activePage: 0, // 合集任务当前查看的篇目（0-based）
      busy: null, // retry | cancel | delete | drive
      htmlContent: null, // HTML 渲染结果（懒渲染缓存）
      htmlRenderedFor: null, // 已渲染的文章内容，列表轮询内容不变时不重渲
      readerOpen: false, // 阅读模式弹窗是否打开
      downloadMenuOpen: false, // 下载下拉菜单是否展开
    };
  },
  computed: {
    badge() {
      const [label, cls] = BADGE_MAP[this.job.status] || ["未知", ""];
      return { label, cls };
    },
    stageText() {
      const job = this.job;
      if (job.status === "error") return job.error || "";
      return job.stage || "";
    },
    elapsed() {
      return elapsedStr(this.job.created_at);
    },
    // 展开详情里的 tab 列表（按任务状态动态生成）
    tabs() {
      const job = this.job;
      if (job.status === "done" && job.article) {
        const list = [
          { key: "html", label: "🌐 HTML" },
        ];
        if (job.transcript) list.push({ key: "transcript", label: "📝 转写稿" });
        if (job.logs && job.logs.length) list.push({ key: "logs", label: "📋 日志" });
        return list;
      }
      if (job.status === "error") {
        const list = [{ key: "error", label: "❌ 错误" }];
        if (job.logs && job.logs.length) list.push({ key: "logs", label: "📋 日志" });
        return list;
      }
      return [];
    },
    logsText() {
      const logs = this.job.logs;
      return logs && logs.length ? logs.join("\n") : "";
    },
    // 合集任务（多P/多集）：page_articles 有独立的多篇文章
    isMultiPage() {
      return Array.isArray(this.job.page_articles) && this.job.page_articles.length > 1;
    },
    // 当前展示的文章：合集为当前篇，普通任务为整篇文章
    currentArticle() {
      if (this.isMultiPage) {
        return this.job.page_articles[this.activePage] || this.job.article;
      }
      return this.job.article;
    },
    // 阅读模式弹窗标题：合集显示「第 N 篇 · 篇名」，普通任务显示任务标题
    readerTitle() {
      if (this.isMultiPage) {
        const t = this.pageTitle(this.currentArticle);
        return t ? `第 ${this.activePage + 1} 篇 · ${t}` : (this.job.title || "阅读模式");
      }
      return this.job.title || "阅读模式";
    },
  },
  watch: {
    job() {
      // 状态变化后，若当前 tab 已不存在则回落到第一个
      if (!this.tabs.some((t) => t.key === this.activeTab)) {
        this.activeTab = this.tabs.length ? this.tabs[0].key : "html";
      }
      // 合集被重试清空或篇目数变化时，回落第一篇
      if (!this.isMultiPage) this.activePage = 0;
      else if (this.activePage >= this.job.page_articles.length) this.activePage = 0;
      // HTML tab 或阅读弹窗可见时，文章内容若已更新则重新渲染（含 KaTeX 与 Mermaid）
      if ((this.activeTab === "html" || this.readerOpen) && this.htmlRenderedFor !== this.currentArticle) {
        this.renderHtml();
        this.renderRichAfterTick();
      }
    },
    downloadMenuOpen(open) {
      // 展开时监听文档点击，点击菜单外部收起
      if (open) document.addEventListener("click", this.onDocClick);
      else document.removeEventListener("click", this.onDocClick);
    },
  },
  methods: {
    truncateUrl, // 模板里使用，需挂到组件实例上
    toggle() {
      if (this.tabs.length === 0) return;
      this.expanded = !this.expanded;
      // 展开时若默认落在 HTML tab，需要惰性渲染文章
      if (this.expanded && this.activeTab === "html") {
        this.renderHtml();
        this.renderRichAfterTick();
      }
    },
    switchTab(key) {
      this.activeTab = key;
      if (key === "html") {
        this.renderHtml();
        this.renderRichAfterTick();
      }
    },
    // ── 下载下拉菜单 ─────────────────────────────
    // 默认动作下载 HTML；展开后可选择 MD / HTML / PDF
    toggleDownloadMenu() {
      this.downloadMenuOpen = !this.downloadMenuOpen;
    },
    closeDownloadMenu() {
      this.downloadMenuOpen = false;
    },
    downloadChoice(fmt) {
      this.downloadMenuOpen = false;
      this.download(fmt);
    },
    onDocClick(e) {
      // 点击菜单外部（当前组件之外）时收起
      if (this.downloadMenuOpen && !(this.$el && this.$el.contains(e.target))) {
        this.downloadMenuOpen = false;
      }
    },
    // markdown → HTML（惰性渲染，切到 HTML tab 才转换）
    renderHtml() {
      const md = this.currentArticle;
      if (!md || this.htmlRenderedFor === md) return;
      // 先保护 LaTeX 数学段（及围栏代码块），再交给 marked，转换后还原，
      // 避免 Markdown 把公式里的 _ 当成强调语法破坏 $$...$$ 等数学标记
      const { protectedMd, segments } = protectMathSegments(fixEmphasisSpacing(md));
      const sanitized = DOMPurify.sanitize(marked.parse(protectedMd));
      this.htmlContent = restoreMathSegments(sanitized, segments);
      this.htmlRenderedFor = md;
    },
    // 渲染容器内的 Mermaid 代码块为图表；懒加载脚本，失败时保留原文代码块
    async renderMermaid(container) {
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
          // mermaid.render 会在 body 挂临时测量节点（id 前缀 d），用后清理
          const tmp = document.getElementById("d" + id);
          if (tmp) tmp.remove();
        }
      }
    },
    // 渲染容器内的 LaTeX 数学公式（$...$ 行内、$$...$$ 独立行）；懒加载脚本，失败保留原文
    async renderKatex(container) {
      if (!container) return;
      const html = container.innerHTML;
      if (!html || !html.includes("$")) return; // 无数学标记直接跳过
      let renderMathInElement;
      try {
        renderMathInElement = await loadKatex();
      } catch {
        return; // 脚本加载失败：保留原文
      }
      try {
        // 与后端文档/离线 HTML 保持一致的分隔符；pre/code 里的内容（含 mermaid）不参与
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
    },
    // DOM 更新后，对可见的 HTML 内容依次渲染 KaTeX 与 Mermaid（HTML tab 与阅读弹窗）
    renderRichAfterTick() {
      this.$nextTick(() => {
        if (this.activeTab === "html") {
          this.renderKatex(this.$refs.htmlPane);
          this.renderMermaid(this.$refs.htmlPane);
        }
        if (this.readerOpen) {
          this.renderKatex(this.$refs.readerBody);
          this.renderMermaid(this.$refs.readerBody);
        }
      });
    },
    // 提取 markdown 首个一级标题（合集下拉选项用），过长截断
    pageTitle(md) {
      const m = md && md.match(/^#\s+(.+)$/m);
      const title = m ? m[1].trim() : "";
      return title.length > 40 ? title.slice(0, 40) + "…" : title;
    },
    // 切换合集篇目：清掉 HTML 缓存；若正停留在 HTML tab 或阅读弹窗打开则立即重渲
    switchPage(i) {
      this.activePage = i;
      this.htmlContent = null;
      this.htmlRenderedFor = null;
      if (this.activeTab === "html" || this.readerOpen) this.renderHtml();
      this.renderRichAfterTick();
    },
    // 打开阅读模式弹窗：惰性渲染当前文章为 HTML，锁定页面滚动，监听 Esc 关闭
    openReader() {
      this.renderHtml();
      this.readerOpen = true;
      document.body.classList.add("reader-open");
      document.addEventListener("keydown", this.onReaderKeydown);
      this.renderRichAfterTick();
    },
    closeReader() {
      this.readerOpen = false;
      document.body.classList.remove("reader-open");
      document.removeEventListener("keydown", this.onReaderKeydown);
    },
    onReaderKeydown(e) {
      if (e.key === "Escape") this.closeReader();
    },
    // 组件卸载时清理弹窗残留的监听与滚动锁
    beforeUnmount() {
      if (this.readerOpen) {
        document.body.classList.remove("reader-open");
        document.removeEventListener("keydown", this.onReaderKeydown);
      }
      if (this.downloadMenuOpen) {
        document.removeEventListener("click", this.onDocClick);
      }
    },
    async retry() {
      if (this.busy) return;
      this.busy = "retry";
      try {
        const resp = await api(`/api/jobs/${this.job.id}/retry`, { method: "POST" });
        const data = await resp.json();
        if (data.ok) {
          this.expanded = false;
          this.$emit("refresh");
        }
      } catch {
        // 保持按钮可点
      } finally {
        this.busy = null;
      }
    },
    async cancel() {
      if (this.busy) return;
      this.busy = "cancel";
      try {
        const resp = await api(`/api/jobs/${this.job.id}/cancel`, { method: "POST" });
        const data = await resp.json();
        if (data.ok) this.$emit("refresh");
      } catch {
        // 保持按钮可点
      } finally {
        this.busy = null;
      }
    },
    async del() {
      if (this.busy) return;
      if (!confirm("确定删除这个任务？")) return;
      this.busy = "delete";
      try {
        const resp = await api(`/api/jobs/${this.job.id}/delete`, { method: "POST" });
        const data = await resp.json();
        if (data.ok) this.$emit("refresh");
      } catch {
        // 保持按钮可点
      } finally {
        this.busy = null;
      }
    },
    async uploadDrive() {
      if (this.busy) return;
      this.busy = "drive";
      try {
        const resp = await api(`/api/jobs/${this.job.id}/save-drive`, { method: "POST" });
        const data = await resp.json();
        if (resp.ok && data.ok) {
          this.$emit("refresh");
        } else {
          alert(data.error || "上传失败");
        }
      } catch {
        alert("请求失败，请检查服务是否运行");
      } finally {
        this.busy = null;
      }
    },
    async download(fmt) {
      try {
        const resp = await api(`/api/jobs/${this.job.id}/download?format=${fmt}`);
        if (!resp.ok) {
          const data = await resp.json().catch(() => ({}));
          alert(data.error || "下载失败");
          return;
        }
        const blob = await resp.blob();
        const disposition = resp.headers.get("Content-Disposition") || "";
        let filename = `article.${fmt}`;
        const match = disposition.match(/filename\*=UTF-8''([^;]+)/);
        if (match) filename = decodeURIComponent(match[1]);
        const a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = filename;
        a.click();
        URL.revokeObjectURL(a.href);
      } catch (err) {
        alert("下载失败：" + err.message);
      }
    },
    async copyArticle(ev) {
      if (!this.currentArticle) return;
      await navigator.clipboard.writeText(this.currentArticle);
      this.flashButton(ev ? ev.currentTarget : ".job-copy-article");
    },
    async copyHtml(ev) {
      if (!this.htmlContent) return;
      await navigator.clipboard.writeText(this.htmlContent);
      this.flashButton(ev ? ev.currentTarget : ".job-copy-html");
    },
    async copyTranscript(ev) {
      if (!this.job.transcript) return;
      await navigator.clipboard.writeText(this.job.transcript);
      this.flashButton(ev ? ev.currentTarget : ".job-copy-transcript");
    },
    // 复制成功后按钮短暂显示"已复制"（target 可以是选择器或按钮元素）
    flashButton(target) {
      const btn = typeof target === "string" ? this.$el.querySelector(target) : target;
      if (!btn) return;
      const original = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => {
        btn.textContent = original;
      }, 1200);
    },
  },
  template: `
<div class="job-item" :class="{ expanded }" @click="toggle">
  <div class="job-item-main">
    <div class="job-item-top">
      <span class="job-badge" :class="badge.cls">{{ badge.label }}</span>
      <span
        class="job-badge badge-multi" v-if="isMultiPage"
        :title="'合集：共 ' + job.page_articles.length + ' 篇'"
      >📚 {{ job.page_articles.length }} 篇合集</span>
      <span class="job-item-url" :title="job.url || ''">{{ job.title || truncateUrl(job.url) }}</span>
    </div>
    <div class="job-item-meta">
      <span class="job-item-url-sub" v-if="job.title" :title="job.url || ''">{{ truncateUrl(job.url) }}</span>
      <span class="job-item-stage" :class="{ 'error-text': job.status === 'error' }">{{ stageText }}</span>
      <span class="job-item-time">{{ elapsed }}</span>
    </div>
  </div>
  <div class="job-item-actions" @click.stop>
    <button
      v-if="job.status === 'error' || job.status === 'cancelled'"
      class="job-retry-btn" title="重试任务" :disabled="busy !== null"
      @click="retry"
    >{{ busy === 'retry' ? '…' : '🔄' }}</button>
    <button
      v-if="job.status === 'running' || job.status === 'queued'"
      class="job-cancel-btn" title="取消任务" :disabled="busy !== null"
      @click="cancel"
    >{{ busy === 'cancel' ? '…' : '✕' }}</button>
    <button
      v-if="job.status === 'done'"
      class="job-drive-btn" title="上传到 Google Drive" :disabled="busy !== null"
      @click="uploadDrive"
    >{{ busy === 'drive' ? '…' : '☁️' }}</button>
    <button
      v-if="job.status === 'done' && currentArticle"
      class="job-read-btn" title="阅读模式：弹窗阅读文章"
      @click="openReader"
    >📖</button>
    <button class="job-delete-btn" title="删除任务" @click="del">🗑</button>
  </div>
  <div class="job-detail-area" v-if="expanded">
    <div class="job-detail" @click.stop>
      <div class="job-detail-tabs" v-if="tabs.length > 1">
        <button
          v-for="t in tabs" :key="t.key"
          class="job-tab" :class="{ active: activeTab === t.key }"
          @click="switchTab(t.key)"
        >{{ t.label }}</button>
      </div>
      <div v-if="activeTab === 'html' && job.status === 'done' && currentArticle">
        <div class="job-tab-toolbar" @click="closeDownloadMenu">
          <button class="ghost job-copy-html" @click="copyHtml">复制 HTML</button>
          <div class="job-download-menu" @click.stop>
            <button class="ghost job-download-main" @click="download('html')" title="下载 HTML">⬇ 下载 HTML</button>
            <button
              class="ghost job-download-toggle" @click="toggleDownloadMenu"
              title="选择下载格式" :aria-expanded="downloadMenuOpen"
            >▾</button>
            <div class="job-download-items" v-if="downloadMenuOpen">
              <button class="ghost" @click="downloadChoice('md')">下载 MD</button>
              <button class="ghost" @click="downloadChoice('html')">下载 HTML</button>
              <button class="ghost" @click="downloadChoice('pdf')">下载 PDF</button>
            </div>
          </div>
          <button class="ghost" @click="openReader">📖 阅读模式</button>
        </div>
        <div class="job-page-picker" v-if="isMultiPage">
          <select
            class="job-page-select" :value="activePage"
            @change="switchPage(Number($event.target.value))"
            :title="'共 ' + job.page_articles.length + ' 篇，选择要查看的篇目'"
          >
            <option v-for="(p, i) in job.page_articles" :key="i" :value="i">第 {{ i + 1 }} 篇 · {{ pageTitle(p) }}</option>
          </select>
          <span class="job-page-count">{{ activePage + 1 }} / {{ job.page_articles.length }}</span>
        </div>
        <div class="job-detail-html" ref="htmlPane" v-html="htmlContent"></div>
      </div>
      <div v-if="activeTab === 'error' && job.status === 'error'">
        <div class="job-detail-article" style="color:var(--danger);background:#fff5f5;">{{ job.error || '未知错误' }}</div>
      </div>
      <div v-if="activeTab === 'transcript' && job.transcript">
        <div class="job-tab-toolbar">
          <button class="ghost job-copy-transcript" @click="copyTranscript">复制转写稿</button>
        </div>
        <div class="job-detail-transcript" v-text="job.transcript"></div>
      </div>
      <div v-if="activeTab === 'logs' && logsText">
        <div class="job-detail-logs" v-text="logsText"></div>
      </div>
    </div>
  </div>
  <!-- 阅读模式弹窗：全屏遮罩 + 阅读排版的文章视图 -->
  <div v-if="readerOpen" class="reader-modal" @click.stop.self="closeReader">
    <div class="reader-modal-panel" role="dialog" aria-modal="true" aria-label="阅读模式">
      <div class="reader-modal-head">
        <div class="reader-modal-title" :title="readerTitle">{{ readerTitle }}</div>
        <div class="reader-modal-tools">
          <select
            v-if="isMultiPage"
            class="job-page-select" :value="activePage"
            @change="switchPage(Number($event.target.value))"
            :title="'共 ' + job.page_articles.length + ' 篇，选择要查看的篇目'"
          >
            <option v-for="(p, i) in job.page_articles" :key="i" :value="i">第 {{ i + 1 }} 篇 · {{ pageTitle(p) }}</option>
          </select>
          <button class="ghost" @click="copyArticle($event)">复制文章</button>
          <button class="ghost" @click="download('md')">下载 MD</button>
          <button class="reader-modal-close" @click="closeReader" title="关闭 (Esc)">✕</button>
        </div>
      </div>
      <div class="reader-modal-body" ref="readerBody">
        <div class="reader-prose job-detail-html" v-html="htmlContent"></div>
      </div>
    </div>
  </div>
</div>`,
};
