// ATLAS 运镜工作台前端。坐标约定：服务器给的 c2w 是 OpenCV（x 右、y 下、z 前），
// three.js 相机是 x 右、y 上、z 后，所以 three 的相机矩阵 = c2w · diag(1, -1, -1, 1)。
"use strict";

const $ = (id) => document.getElementById(id);
const S = {
  model: null, scene: null, baseCam: null, keyframes: [], sel: 0,
  path: null, mode: "free", playing: false, playT0: 0, pointSize: 0.01,
};
const FIELDS = [
  // 键, 标签, 最小, 最大, 步长, 单位
  ["t", "输出时间", 0, 30, 0.1, "s"],
  ["src", "源视频时间", 0, 10, 0.1, "s"],
  ["right", "右移", -4, 4, 0.05, "格"],
  ["up", "上移", -3, 3, 0.05, "格"],
  ["forward", "前进", -4, 4, 0.05, "格"],
  ["yaw", "左右转（右正）", -60, 60, 1, "°"],
  ["pitch", "上下转（上正）", -40, 40, 1, "°"],
  ["fov", "视场角", 20, 100, 1, "°"],
];

// ---------------------------------------------------------------- three.js
const wrap = $("canvas-wrap");
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
wrap.appendChild(renderer.domElement);
const scene3 = new THREE.Scene();
scene3.background = new THREE.Color(0x15191a);
const freeCam = new THREE.PerspectiveCamera(60, 1, 0.01, 1000);
const shotCam = new THREE.PerspectiveCamera(60, 4 / 3, 0.01, 1000);
// OrbitControls 在创建时记下相机的"上方向"，换基准机位（上方向变了）时要重新创建
let controls = makeControls();
function makeControls() {
  const c = new THREE.OrbitControls(freeCam, renderer.domElement);
  c.enableDamping = true;
  return c;
}
const groups = { points: null, cams: new THREE.Group(), kfs: new THREE.Group(), path: new THREE.Group(), cursor: new THREE.Group() };
Object.values(groups).forEach((g) => g && scene3.add(g));

function cvToThree(c2w) {
  const m = new THREE.Matrix4();
  const a = c2w;
  // 行优先写入，同时把 y、z 两列取反
  m.set(a[0][0], -a[0][1], -a[0][2], a[0][3],
        a[1][0], -a[1][1], -a[1][2], a[1][3],
        a[2][0], -a[2][1], -a[2][2], a[2][3],
        0, 0, 0, 1);
  return m;
}

function placeCamera(cam, c2w, fov, aspect) {
  const p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();
  cvToThree(c2w).decompose(p, q, s);
  cam.position.copy(p);
  cam.quaternion.copy(q);
  cam.fov = fov;
  if (aspect) cam.aspect = aspect;
  cam.updateProjectionMatrix();
}

function frustum(c2w, fov, aspect, depth, color, opacity = 1) {
  const h = Math.tan((fov * Math.PI) / 360) * depth, w = h * aspect;
  // 相机坐标（OpenCV）：z 前、y 下
  const pts = [[0, 0, 0], [-w, -h, depth], [w, -h, depth], [w, h, depth], [-w, h, depth]];
  const P = pts.map(([x, y, z]) => new THREE.Vector3(
    c2w[0][0] * x + c2w[0][1] * y + c2w[0][2] * z + c2w[0][3],
    c2w[1][0] * x + c2w[1][1] * y + c2w[1][2] * z + c2w[1][3],
    c2w[2][0] * x + c2w[2][1] * y + c2w[2][2] * z + c2w[2][3]));
  const idx = [0, 1, 0, 2, 0, 3, 0, 4, 1, 2, 2, 3, 3, 4, 4, 1];
  const g = new THREE.BufferGeometry().setFromPoints(idx.map((i) => P[i]));
  // 顶边加粗标记"上方"，帮助判断相机有没有倒过来
  const up = new THREE.BufferGeometry().setFromPoints([P[1], P[2]]);
  const grp = new THREE.Group();
  grp.add(new THREE.LineSegments(g, new THREE.LineBasicMaterial({ color, transparent: opacity < 1, opacity })));
  grp.add(new THREE.Line(up, new THREE.LineBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.6 })));
  return grp;
}

function clear(g) { while (g.children.length) { const c = g.children.pop(); c.traverse?.((o) => { o.geometry?.dispose(); o.material?.dispose?.(); }); } }

