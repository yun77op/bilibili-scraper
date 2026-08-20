// 任务条目组件：状态徽章、展开详情（HTML/摘要/转写稿/日志 tab）、操作按钮
// 展开状态与 tab 是组件内部状态，列表刷新时 Vue 按 :key 保留实例，不重建
import { api, toast } from "/static/common.js?v=20260819t2";
import { sanitizeMarkdown, renderRich } from "/static/markdown.js?v=20260819t2";

const BADGE_MAP = {
  queued: ["排队中", "badge-queued"],
  running: ["处理中", "badge-running"],
  done: ["完成", "badge-done"],
  error: ["失败", "badge-error"],
  cancelled: ["已取消", "badge-cancelled"],
};

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
      busy: null, // retry | cancel | delete | notion
      htmlContent: null, // HTML 渲染结果（懒渲染缓存）
      htmlRenderedFor: null, // 已渲染的文章内容，列表轮询内容不变时不重渲
      readerOpen: false, // 阅读模式弹窗是否打开
      readerProgress: 0, // 阅读进度 0-100（按阅读区滚动条位置计算）
      readerObserver: null, // 阅读区尺寸变化监听（刷新进度）
      downloadMenuOpen: false, // 下载下拉菜单是否展开
      summaryFormat: "paragraph", // paragraph | bullets | oneliner
      summaryLength: "medium", // short | medium | long
      summaryBusy: false,
      summaryError: "",
      summaryLocal: {}, // "page:fmt:length" → 刚生成的文本，避免等列表轮询
      summaryHtml: "",
      summaryRenderedFor: null,
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
          { key: "summary", label: "✨ 摘要" },
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
    summaryPage() {
      return this.isMultiPage ? this.activePage : 0;
    },
    summaryKey() {
      return `${this.summaryPage}:${this.summaryFormat}:${this.summaryLength}`;
    },
    currentSummary() {
      const local = this.summaryLocal[this.summaryKey];
      if (local) return local;
      const pages = this.job.summaries && this.job.summaries.pages;
      const slot = pages && pages[String(this.summaryPage)];
      const fmtSlot = slot && slot[this.summaryFormat];
      const text = fmtSlot && fmtSlot[this.summaryLength];
      return typeof text === "string" && text.trim() ? text : "";
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
      // 重试会清空文章：丢掉组件内的摘要缓存，避免展示上一轮结果
      if (this.job.status !== "done" || !this.job.article) {
        this.summaryLocal = {};
        this.summaryHtml = "";
        this.summaryRenderedFor = null;
        this.summaryError = "";
      }
      // HTML tab 或阅读弹窗可见时，文章内容若已更新则重新渲染（含 KaTeX 与 Mermaid）
      if ((this.activeTab === "html" || this.readerOpen) && this.htmlRenderedFor !== this.currentArticle) {
        this.renderHtml();
        this.renderRichAfterTick();
      }
      if (this.activeTab === "summary") this.renderSummary();
    },
    downloadMenuOpen(open) {
      // 展开时监听文档点击，点击菜单外部收起
      if (open) document.addEventListener("click", this.onDocClick);
      else document.removeEventListener("click", this.onDocClick);
    },
    // 阅读弹窗打开时，文章内容重渲（任务更新/切篇）后按新高度刷新进度
    htmlContent() {
      if (this.readerOpen) this.$nextTick(() => this.updateReaderProgress());
    },
    currentSummary() {
      if (this.activeTab === "summary") this.renderSummary();
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
      this.summaryError = "";
      if (key === "html") {
        this.renderHtml();
        this.renderRichAfterTick();
      }
      if (key === "summary") this.renderSummary();
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
      this.htmlContent = sanitizeMarkdown(md);
      this.htmlRenderedFor = md;
    },
    // DOM 更新后，对可见的 HTML 内容依次渲染 KaTeX 与 Mermaid（HTML tab 与阅读弹窗）
    renderRichAfterTick() {
      this.$nextTick(() => {
        if (this.activeTab === "html") {
          renderRich(this.$refs.htmlPane);
        }
        if (this.readerOpen) {
          renderRich(this.$refs.readerBody);
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
      if (this.activeTab === "summary") {
        this.summaryError = "";
        this.renderSummary();
      }
      // 阅读弹窗里切篇：回到顶部并从 0 重新计进度
      if (this.readerOpen) {
        this.readerProgress = 0;
        this.$nextTick(() => {
          const body = this.$refs.readerBody;
          if (body) body.scrollTop = 0;
          this.updateReaderProgress();
        });
      }
    },
    // 打开阅读模式弹窗：惰性渲染当前文章为 HTML，锁定页面滚动，监听 Esc 关闭
    openReader() {
      this.renderHtml();
      this.readerProgress = 0;
      this.readerOpen = true;
      document.body.classList.add("reader-open");
      document.addEventListener("keydown", this.onReaderKeydown);
      this.renderRichAfterTick();
      // 弹窗挂载后：初始化进度，并监听阅读区尺寸变化
      // （KaTeX/Mermaid 渲染、任务内容更新、切篇都会改变高度，据此刷新进度）
      this.$nextTick(() => {
        this.updateReaderProgress();
        const body = this.$refs.readerBody;
        if (body && typeof ResizeObserver !== "undefined") {
          this.readerObserver = new ResizeObserver(() => this.updateReaderProgress());
          this.readerObserver.observe(body);
        }
      });
    },
    closeReader() {
      this.readerOpen = false;
      document.body.classList.remove("reader-open");
      document.removeEventListener("keydown", this.onReaderKeydown);
      if (this.readerObserver) {
        this.readerObserver.disconnect();
        this.readerObserver = null;
      }
    },
    onReaderKeydown(e) {
      if (e.key === "Escape") this.closeReader();
    },
    // 按阅读区滚动条位置计算阅读进度（0-100）
    updateReaderProgress() {
      const el = this.$refs.readerBody;
      if (!el) return;
      const max = el.scrollHeight - el.clientHeight;
      if (max <= 0) {
        // 内容不满一屏，全部可见即视为读完
        this.readerProgress = 100;
      } else {
        const pct = Math.round((el.scrollTop / max) * 100);
        this.readerProgress = Math.min(100, Math.max(0, pct));
      }
    },
    onReaderScroll() {
      this.updateReaderProgress();
    },
    // 组件卸载时清理弹窗残留的监听与滚动锁
    beforeUnmount() {
      if (this.readerOpen) {
        document.body.classList.remove("reader-open");
        document.removeEventListener("keydown", this.onReaderKeydown);
      }
      if (this.readerObserver) {
        this.readerObserver.disconnect();
        this.readerObserver = null;
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
    async uploadNotion() {
      if (this.busy) return;
      this.busy = "notion";
      const pending = toast("正在写入 Notion…", { timeout: 0 });
      try {
        const resp = await api(`/api/jobs/${this.job.id}/save-notion`, { method: "POST" });
        const data = await resp.json();
        pending.close();
        if (resp.ok && data.ok) {
          const n = Number(data.count) || (data.links || []).length || 1;
          toast(n > 1 ? `已写入 Notion（${n} 页）` : "已写入 Notion", {
            href: (data.links && data.links[0]) || "",
            hrefLabel: "打开",
            timeout: 8000,
          });
          this.$emit("refresh");
        } else {
          toast(data.error || "写入 Notion 失败", { type: "err", timeout: 8000 });
        }
      } catch {
        pending.close();
        toast("请求失败，请检查服务是否运行", { type: "err", timeout: 8000 });
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
    renderSummary() {
      const md = this.currentSummary;
      if (!md) {
        this.summaryHtml = "";
        this.summaryRenderedFor = null;
        return;
      }
      if (this.summaryRenderedFor === md) return;
      this.summaryHtml = sanitizeMarkdown(md);
      this.summaryRenderedFor = md;
    },
    setSummaryFormat(fmt) {
      if (this.summaryFormat === fmt) return;
      this.summaryFormat = fmt;
      this.summaryError = "";
    },
    setSummaryLength(length) {
      if (this.summaryLength === length) return;
      this.summaryLength = length;
      this.summaryError = "";
    },
    async generateSummary(regenerate) {
      if (this.summaryBusy) return;
      const page = this.summaryPage;
      const fmt = this.summaryFormat;
      const length = this.summaryLength;
      const key = `${page}:${fmt}:${length}`;
      this.summaryBusy = true;
      this.summaryError = "";
      try {
        const resp = await api(`/api/jobs/${this.job.id}/summary`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            format: fmt,
            length,
            page,
            regenerate: !!regenerate,
          }),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
          this.summaryError = data.error || "生成摘要失败";
          return;
        }
        if (data.summary) {
          this.summaryLocal = { ...this.summaryLocal, [key]: data.summary };
        }
      } catch {
        this.summaryError = "请求失败，请检查服务是否运行";
      } finally {
        this.summaryBusy = false;
      }
    },
    async copySummary(ev) {
      if (!this.currentSummary) return;
      await navigator.clipboard.writeText(this.currentSummary);
      this.flashButton(ev ? ev.currentTarget : ".job-copy-summary");
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
      class="job-notion-btn" title="写入 Notion" :disabled="busy !== null"
      @click="uploadNotion"
    >{{ busy === 'notion' ? '…' : '📝' }}</button>
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
      <div v-if="activeTab === 'summary' && job.status === 'done' && currentArticle">
        <div class="job-tab-toolbar job-summary-toolbar">
          <div class="summary-seg" role="group" aria-label="摘要格式">
            <button type="button" :class="{ active: summaryFormat === 'paragraph' }" @click="setSummaryFormat('paragraph')">段落</button>
            <button type="button" :class="{ active: summaryFormat === 'bullets' }" @click="setSummaryFormat('bullets')">要点</button>
            <button type="button" :class="{ active: summaryFormat === 'oneliner' }" @click="setSummaryFormat('oneliner')">一句话</button>
          </div>
          <div class="summary-seg" role="group" aria-label="摘要篇幅">
            <button type="button" :class="{ active: summaryLength === 'short' }" @click="setSummaryLength('short')">短</button>
            <button type="button" :class="{ active: summaryLength === 'medium' }" @click="setSummaryLength('medium')">中</button>
            <button type="button" :class="{ active: summaryLength === 'long' }" @click="setSummaryLength('long')">长</button>
          </div>
          <button
            class="ghost"
            :disabled="summaryBusy"
            @click="generateSummary(!!currentSummary)"
          >{{ summaryBusy ? '生成中…' : (currentSummary ? '重新生成' : '生成摘要') }}</button>
          <button
            v-if="currentSummary"
            class="ghost job-copy-summary"
            @click="copySummary"
          >复制摘要</button>
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
        <div v-if="summaryError" class="job-summary-error">{{ summaryError }}</div>
        <div v-else-if="!currentSummary && !summaryBusy" class="job-summary-empty">
          根据当前文章生成摘要。切换格式或篇幅后，未缓存的组合需要再点一次生成。
        </div>
        <div v-else-if="summaryBusy && !currentSummary" class="job-summary-empty">正在根据文章生成摘要…</div>
        <div v-else class="job-detail-html job-summary-body" v-html="summaryHtml"></div>
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
  <!-- 阅读模式弹窗：Teleport 到 body 渲染，避免继承任务条目的 cursor/字号/层叠上下文等样式 -->
  <Teleport to="body">
    <div v-if="readerOpen" class="reader-modal" @click.stop.self="closeReader">
      <div class="reader-modal-panel" role="dialog" aria-modal="true" aria-label="阅读模式">
        <!-- 阅读进度条：顶部细条，按阅读区滚动条位置填充 -->
        <div
          class="reader-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100"
          :aria-valuenow="readerProgress" :title="'阅读进度 ' + readerProgress + '%'"
        >
          <div class="reader-progress-fill" :style="{ width: readerProgress + '%' }"></div>
        </div>
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
        <div class="reader-modal-body" ref="readerBody" @scroll="onReaderScroll">
          <div class="reader-prose job-detail-html" v-html="htmlContent"></div>
        </div>
        <!-- 阅读进度百分比：固定在阅读区右下角 -->
        <div class="reader-progress-pill" :class="{ done: readerProgress >= 100 }">{{ readerProgress }}%</div>
      </div>
    </div>
  </Teleport>
</div>`,
};
