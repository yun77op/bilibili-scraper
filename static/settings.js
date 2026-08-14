// 设置页入口（ESM）：配置加载/保存、Google Drive 授权、YouTube 登录、管理员区
import { createApp, ref, onMounted } from "/static/vendor/vue/vue.esm-browser.prod.js";
import { api, Navbar } from "/static/common.js";

const App = {
  components: { Navbar },
  setup() {
    // ── 表单状态（GET /api/config 回填，POST /api/config 保存）──
    const saveStatus = ref("就绪");
    const saving = ref(false);

    const gdriveEnabled = ref(false);
    const gdriveFolder = ref("");
    const gdriveFormat = ref("html");
    const dateSubdir = ref(false);
    const youtubeCookie = ref("");

    const gdriveStatusText = ref("检测中…");
    const gdriveAuthBtnText = ref("授权 Google Drive");
    const gdriveBusy = ref(false);

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

    function updateGdriveStatus(authed) {
      gdriveStatusText.value = authed ? "已授权 ✓" : "未授权";
      gdriveAuthBtnText.value = authed ? "重新授权" : "授权 Google Drive";
    }

    async function loadConfig() {
      const resp = await api("/api/config");
      const cfg = await resp.json();
      const s = cfg.settings || {};

      gdriveEnabled.value = Boolean(s.gdrive_enabled);
      gdriveFolder.value = s.gdrive_folder_id || "";
      gdriveFormat.value = s.gdrive_format || "html";
      dateSubdir.value = Boolean(s.date_subdir);
      updateGdriveStatus(cfg.gdrive_authenticated);
      youtubeCookieStatusText.value = s.youtube_cookie_configured ? "已配置" : "未配置";

      // 管理员区块
      if (cfg.user && cfg.user.is_admin) {
        isAdmin.value = true;
        serverInfo.value = [
          `DeepSeek：${cfg.deepseek_configured ? "已配置 ✓" : "未配置（需在服务器 .env.local 设置 DEEPSEEK_API_KEY）"}`,
          `模型：${cfg.deepseek_model || ""} ｜ Whisper 设备：${cfg.whisper_device || ""}`,
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
            gdrive_enabled: gdriveEnabled.value,
            gdrive_folder_id: gdriveFolder.value.trim(),
            gdrive_format: gdriveFormat.value,
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

    // ── Google Drive 授权（弹窗 + 轮询关闭 + 5 分钟超时）──
    async function gdriveAuth() {
      if (gdriveBusy.value) return;
      gdriveBusy.value = true;
      gdriveStatusText.value = "正在生成授权链接…";
      try {
        const resp = await api("/api/gdrive/auth-url", { method: "POST" });
        const data = await resp.json();
        if (data.error) {
          gdriveStatusText.value = "失败: " + data.error;
          gdriveBusy.value = false;
          return;
        }
        gdriveStatusText.value = "请在弹出窗口中完成授权…";
        const popup = window.open(data.url, "gdrive-auth", "width=600,height=700");
        if (!popup) {
          gdriveStatusText.value = "请允许弹窗，或手动打开授权链接";
          gdriveBusy.value = false;
          return;
        }
        // 轮询弹窗关闭（成功授权后弹窗自动关闭）
        const checkInterval = setInterval(async () => {
          if (popup.closed) {
            clearInterval(checkInterval);
            await checkGdriveStatus();
            gdriveBusy.value = false;
          }
        }, 1000);
        // 5 分钟超时
        setTimeout(() => {
          clearInterval(checkInterval);
          if (!popup.closed) popup.close();
          checkGdriveStatus();
          gdriveBusy.value = false;
        }, 300000);
      } catch (err) {
        gdriveStatusText.value = "请求失败: " + err.message;
        gdriveBusy.value = false;
      }
    }

    async function checkGdriveStatus() {
      try {
        const resp = await api("/api/gdrive/status");
        const data = await resp.json();
        updateGdriveStatus(data.authenticated);
        return data.authenticated;
      } catch {
        return false;
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
      gdriveEnabled, gdriveFolder, gdriveFormat, dateSubdir, youtubeCookie,
      gdriveStatusText, gdriveAuthBtnText, gdriveBusy, gdriveAuth,
      youtubeCookieStatusText, youtubeLoginStatusText, youtubeBusy, youtubeLogin,
      isAdmin, serverInfo, users, fmtDate, toggleUser,
      save,
    };
  },
};

createApp(App).mount("#app");
