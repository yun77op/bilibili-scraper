const form = document.querySelector("#jobForm");
const urlInputs = document.querySelector("#urlInputs");
const pdfDirInput = document.querySelector("#pdfDirInput");
const youtubeCookieInput = document.querySelector("#youtubeCookieInput");
const youtubeCookieStatus = document.querySelector("#youtubeCookieStatus");
const youtubeLoginBtn = document.querySelector("#youtubeLoginBtn");
const youtubeLoginStatus = document.querySelector("#youtubeLoginStatus");
const submitBtn = document.querySelector("#submitBtn");
const statusPill = document.querySelector("#statusPill");
const stageText = document.querySelector("#stageText");
const elapsedText = document.querySelector("#elapsedText");
const progressBar = document.querySelector("#progressBar");
const autoSaveCheck = document.querySelector("#autoSaveCheck");
const saveFormatSelect = document.querySelector("#saveFormatSelect");
const dateSubdirCheck = document.querySelector("#dateSubdirCheck");
const saveConfigBtn = document.querySelector("#saveConfigBtn");
const gdriveCheck = document.querySelector("#gdriveCheck");
const gdriveFolderInput = document.querySelector("#gdriveFolderInput");
const gdriveFormatSelect = document.querySelector("#gdriveFormatSelect");
const gdriveStatus = document.querySelector("#gdriveStatus");
const gdriveAuthBtn = document.querySelector("#gdriveAuthBtn");
const gdriveDetail = document.querySelector("#gdriveDetail");

// ── 多行 URL 输入管理 ────────────────────────────────────
function createUrlRow() {
  const row = document.createElement("div");
  row.className = "url-row";
  row.innerHTML = `
    <input class="url-input" type="text" placeholder="粘贴 B站 / YouTube 链接，按回车开始" autocomplete="off" />
    <button class="url-row-btn add-row" type="button" title="添加一行">+</button>
    <button class="url-row-btn remove-row" type="button" title="移除此行">−</button>
  `;
  return row;
}

function allRows() {
  return [...urlInputs.querySelectorAll(".url-row")];
}

function syncRemoveButtons() {
  const rows = allRows();
  rows.forEach((row) => {
    const btn = row.querySelector(".remove-row");
    btn.style.display = rows.length <= 1 ? "none" : "";
  });
}

function addUrlRow() {
  const row = createUrlRow();
  urlInputs.appendChild(row);
  syncRemoveButtons();
  row.querySelector(".url-input").focus();
}

function removeUrlRow(row) {
  const rows = allRows();
  if (rows.length <= 1) return;
  row.remove();
  syncRemoveButtons();
}

function resetUrlInputs() {
  urlInputs.innerHTML = "";
  urlInputs.appendChild(createUrlRow());
  syncRemoveButtons();
}

// 事件委托：+ / - 按钮
urlInputs.addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const row = btn.closest(".url-row");
  if (btn.classList.contains("add-row")) {
    addUrlRow();
  } else if (btn.classList.contains("remove-row")) {
    removeUrlRow(row);
  }
});

function getUrls() {
  const inputs = urlInputs.querySelectorAll(".url-input");
  return [...inputs].map((inp) => inp.value.trim()).filter(Boolean);
}

// 初始状态：隐藏唯一那行的移除按钮
syncRemoveButtons();

let expandedJobId = null;

fetch("/api/config")
  .then((r) => r.json())
  .then((cfg) => {
    if (cfg.pdf_dir) {
      pdfDirInput.value = cfg.pdf_dir;
    }
    autoSaveCheck.checked = Boolean(cfg.auto_save);
    dateSubdirCheck.checked = Boolean(cfg.date_subdir);
    saveFormatSelect.value = cfg.save_format || "pdf";
    if (cfg.youtube_cookie_configured) {
      youtubeCookieStatus.textContent = "已配置（重新保存时需重新粘贴）";
    } else {
      youtubeCookieStatus.textContent = "未配置";
    }
    gdriveCheck.checked = Boolean(cfg.gdrive_enabled);
    gdriveFolderInput.value = cfg.gdrive_folder_id || "";
    gdriveFormatSelect.value = cfg.gdrive_format || "html";
    gdriveDetail.classList.toggle("visible", gdriveCheck.checked);
    updateGdriveStatus(cfg.gdrive_authenticated);
    updateWorkerStatus(cfg.worker_alive);
  })
  .catch(() => {});

