// 知识库问答页（ESM）：SSE 流式对话、Markdown 渲染、参考来源与思考过程展示
import { createApp, reactive, ref, onMounted, onUnmounted } from "/static/vendor/vue/vue.esm-browser.prod.js";
import { api, Navbar } from "/static/common.js";
import { sanitizeMarkdown, renderRich } from "/static/markdown.js";

const App = {
  components: { Navbar },
  setup() {
    const question = ref("");
    const sending = ref(false);
    const rebuilding = ref(false);
    const messages = ref([]);
    const chatBox = ref(null);
    let msgSeq = 0;
    let abortController = null;

    const kbStatus = reactive({
      articles: 0,
      chunks: 0,
      built_at: null,
      up_to_date: null,
      building: false,
      configured: null,
      model: "",
    });

    // 示例问题：后端每次随机从当前语料生成；接口异常时本地兜底为空（不展示写死问题）
    const FALLBACK_SAMPLES = [];
    const samples = ref(FALLBACK_SAMPLES.slice());

    const statusText = () => {
      if (kbStatus.up_to_date === null) return "读取中…";
      if (kbStatus.building) return "索引重建中…";
      const count = `${kbStatus.articles} 篇 · ${kbStatus.chunks} 片段`;
      if (!kbStatus.up_to_date) return `${count}（待更新）`;
      const t = kbStatus.built_at
        ? new Date(kbStatus.built_at * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
        : "";
      return `${count}${t ? " · " + t : ""}`;
    };

    // ── 索引状态 / 重建 ────────────────────────────
    async function loadStatus() {
      try {
        const resp = await api("/api/kb/status");
        const data = await resp.json();
        Object.assign(kbStatus, {
          articles: data.articles || 0,
          chunks: data.chunks || 0,
          built_at: data.built_at || null,
          up_to_date: !!data.up_to_date,
          building: !!data.building,
          configured: data.configured,
          model: data.model || "",
        });
        if (Array.isArray(data.samples) && data.samples.length) {
          samples.value = data.samples;
        }
      } catch {
        // 忽略（未登录会跳转）
      }
    }

    async function rebuild() {
      if (rebuilding.value) return;
      rebuilding.value = true;
      try {
        const resp = await api("/api/kb/rebuild", { method: "POST" });
        const data = await resp.json();
        if (!resp.ok) {
          alert(data.error || "重建失败");
          return;
        }
        Object.assign(kbStatus, {
          articles: data.articles || 0,
          chunks: data.chunks || 0,
          built_at: data.built_at || null,
          up_to_date: true,
        });
      } catch (err) {
        alert("重建失败：" + err.message);
      } finally {
        rebuilding.value = false;
      }
    }

    // ── 对话 ──────────────────────────────────────
    function scrollToBottom(force = false) {
      const el = chatBox.value;
      if (!el) return;
      if (!force) {
        // 用户向上翻阅历史时不要打断
        const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
        if (!nearBottom) return;
      }
      el.scrollTop = el.scrollHeight;
    }

    function historyForApi() {
      const out = [];
      for (const m of messages.value) {
        if (m.role === "user") {
          out.push({ role: "user", content: m.content });
        } else if (m.role === "assistant" && m.done && !m.error && m.content) {
          out.push({ role: "assistant", content: m.content });
        }
      }
      return out.slice(-8);
    }

    function parseEvent(raw) {
      let event = "message";
      const dataLines = [];
      for (const line of raw.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5));
      }
      if (!dataLines.length) return null;
      try {
        return { event, data: JSON.parse(dataLines.join("\n")) };
      } catch {
        return null;
      }
    }

    async function send() {
      const text = question.value.trim();
      if (!text || sending.value) return;

      const key = `m${++msgSeq}`;
      const userMsg = { key, role: "user", content: text };
      const botMsg = {
        key: `m${++msgSeq}`,
        role: "assistant",
        content: "",
        html: "",
        reasoning: "",
        sources: [],
        status: "loading",
        error: "",
        done: false,
      };
      messages.value.push(userMsg, botMsg);
      question.value = "";
      sending.value = true;
      scrollToBottom();

      const history = historyForApi();
      abortController = new AbortController();
      let sawDelta = false;

      try {
        const resp = await api("/api/kb/chat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: text, history }),
          signal: abortController.signal,
        });
        if (!resp.ok || !resp.body) {
          let message = `请求失败（${resp.status}）`;
          try {
            const data = await resp.json();
            message = data.error || message;
          } catch {
            /* 保留默认信息 */
          }
          throw new Error(message);
        }

        const reader = resp.body.getReader();
        const decoder = new TextDecoder("utf-8");
        let buf = "";

        const handle = (ev) => {
          if (ev.event === "status") {
            // 可选：展示检索信息
          } else if (ev.event === "sources") {
            botMsg.sources = ev.data.sources || [];
          } else if (ev.event === "reasoning") {
            botMsg.reasoning += ev.data.text || "";
            scrollToBottom();
          } else if (ev.event === "delta") {
            sawDelta = true;
            botMsg.content += ev.data.text || "";
            botMsg.html = sanitizeMarkdown(botMsg.content);
            scrollToBottom();
          } else if (ev.event === "done") {
            botMsg.done = true;
            botMsg.status = "";
          } else if (ev.event === "error") {
            throw new Error(ev.data.message || "生成失败");
          }
        };

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += decoder.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf("\n\n")) !== -1) {
            const raw = buf.slice(0, idx);
            buf = buf.slice(idx + 2);
            const ev = parseEvent(raw);
            if (ev) handle(ev);
          }
        }
        botMsg.done = true;
        botMsg.status = "";
      } catch (err) {
        if (err.name === "AbortError") {
          botMsg.status = "";
          botMsg.error = "已停止生成";
        } else {
          botMsg.error = err.message || "生成失败";
        }
        botMsg.done = true;
      } finally {
        sending.value = false;
        abortController = null;
        scrollToBottom(true);
        if (sawDelta) {
          // 消息定型后渲染富内容（KaTeX / Mermaid）
          requestAnimationFrame(() => {
            const el = document.getElementById("kb-answer-" + botMsg.key);
            if (el) renderRich(el);
          });
        }
        saveSession();
      }
    }

    function ask(sample) {
      question.value = sample;
      send();
    }

    function onKeydown(e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        send();
      }
    }

    function clearChat() {
      messages.value = [];
      sessionStorage.removeItem("kb_chat");
    }

    // ── 会话记忆（刷新后保留对话）──────────────────
    function saveSession() {
      try {
        sessionStorage.setItem("kb_chat", JSON.stringify(messages.value.map((m) => ({
          key: m.key, role: m.role, content: m.content,
          reasoning: m.reasoning, sources: m.sources, error: m.error, done: m.done,
        }))));
      } catch {
        /* 忽略存储失败 */
      }
    }

    function restoreSession() {
      try {
        const raw = sessionStorage.getItem("kb_chat");
        if (!raw) return;
        const saved = JSON.parse(raw);
        if (!Array.isArray(saved)) return;
        messages.value = saved.map((m) => {
          if (m.role === "assistant") {
            m.html = m.content ? sanitizeMarkdown(m.content) : "";
          }
          return m;
        });
      } catch {
        /* 忽略 */
      }
    }

    onMounted(() => {
      loadStatus();
      restoreSession();
      const el = chatBox.value;
      if (el) el.scrollTop = el.scrollHeight;
    });

    onUnmounted(() => {
      if (abortController) abortController.abort();
    });

    return {
      question, sending, rebuilding, messages, chatBox, kbStatus, samples,
      statusText, loadStatus, rebuild, send, ask, onKeydown, clearChat,
    };
  },
};

createApp(App).mount("#app");
