const form = document.querySelector("#jobForm");
const urlInput = document.querySelector("#urlInput");
const pdfDirInput = document.querySelector("#pdfDirInput");
const submitBtn = document.querySelector("#submitBtn");
const statusPill = document.querySelector("#statusPill");
const stageText = document.querySelector("#stageText");
const elapsedText = document.querySelector("#elapsedText");
const progressBar = document.querySelector("#progressBar");
const logBox = document.querySelector("#logBox");
const transcriptBox = document.querySelector("#transcriptBox");
const articleBox = document.querySelector("#articleBox");
const saveDocBtn = document.querySelector("#saveDocBtn");
const saveStatus = document.querySelector("#saveStatus");
const autoSaveCheck = document.querySelector("#autoSaveCheck");
const dateSubdirCheck = document.querySelector("#dateSubdirCheck");
const saveConfigBtn = document.querySelector("#saveConfigBtn");

let pollTimer = null;
let currentJobId = null;
let abortBatch = false;
let batchLog = "";

fetch("/api/config")
  .then((r) => r.json())
  .then((cfg) => {
    if (cfg.pdf_dir) pdfDirInput.value = cfg.pdf_dir;
    autoSaveCheck.checked = cfg.auto_save;
    dateSubdirCheck.checked = cfg.date_subdir;
  });

saveConfigBtn.addEventListener("click", async () => {
  saveConfigBtn.disabled = true;
  try {
    const resp = await fetch("/api/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pdf_dir: pdfDirInput.value.trim(),
        auto_save: autoSaveCheck.checked,
        date_subdir: dateSubdirCheck.checked,
      }),
    });
    const data = await resp.json();
    saveConfigBtn.textContent = data.ok ? "已保存" : "失败";
  } catch {
    saveConfigBtn.textContent = "失败";
  }
  saveConfigBtn.disabled = false;
  setTimeout(() => { saveConfigBtn.textContent = "保存配置"; }, 1500);
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const raw = urlInput.value.trim();
  if (!raw) return;

  const urls = raw
    .split("\n")
    .map((s) => s.trim())
    .filter((s) => s.startsWith("http"));

  if (urls.length === 0) return;

  setBusy(true);
  abortBatch = false;
  submitBtn.textContent = "停止";
  batchLog = "";
  logBox.textContent = "";
  const total = urls.length;

  for (let i = 0; i < urls.length; i++) {
    if (abortBatch) break;

    const url = urls[i];
    const label = total > 1 ? `[${i + 1}/${total}] ` : "";

    resetOutput();
    batchLog += `${label}提交: ${url}\n`;
    logBox.textContent = batchLog;

    try {
      const resp = await fetch("/api/jobs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });
      if (!resp.ok) {
        batchLog += `${label}创建失败: ${await resp.text()}\n`;
        logBox.textContent = batchLog;
        continue;
      }
      const data = await resp.json();
      currentJobId = data.id;

      const result = await pollUntilDone(data.id, label);
      if (result.status === "done" && result.article && autoSaveCheck.checked) {
        await autoSaveDoc();
      }
    } catch (err) {
      batchLog += `${label}异常: ${err.message}\n`;
      logBox.textContent = batchLog;
    }

    batchLog += "\n";
    logBox.textContent = batchLog;

    if (i < urls.length - 1 && !abortBatch) {
      await new Promise((r) => setTimeout(r, 5000));
    }
  }

  setBusy(false);
  submitBtn.textContent = "开始处理";
  stageText.textContent = abortBatch ? "已停止" : "全部完成";
  statusPill.textContent = abortBatch ? "已停止" : "完成";
});

saveDocBtn.addEventListener("click", saveDoc);

document.querySelectorAll("[data-copy-target]").forEach((button) => {
  button.addEventListener("click", async () => {
    const target = document.querySelector(`#${button.dataset.copyTarget}`);
    await navigator.clipboard.writeText(target.value);
    const original = button.textContent;
    button.textContent = "已复制";
    setTimeout(() => {
      button.textContent = original;
    }, 1200);
  });
});

function pollUntilDone(id, label) {
  return new Promise((resolve, reject) => {
    const poll = async () => {
      try {
        const resp = await fetch(`/api/jobs/${id}`);
        const data = await resp.json();
        renderJob(data, label);

        if (data.status === "done" || data.status === "error") {
          resolve(data);
          return;
        }
        pollTimer = setTimeout(poll, 1800);
      } catch (err) {
        reject(err);
      }
    };
    poll();
  });
}

function renderJob(data, label) {
  statusPill.textContent = statusLabel(data.status);
  stageText.textContent = (label || "") + (data.stage || "");
  if (data.elapsed) {
    const m = Math.floor(data.elapsed / 60);
    const s = data.elapsed % 60;
    elapsedText.textContent = `${m}:${String(s).padStart(2, "0")}`;
  } else {
    elapsedText.textContent = "";
  }
  progressBar.style.width = `${data.progress || 0}%`;
  logBox.textContent = batchLog + (data.logs || []).map((l) => (label || "") + l).join("\n");
  logBox.scrollTop = logBox.scrollHeight;
  transcriptBox.value = data.transcript || "";
  articleBox.value = data.article || "";
}

function setBusy(isBusy) {
  submitBtn.disabled = false;
  urlInput.disabled = isBusy;
  pdfDirInput.disabled = isBusy;
  autoSaveCheck.disabled = isBusy;
  dateSubdirCheck.disabled = isBusy;
  if (!isBusy) submitBtn.textContent = "开始处理";
}

function resetOutput() {
  currentJobId = null;
  statusPill.textContent = "处理中";
  stageText.textContent = "";
  progressBar.style.width = "3%";
  transcriptBox.value = "";
  articleBox.value = "";
  saveDocBtn.disabled = true;
  saveStatus.textContent = "";
  elapsedText.textContent = "";
}

function statusLabel(status) {
  return {
    queued: "排队中",
    running: "处理中",
    done: "完成",
    error: "失败",
  }[status] || "未知";
}

async function saveDoc() {
  if (!currentJobId) return;
  saveDocBtn.disabled = true;
  saveStatus.textContent = "正在保存...";

  try {
    const pdfDir = pdfDirInput.value.trim();
    const response = await fetch(`/api/jobs/${currentJobId}/save-doc`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pdf_dir: pdfDir || null, date_subdir: dateSubdirCheck.checked }),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "保存失败");
    }
    saveStatus.textContent = `已保存：${data.path}；PDF：${data.pdf_path}`;
  } catch (error) {
    saveDocBtn.disabled = false;
    saveStatus.textContent = String(error.message || error);
  }
}

async function autoSaveDoc() {
  try {
    const pdfDir = pdfDirInput.value.trim();
    await fetch(`/api/jobs/${currentJobId}/save-doc`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pdf_dir: pdfDir || null, date_subdir: dateSubdirCheck.checked }),
    });
  } catch {}
}

submitBtn.addEventListener("click", (e) => {
  if (submitBtn.textContent === "停止") {
    e.preventDefault();
    abortBatch = true;
    clearTimeout(pollTimer);
    setBusy(false);
    submitBtn.textContent = "开始处理";
    stageText.textContent = "已停止";
    statusPill.textContent = "已停止";
  }
});
