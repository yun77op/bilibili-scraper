// 设置页入口（ESM）：配置加载/保存、Notion OAuth、YouTube 登录、管理员区
import { createApp, ref, onMounted } from "/static/vendor/vue/vue.esm-browser.prod.js";
import { api, Navbar } from "/static/common.js";

const App = {
  components: { Navbar },
  setup() {
    // ── 表单状态（GET /api/config 回填，POST /api/config 保存）──
    const saveStatus = ref("就绪");
    const saving = ref(false);

    const notionEnabled = ref(false);
    const notionParent = ref("");
    const dateSubdir = ref(false);
    const youtubeCookie = ref("");

    const notionConfigured = ref(false);
    const notionOAuthReady = ref(false);
    const notionStatusText = ref("检测中…");
    const notionAuthBtnText = ref("授权 Notion");
    const notionBusy = ref(false);

    const youtubeCookieStatusText = ref("未配置");
    const youtubeLoginStatusText = ref("");
    const youtubeBusy = ref(false);

    // ── 管理员区块 ──
    const isAdmin = ref(false);
    const serverInfo = ref([]);
    const users = ref([]);

    function fmtDate(ts) {
      if (!ts) return "";
      return new Date(ts * 1000).toLocaleDateString();
    }

    function updateNotionStatus(authed, workspace, oauthReady) {
      notionConfigured.value = Boolean(authed);
      notionOAuthReady.value = oauthReady !== false;
      if (!notionOAuthReady.value) {
        notionStatusText.value = "服务端未配置 Notion OAuth";
        notionAuthBtnText.value = "授权 Notion";
        return;
      }
      if (authed) {
        notionStatusText.value = workspace ? `已授权 ✓（${workspace}）` : "已授权 ✓";
        notionAuthBtnText.value = "重新授权";
      } else {
        notionStatusText.value = "未授权";
        notionAuthBtnText.value = "授权 Notion";
      }
    }

    async function loadConfig() {
      const resp = await api("/api/config");
      const cfg = await resp.json();
      const s = cfg.settings || {};

      notionEnabled.value = Boolean(s.notion_enabled);
      notionParent.value = s.notion_parent_page_id || "";
      dateSubdir.value = Boolean(s.date_subdir);
      updateNotionStatus(cfg.notion_configured, cfg.notion_workspace, cfg.notion_oauth_ready);
      youtubeCookieStatusText.value = s.youtube_cookie_configured ? "已配置" : "未配置";

      // 管理员区块
      if (cfg.user && cfg.user.is_admin) {
        isAdmin.value = true;
        serverInfo.value = [
          `DeepSeek：${cfg.deepseek_configured ? "已配置 ✓" : "未配置（需在服务器 .env.local 设置 DEEPSEEK_API_KEY）"}`,
          `模型：${cfg.deepseek_model || ""} ｜ 转写：${cfg.transcribe_provider === "groq" ? `Groq ${cfg.groq_model || ""}${cfg.groq_configured ? "" : "（未配置 GROQ_API_KEY）"}` : `本地 ${cfg.whisper_device || ""}`}`,
          `Worker：${cfg.worker_alive ? "在线" : "离线"}`,
        ];
        loadUsers();
      }
    }

    async function save() {
      if (saving.value) return;
      saving.value = true;
      try {
        const resp = await api("/api/config", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            notion_enabled: notionEnabled.value,
            notion_parent_page_id: notionParent.value.trim(),
            date_subdir: dateSubdir.value,
            youtube_cookie: youtubeCookie.value.trim(),
          }),
        });
        const data = await resp.json();
        if (data.ok) {
          saveStatus.value = "已保存 ✓";
          if (youtubeCookie.value.trim()) {
            youtubeCookieStatusText.value = "已配置";
            youtubeCookie.value = "";
          }
        } else {
          saveStatus.value = "保存失败：" + (data.error || "");
        }
      } catch (err) {
        saveStatus.value = "保存失败：" + err.message;
      }
      saving.value = false;
      setTimeout(() => { saveStatus.value = "就绪"; }, 2000);
    }

    // ── Notion OAuth（弹窗 + 轮询关闭 + 5 分钟超时）──
    async function notionAuth() {
      if (notionBusy.value) return;
      notionBusy.value = true;
      notionStatusText.value = "正在生成授权链接…";
      try {
        const resp = await api("/api/notion/auth-url", { method: "POST" });
        const data = await resp.json();
        if (data.error) {
          notionStatusText.value = "失败: " + data.error;
          notionBusy.value = false;
          return;
        }
        notionStatusText.value = "请在弹出窗口中完成授权…";
        const popup = window.open(data.url, "notion-auth", "width=600,height=700");
        if (!popup) {
          notionStatusText.value = "请允许弹窗，或手动打开授权链接";
          notionBusy.value = false;
          return;
        }
        const checkInterval = setInterval(async () => {
          if (popup.closed) {
            clearInterval(checkInterval);
            await checkNotionStatus();
            notionBusy.value = false;
          }
        }, 1000);
        setTimeout(() => {
          clearInterval(checkInterval);
          if (!popup.closed) popup.close();
          checkNotionStatus();
          notionBusy.value = false;
        }, 300000);
      } catch (err) {
        notionStatusText.value = "请求失败: " + err.message;
        notionBusy.value = false;
      }
    }

    async function checkNotionStatus() {
      try {
        const resp = await api("/api/notion/status");
        const data = await resp.json();
        updateNotionStatus(data.authenticated, data.workspace, data.oauth_ready);
        return data.authenticated;
      } catch {
        return false;
      }
    }

    async function disconnect() {
      if (!confirm("确定取消 Notion 授权？")) return;
      try {
        await api("/api/notion/disconnect", { method: "POST" });
        await checkNotionStatus();
      } catch {
        // 保持当前状态
      }
    }

    // ── 浏览器登录 YouTube ──
    async function youtubeLogin() {
      if (youtubeBusy.value) return;
      youtubeBusy.value = true;
      youtubeLoginStatusText.value = "正在启动浏览器，请在弹出的窗口中登录 YouTube…";
      try {
        const resp = await api("/api/youtube-login", { method: "POST" });
        const data = await resp.json();
        if (data.ok) {
          youtubeLoginStatusText.value = "登录成功，Cookie 已保存 ✓";
          youtubeCookieStatusText.value = "已配置";
          youtubeCookie.value = "";
        } else {
          youtubeLoginStatusText.value = "失败: " + (data.error || "未知错误");
        }
      } catch (err) {
        youtubeLoginStatusText.value = "请求失败: " + err.message;
      }
      youtubeBusy.value = false;
    }

    // ── 管理员：用户管理 ──
    async function loadUsers() {
      try {
        const resp = await api("/api/admin/users");
        const data = await resp.json();
        users.value = data.users || [];
      } catch {
        // 权限不足时忽略
      }
    }

    async function toggleUser(u) {
      try {
        const resp = await api(`/api/admin/users/${u.id}/toggle`, { method: "POST" });
        const data = await resp.json();
        if (data.ok) {
          loadUsers();
        } else {
          alert(data.error || "操作失败");
        }
      } catch {
        // 权限不足时忽略
      }
    }

    onMounted(loadConfig);

    return {
      saveStatus, saving,
      notionEnabled, notionParent, dateSubdir, youtubeCookie,
      notionConfigured, notionStatusText, notionAuthBtnText, notionBusy,
      notionAuth, disconnect,
      youtubeCookieStatusText, youtubeLoginStatusText, youtubeBusy, youtubeLogin,
      isAdmin, serverInfo, users, fmtDate, toggleUser,
      save,
    };
  },
};

createApp(App).mount("#app");
