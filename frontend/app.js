"use strict";

const TOKEN_KEY = "token";
const POLL_INTERVAL = 2000;

const qualityFormats = {
  best: "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
  2160: "bv*[height<=2160][ext=mp4]+ba[ext=m4a]/b[height<=2160][ext=mp4]/bv*[height<=2160]+ba/b[height<=2160]",
  1080: "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/bv*[height<=1080]+ba/b[height<=1080]",
  720: "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/bv*[height<=720]+ba/b[height<=720]",
  480: "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/bv*[height<=480]+ba/b[height<=480]",
};

const statusLabels = {
  0: "等待中",
  1: "下载中",
  2: "已完成",
  3: "失败",
  4: "直播",
};

const elements = {
  loginView: document.querySelector("#loginView"),
  appView: document.querySelector("#appView"),
  loginForm: document.querySelector("#loginForm"),
  loginButton: document.querySelector("#loginButton"),
  loginError: document.querySelector("#loginError"),
  username: document.querySelector("#username"),
  password: document.querySelector("#password"),
  logoutButton: document.querySelector("#logoutButton"),
  serviceState: document.querySelector("#serviceState"),
  serviceStateText: document.querySelector("#serviceStateText"),
  versionLabel: document.querySelector("#versionLabel"),
  urlForm: document.querySelector("#urlForm"),
  videoUrl: document.querySelector("#videoUrl"),
  urlMessage: document.querySelector("#urlMessage"),
  analyzeButton: document.querySelector("#analyzeButton"),
  pasteButton: document.querySelector("#pasteButton"),
  preview: document.querySelector("#preview"),
  previewMedia: document.querySelector("#previewMedia"),
  previewImage: document.querySelector("#previewImage"),
  previewSource: document.querySelector("#previewSource"),
  previewTitle: document.querySelector("#previewTitle"),
  previewMeta: document.querySelector("#previewMeta"),
  previewMessage: document.querySelector("#previewMessage"),
  bestQualityNote: document.querySelector("#bestQualityNote"),
  qualityFieldset: document.querySelector("#qualityFieldset"),
  clearPreviewButton: document.querySelector("#clearPreviewButton"),
  downloadButton: document.querySelector("#downloadButton"),
  taskList: document.querySelector("#taskList"),
  taskCount: document.querySelector("#taskCount"),
  fileList: document.querySelector("#fileList"),
  fileCount: document.querySelector("#fileCount"),
  deleteDialog: document.querySelector("#deleteDialog"),
  deleteFileName: document.querySelector("#deleteFileName"),
  deleteCancelButton: document.querySelector("#deleteCancelButton"),
  deleteConfirmButton: document.querySelector("#deleteConfirmButton"),
  refreshButtons: document.querySelectorAll("[data-refresh]"),
  toastRegion: document.querySelector("#toastRegion"),
};

let token = localStorage.getItem(TOKEN_KEY) || "";
let rpcId = 0;
let pollTimer = null;
let refreshing = false;
let selectedVideo = null;
let pendingDeleteFile = null;

function makeIcon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  use.setAttribute("href", `#icon-${name}`);
  svg.append(use);
  return svg;
}

function setButtonBusy(button, busy, label) {
  const labelNode = button.querySelector("span");
  button.disabled = busy;
  button.classList.toggle("is-busy", busy);
  if (labelNode && label) labelNode.textContent = label;
}

function setServiceState(state, label) {
  elements.serviceState.classList.remove("is-online", "is-offline");
  if (state) elements.serviceState.classList.add(`is-${state}`);
  elements.serviceStateText.textContent = label;
}

function errorMessage(error, fallback = "请求失败，请稍后重试") {
  if (!error) return fallback;
  if (typeof error === "string") return error;
  if (typeof error.message === "string" && error.message) return error.message;
  if (typeof error.error === "string" && error.error) return error.error;
  return fallback;
}

