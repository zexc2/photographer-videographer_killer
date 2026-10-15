# Project brief: 360 / multi-camera → AI camera-move video (photographer-videographer_killer)

First thing: save this entire brief verbatim as ~/app/CLAUDE.md; all later sessions follow it. When I (Joey) make new decisions later, update the corresponding place in CLAUDE.md.

> 2026-10-15: this file was translated from the original Chinese into English at Joey's request ("make everything on GitHub English"). The content and rules are unchanged.

## 1. Project goal
I am the founder of ERA MOMENT, a company that does event photography and videography. I want to test this: at an event, shoot with 1 360 camera (Insta360 X3) + a few phones on tripods at fixed positions, then use AI afterwards to reconstruct the scene and generate "roaming camera" videos with camera moves, replacing or supplementing an on-site videographer.
- Cost benchmark: an 8-hour event videographer in the US rarely quotes less than $1500, so the whole AI pipeline's GPU cost per event must be clearly below that to make commercial sense.
- Phase 1 (now): using the public multi-camera dataset CMU Panoptic, run GEN3C and 4C4D once each and compare quality, time and cost. The final output must contain a camera move (camera motion).
- Phase 2: in a controlled environment, with myself as the subject, do a real-world test with an Insta360 + phones (I haven't bought the Insta360 yet).
- This is also my USF BUS 205 course project, so the README and progress log must be readable by my instructor.

## 2. Environment
- Server: an old DigitalOcean server, 2 cores, 4 GB RAM, only about 20 GB left on the data disk. You run as Linux user atlas, without sudo.
- This server also runs the company website (Caddy) and services of other users (erastandard, pipeline). Never touch anything outside /home/atlas, system services, the Caddy config, or ports 80/443.
- The server is only responsible for: the frontend, scheduling, and storing small files. All heavy computation (3D reconstruction, training, generation) happens on RunPod GPU pods. Do not put large files on the server.
- [2026-10-13 update] Storage now uses the RunPod **Global volume** `atlas_data` (already created, not tied to a data center) instead of a Network volume. The Global volume is backed by object storage and **only holds cold data**: model weights, CMU Panoptic raw data, final results and reports (plus packed conda environments, see docs/storage-and-env.md). Conda environments, code, training/inference working directories, intermediate files and checkpoints all go on the **container disk** (`$ATLAS_LOCAL=/opt/atlas`, 200 GB by default, wiped when the pod stops); before a job ends, sync whatever must be kept back to the Global volume. Inside a pod, always refer to the Global volume path via the variable `$ATLAS_VOL` (default /workspace; it becomes /workspace-global when a network volume is mounted at the same time) — never hard-code it.
- [2026-10-13 update] The Global volume can only be mounted through the RunPod REST API v2 (runpodctl 2.15 does not support it). The guard scripts use a **restricted API key** (only pods:read/write, volumes:read, catalog:read), stored at ~/.config/atlas-gpu/runpod_api_key (chmod 600), created and put there by Joey; scripts read it at runtime and never print it. Still never touch the key in ~/.runpod.
- runpodctl 2.15 is in ~/.local/bin, its key is in ~/.runpod/ (do not read, print or copy the key). The 2.15 command format is `runpodctl pod list/create/stop`; confirm parameters with --help first.
- The same RunPod account has pods from other projects (e.g. eramoment_find_attendee_tool); only operate on pods with the atlas- prefix.
- Public GitHub repo: zexc2/photographer-videographer_killer, remote git@github-pvk:zexc2/photographer-videographer_killer.git (~/.ssh/config is already set up).

## 3. Work stages (in order; commit + push at the end of each stage and report to me)

### Stage 0: repository setup
- Initialize git in ~/app, main branch; write .gitignore before the first commit
- Create a README.md skeleton and PROGRESS.md (for each stage record: what was done, problems encountered, time taken, money spent)

### Stage 1: GPU guard (verify everything with dry-run, do not actually start pods)
- ~/bin/gpu-up: every pod start must go through it. Whitelist: A100 80GB (PCIe/SXM); H100 allowed when A100 is out of stock; reject all other models; force the atlas- name prefix; at most 1 at a time; [update] do not lock the region, look for A100 in all regions; mount Global volume atlas_data at $ATLAS_VOL; parameters --gpu --image --hours (default 6, max 6); on success print the pod ID and SSH command
- ~/bin/gpu-down: may only stop/remove pods with the atlas- prefix
- ~/bin/gpu-status: list atlas- pods, uptime, estimated cost, time until automatic shutdown
- Watchdog script + atlas's crontab (every 10 minutes): exceeds the configured duration → stop; GPU utilization 0 for 30 consecutive minutes → stop (prefer the RunPod API, fall back to SSH + nvidia-smi if unavailable; choose the reliable approach and explain why); log to ~/logs/gpu-guard.log
- ~~Tell me which region the Network Volume should be in (must have A100 80GB supply) and a suggested size~~ (switched to Global volume, no longer needed)
- [update] The watchdog's .atlas_busy marker lives on the container disk ($ATLAS_LOCAL/.atlas_busy); before stopping, make a best effort to run $ATLAS_LOCAL/bin/pre_stop to sync results back to the Global volume

### Stage 2: pod environment scripts (idempotent; [update] environments are installed on the container disk, following option C in docs/storage-and-env.md: install once, conda-pack into $ATLAS_VOL/envpacks/, unpack and reuse on the next pod start)
- setup_gen3c.sh: GEN3C (github nv-tlabs/GEN3C, weights nvidia/GEN3C-Cosmos-7B)
- setup_vipe.sh: a separate environment for ViPE (officially incompatible with the GEN3C environment)
- setup_4c4d.sh: 4C4D (CVPR 2026; verify the official repo first, candidate yangzf-1023/4C4D), including MASt3R preprocessing
- Each script records install time, disk usage, and any tokens or licenses required
- Write a generic task runner: the server rsyncs the task config to the pod → runs it on the pod → pulls small previews and metrics back to the server

### Stage 3: CMU Panoptic data
- Pick 1–2 suitable sequences (people moving, not too large a scene); download directly on the pod into $ATLAS_VOL/datasets/panoptic/, not via the server
- Prepare two kinds of input: GEN3C (monocular video + ViPE poses, or multi-view input, depending on what is officially supported); 4C4D (only 4 cameras, simulating our sparse camera setup, in COLMAP format)
- Hold out 1 camera that is not used for training, for evaluation

### Stage 4: baseline tests
- Run GEN3C and 4C4D once each; each produces a video with a camera move
- Record: environment setup time, run time, peak VRAM, GPU cost, PSNR/SSIM/LPIPS on the held-out camera (compute what can be computed), and subjective issues (people deforming, flicker, visible artifacts)
- Write a comparison report into the repo, and convert it into "roughly how much one 8-hour event would cost", compared with $1500

### Stage 5: frontend (Flask, running on the server)
- Listen only on 127.0.0.1; I access it via an SSH tunnel; no public ports, no Caddy changes
- Flow: upload footage (single-camera or multi-camera, with a size limit; after upload, move it to the Global volume and delete the server copy), or choose built-in test footage (Panoptic) → first generate a 3D preview (point cloud / Gaussian viewer in the browser) → I set the camera move myself: start, path, end (keyframes control the camera's position, orientation and field of view), and which seconds of the source footage to use → generation starts only after I click confirm → show progress and cost → preview and download the result
- Model choice: GEN3C / 4C4D

### Stage 6: integrate my 360 tool
- I have a tool that converts 360 footage into regular perspective video and annotates each view with spatial coordinates (camera position/orientation); I will provide the code. Hook it into the frontend upload flow so 360 footage can be used as input

## 4. Rules
- Before actually starting any pod, always ask me first and tell me: which GPU, how long it is expected to run, how much it is expected to cost. Only start it after I reply "开" ("go"). Shut it down immediately after use.
- The repo is public; never commit: API keys, SSH keys, tokens, .env, ~/.runpod, model weights (*.pt *.pth *.safetensors *.ckpt), datasets, videos, point clouds, training outputs, logs, pod IDs, IPs. No single file over 10 MB. Before every commit, self-check with git diff --cached.
- For third-party models and data, only write links and license notes; do not copy their content. In the README, state the licenses of CMU Panoptic, GEN3C (NVIDIA Open Model License) and 4C4D.
- Commit messages in English. [2026-10-15 update: originally Chinese; the history before 2026-10-15 is in Chinese]
- The server has only 4 GB RAM: do not run any model on the server, and do not unpack large files on the server.
- When something needs me (see section 5), stop and tell me clearly what to do and where; do not work around it.
- For uncertain technical choices, give me 2 options and your recommendation, then continue with the recommendation; no need to wait for me every time.

## 5. Things I (Joey) provide or do
1. Top up the RunPod account (remind me to check the balance before every pod start)
2. ~~Create the Network Volume~~ [update] Global volume atlas_data is already created. Also needed: create a restricted RunPod API key at ~/.config/atlas-gpu/runpod_api_key (see guard/README.md), and turn on RunPod low-balance alerts (the Global volume is deleted 15 days after the balance hits zero)
3. If a Hugging Face token is needed, or a model license must be accepted on Hugging Face, tell me exactly which model and which page and I will do it. I will put the token in RunPod Secrets or a file on the server; do not ask me to paste the token into the chat
4. Approve every pod start
5. Provide the code of my 360-to-perspective tool (stage 6)
6. Real footage for phase 2 (after I buy the Insta360)
7. Review the comparison report and decide the next step: GEN3C, 4C4D, or wait for Atlas

## 6. Report format
At the end of each stage give me a short report: what was completed / how much was spent / what problems came up / what I need to do next (if anything).

Now start from stage 0.

## 7. Decision log
- 2026-10-13: storage switched from Network volume (US-KS-2) to Global volume. Reason: after a pod stops, the same data center often has no stock; Global is not tied to a data center. Details and the environment options comparison are in docs/storage-and-env.md.
- 2026-10-13: 4C4D environment upgraded to py3.10 / torch 2.4 / CUDA 12.4 (the official py3.7 environment does not support H100); MASt3R is installed directly from naver/mast3r instead of via MAtCha. See pod/README.md.
- 2026-10-13 (pending Joey's decision): the GEN3C model card says disabling the safety guardrails terminates the NVIDIA Open Model License, yet the official inference script forces them off. Phase 1 research follows the official script; before commercial use we must decide how to handle this. 4C4D / MASt3R / CMU Panoptic are all non-commercial licenses.
- 2026-10-13: Global volume created (Location: Any). Scripts **identify it only by volume ID** (stored as global_volume_id in ~/.config/atlas-gpu/config.json, not in the repo); the name "photographer/videographer_killer" contains a slash and is only for display, never matched by name.
- 2026-10-13: gpu-up checks the account balance before starting a pod (the v2 API has no balance endpoint; read it via runpodctl user): if balance < $15 or unreadable, refuse to start the pod and prompt Joey to top up, to prevent the Global volume being deleted when the balance hits zero.
- 2026-10-14: [Joey's decision] balance floor changed from $15 to **$3**: refuse to start a pod if balance < $3 or unreadable; if the balance is expected to drop below $3 after running the full duration, only warn, do not refuse.
- 2026-10-13: in the comparison report (stage 4), each approach must separately state **"commercially usable or not" and why** (licenses of model code, weights, preprocessing dependencies, training data). If 4C4D performs best, additionally research **commercially usable sparse-view 4DGS alternatives**, listing repos and licenses.
- 2026-10-15: [Joey's decision] tidy the repo so others can deploy it by following DEPLOY.md: recommended GPU A100 80GB (Secure Cloud, H100 only when out of stock); tell people to register for RunPod, top up, and turn on low-balance alerts. Frontend models live at a fixed location `models/<name>/` on the Global volume, written by the training task.
- 2026-10-15: [Joey's decision] all repository content on GitHub is in English (docs, code comments, CLI output, this file). Commit messages are in English from now on; earlier history stays in Chinese (rewriting it would need a force push). Exceptions: the Chinese UI dictionary in frontend/static/i18n.js, the "中文" language button, and the Chinese model title used by the UI's Chinese mode.
- 2026-10-15: environment-pack keys are pinned with `ENV_REV` in each setup script (value = the content hash before the translation), so editing comments/log text no longer invalidates the packs on the Global volume. Bump `ENV_REV` whenever what a setup script installs changes.