saveConfigBtn.addEventListener("click", async () => {
  saveConfigBtn.disabled = true;
  try {
    const youtubeCookie = youtubeCookieInput.value.trim();
    const resp = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pdf_dir: pdfDirInput.value.trim(),
        auto_save: autoSaveCheck.checked,
        date_subdir: dateSubdirCheck.checked,
        save_format: saveFormatSelect.value,
        youtube_cookie: youtubeCookie,
        gdrive_enabled: gdriveCheck.checked,
        gdrive_folder_id: gdriveFolderInput.value.trim(),
        gdrive_format: gdriveFormatSelect.value,
      }),
    });
    const data = await resp.json();
    saveConfigBtn.textContent = data.ok ? "已保存" : "失败";
    if (data.ok && youtubeCookie) {
      youtubeCookieStatus.textContent = "已配置";
    } else if (data.ok && !youtubeCookie) {
      youtubeCookieStatus.textContent = "未配置";
    }
  } catch {
    saveConfigBtn.textContent = "失败";
  }
  saveConfigBtn.disabled = false;
  setTimeout(() => {
    saveConfigBtn.textContent = "保存配置";
  }, 1500);
});

// ── 浏览器登录 YouTube ──────────────────────────────────
youtubeLoginBtn.addEventListener("click", async () => {
  youtubeLoginBtn.disabled = true;
  youtubeLoginStatus.textContent = "正在启动浏览器，请在弹出的窗口中登录 YouTube…";
  try {
    const resp = await fetch("/api/youtube-login", { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      youtubeLoginStatus.textContent = "登录成功，Cookie 已保存 ✓";
      youtubeCookieStatus.textContent = "已配置";
      youtubeCookieInput.value = "";
    } else {
      youtubeLoginStatus.textContent = "失败: " + (data.error || "未知错误");
    }
  } catch {
    youtubeLoginStatus.textContent = "请求失败，请检查服务是否运行";
  }
  youtubeLoginBtn.disabled = false;
});

// ── Google Drive checkbox toggle ──────────────────────────
gdriveCheck.addEventListener("change", () => {
  gdriveDetail.classList.toggle("visible", gdriveCheck.checked);
});

// ── Google Drive 授权 ─────────────────────────────────────
function updateGdriveStatus(authed) {
  if (authed) {
    gdriveStatus.textContent = "已授权 ✓";
    gdriveAuthBtn.textContent = "重新授权";
  } else {
    gdriveStatus.textContent = "未授权";
    gdriveAuthBtn.textContent = "授权 Google Drive";
  }
}

async function checkGdriveStatus() {
  try {
    const resp = await fetch("/api/gdrive/status");
    const data = await resp.json();
    updateGdriveStatus(data.authenticated);
    return data.authenticated;
  } catch {
    return false;
  }
}

gdriveAuthBtn.addEventListener("click", async () => {
  gdriveAuthBtn.disabled = true;
  gdriveStatus.textContent = "正在生成授权链接…";
  try {
    const resp = await fetch("/api/gdrive/auth-url", { method: "POST" });
    const data = await resp.json();
    if (data.error) {
      gdriveStatus.textContent = "失败: " + data.error;
      gdriveAuthBtn.disabled = false;
      return;
    }
    gdriveStatus.textContent = "请在弹出窗口中完成授权…";
    const popup = window.open(data.url, "gdrive-auth", "width=600,height=700");
    if (!popup) {
      gdriveStatus.textContent = "请允许弹窗，或手动打开授权链接";
      gdriveAuthBtn.disabled = false;
      return;
    }
    // Poll for auth completion (popup closes itself on success)
    const checkInterval = setInterval(async () => {
      if (popup.closed) {
        clearInterval(checkInterval);
        await checkGdriveStatus();
        gdriveAuthBtn.disabled = false;
      }
    }, 1000);
    // Timeout after 5 minutes
    setTimeout(() => {
      clearInterval(checkInterval);
      if (!popup.closed) {
        popup.close();
      }
      checkGdriveStatus();
      gdriveAuthBtn.disabled = false;
    }, 300000);
  } catch (err) {
    gdriveStatus.textContent = "请求失败: " + err.message;
    gdriveAuthBtn.disabled = false;
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const urls = getUrls();
  if (urls.length === 0) {
    return;
  }

  submitBtn.disabled = true;
  const originalText = submitBtn.textContent;
  submitBtn.textContent = "提交中…";
  statusPill.textContent = "提交中";
  stageText.textContent = `正在提交 ${urls.length} 个任务…`;
  progressBar.style.width = "3%";
  elapsedText.textContent = "";

  let success = 0;
  let fail = 0;
  let lastError = "";

  for (const url of urls) {
    try {
      const resp = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });

      if (!resp.ok) {
        let message = await resp.text();
        try {
          const json = JSON.parse(message);
          message = json.error || json.message || message;
        } catch {
          // 保留原始响应文本
        }
        fail += 1;
        lastError = message;
        continue;
      }

      success += 1;
    } catch (err) {
      fail += 1;
      lastError = err.message;
    }
  }

  // 清空输入框，恢复到单行空输入，等待下一次提交
  resetUrlInputs();
  urlInputs.querySelector(".url-input")?.focus();

  // 顶部状态区给出提交反馈；后续任务进度在下方任务列表查看
  if (fail === 0) {
    stageText.textContent = `已提交 ${success} 个任务，进度请在下方任务列表查看`;
    statusPill.textContent = "已提交";
  } else if (success === 0) {
    stageText.textContent = `提交失败${lastError ? "：" + lastError : ""}`;
    statusPill.textContent = "失败";
  } else {
    stageText.textContent = `已提交 ${success} 任务，${fail} 失败${lastError ? "：" + lastError : ""}`;
    statusPill.textContent = "部分失败";
  }

  submitBtn.disabled = false;
  submitBtn.textContent = originalText;
  loadJobHistory();
});