function resize() {
  const r = wrap.getBoundingClientRect();
  renderer.setSize(r.width, r.height, false);
  freeCam.aspect = r.width / Math.max(r.height, 1);
  freeCam.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(wrap);

function shotAspect() { const [w, h] = $("res").value.split("x").map(Number); return w / h; }

function loop() {
  requestAnimationFrame(loop);
  if (S.playing && S.path) {
    const n = S.path.frames.length, fps = +$("fps").value;
    const i = Math.floor(((performance.now() - S.playT0) / 1000) * fps);
    if (i >= n) { S.playing = false; $("play").textContent = "▶ 播放"; }
    else { $("scrub").value = i; showFrame(i); }
  }
  controls.update();
  if (S.mode === "shot") {
    // 镜头视角：按输出画幅留黑边
    const r = wrap.getBoundingClientRect(), a = shotAspect();
    let w = r.width, h = r.width / a;
    if (h > r.height) { h = r.height; w = h * a; }
    renderer.setScissorTest(true);
    renderer.setViewport(0, 0, r.width, r.height); renderer.setScissor(0, 0, r.width, r.height);
    renderer.setClearColor(0x000000); renderer.clear();
    renderer.setViewport((r.width - w) / 2, (r.height - h) / 2, w, h);
    renderer.setScissor((r.width - w) / 2, (r.height - h) / 2, w, h);
    groups.cams.visible = groups.kfs.visible = groups.path.visible = groups.cursor.visible = false;
    renderer.render(scene3, shotCam);
    groups.cams.visible = groups.kfs.visible = groups.path.visible = groups.cursor.visible = true;
    renderer.setScissorTest(false);
  } else {
    const r = wrap.getBoundingClientRect();
    renderer.setViewport(0, 0, r.width, r.height);
    renderer.render(scene3, freeCam);
  }
}

// ---------------------------------------------------------------- 数据
async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body.error || body || r.statusText);
  return body;
}

async function loadModels() {
  const d = await api("/api/models");
  $("fake-tag").hidden = !d.fake;
  $("model").innerHTML = d.models.map((m) => `<option value="${m.key}">${m.title}</option>`).join("");
  S.models = d.models;
  await selectModel(d.models[0].key);
}

async function selectModel(key) {
  S.model = S.models.find((m) => m.key === key);
  clear(groups.cams); clear(groups.kfs); clear(groups.path);
  if (groups.points) { scene3.remove(groups.points); groups.points = null; }
  $("no-scene").hidden = S.model.scene_ready;
  $("render-btn").disabled = !S.model.scene_ready;
  if (!S.model.scene_ready) { $("cams").innerHTML = ""; $("kf-list").innerHTML = ""; $("kf-editor").innerHTML = ""; return; }
  S.scene = await api(`/api/scene/${key}`);
  FIELDS.find((f) => f[0] === "src")[3] = S.scene.source_seconds;
  const buf = await (await fetch(`/api/scene/${key}/points.bin`)).arrayBuffer();
  const n = new DataView(buf).getUint32(0, true);
  const xyz = new Float32Array(buf, 4, n * 3);
  const rgb = new Uint8Array(buf, 4 + n * 12, n * 3);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.BufferAttribute(xyz, 3));
  geo.setAttribute("color", new THREE.BufferAttribute(new Float32Array(rgb).map((v) => v / 255), 3));
  S.pointSize = S.scene.spacing * 0.04;
  groups.points = new THREE.Points(geo, new THREE.PointsMaterial({ size: S.pointSize, vertexColors: true, sizeAttenuation: true }));
  scene3.add(groups.points);
  const train = new Set(S.model.train_cams);
  for (const [name, c] of Object.entries(S.scene.cameras)) {
    groups.cams.add(frustum(c.c2w, c.fov, c.width / c.height, S.scene.spacing * 0.5, train.has(name) ? 0xb9c4bf : 0x6f7a75));
  }
  $("cams").innerHTML = Object.keys(S.scene.cameras).map((name) =>
    `<button class="cam" data-cam="${name}" title="${name}"><img loading="lazy" alt="${name} 的画面" src="/api/scene/${key}/thumb/${name}.jpg"><span>${train.has(name) ? "★ " : ""}${name}</span></button>`).join("");
  document.querySelectorAll(".cam").forEach((b) => b.onclick = () => selectBase(b.dataset.cam));
  const first = Object.keys(S.scene.cameras).includes("cam00") ? "cam00" : Object.keys(S.scene.cameras)[0];
  selectBase(first);
}