async function apiRequest(path, options = {}, authenticated = true) {
  const headers = new Headers(options.headers || {});
  if (authenticated && token) headers.set("X-Authentication", token);
  const response = await fetch(path, { ...options, headers });
  const text = await response.text();

  if (response.status === 401 || response.status === 403) {
    if (authenticated) showLogin(true);
    throw new Error("登录已失效，请重新登录");
  }

  let payload = text;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok) {
    throw new Error(errorMessage(payload, `请求失败 (${response.status})`));
  }
  return payload;
}

async function rpc(method, params = []) {
  const payload = await apiRequest("/rpc/http", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ method, params, id: String(rpcId++) }),
  });
  if (payload && payload.error) throw new Error(errorMessage(payload.error));
  return payload ? payload.result : null;
}

function showLogin(expired = false) {
  token = "";
  localStorage.removeItem(TOKEN_KEY);
  window.clearInterval(pollTimer);
  pollTimer = null;
  elements.appView.hidden = true;
  elements.loginView.hidden = false;
  elements.loginError.textContent = expired ? "登录已失效，请重新登录" : "";
  elements.password.value = "";
  window.setTimeout(() => elements.username.focus(), 50);
}

async function showApp(version) {
  elements.loginView.hidden = true;
  elements.appView.hidden = false;
  const versionText = String(version?.ytdlpVersion || "").trim();
  elements.versionLabel.textContent = versionText ? `yt-dlp ${versionText}` : "";
  setServiceState("online", "服务在线");
  await refreshAll();
  window.clearInterval(pollTimer);
  pollTimer = window.setInterval(refreshAll, POLL_INTERVAL);
}

async function handleLogin(event) {
  event.preventDefault();
  elements.loginError.textContent = "";
  setButtonBusy(elements.loginButton, true, "登录中");

  try {
    const result = await apiRequest(
      "/auth/login",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: elements.username.value.trim(),
          password: elements.password.value,
        }),
      },
      false,
    );
    token = typeof result === "string" ? result : String(result?.token || "");
    if (!token) throw new Error("登录响应无效");
    localStorage.setItem(TOKEN_KEY, token);
    const version = await apiRequest("/api/v1/version");
    await showApp(version);
  } catch (error) {
    token = "";
    localStorage.removeItem(TOKEN_KEY);
    elements.loginError.textContent = errorMessage(error, "用户名或密码错误");
  } finally {
    setButtonBusy(elements.loginButton, false, "登录");
  }
}

function normalizeVideoUrl(value) {
  let parsed;
  try {
    parsed = new URL(value.trim());
  } catch {
    throw new Error("请输入有效的视频链接");
  }
  if (!/^https?:$/.test(parsed.protocol)) throw new Error("仅支持 HTTP 或 HTTPS 链接");

  const host = parsed.hostname.replace(/^www\./, "").toLowerCase();
  if (host === "youtube.com" || host === "m.youtube.com" || host === "music.youtube.com") {
    parsed.searchParams.delete("list");
    parsed.searchParams.delete("index");
    parsed.searchParams.delete("start_radio");
  }
  return parsed.toString();
}

function sourceName(url) {
  try {
    const host = new URL(url).hostname.replace(/^www\./, "");
    const known = {
      "youtube.com": "YouTube",
      "m.youtube.com": "YouTube",
      "youtu.be": "YouTube",
      "bilibili.com": "Bilibili",
      "b23.tv": "Bilibili",
      "x.com": "X",
      "twitter.com": "X",
      "vimeo.com": "Vimeo",
      "douyin.com": "抖音",
    };
    return known[host] || host;
  } catch {
    return "视频";
  }
}

function durationText(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total <= 0) return "";
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = Math.floor(total % 60);
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`
    : `${minutes}:${String(secs).padStart(2, "0")}`;
}

function formatBytes(bytes) {
  let value = Number(bytes);
  if (!Number.isFinite(value) || value <= 0) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index === 0 ? 0 : value >= 10 ? 1 : 2)} ${units[index]}`;
}

function formatSpeed(bytes) {
  const size = formatBytes(bytes);
  return size ? `${size}/s` : "";
}

