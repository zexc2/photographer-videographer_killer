# Deployment guide: running the camera-move workbench from scratch

After the 6 steps below you can pick a camera in your browser, set a camera move, and have a GPU render the video. In total you need:

- about **30 minutes** of setup time;
- a one-time **~4 hours, ~$8** of GPU time to prepare the sample model;
- after that, **~1.5 minutes, ~$0.05** per render.

```
Your computer (browser) ──SSH tunnel──▶ Your Linux server (frontend + orchestration, no GPU needed) ──API──▶ RunPod GPU (A100 80GB)
                                                                                    └─ Global volume (models, environments, results)
```

> **License reminder**: 4C4D, MASt3R and the N3V dataset used by the sample are all **restricted to non-commercial research**; see [the license table in the README](README.md#third-party-model-and-data-licenses).

---

## 0. What you need

| Item | Requirements |
|---|---|
| A Linux machine | Runs the frontend and orchestration; no GPU needed, 2 cores / 4 GB is enough; a cloud server or your own Linux computer both work. Needs Python ≥ 3.9, rsync, ssh, cron; **no sudo needed** (only if you have to install missing software) |
| A RunPod account | Created in the next step; needs a credit card to add credit |
| Your own computer | Has a browser and can SSH into the Linux machine above |

---

## 1. Sign up for RunPod and add credit

1. Open **https://www.runpod.io/** → click **Sign Up** and create an account (Google or GitHub login works).
2. Left menu **Billing** → add credit. **We suggest $25 the first time**: the sample model costs about $8, and the rest covers a few hundred renders.
3. On the Billing page, **turn on low-balance email alerts** (e.g. alert below $10).

> ⚠️ **Never let your balance reach zero.** Under RunPod's rules, once the balance has been zero for a while (currently 15 days), storage volumes are **permanently deleted** — and your trained models and environments go with them.
> This project's guard also **refuses to start a GPU when the balance is below $3**.

### Recommended GPU: A100 80GB

This project uses only **NVIDIA A100 80GB**, on **Secure Cloud**:

| Model | Reference price | Notes |
|---|---|---|
| **A100 SXM 80GB** (first choice) | about $1.79/hour | All our measured numbers are from this card |
| A100 PCIe 80GB | about $1.64/hour | Works equally well |
| H100 (SXM / PCIe / NVL) | about $2.9–4.0/hour | Used **only when no A100 is available anywhere** |

Why 80GB: peak GPU memory is 24.6 GB for 4C4D training and 43.6 GB for GEN3C; 24 GB and 48 GB cards either can't fit it or leave no headroom.

**You don't need to pick a GPU or start a pod manually on the RunPod website.** Once deployed, every GPU start goes through this project's `gpu-up`, which only allows the models above, automatically searches all regions for an available A100, and falls back to H100 only if none is found. If you do start a pod manually on the website, also choose A100 80GB, and do **not** give it a name starting with `atlas-`, so the guard doesn't treat it as its own and shut it down.

Prices on the RunPod site are authoritative.

---

## 2. Create a storage volume and API keys on RunPod

### 2.1 Global volume (storage volume)

1. Left menu **Storage** → create a new **Global volume**:
   - any name, e.g. `atlas_data`;
   - set Location to **Any**, i.e. not tied to a data center, so it can be mounted wherever an A100 is available;
   - suggested size **50 GB**. The sample actually uses about 20 GB: the 4C4D environment pack, data and model.
2. Once created, **copy its ID** — you'll need it in step 4.

### 2.2 Two API keys

Left menu **Settings → API Keys → Create API Key**; create two:

| Purpose | Permissions | Where it's stored |
|---|---|---|
| Guard starts / stops pods | **Restricted**, check only `pods:read`, `pods:write`, `volumes:read`, `catalog:read` | `~/.config/atlas-gpu/runpod_api_key` (step 4) |
| runpodctl reads the account balance | **Read Only** | in `runpodctl config` (step 3) |

If the permission names in the dashboard differ from these, choose by meaning: Pods read/write, Volumes read-only, Catalog read-only, everything else unchecked.

> 🔒 Keep the keys only on your own server. **Do not** commit them to git or paste them into chats or issues. The repo's `.gitignore` already excludes common secret file names.

---

## 3. Prepare the server

SSH into your Linux server:

```bash
git clone https://github.com/zexc2/photographer-videographer_killer.git ~/app
cd ~/app
```

Install **runpodctl 2.x**: download the Linux build (usually `linux-amd64`) from https://github.com/runpod/runpodctl/releases, put it at `~/.local/bin/runpodctl`, then:

```bash
chmod +x ~/.local/bin/runpodctl
runpodctl config --apiKey <Read Only key from step 2.2>
runpodctl user          # if you see your account info, it's configured
```

---

## 4. One-step deploy

```bash
cd ~/app
./deploy.sh
```

It will, in order:

- check system tools;
- install Python dependencies (if the system Python lacks Flask, it creates `.venv` in the repo);
- install the GPU guard commands (`~/bin/gpu-up` etc.) and the every-10-minutes monitor;
- start the camera-move workbench and set it to start on boot;
- finally list anything still unfinished.

On the first run, these two items are usually not ready yet; fill them in as prompted:

```bash
# ① Restricted key for the guard (paste it with an editor, not echo, so it doesn't end up in shell history)
install -m 600 /dev/null ~/.config/atlas-gpu/runpod_api_key
nano ~/.config/atlas-gpu/runpod_api_key

# ② Global volume ID: set global_volume_id to the ID copied in step 2.1
nano ~/.config/atlas-gpu/config.json
```

If it says `~/bin` is not on your PATH, add it as instructed. Then **run `./deploy.sh` again** until every item shows ✅.

Finally, do a read-only check — it won't actually start a pod:

```bash
gpu-up --dry-run        # shows the selected A100, price, balance, Global volume
gpu-status              # lists atlas- pods (should be empty now) and the balance
```

---

## 5. Prepare the sample model (one-time, ~4 hours, ~$8)

The workbench needs a trained 4C4D model. This step does the following on the GPU:

1. installs the 4C4D environment and packs it to the Global volume, so later starts just unpack it;
2. downloads the N3V `flame_steak` data;
3. trains the model on 4 cameras;
4. puts the model at `models/n3v_flame_steak/` on the Global volume.

```bash
gpu-up --hours 6                 # start an A100 (max 6 hours; the guard shuts it down when time is up)
atlas-run --detach n3v_repro     # push the job and run it in the background
gpu-status                       # check run time and cost at any time
```

5 minutes after the job finishes, the guard **shuts the pod down automatically** — no need to babysit it. To check progress, use `atlas-run --list` to find the run ID, then `atlas-run --attach <run_id>`.

When it's done, `~/runs/<run_id>/out/` on the server contains the training metrics and the official camera-move video, which you can compare with [our reproduction results](docs/reproduction-n3v.md) (PSNR about 24).

---

## 6. Open the workbench

On **your own computer**:

```bash
ssh -L 8765:127.0.0.1:8765 <username>@<server address>
```

Keep that window open and open **http://localhost:8765** in your browser.

If the workbench runs on your own Linux computer, no tunnel is needed — just open that address.

1. First time: click **"Prepare scene preview"**. This starts a GPU once to export the point cloud, cameras and thumbnails — about 1.5 minutes, $0.04.
2. Pick a base camera, add keyframes to set the camera path, and preview it in "Camera view".
3. Click **"Confirm & render"**. A dialog shows the GPU, estimated time and cost; after you confirm, the video is ready in about 1.5 minutes.

Full usage: [frontend/README.md](frontend/README.md).

---

## Cost overview (A100 SXM, measured 2026-10)

| Operation | Time | Cost |
|---|---|---|
| Prepare sample model (one-time) | about 4 hours | about $8 |
| Prepare scene preview (once per scene) | about 1.5 minutes | about $0.04 |
| Render a 4-second camera move | about 1.5 minutes | about $0.05 |
| Global volume storage | ongoing | at RunPod storage prices; a few dollars per month for 50 GB |

## Guard against runaway spending (automatic)

- Only A100 80GB is allowed, H100 only when none is available; names must start with `atlas-`, at most 1 at a time.
- Every pod has a time limit (default and maximum 6 hours; workbench jobs get 30 minutes) and is shut down automatically when it's reached.
- Shut down automatically after 30 consecutive minutes of GPU idle, and 5 minutes after a job finishes.
- Refuses to start a pod when the balance is below $3.
- Only touches pods whose names start with `atlas-`, never pods from other projects in your account.

Details: [guard/README.md](guard/README.md).

## FAQ

| Symptom | What to do |
|---|---|
| `gpu-up` says nothing is available | A100s are occasionally out of stock in every region; the guard automatically tries H100. If neither is available, try again in a few minutes |
| `gpu-up` says the balance is too low or can't be read | Add credit under Billing; check that `runpodctl user` works |
| A job fails in the page saying there is no model yet | Complete step 5 first |
| You suspect a pod wasn't shut down | Check with `gpu-status`; stop everything with `gpu-down --all` |
| The page won't open | Run `./deploy.sh --check`; look at `~/logs/frontend.log`; make sure the SSH tunnel is still open |
| Guard log | `~/logs/gpu-guard.log` |
