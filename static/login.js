// 登录页入口（ESM）
import { createApp, ref } from "/static/vendor/vue/vue.esm-browser.prod.js";

const App = {
  setup() {
    const username = ref("");
    const password = ref("");
    const error = ref("");
    const busy = ref(false);

    async function submit() {
      error.value = "";
      busy.value = true;
      try {
        const resp = await fetch("/api/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: username.value.trim(),
            password: password.value,
          }),
        });
        const data = await resp.json();
        if (resp.ok && data.ok) {
          location.href = "/app";
        } else {
          error.value = data.error || "登录失败";
        }
      } catch {
        error.value = "请求失败，请检查服务是否运行";
      }
      busy.value = false;
    }

    return { username, password, error, busy, submit };
  },
};

createApp(App).mount("#app");
