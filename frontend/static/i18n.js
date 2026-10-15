// UI strings for the camera path workbench (English by default, switchable to Chinese).
// Static text: HTML elements carry data-i18n="key" (textContent), data-i18n-title, data-i18n-aria.
// The backend returns only code + params: errors use err.<code>, job progress stage.<code>, job failure reasons jobErr.<code>.
"use strict";

const I18N = {
  en: {
    "doc.title": "ATLAS Camera Path Workbench",
    "brand": "ATLAS Workbench",
    "model.aria": "Choose a scene",
    "fake": "Simulation mode: no GPU, test footage only",
    "results": "Results ↗",
    "gpu.none": "GPU: —", "gpu.off": "GPU: off", "gpu.on": "GPU: {names} running",
    "balance": "Balance: ${v}", "balance.none": "Balance: —",
    "lang.aria": "Language",

    "view.free": "Free view", "view.shot": "Camera view",
    "play": "▶ Play", "pause": "❚❚ Pause", "play.title": "Play the camera path preview", "scrub.aria": "Preview time",
    "scrub": "{t} s · source {s} s",
    "viewer.hint": "The point cloud is only a position guide; real frames are rendered on the GPU after Confirm & render. Grey frames are real cameras, orange shows your keyframes and camera path.",
    "noscene.title": "This scene has no 3D preview yet",
    "noscene.body": "One short GPU run exports the point cloud, cameras and thumbnails from the trained model. Only needed once per scene.",
    "noscene.btn": "Prepare scene preview",

    "step1": "1. Pick a base camera",
    "step1.hint": "★ marks the 4 training cameras. Every keyframe move and turn is relative to the base camera.",
    "cam.alt": "View from {name}",
    "step2": "2. Set the camera path",
    "duration": "Duration", "sec": "s", "fps": "Frame rate", "res": "Resolution",
    "res.train": "1352×1014 (training)", "res.wide": "1280×720 (16:9)",
    "kf.title": "Keyframes", "kf.add": "+ Add",
    "kf.item": "Keyframe {n}", "kf.times": "{t} s · src {s} s",
    "kf.del": "Delete keyframe", "kf.reset": "Reset to base camera",
    "kf.hint": "1 sp = one camera spacing, the average distance between neighbouring real cameras ({v} scene units). Quality drops quickly outside the area the cameras cover.",
    "f.t": "Output time", "f.src": "Source time", "f.right": "Right", "f.up": "Up", "f.forward": "Forward",
    "f.yaw": "Pan (right +)", "f.pitch": "Tilt (up +)", "f.fov": "Field of view", "unit.sp": "sp",

    "step3": "3. Confirm & render",
    "render.btn": "Confirm & render…",
    "est.frames": "{n} frames · {res} · {fps} fps",
    "est.cost": "Estimated GPU time <b>~{m} min</b>, <b>~${c}</b> ({gpu}, ${p}/h)",
    "est.toolong": "Over the {max}-frame limit. Shorten the duration.",
    "path.error": "Camera path error: {msg}",
    "load.error": "Failed to load: {msg}",

    "jobs.title": "Render jobs", "jobs.empty": "No render jobs yet.",
    "job.export": "Scene preview", "job.render": "Render job",
    "state.queued": "Queued", "state.running": "Running", "state.done": "Done", "state.failed": "Failed",
    "job.cost": " · GPU cost ${v}", "job.estimate": " · estimate ${v}",
    "job.download": "Download video", "job.log": "View log",

    "modal.render.title": "Start a GPU and render this camera path?",
    "modal.export.title": "Start a GPU to prepare the scene preview?",
    "modal.gpu": "GPU", "modal.frames": "Frames", "modal.frames.v": "{n} ({d} s)",
    "modal.time": "Estimated time", "modal.time.v": "~{m} min",
    "modal.cost": "Estimated cost", "modal.cost.v": "~${c}", "modal.balance": "Current balance",
    "modal.note": "The server starts one GPU (capped at 30 minutes by the guard) and shuts it down when the job finishes.",
    "modal.ok.render": "Start GPU & render", "modal.ok.export": "Start GPU & export", "modal.cancel": "Cancel",

    "stage.queued": "Waiting to start",
    "stage.starting_gpu": "Starting GPU (~1 min)",
    "stage.reusing_gpu": "Reusing the running GPU",
    "stage.pushing": "Sending job to GPU",
    "stage.preparing": "Preparing environment and model on GPU (~2–3 min)",
    "stage.rendering": "Rendering frame {done}/{total}",
    "stage.fetching": "Downloading result",
    "stage.stopping_gpu": "Shutting down GPU",
    "stage.done": "Finished",
    "stage.failed": "Failed",
    "stage.fake": " (simulated)",

    "jobErr.gpu_busy": "The GPU is in use by another job ({name}). Try again later.",
    "jobErr.gpu_start_failed": "Could not start a GPU. See the log for details.",
    "jobErr.no_pod_id": "The GPU started but returned no pod ID. The guard will shut it down at the time limit.",
    "jobErr.run_not_started": "The job did not start on the GPU.",
    "jobErr.pod_task_failed": "The job failed on the GPU. See the log for details.",
    "jobErr.unexpected": "Unexpected error. See the log for details.",

    "err.model_not_found": "Unknown scene: {model}",
    "err.scene_not_ready": "This scene has no 3D preview yet.",
    "err.job_running": "A GPU job is already running. Wait for it to finish.",
    "err.showcase_missing": "Showcase assets have not been built yet.",
    "err.base_cam_not_found": "Base camera not found: {cam}",
    "err.bad_duration_fps": "Duration must be {min}–{max} s and frame rate one of {fps}.",
    "err.bad_resolution": "Resolution must be even and at most {max_w}×{max_h}.",
    "err.no_keyframes": "Add at least one keyframe.",
    "err.confirm_required": "Rendering needs confirmation.",
    "err.too_many_frames": "Too many frames (limit {max}).",
    "err.bad_request": "Invalid request.",
    "err.not_found": "Not found.",
    "err.conflict": "Conflict with the current state.",
    "err.network": "Network error.",
    "err.unknown": "Something went wrong ({code}).",
  },

  zh: {
    "doc.title": "ATLAS 运镜工作台",
    "brand": "ATLAS 运镜工作台",
    "model.aria": "选择场景",
    "fake": "模拟模式：不开 GPU，结果是测试画面",
    "results": "结果展示页 ↗",
    "gpu.none": "GPU：—", "gpu.off": "GPU：未开启", "gpu.on": "GPU：{names} 运行中",
    "balance": "余额：${v}", "balance.none": "余额：—",
    "lang.aria": "界面语言",

    "view.free": "自由视角", "view.shot": "镜头视角",
    "play": "▶ 播放", "pause": "❚❚ 暂停", "play.title": "播放运镜预览", "scrub.aria": "预览时间",
    "scrub": "{t} s · 源 {s} s",
    "viewer.hint": "点云只是位置参考，真实画面要点\"确认生成\"后由 GPU 渲染。灰色线框是真实机位，橙色是你的关键帧，橙线是运镜路径。",
    "noscene.title": "这个场景还没有 3D 预览",
    "noscene.body": "需要开一次 GPU，从训练好的模型里导出点云、机位和缩略图。每个场景只需做一次。",
    "noscene.btn": "准备场景预览",

    "step1": "1. 选一个固定机位作为起点",
    "step1.hint": "带 ★ 的是训练用的 4 个机位。所有关键帧的位移、转角都以这个机位为参照。",
    "cam.alt": "{name} 的画面",
    "step2": "2. 设置运镜",
    "duration": "总时长", "sec": "秒", "fps": "帧率", "res": "分辨率",
    "res.train": "1352×1014（训练分辨率）", "res.wide": "1280×720（16:9）",
    "kf.title": "关键帧", "kf.add": "＋ 添加",
    "kf.item": "关键帧 {n}", "kf.times": "{t} s · 源 {s} s",
    "kf.del": "删除这个关键帧", "kf.reset": "回到基准机位",
    "kf.hint": "\"格\" = 相邻真实机位的平均间距（{v} 场景单位）。机位覆盖范围之外，画质会明显下降。",
    "f.t": "输出时间", "f.src": "源视频时间", "f.right": "右移", "f.up": "上移", "f.forward": "前进",
    "f.yaw": "左右转（右正）", "f.pitch": "上下转（上正）", "f.fov": "视场角", "unit.sp": "格",

    "step3": "3. 确认生成",
    "render.btn": "确认生成…",
    "est.frames": "{n} 帧 · {res} · {fps} fps",
    "est.cost": "预计 GPU 约 <b>{m} 分钟</b>，约 <b>${c}</b>（{gpu}，${p}/h）",
    "est.toolong": "帧数超过 {max} 帧上限，请缩短时长",
    "path.error": "轨迹有误：{msg}",
    "load.error": "加载失败：{msg}",

    "jobs.title": "任务", "jobs.empty": "还没有任务。",
    "job.export": "准备场景预览", "job.render": "运镜渲染",
    "state.queued": "排队中", "state.running": "进行中", "state.done": "完成", "state.failed": "失败",
    "job.cost": " · GPU 花费 ${v}", "job.estimate": " · 预估 ${v}",
    "job.download": "下载视频", "job.log": "查看日志",

    "modal.render.title": "开 GPU 并生成这段运镜？",
    "modal.export.title": "开 GPU 准备场景预览？",
    "modal.gpu": "GPU", "modal.frames": "帧数", "modal.frames.v": "{n}（{d} 秒）",
    "modal.time": "预计时间", "modal.time.v": "约 {m} 分钟",
    "modal.cost": "预计花费", "modal.cost.v": "约 ${c}", "modal.balance": "当前余额",
    "modal.note": "确认后服务器会开一台 GPU（护栏上限 30 分钟），完成后自动关闭。",
    "modal.ok.render": "开 GPU 并生成", "modal.ok.export": "开 GPU 并导出", "modal.cancel": "取消",

    "stage.queued": "排队中",
    "stage.starting_gpu": "开启 GPU（约 1 分钟）",
    "stage.reusing_gpu": "复用已开启的 GPU",
    "stage.pushing": "推送任务到 GPU",
    "stage.preparing": "GPU 上准备环境和模型（约 2–3 分钟）",
    "stage.rendering": "渲染中 {done}/{total} 帧",
    "stage.fetching": "拉回结果",
    "stage.stopping_gpu": "正在关闭 GPU",
    "stage.done": "完成",
    "stage.failed": "失败",
    "stage.fake": "（模拟）",

    "jobErr.gpu_busy": "GPU 正被其他任务使用（{name}），请稍后再试",
    "jobErr.gpu_start_failed": "开启 GPU 失败，详见日志",
    "jobErr.no_pod_id": "GPU 已开启但没有返回 pod ID，护栏会按时长上限关机",
    "jobErr.run_not_started": "任务没有在 GPU 上启动",
    "jobErr.pod_task_failed": "GPU 上的任务失败，详见日志",
    "jobErr.unexpected": "意外错误，详见日志",

    "err.model_not_found": "没有这个场景：{model}",
    "err.scene_not_ready": "这个场景还没有 3D 预览",
    "err.job_running": "已有一个 GPU 任务在进行，请等它结束",
    "err.showcase_missing": "还没有生成展示素材",
    "err.base_cam_not_found": "基准机位不存在：{cam}",
    "err.bad_duration_fps": "时长需在 {min}–{max} 秒，帧率为 {fps} 之一",
    "err.bad_resolution": "分辨率必须是偶数，且不超过 {max_w}×{max_h}",
    "err.no_keyframes": "至少需要一个关键帧",
    "err.confirm_required": "生成前需要确认",
    "err.too_many_frames": "帧数超过上限 {max}",
    "err.bad_request": "请求无效",
    "err.not_found": "找不到",
    "err.conflict": "与当前状态冲突",
    "err.network": "网络错误",
    "err.unknown": "出错了（{code}）",
  },
};