function selectBase(name) {
  S.baseCam = name;
  document.querySelectorAll(".cam").forEach((b) => b.classList.toggle("on", b.dataset.cam === name));
  const c = S.scene.cameras[name];
  const dur = +$("duration").value, src = Math.min(dur, S.scene.source_seconds);
  // 默认示例：从这个机位出发，向右平移 2 格并略微向左转，保持主体在画面里
  S.keyframes = [
    { t: 0, src: 0, right: 0, up: 0, forward: 0, yaw: 0, pitch: 0, fov: round(c.fov, 1) },
    { t: dur, src, right: 2, up: 0, forward: 0.5, yaw: -10, pitch: 0, fov: round(c.fov, 1) },
  ];
  S.sel = 0;
  // 自由视角：站在基准机位后上方看过去
  const m = cvToThree(c.c2w), p = new THREE.Vector3(), q = new THREE.Quaternion(), s = new THREE.Vector3();
  m.decompose(p, q, s);
  const fwd = new THREE.Vector3(0, 0, -1).applyQuaternion(q), up = new THREE.Vector3(0, 1, 0).applyQuaternion(q);
  freeCam.up.copy(up);
  freeCam.position.copy(p).addScaledVector(fwd, -S.scene.spacing * 3).addScaledVector(up, S.scene.spacing * 1.5);
  controls.dispose();
  controls = makeControls();
  controls.enabled = S.mode === "free";
  controls.target.copy(p).addScaledVector(fwd, S.scene.spacing * 6);
  renderKeyframes();
  updatePath();
}

const round = (v, d = 2) => Math.round(v * 10 ** d) / 10 ** d;

function renderKeyframes() {
  S.keyframes.sort((a, b) => a.t - b.t);
  $("kf-list").innerHTML = S.keyframes.map((k, i) =>
    `<li><button data-i="${i}" class="${i === S.sel ? "on" : ""}"><span>关键帧 ${i + 1}</span><span class="mono">${k.t.toFixed(1)} s · 源 ${k.src.toFixed(1)} s</span></button></li>`).join("");
  document.querySelectorAll("#kf-list button").forEach((b) => b.onclick = () => { S.sel = +b.dataset.i; renderKeyframes(); });
  const k = S.keyframes[S.sel];
  if (!k) { $("kf-editor").innerHTML = ""; return; }
  $("kf-editor").innerHTML = FIELDS.map(([key, label, mn, mx, st, unit]) => `
    <label class="slider"><span>${label}</span>
      <input type="range" id="f-${key}" min="${key === "t" ? 0 : mn}" max="${key === "t" ? +$("duration").value : mx}" step="${st}" value="${k[key]}">
      <output id="o-${key}">${(+k[key]).toFixed(st < 1 ? 2 : 0)} ${unit}</output></label>`).join("") +
    `<div class="kf-actions"><button id="kf-del" class="small" ${S.keyframes.length <= 1 ? "disabled" : ""}>删除这个关键帧</button>
     <button id="kf-reset" class="small">回到基准机位</button></div>
     <p class="hint">"格" = 相邻真实机位的平均间距（${S.scene.spacing.toFixed(3)} 场景单位）。机位覆盖范围之外，画质会明显下降。</p>`;
  FIELDS.forEach(([key, , , , st, unit]) => {
    $(`f-${key}`).oninput = (e) => {
      k[key] = +e.target.value;
      $(`o-${key}`).textContent = `${k[key].toFixed(st < 1 ? 2 : 0)} ${unit}`;
      if (key === "t") renderKeyframesList();
      schedulePath();
    };
  });
  $("kf-del").onclick = () => { S.keyframes.splice(S.sel, 1); S.sel = Math.max(0, S.sel - 1); renderKeyframes(); updatePath(); };
  $("kf-reset").onclick = () => { Object.assign(k, { right: 0, up: 0, forward: 0, yaw: 0, pitch: 0, fov: round(S.scene.cameras[S.baseCam].fov, 1) }); renderKeyframes(); updatePath(); };
}

function renderKeyframesList() {
  document.querySelectorAll("#kf-list button").forEach((b) => {
    const k = S.keyframes[+b.dataset.i];
    b.lastElementChild.textContent = `${k.t.toFixed(1)} s · 源 ${k.src.toFixed(1)} s`;
  });
}

