// 公共模块（ESM）：API 封装、HTML 转义、导航栏组件
// 由各页面入口 <script type="module"> 引入

export async function api(path, options = {}) {
  const resp = await fetch(path, {
    credentials: "same-origin",
    ...options,
  });
  if (resp.status === 401) {
    location.href = "/login";
    throw new Error("未登录或会话已过期");
  }
  return resp;
}

export function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  const div = document.createElement("div");
  div.textContent = String(str);
  return div.innerHTML;
}

// 顶部导航栏组件（登录后页面共用）
export const Navbar = {
  template: `
    <nav class="main-nav">
      <div class="nav-links">
        <a class="nav-link" :class="{ active: current === 'index' }" href="/">转换</a>
        <a class="nav-link" :class="{ active: current === 'settings' }" href="/settings">设置</a>
      </div>
      <div class="nav-user">
        <span class="nav-username" v-if="username">{{ username }}</span>
        <button class="ghost" @click="logout">退出登录</button>
      </div>
    </nav>`,
  props: {
    current: { type: String, default: "" },
  },
  data() {
    return { username: "" };
  },
  async mounted() {
    try {
      const resp = await api("/api/config");
      const cfg = await resp.json();
      const user = cfg.user || {};
      this.username = user.username
        ? `${user.username}${user.is_admin ? "（管理员）" : ""}`
        : "";
    } catch {
      // 忽略
    }
  },
  methods: {
    async logout() {
      try {
        await fetch("/api/logout", { method: "POST", credentials: "same-origin" });
      } catch {
        // 忽略
      }
      location.href = "/login";
    },
  },
};