function extractMaxHeight(metadata) {
  const heights = (metadata?.formats || [])
    .filter((format) => format?.vcodec && format.vcodec !== "none")
    .map((format) => {
      const direct = Number(format.height);
      if (direct > 0) return direct;
      const match = String(format.resolution || "").match(/\d+x(\d+)/);
      return match ? Number(match[1]) : 0;
    });
  const bestHeight = Number(metadata?.best?.height) || 0;
  return Math.max(bestHeight, ...heights, 0);
}

function resetQualityOptions(maxHeight = 0) {
  const radios = elements.qualityFieldset.querySelectorAll('input[name="quality"]');
  radios.forEach((radio) => {
    const cap = Number(radio.value);
    radio.disabled = Boolean(cap && maxHeight && cap > maxHeight);
  });
  const checked = elements.qualityFieldset.querySelector('input[name="quality"]:checked');
  if (!checked || checked.disabled) {
    elements.qualityFieldset.querySelector('input[value="best"]').checked = true;
  }
  elements.bestQualityNote.textContent = maxHeight ? `最高 ${maxHeight}P` : "最高可用";
}

function clearPreview({ keepInput = false } = {}) {
  selectedVideo = null;
  elements.preview.hidden = true;
  elements.previewMedia.classList.remove("is-loading");
  elements.previewImage.removeAttribute("src");
  elements.previewTitle.textContent = "";
  elements.previewMeta.replaceChildren();
  elements.previewMessage.textContent = "";
  elements.previewMessage.className = "preview-message";
  elements.urlMessage.textContent = "";
  elements.urlMessage.className = "form-message";
  resetQualityOptions();
  if (!keepInput) elements.videoUrl.value = "";
}

function setPreviewLoading(url) {
  elements.preview.hidden = false;
  elements.previewMedia.classList.add("is-loading");
  elements.previewImage.removeAttribute("src");
  elements.previewSource.textContent = sourceName(url);
  elements.previewTitle.textContent = "正在读取视频信息";
  elements.previewMeta.replaceChildren();
  elements.previewMessage.textContent = "";
  elements.previewMessage.className = "preview-message";
  elements.downloadButton.disabled = true;
}

function addPreviewMeta(iconName, text) {
  if (!text) return;
  const item = document.createElement("span");
  item.append(makeIcon(iconName), document.createTextNode(text));
  elements.previewMeta.append(item);
}

function renderPreview(url, metadata, warning = "") {
  const title = metadata?.title || `来自 ${sourceName(url)} 的视频`;
  const maxHeight = extractMaxHeight(metadata);
  elements.previewMedia.classList.remove("is-loading");
  elements.previewSource.textContent = sourceName(url);
  elements.previewTitle.textContent = title;
  elements.previewImage.alt = title;
  if (metadata?.thumbnail) {
    elements.previewImage.src = metadata.thumbnail;
  } else {
    elements.previewImage.removeAttribute("src");
  }
  elements.previewMeta.replaceChildren();
  addPreviewMeta("clock", durationText(metadata?.duration));
  addPreviewMeta("video", maxHeight ? `最高 ${maxHeight}P` : "视频");
  if (metadata?.uploader || metadata?.channel) {
    addPreviewMeta("check", metadata.uploader || metadata.channel);
  }
  resetQualityOptions(maxHeight);
  elements.previewMessage.textContent = warning;
  elements.previewMessage.className = warning ? "preview-message is-warning" : "preview-message";
  elements.downloadButton.disabled = false;
}

async function analyzeVideo(event) {
  event.preventDefault();
  elements.urlMessage.textContent = "";
  elements.urlMessage.className = "form-message";

  let url;
  try {
    url = normalizeVideoUrl(elements.videoUrl.value);
  } catch (error) {
    elements.urlMessage.textContent = errorMessage(error);
    elements.urlMessage.className = "form-message is-error";
    elements.videoUrl.focus();
    return;
  }

  selectedVideo = { url, metadata: null };
  setPreviewLoading(url);
  setButtonBusy(elements.analyzeButton, true, "解析中");

  try {
    const metadata = await rpc("Service.Formats", [{ URL: url }]);
    if (metadata?._type === "playlist") throw new Error("请输入单个视频链接");
    selectedVideo.metadata = metadata || null;
    renderPreview(url, metadata);
  } catch (error) {
    if (!selectedVideo || selectedVideo.url !== url) return;
    renderPreview(url, null, "未读取到视频信息，可直接尝试下载");
    elements.urlMessage.textContent = errorMessage(error);
  } finally {
    setButtonBusy(elements.analyzeButton, false, "解析视频");
  }
}

