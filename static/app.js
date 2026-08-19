// 首页入口（ESM）：表单提交、状态区、Worker 轮询、任务列表与分页
import { createApp, reactive, ref, computed, onMounted, onUnmounted } from "/static/vendor/vue/vue.esm-browser.prod.js";
import { api, Navbar } from "/static/common.js?v=20260819t2";
import JobItem from "/static/components/job-item.js?v=20260819t2";

const App = {
  components: { Navbar, JobItem },
  setup() {
    // ── 多行 URL 输入 ──
    const rows = reactive([{ value: "" }]);
    const addRow = () => rows.push({ value: "" });
    const removeRow = (i) => {
      if (rows.length > 1) rows.splice(i, 1);
    };

    // ── 提交状态区 ──
    const submitting = ref(false);
    const statusPill = ref("就绪");
    const stageText = ref("");

    // ── 任务列表 + 分页 ──
    const PER_PAGE = 20;
    const jobs = ref([]);
    const total = ref(0);
    const active = ref(0);
    const page = ref(1);
    const pages = ref(1);

    const jobCountText = computed(() =>
      total.value ? `共 ${total.value} 条，${active.value} 个进行中` : ""
    );

    async function loadJobs() {
      try {
        const resp = await api(`/api/jobs?page=${page.value}&per_page=${PER_PAGE}`);
        const data = await resp.json();
        jobs.value = data.jobs || [];
        total.value = data.total || 0;
        active.value = data.active || 0;
        pages.value = data.pages || 1;
      } catch {
        // 忽略
      }
    }

    // 页码序列：最多 7 个数字页码，当前页居中，两端用省略号折叠
    const pageItems = computed(() => {
      const p = pages.value;
      if (p <= 7) return Array.from({ length: p }, (_, i) => i + 1);
      const cur = page.value;
      const start = Math.min(Math.max(cur - 3, 1), p - 6);
      const end = start + 6;
      const items = [];
      if (start > 1) items.push(1);
      if (start > 2) items.push("…");
      for (let i = start; i <= end; i++) items.push(i);
      if (end < p - 1) items.push("…");
      if (end < p) items.push(p);
      return items;
    });

    function goPage(p) {
      if (p === page.value || p < 1 || p > pages.value) return;
      page.value = p;
      loadJobs();
    }

    // ── Worker 状态 ──
    const workerAlive = ref(null);
    const workerText = computed(() =>
      workerAlive.value === null
        ? "检测中…"
        : workerAlive.value
          ? "Worker 在线"
          : "Worker 离线"
    );

    async function checkWorker() {
      try {
        const resp = await api("/api/config");
        const cfg = await resp.json();
        workerAlive.value = !!cfg.worker_alive;
      } catch {
        // 忽略轮询错误
      }
    }

    // ── 提交 ──
    async function submit() {
      const urls = rows.map((r) => r.value.trim()).filter(Boolean);
      if (urls.length === 0) return;

      submitting.value = true;
      statusPill.value = "提交中";
      stageText.value = `正在提交 ${urls.length} 个任务…`;

      let success = 0;
      let fail = 0;
      let lastError = "";
      for (const url of urls) {
        try {
          const resp = await api("/api/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
          });
          if (!resp.ok) {
            let message = await resp.text();
            try {
              message = JSON.parse(message).error || message;
            } catch {
              // 保留原始响应文本
            }
            fail += 1;
            lastError = message;
          } else {
            success += 1;
          }
        } catch (err) {
          fail += 1;
          lastError = err.message;
        }
      }

      // 清空输入框，恢复到单行空输入
      rows.splice(0, rows.length, { value: "" });

      if (fail === 0) {
        stageText.value = `已提交 ${success} 个任务，进度请在下方任务列表查看`;
        statusPill.value = "已提交";
      } else if (success === 0) {
        stageText.value = `提交失败${lastError ? "：" + lastError : ""}`;
        statusPill.value = "失败";
      } else {
        stageText.value = `已提交 ${success} 任务，${fail} 失败${lastError ? "：" + lastError : ""}`;
        statusPill.value = "部分失败";
      }

      submitting.value = false;
      page.value = 1; // 新任务按最新排序在第 1 页，提交后跳回首页
      loadJobs();
    }

    let jobTimer = null;
    let workerTimer = null;
    onMounted(() => {
      loadJobs();
      checkWorker();
      jobTimer = setInterval(loadJobs, 5000);
      workerTimer = setInterval(checkWorker, 15000);
    });
    onUnmounted(() => {
      clearInterval(jobTimer);
      clearInterval(workerTimer);
    });

    return {
      rows, addRow, removeRow,
      submitting, statusPill, stageText,
      jobs, jobCountText, page, pages, pageItems, goPage,
      workerAlive, workerText,
      submit, loadJobs,
    };
  },
};

createApp(App).mount("#app");