// ── Worker 状态轮询 ──────────────────────────────────

function updateWorkerStatus(alive) {
  const dot = document.querySelector(".worker-dot");
  const text = document.querySelector(".worker-text");
  if (dot && text) {
    if (alive) {
      dot.className = "worker-dot online";
      text.textContent = "Worker 在线";
    } else {
      dot.className = "worker-dot offline";
      text.textContent = "Worker 离线";
    }
  }
}

// 每 15 秒同步一次 Worker 状态
setInterval(async () => {
  try {
    const resp = await fetch("/api/config");
    const cfg = await resp.json();
    updateWorkerStatus(cfg.worker_alive);
  } catch {
    // 忽略轮询错误
  }
}, 15000);

// ── 任务历史 ──────────────────────────────────

const jobList = document.querySelector("#jobList");
const jobCount = document.querySelector("#jobCount");

function statusBadge(status) {
  const map = {
    queued: ["排队中", "badge-queued"],
    running: ["处理中", "badge-running"],
    done: ["完成", "badge-done"],
    error: ["失败", "badge-error"],
    cancelled: ["已取消", "badge-cancelled"],
  };
  const [label, cls] = map[status] || ["未知", ""];
  return `<span class="job-badge ${cls}">${label}</span>`;
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

async function loadJobHistory() {
  try {
    const resp = await fetch("/api/jobs");
    const jobs = await resp.json();
    renderJobList(jobs);
  } catch {
    // 忽略
  }
}

function renderJobList(jobs) {
  if (!jobs || jobs.length === 0) {
    jobList.innerHTML = '<p class="job-empty">暂无历史任务</p>';
    jobCount.textContent = "";
    return;
  }

  const active = jobs.filter((j) => j.status === "running" || j.status === "queued").length;
  jobCount.textContent = `共 ${jobs.length} 条，${active} 个进行中`;

  jobList.innerHTML = jobs
    .map((job) => {
      const isExpanded = job.id === expandedJobId;
      const isError = job.status === "error";
      const stageCls = isError ? "job-item-stage error-text" : "job-item-stage";

      let detailHtml = "";
      if (isExpanded && job.status === "done" && job.article) {
        const hasTranscript = Boolean(job.transcript);
        const hasLogs = job.logs && job.logs.length > 0;
        detailHtml = `
        <div class="job-detail" onclick="event.stopPropagation()">
          <div class="job-detail-tabs">
            <button class="job-tab active" data-tab="article" data-id="${job.id}">📄 文章</button>
            ${hasTranscript ? `<button class="job-tab" data-tab="transcript" data-id="${job.id}">📝 转写稿</button>` : ""}
            ${hasLogs ? `<button class="job-tab" data-tab="logs" data-id="${job.id}">📋 日志</button>` : ""}
          </div>
          <div class="job-tab-content active" data-tab="article" data-id="${job.id}">
            <div class="job-tab-toolbar">
              <button class="ghost job-copy-article" data-id="${job.id}">复制文章</button>
            </div>
            <div class="job-detail-article">${escapeHtml(job.article)}</div>
          </div>
          ${hasTranscript ? `
          <div class="job-tab-content" data-tab="transcript" data-id="${job.id}">
            <div class="job-tab-toolbar">
              <button class="ghost job-copy-transcript" data-id="${job.id}">复制转写稿</button>
            </div>
            <div class="job-detail-transcript">${escapeHtml(job.transcript)}</div>
          </div>` : ""}
          ${hasLogs ? `
          <div class="job-tab-content" data-tab="logs" data-id="${job.id}">
            <div class="job-detail-logs">${escapeHtml(job.logs.join("\n"))}</div>
          </div>` : ""}
        </div>`;
      } else if (isExpanded && isError) {
        const hasLogs = job.logs && job.logs.length > 0;
        detailHtml = `
        <div class="job-detail" onclick="event.stopPropagation()">
          ${hasLogs ? `
          <div class="job-detail-tabs">
            <button class="job-tab active" data-tab="error" data-id="${job.id}">❌ 错误</button>
            <button class="job-tab" data-tab="logs" data-id="${job.id}">📋 日志</button>
          </div>
          <div class="job-tab-content active" data-tab="error" data-id="${job.id}">
            <div class="job-detail-article" style="color:var(--danger);background:#fff5f5;">${escapeHtml(job.error || "未知错误")}</div>
          </div>
          <div class="job-tab-content" data-tab="logs" data-id="${job.id}">
            <div class="job-detail-logs">${escapeHtml(job.logs.join("\n"))}</div>
          </div>` : `
          <div class="job-detail-article" style="color:var(--danger);background:#fff5f5;">${escapeHtml(job.error || "未知错误")}</div>`}
        </div>`;
      }

      return `
    <div class="job-item${isExpanded ? " expanded" : ""}" data-id="${job.id}">
      <div class="job-item-main">
        <div class="job-item-top">
          ${statusBadge(job.status)}
          <span class="job-item-url" title="${escapeHtml(job.url || "")}">${escapeHtml(job.title || truncateUrl(job.url))}</span>
        </div>
        <div class="job-item-meta">
          ${job.title ? `<span class="job-item-url-sub" title="${escapeHtml(job.url || "")}">${truncateUrl(job.url)}</span>` : ""}
          <span class="${stageCls}">${escapeHtml(job.stage || (isError ? job.error || "" : ""))}</span>
          <span class="job-item-time">${elapsedStr(job.created_at)}</span>
        </div>
      </div>
      <div class="job-item-actions">
        ${
          job.status === "error" || job.status === "cancelled"
            ? `<button class="job-retry-btn" data-id="${job.id}" title="重试任务">🔄</button>`
            : ""
        }
        ${
          job.status === "running" || job.status === "queued"
            ? `<button class="job-cancel-btn" data-id="${job.id}" title="取消任务">✕</button>`
            : ""
        }
        ${
          job.status === "done"
            ? `<button class="job-drive-btn" data-id="${job.id}" title="保存到 Google Drive">☁️</button>
               <button class="job-local-btn" data-id="${job.id}" title="保存到本地 (MD+PDF)">💾</button>`
            : ""
        }
        <button class="job-delete-btn" data-id="${job.id}" title="删除任务">🗑</button>
      </div>
      ${detailHtml}
    </div>`;
    })
    .join("");

  // 绑定任务条目点击（展开/折叠）
  jobList.querySelectorAll(".job-item").forEach((item) => {
    item.addEventListener("click", (e) => {
      // 不拦截按钮点击
      if (e.target.closest("button")) return;
      const id = item.dataset.id;
      if (expandedJobId === id) {
        expandedJobId = null;
      } else {
        expandedJobId = id;
      }
      renderJobList(jobs);
    });
  });

  // 绑定重试按钮事件
  jobList.querySelectorAll(".job-retry-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const resp = await fetch(`/api/jobs/${id}/retry`, { method: "POST" });
        const data = await resp.json();
        if (data.ok) {
          if (expandedJobId === id) expandedJobId = null;
          loadJobHistory();
        }
      } catch {
        btn.disabled = false;
        btn.textContent = "🔄";
      }
    });
  });

  // 绑定取消按钮事件
  jobList.querySelectorAll(".job-cancel-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      btn.disabled = true;
      btn.textContent = "…";
      try {
        const resp = await fetch(`/api/jobs/${id}/cancel`, { method: "POST" });
        const data = await resp.json();
        if (data.ok) {
          loadJobHistory();
        }
      } catch {
        btn.disabled = false;
        btn.textContent = "✕";
      }
    });
  });

  // 绑定删除按钮事件
  jobList.querySelectorAll(".job-delete-btn").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.id;
      if (!confirm("确定删除这个任务？")) return;
      if (expandedJobId === id) expandedJobId = null;
      btn.disabled = true;
      try {
        const resp = await fetch(`/api/jobs/${id}/delete`, { method: "POST" });
        const data = await resp.json();
        if (data.ok) {
          loadJobHistory();
        }
      } catch {
        btn.disabled = false;
      }
    });
  });

  // 绑定保存到 Drive / 保存到本地按钮
  function bindSaveBtn(selector, endpoint, icon) {
    jobList.querySelectorAll(selector).forEach((btn) => {
      btn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const id = btn.dataset.id;
        btn.disabled = true;
        btn.textContent = "…";
        try {
          const resp = await fetch(`/api/jobs/${id}/${endpoint}`, { method: "POST" });
          const data = await resp.json();
          if (resp.ok && data.ok) {
            btn.textContent = "✓";
            loadJobHistory();
          } else {
            alert(data.error || "保存失败");
            btn.disabled = false;
            btn.textContent = icon;
          }
        } catch {
          alert("请求失败，请检查服务是否运行");
          btn.disabled = false;
          btn.textContent = icon;
        }
      });
    });
  }
  bindSaveBtn(".job-drive-btn", "save-drive", "☁️");
  bindSaveBtn(".job-local-btn", "save-local", "💾");

  // 绑定复制文章按钮
  jobList.querySelectorAll(".job-copy-article").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const job = jobs.find((j) => j.id === btn.dataset.id);
      if (!job || !job.article) return;
      await navigator.clipboard.writeText(job.article);
      const original = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => { btn.textContent = original; }, 1200);
    });
  });

  // 绑定复制转写稿按钮
  jobList.querySelectorAll(".job-copy-transcript").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const job = jobs.find((j) => j.id === btn.dataset.id);
      if (!job || !job.transcript) return;
      await navigator.clipboard.writeText(job.transcript);
      const original = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => { btn.textContent = original; }, 1200);
    });
  });

  // 绑定 Tab 切换
  jobList.querySelectorAll(".job-tab").forEach((tab) => {
    tab.addEventListener("click", (e) => {
      e.stopPropagation();
      const id = tab.dataset.id;
      const tabName = tab.dataset.tab;

      // 切换 tab 激活状态
      tab.parentElement.querySelectorAll(".job-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");

      // 切换内容区
      const detail = tab.closest(".job-detail");
      detail.querySelectorAll(".job-tab-content").forEach((c) => c.classList.remove("active"));
      const content = detail.querySelector(`.job-tab-content[data-tab="${tabName}"]`);
      if (content) content.classList.add("active");
    });
  });
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// 页面加载时拉取，之后每 5 秒刷新
loadJobHistory();
setInterval(loadJobHistory, 5000);