const LANG_KEY = "atlas.lang";
let LANG = "en";
try { const v = localStorage.getItem(LANG_KEY); if (v === "en" || v === "zh") LANG = v; } catch { /* unreadable: fall back to English */ }

function t(key, params = {}) {
  let s = I18N[LANG][key] ?? I18N.en[key] ?? key;
  return s.replace(/\{(\w+)\}/g, (_, k) => {
    const v = params[k];
    return v == null ? "" : Array.isArray(v) ? v.join("/") : String(v);
  });
}

// Backend error: {code, params} → one sentence in the current language
function tErr(e) {
  const code = e?.code || "unknown";
  return I18N.en["err." + code] ? t("err." + code, e.params || {}) : t("err.unknown", { code });
}

function applyStatic() {
  document.documentElement.lang = LANG === "zh" ? "zh-CN" : "en";
  document.title = t("doc.title");
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  document.querySelectorAll("[data-i18n-aria]").forEach((el) => { el.setAttribute("aria-label", t(el.dataset.i18nAria)); });
  document.querySelectorAll("[data-lang]").forEach((b) => {
    const on = b.dataset.lang === LANG;
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on);
  });
}

function setLang(lang, onChange) {
  if (lang !== "en" && lang !== "zh") return;
  LANG = lang;
  try { localStorage.setItem(LANG_KEY, lang); } catch { /* cannot store: switch anyway */ }
  applyStatic();
  onChange?.();
}