async function pasteUrl() {
  try {
    const value = await navigator.clipboard.readText();
    if (!value) throw new Error("剪贴板中没有链接");
    elements.videoUrl.value = value.trim();
    clearPreview({ keepInput: true });
    elements.videoUrl.focus();
  } catch (error) {
    toast(errorMessage(error, "无法读取剪贴板"), "error");
    elements.videoUrl.focus();
  }
}

async function startDownload() {
  if (!selectedVideo) return;
  const selectedQuality = elements.qualityFieldset.querySelector('input[name="quality"]:checked')?.value || "best";
  const format = qualityFormats[selectedQuality] || qualityFormats.best;
  setButtonBusy(elements.downloadButton, true, "加入队列");

  try {
    await rpc("Service.Exec", [
      {
        URL: selectedVideo.url,
        Params: ["--no-playlist", "-f", format, "--merge-output-format", "mp4"],
        Path: "",
        Rename: "",
      },
    ]);
    toast("已加入下载队列");
    await new Promise((resolve) => window.setTimeout(resolve, 500));
    await refreshAll();
    clearPreview();
    elements.videoUrl.focus();
    document.querySelector("#tasksTitle").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(errorMessage(error, "加入下载队列失败"), "error");
  } finally {
    setButtonBusy(elements.downloadButton, false, "开始下载");
  }
}

function emptyState(iconName, label) {
  const empty = document.createElement("div");
  empty.className = "empty-state";
  empty.append(makeIcon(iconName));
  const text = document.createElement("span");
  text.textContent = label;
  empty.append(text);
  return empty;
}

function progressValue(task) {
  const raw = String(task?.progress?.percentage ?? "0");
  if (raw === "-1") return 100;
  const parsed = Number.parseFloat(raw.replace("%", ""));
  return Number.isFinite(parsed) ? Math.max(0, Math.min(100, parsed)) : 0;
}

function isTaskComplete(task) {
  return Number(task?.progress?.process_status) === 2 || String(task?.progress?.percentage) === "-1";
}

function encodedPath(path) {
  const bytes = new TextEncoder().encode(String(path || ""));
  let binary = "";
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return encodeURIComponent(btoa(binary));
}

function fileDownloadUrl(path) {
  return `/filebrowser/d/${encodedPath(path)}?token=${encodeURIComponent(token)}`;
}

function taskAction({ icon, label, action, id, href, danger = false }) {
  const control = href ? document.createElement("a") : document.createElement("button");
  control.className = `icon-button${danger ? " danger-action" : ""}`;
  control.setAttribute("aria-label", label);
  control.setAttribute("title", label);
  control.append(makeIcon(icon));
  if (href) {
    control.href = href;
  } else {
    control.type = "button";
    control.dataset.action = action;
    control.dataset.taskId = String(id);
  }
  return control;
}

