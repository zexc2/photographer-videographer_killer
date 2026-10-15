# Storage and environment plan (decided 2026-10-13)

## 1. Why we switched to a Global volume

A Network volume is tied to one data center. After a pod is stopped, that data center often has no A100 left, so we can't start one.
A [Global volume](https://docs.runpod.io/storage/globalvolume) isn't tied to a data center and can be mounted by GPU pods in any region, so `gpu-up` can search for A100s in **all regions**.

Key points verified from the official docs (2026-10; Global volume is still in **beta**):

| Item | Details |
|---|---|
| Backend | Object storage, **not full POSIX**: no file locks, non-atomic rename, no hard links, no permission bits; eventually consistent; when two pods write the same file, the last write wins |
| Good for | Read-heavy, write-light data, e.g. model weights, raw datasets |
| Not good for | Frequent writes: training, checkpoints, conda environments, many small files |
| Mount path | Defaults to `/workspace` when only Global is mounted; becomes `/workspace-global` when a network volume is also mounted. We **set the path explicitly** when creating the pod (default `/workspace`, changeable via `ATLAS_VOL_MOUNT`) and pass it into the pod as the environment variable **`$ATLAS_VOL`**; scripts on the pod use only this variable |
| Billing | Billed by usage (no preset capacity); plus request fees: writes/listings $0.005 per 1,000, reads $0.0005 per 1,000. The per-GB storage price isn't in the docs; the bill is authoritative |
| ⚠️ Balance | **When the account balance hits zero, the volume is flagged and permanently deleted after 15 days.** Be sure to turn on low-balance alerts |
| API | Only REST API v2 supports it (`mounts.global[].volumeId`); runpodctl 2.15 doesn't → the guard uses the v2 API instead |

## 2. Where data lives

| Location | Environment variable | What goes there | Lifetime |
|---|---|---|---|
| Global volume | `$ATLAS_VOL` (default `/workspace`) | **Cold data only**: `weights/` model weights, `datasets/panoptic/` raw data, `envpacks/` environment archives (see option C below), `results/<run>/` final results and reports | Permanent |
| Container disk | `$ATLAS_LOCAL` (`/opt/atlas`) | `envs/` conda environments, `src/` code repos, `work/<run>/` working directories, intermediate files, checkpoints, HF download cache, the `.atlas_busy` idle marker, `bin/pre_stop` | **Wiped when the pod stops** |

Rules:
- Before a job ends, the job runner syncs the results to keep back to `$ATLAS_VOL/results/<run>/`.
- Before the monitor stops a pod, it first makes a best effort to run `$ATLAS_LOCAL/bin/pre_stop` (up to 10 minutes) to sync results back.
- When writing to the Global volume: pack first, prefer a few large files over many small ones; use `rsync -rt --inplace --no-perms --no-owner --no-group` (avoids relying on atomic rename and permission bits).
- Hugging Face downloads use file locks and permission bits, so download to the container disk first, then copy everything to `$ATLAS_VOL/weights/`.

**Suggested container disk: 200 GB** (the `gpu-up --disk` default). Estimated usage per session:

| Item | Size |
|---|---|
| 1–2 conda environments | 15–35 GB |
| Copying weights to local NVMe for speed, if needed | 0–80 GB |
| Data | 10–20 GB |
| Working directories, intermediate files, checkpoints | 30–60 GB |

The container disk is billed at $0.10/GB/month; 200 GB is about $0.03/hour, which is negligible. If some machines can't provide 200 GB, it can be reduced to 120 GB.

## 3. conda environments: options compared

The install times below are **estimates** based on each project's dependencies (PyTorch CUDA extensions, transformer-engine, flash-attn, the 3DGS rasterizer, MASt3R's curope, etc.), to be updated with measurements after the first install in stage 2. GPU priced at A100 SXM $1.79/h.

| | A) Custom Docker image (GHCR / Docker Hub) | B) Install on the container disk at every start | **C) Recommended: install once, conda-pack it to the Global volume** |
|---|---|---|---|
| How | One image per model (dependencies only, no third-party code or weights), built with GitHub Actions and pushed to GHCR | Run the setup script every time a pod starts | First time = B; after installing, pack each environment with `conda-pack` into **one large file** `envpacks/<env>.tar.zst` on the Global volume; later starts just unpack it to the container disk |
| First-time effort | 0.5–1 day to write and debug Dockerfiles; GitHub's free runners have no GPU and only about 14 GB of disk left, requiring manual cleanup, and compiling flash-attn may approach the 6-hour job limit | 0 | Same as B: GEN3C 40–70 minutes, ViPE 15–25 minutes, 4C4D+MASt3R 15–30 minutes; plus about 5 minutes to pack |
| First-time cost | $0 GPU (CI is free), high labor cost | — | One-time about $1.2–3.7 (all three installed) |
| Time per start | Pull a 15–30 GB image, about 5–15 minutes | **40–125 minutes** | Read and unpack 5–10 GB, about 3–8 minutes (Global volume read speed to be measured the first time) |
| Cost per start | About $0.15–0.45 (whether image pulls are billed needs to be measured) | **About $1.2–3.7**, and it eats into the 6-hour limit | About $0.1–0.25 |
| Stability | High; the environment is fully pinned | Low; upstream pip packages change, so any install can fail | High; the environment is fixed once packed |
| Changing the environment | Edit the Dockerfile, rebuild, push | Edit the script | Edit the script, reinstall once, repack |
| Other | Public images must not contain third-party code; private images need registry credentials configured in RunPod | — | Can't use `pip install -e` (conda-pack doesn't support it); code goes in `$ATLAS_LOCAL/src` via `PYTHONPATH`; CUDA extensions compiled for `sm_80;sm_90`, so they work on both A100 and H100; base image version pinned |

**We recommend C**, with B as its automatic fallback: the setup script first looks for an archive in `envpacks/`; if there is one, it unpacks it, otherwise it installs and packs. Reasons:
- Each start costs only a few extra minutes.
- No CI to set up.
- No public distribution of third-party content.
- What's stored on the Global volume is one large file, which fits object storage's "read-heavy, write-light, large files" profile.

If we later need faster startup or reproducible releases, the same setup scripts can go straight into a Dockerfile to upgrade to option A.