$("kf-add").onclick = () => {
  // 在当前预览时间处添加，位姿取前一个关键帧
  const dur = +$("duration").value;
  const t = S.path ? +$("scrub").value / +$("fps").value : dur / 2;
  const prev = [...S.keyframes].reverse().find((k) => k.t <= t) || S.keyframes[0];
  const k = { ...prev, t: round(Math.min(t, dur), 2), src: round(Math.min(prev.src + (t - prev.t), S.scene.source_seconds), 2) };
  S.keyframes.push(k);
  S.keyframes.sort((a, b) => a.t - b.t);
  S.sel = S.keyframes.indexOf(k);
  renderKeyframes();
  updatePath();
};

let pathTimer = null;
function schedulePath() { clearTimeout(pathTimer); pathTimer = setTimeout(updatePath, 250); }

function spec() {
  const [width, height] = $("res").value.split("x").map(Number);
  return { base_cam: S.baseCam, keyframes: S.keyframes, duration: +$("duration").value, fps: +$("fps").value, width, height };
}

async function updatePath() {
  if (!S.scene || !S.baseCam) return;
  try {
    S.path = await api(`/api/path/${S.model.key}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(spec()) });
  } catch (e) { $("estimate").textContent = "轨迹有误：" + e.message; $("render-btn").disabled = true; return; }
  clear(groups.path); clear(groups.kfs);
  const P = S.path.frames.map((f) => new THREE.Vector3(f.c2w[0][3], f.c2w[1][3], f.c2w[2][3]));
  groups.path.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(P), new THREE.LineBasicMaterial({ color: 0xf08a4b })));
  const a = shotAspect(), n = S.path.frames.length, fps = +$("fps").value;
  S.keyframes.forEach((k) => {
    const i = Math.min(n - 1, Math.round((k.t / Math.max(+$("duration").value, 1e-6)) * (n - 1)));
    const f = S.path.frames[i];
    groups.kfs.add(frustum(f.c2w, f.fov, a, S.scene.spacing * 0.6, 0xf08a4b));
  });
  $("scrub").max = n - 1;
  showFrame(Math.min(+$("scrub").value, n - 1));
  const e = S.path.estimate;
  $("estimate").innerHTML = `<span>${n} 帧 · ${$("res").value.replace("x", "×")} · ${fps} fps</span>
    <span>预计 GPU 约 <b>${e.minutes} 分钟</b>，约 <b>$${e.cost_usd.toFixed(2)}</b>（${e.gpu}，$${e.price_per_hr}/h）</span>
    ${S.path.too_long ? '<span style="color:var(--bad)">帧数超过 900 帧上限，请缩短时长</span>' : ""}`;
  $("render-btn").disabled = S.path.too_long || !!S.runningJob;
}

function showFrame(i) {
  if (!S.path) return;
  const f = S.path.frames[i];
  placeCamera(shotCam, f.c2w, f.fov, shotAspect());
  $("scrub-label").textContent = `${(i / +$("fps").value).toFixed(2)} s · 源 ${f.src.toFixed(2)} s`;
  clear(groups.cursor);
  groups.cursor.add(frustum(f.c2w, f.fov, shotAspect(), S.scene.spacing * 0.8, 0xffffff, 0.9));
}

$("scrub").oninput = (e) => { S.playing = false; $("play").textContent = "▶ 播放"; showFrame(+e.target.value); };
$("play").onclick = () => {
  if (!S.path) return;
  S.playing = !S.playing;
  $("play").textContent = S.playing ? "❚❚ 暂停" : "▶ 播放";
  if (S.playing) { if (+$("scrub").value >= S.path.frames.length - 1) $("scrub").value = 0; S.playT0 = performance.now() - (+$("scrub").value / +$("fps").value) * 1000; }
};
$("mode-free").onclick = () => setMode("free");
$("mode-shot").onclick = () => setMode("shot");
function setMode(m) { S.mode = m; $("mode-free").classList.toggle("on", m === "free"); $("mode-shot").classList.toggle("on", m === "shot"); controls.enabled = m === "free"; }
["duration", "fps", "res"].forEach((id) => $(id).onchange = () => {
  const dur = +$("duration").value;
  S.keyframes.forEach((k) => { if (k.t > dur) k.t = dur; });
  renderKeyframes(); updatePath();
});
$("model").onchange = (e) => selectModel(e.target.value);

// ---------------------------------------------------------------- 确认与任务
function confirmModal(title, rows, okText) {
  return new Promise((resolve) => {
    $("modal-title").textContent = title;
    $("modal-body").innerHTML = `<table>${rows.map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`).join("")}</table>
      <p class="hint">确认后服务器会开一台 GPU（护栏上限 30 分钟），完成后自动关闭。</p>`;
    $("modal-ok").textContent = okText;
    $("modal").hidden = false;
    $("modal-ok").focus();
    $("modal-ok").onclick = () => { $("modal").hidden = true; resolve(true); };
    $("modal-cancel").onclick = () => { $("modal").hidden = true; resolve(false); };
  });
}

$("render-btn").onclick = async () => {
  const e = S.path.estimate, st = S.status || {};
  const ok = await confirmModal("开 GPU 并生成这段运镜？", [
    ["GPU", `${e.gpu}（$${e.price_per_hr}/h）`], ["帧数", `${S.path.frames.length}（${$("duration").value} 秒）`],
    ["预计时间", `约 ${e.minutes} 分钟`], ["预计花费", `约 $${e.cost_usd.toFixed(2)}`],
    ["当前余额", st.balance != null ? `$${st.balance.toFixed(2)}` : "—"]], "开 GPU 并生成");
  if (!ok) return;
  try { await api(`/api/render/${S.model.key}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...spec(), confirm: true }) }); }
  catch (err) { alert(err.message); }
  pollJobs();
};

$("export-btn").onclick = async () => {
  const e = S.model.export_estimate;
  const ok = await confirmModal("开 GPU 准备场景预览？", [["GPU", `${e.gpu}（$${e.price_per_hr}/h）`],
    ["预计时间", `约 ${e.minutes} 分钟`], ["预计花费", `约 $${e.cost_usd.toFixed(2)}`]], "开 GPU 并导出");
  if (!ok) return;
  try { await api(`/api/scene/${S.model.key}/export`, { method: "POST" }); } catch (err) { alert(err.message); }
  pollJobs();
};

const STATE = { queued: "排队中", running: "进行中", done: "完成", failed: "失败" };
async function pollJobs() {
  let d;
  try { d = await api("/api/jobs"); } catch { return; }
  const wasRunning = S.runningJob;
  S.runningJob = d.running;
  $("render-btn").disabled = !S.path || S.path.too_long || !!d.running || !S.model?.scene_ready;
  $("export-btn").disabled = !!d.running;
  $("jobs").innerHTML = d.jobs.length ? d.jobs.map((j) => `
    <div class="job">
      <div class="job-top"><b>${j.kind === "export" ? "准备场景预览" : "运镜渲染"}</b><span class="state ${j.state}">${STATE[j.state] || j.state}</span></div>
      <div>${j.stage || ""}${j.error ? `：${j.error}` : ""}</div>
      ${j.state === "running" && j.progress != null ? `<div class="progress"><div style="width:${(j.progress * 100).toFixed(0)}%"></div></div>` : ""}
      <div class="hint">${j.id}${j.cost_usd != null ? ` · GPU 花费 $${j.cost_usd.toFixed(2)}` : ""}${j.estimate ? ` · 预估 $${j.estimate.cost_usd.toFixed(2)}` : ""}</div>
      ${j.has_video ? `<video controls preload="metadata" src="/api/jobs/${j.id}/video"></video><a href="/api/jobs/${j.id}/video?download=1">下载视频</a>` : ""}
      ${j.state === "failed" ? `<a href="/api/jobs/${j.id}/log" target="_blank">查看日志</a>` : ""}
    </div>`).join("") : '<p class="hint">还没有任务。</p>';
  // 场景导出完成后刷新
  if (wasRunning && !d.running) { const m = await api("/api/models"); S.models = m.models; if (!S.model.scene_ready && S.models.find((x) => x.key === S.model.key)?.scene_ready) selectModel(S.model.key); }
}

async function pollStatus() {
  try {
    const st = await api("/api/status");
    S.status = st;
    $("balance").textContent = st.balance != null ? `余额：$${st.balance.toFixed(2)}` : "余额：—";
    $("gpu").textContent = st.pods?.length ? `GPU：${st.pods.map((p) => p.name).join("、")} 运行中` : "GPU：未开启";
  } catch { /* 网络抖动时下次再试 */ }
}

loadModels().catch((e) => { $("estimate").textContent = "加载失败：" + e.message; });
pollJobs(); pollStatus();
setInterval(pollJobs, 5000);
setInterval(pollStatus, 30000);
resize();
loop();