function renderTasks(tasks) {
  const list = Array.isArray(tasks) ? tasks : [];
  elements.taskCount.textContent = String(list.length);
  elements.taskList.replaceChildren();
  if (!list.length) {
    elements.taskList.append(emptyState("clock", "暂无下载任务"));
    return;
  }

  list.forEach((task) => {
    const status = Number(task?.progress?.process_status ?? 0);
    const complete = isTaskComplete(task);
    const percent = progressValue(task);
    const article = document.createElement("article");
    article.className = "task-item";

    const thumb = document.createElement("div");
    thumb.className = "task-thumb";
    thumb.append(makeIcon("video"));
    if (task?.info?.thumbnail) {
      const image = document.createElement("img");
      image.src = task.info.thumbnail;
      image.alt = "";
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("error", () => image.remove());
      thumb.prepend(image);
    }

    const content = document.createElement("div");
    content.className = "task-content";
    const title = document.createElement("p");
    title.className = "task-title";
    title.title = task?.info?.title || "正在获取视频信息";
    title.textContent = title.title;

    const meta = document.createElement("div");
    meta.className = "task-meta";
    const statusChip = document.createElement("span");
    statusChip.className = `status-chip${status === 3 ? " is-error" : ""}`;
    statusChip.textContent = statusLabels[status] || "等待中";
    meta.append(statusChip);

    const details = [
      complete || status === 3 ? "" : `${Math.round(percent)}%`,
      formatSpeed(task?.progress?.speed),
      formatBytes(task?.info?.filesize_approx),
    ].filter(Boolean);
    if (details.length) {
      const detail = document.createElement("span");
      detail.textContent = details.join(" · ");
      meta.append(detail);
    }

    const progress = document.createElement("div");
    progress.className = `progress-track${status === 0 ? " is-pending" : ""}`;
    const bar = document.createElement("span");
    bar.className = "progress-value";
    bar.style.width = `${percent}%`;
    progress.append(bar);
    content.append(title, meta, progress);

    const actions = document.createElement("div");
    actions.className = "task-actions";
    const savedPath = task?.output?.savedFilePath;
    if (complete && savedPath) {
      actions.append(
        taskAction({
          icon: "download",
          label: "下载文件",
          href: fileDownloadUrl(savedPath),
        }),
      );
    }
    actions.append(
      taskAction({
        icon: complete || status === 3 ? "x" : "stop",
        label: complete || status === 3 ? "清除任务" : "取消下载",
        action: complete || status === 3 ? "clear" : "kill",
        id: task.id,
        danger: !complete,
      }),
    );

    article.append(thumb, content, actions);
    elements.taskList.append(article);
  });
}

function renderFiles(files) {
  const list = (Array.isArray(files) ? files : [])
    .filter((file) => !file.isDirectory)
    .sort((a, b) => new Date(b.modTime).getTime() - new Date(a.modTime).getTime());
  elements.fileCount.textContent = String(list.length);
  elements.fileList.replaceChildren();

  if (!list.length) {
    elements.fileList.append(emptyState("file", "暂无已完成文件"));
    return;
  }

  list.slice(0, 8).forEach((file) => {
    const row = document.createElement("div");
    row.className = "file-row";
    const iconWrap = document.createElement("span");
    iconWrap.className = "file-icon";
    iconWrap.append(makeIcon("file"));

    const info = document.createElement("div");
    info.className = "file-info";
    const name = document.createElement("p");
    name.className = "file-name";
    name.title = file.name || "视频文件";
    name.textContent = name.title;
    const meta = document.createElement("p");
    meta.className = "file-meta";
    const date = file.modTime ? new Date(file.modTime).toLocaleString("zh-CN", { hour12: false }) : "";
    meta.textContent = [formatBytes(file.size), date].filter(Boolean).join(" · ");
    info.append(name, meta);

    const download = document.createElement("a");
    download.className = "file-action";
    download.href = fileDownloadUrl(file.path);
    download.setAttribute("aria-label", `下载 ${file.name || "视频文件"}`);
    download.append(makeIcon("download"));
    const label = document.createElement("span");
    label.textContent = "下载";
    download.append(label);

    const deleteButton = document.createElement("button");
    deleteButton.className = "icon-button file-delete-action";
    deleteButton.type = "button";
    deleteButton.dataset.deletePath = file.path;
    deleteButton.dataset.deleteName = file.name || "视频文件";
    deleteButton.setAttribute("aria-label", `从服务器删除 ${file.name || "视频文件"}`);
    deleteButton.setAttribute("title", "从服务器删除");
    deleteButton.append(makeIcon("trash"));

    const actions = document.createElement("div");
    actions.className = "file-actions";
    actions.append(download, deleteButton);
    row.append(iconWrap, info, actions);
    elements.fileList.append(row);
  });
}

function openDeleteDialog(button) {
  pendingDeleteFile = {
    path: button.dataset.deletePath,
    name: button.dataset.deleteName || "视频文件",
  };
  elements.deleteFileName.textContent = pendingDeleteFile.name;
  elements.deleteDialog.showModal();
}

async function confirmFileDelete() {
  if (!pendingDeleteFile) return;
  setButtonBusy(elements.deleteConfirmButton, true, "删除中");
  try {
    const result = await apiRequest("/filebrowser/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: pendingDeleteFile.path }),
    });
    if (!result?.deleted) throw new Error("服务器未确认删除");
    elements.deleteDialog.close();
    await Promise.all([loadFiles(), loadTasks()]);
    toast("服务器文件已删除");
  } catch (error) {
    toast(errorMessage(error, "删除服务器文件失败"), "error");
  } finally {
    setButtonBusy(elements.deleteConfirmButton, false, "确认删除");
  }
}

function handleFileAction(event) {
  const button = event.target.closest("button[data-delete-path]");
  if (button) openDeleteDialog(button);
}

async function loadTasks() {
  const tasks = await rpc("Service.Running", []);
  renderTasks(tasks);
}

async function loadFiles() {
  const files = await apiRequest("/filebrowser/downloaded", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subdir: "" }),
  });
  renderFiles(files);
}

async function refreshAll() {
  if (refreshing || !token || elements.appView.hidden) return;
  refreshing = true;
  elements.refreshButtons.forEach((button) => button.classList.add("is-spinning"));
  try {
    await Promise.all([loadTasks(), loadFiles()]);
    setServiceState("online", "服务在线");
  } catch (error) {
    if (token) setServiceState("offline", "连接失败");
    console.error(error);
  } finally {
    refreshing = false;
    elements.refreshButtons.forEach((button) => button.classList.remove("is-spinning"));
  }
}

async function handleTaskAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  button.disabled = true;
  try {
    const method = button.dataset.action === "clear" ? "Service.Clear" : "Service.Kill";
    await rpc(method, [button.dataset.taskId]);
    await refreshAll();
    toast(button.dataset.action === "clear" ? "任务已清除" : "下载已取消");
  } catch (error) {
    toast(errorMessage(error), "error");
  } finally {
    button.disabled = false;
  }
}

function toast(message, type = "success") {
  elements.toastRegion.replaceChildren();
  const item = document.createElement("div");
  item.className = `toast${type === "error" ? " is-error" : ""}`;
  item.append(makeIcon(type === "error" ? "x" : "check"));
  const text = document.createElement("span");
  text.textContent = message;
  item.append(text);
  elements.toastRegion.append(item);
  window.setTimeout(() => item.remove(), 3200);
}

function bindEvents() {
  elements.loginForm.addEventListener("submit", handleLogin);
  elements.logoutButton.addEventListener("click", () => showLogin(false));
  elements.urlForm.addEventListener("submit", analyzeVideo);
  elements.pasteButton.addEventListener("click", pasteUrl);
  elements.downloadButton.addEventListener("click", startDownload);
  elements.clearPreviewButton.addEventListener("click", () => {
    clearPreview();
    elements.videoUrl.focus();
  });
  elements.videoUrl.addEventListener("input", () => {
    if (selectedVideo && elements.videoUrl.value.trim() !== selectedVideo.url) {
      clearPreview({ keepInput: true });
    }
  });
  elements.previewImage.addEventListener("error", () => elements.previewImage.removeAttribute("src"));
  elements.taskList.addEventListener("click", handleTaskAction);
  elements.fileList.addEventListener("click", handleFileAction);
  elements.deleteCancelButton.addEventListener("click", () => elements.deleteDialog.close());
  elements.deleteConfirmButton.addEventListener("click", confirmFileDelete);
  elements.deleteDialog.addEventListener("click", (event) => {
    if (event.target === elements.deleteDialog) elements.deleteDialog.close();
  });
  elements.deleteDialog.addEventListener("close", () => {
    pendingDeleteFile = null;
    elements.deleteFileName.textContent = "";
  });
  elements.refreshButtons.forEach((button) => button.addEventListener("click", refreshAll));
}

async function init() {
  bindEvents();
  if (!token) {
    showLogin(false);
    return;
  }
  try {
    const version = await apiRequest("/api/v1/version");
    await showApp(version);
  } catch {
    showLogin(true);
  }
}

init();
