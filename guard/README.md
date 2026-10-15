# GPU guard (Stage 1)

Every RunPod pod start, stop, status check and automatic patrol goes through here. The goal is to **never let GPUs burn money for nothing** and **never touch other projects' pods**.

| Command | Purpose |
|---|---|
| `gpu-up [--gpu auto] [--image IMG] [--hours 6] [--name xxx] [--disk 200] [--dry-run]` | Start a pod |
| `gpu-down <name or ID> [--delete] [--dry-run]` / `gpu-down --all` | Stop (or delete) atlas- pods |
| `gpu-status [-v]` | List atlas- pods: uptime, estimated cost, time until auto-stop, GPU utilization, idle time; `-v` shows the SSH command |
| `gpu-guard [--dry-run] [-v]` | Patrol, run by cron every 10 minutes |

All four commands are symlinks to `atlas_gpu.py`, installed with `install.sh` (idempotent). Installation also generates a server-dedicated SSH key `~/.ssh/atlas_runpod`.

## Rules (hard-coded)

- **GPU whitelist**: A100 80GB (SXM / PCIe). H100 (SXM / PCIe / NVL) is only allowed when both A100 types are out of stock in **all regions**, or all fail to start. Every other model is rejected.
- **No region lock**: pods are created without specifying a data center, so RunPod schedules them in any region with stock. Secure Cloud only.
- **Name**: forced `atlas-` prefix; gpu-down / gpu-guard only ever act on `atlas-` prefixed pods (covered by tests).
- **Count**: at most 1 billed atlas- pod at a time (PROVISIONING / STARTING / RUNNING all count).
- **Storage**: mounts the Global volume `atlas_data`. The in-pod path is set by the variable `ATLAS_VOL_MOUNT` (default `/workspace`) and passed into the pod as the env var `$ATLAS_VOL`; the container disk root is `$ATLAS_LOCAL=/opt/atlas`, 200 GB by default. See [docs/storage-and-env.md](../docs/storage-and-env.md).
- **Duration**: `--hours` defaults to 6, max 6; the patrol stops the pod when time is up.
- **Balance**: before starting a pod the account balance is read with `runpodctl user` (the v2 API has no balance endpoint); **if the balance is < $3 or unreadable, starting a pod is refused** (Joey's decision 2026-10-14, previously $15), because the Global volume is permanently deleted 15 days after the balance hits zero. If the balance would drop below $3 after running the full duration, a warning is shown. `gpu-status` also shows the balance.

## API key

The Global volume can only be mounted through the **RunPod REST API v2**; runpodctl 2.15 does not support it. So the guard uses a **separate restricted key**:

1. RunPod console → Settings → API Keys → Create API Key, suggested name `atlas-gpu-guard`, permission **Restricted**, and only tick:
   - `pods:read`, `pods:write` (list, start, stop pods)
   - `volumes:read` (find the Global volume by name)
   - `catalog:read` (query GPU stock and prices)

   If the console shows different permission names, choose by meaning: Pods read/write, Volumes read-only, Catalog read-only, nothing else.
2. Save it on the server **yourself** (do not paste it into the chat):
   ```bash
   install -m 600 /dev/null ~/.config/atlas-gpu/runpod_api_key
   nano ~/.config/atlas-gpu/runpod_api_key   # paste the key, save
   ```

The script only reads this file when sending a request, never prints it or writes it to logs, and refuses to use it if its permissions are wider than 600. This script never touches runpodctl's key in `~/.runpod`.

Without this key:
- `gpu-up --dry-run` still previews (stock comes from runpodctl)
- Actually starting a pod is refused
- The patrol falls back to runpodctl and can still list pods and stop them on the time limit

## Patrol logic (gpu-guard)

0. **Auto-stop when runs finish** (added 2026-10-14):
   - When `atlas-run` starts a run, it registers the run ID in the guard's state file.
   - When the patrol finds that all registered runs have finished (done or failed) and the last one finished at least 5 minutes ago (leaving time for a follow-up run), it runs pre_stop and then stops the pod.
   - This way stopping the pod does not depend on Claude's session still being alive. On 2026-10-14 the session dropped and the pod sat idle for about 35 minutes after the run finished until the time limit stopped it; that is why this exists.
   - Use `atlas-run --keep-alive` if you do not want the auto-stop.

1. **Time limit**: current run time ≥ configured limit → stop. This step only relies on the pod list, not SSH; on API failure it automatically switches to runpodctl. So in the worst case a pod runs for at most 6 hours.
2. **Idle**:
   - First check the live GPU utilization `runtime.gpus[].util` returned by API v2. **≥ 5%** counts as busy, no SSH needed. The threshold used to be > 0, but leftover processes report 1–2% and reset the idle timer; on 2026-10-14 this prevented an idle stop in practice.
   - If it is 0 or unavailable, SSH into the pod to double-check: `nvidia-smi` is sampled 5 times over 5 seconds and the maximum is taken, and `$ATLAS_LOCAL/.atlas_busy` is checked. If that marker was touched within the last 15 minutes the pod is not idle, because GPU utilization is naturally 0 while downloading data, unpacking environments or doing CPU preprocessing.
   - Below 5% for **≥ 30 consecutive minutes** → stop.
   - When nothing can be measured: not counted as idle within 30 minutes of boot; after that the idle timer runs (an unreachable pod is still costing money).
3. **Sync before stop**: the container disk is wiped on stop, so before stopping the guard makes a best-effort SSH call to `$ATLAS_LOCAL/bin/pre_stop` (provided by the Stage 2 task runner, up to 10 minutes) to sync results back to the Global volume. It stops anyway if that fails.
4. atlas- pods not started via gpu-up are managed too, with the 6-hour limit.
5. Logs go to `~/logs/gpu-guard.log`.

### Idle detection options

| Option | Notes |
|---|---|
| A. RunPod API utilization only | No SSH needed; but it is a single snapshot and cannot see the busy marker |
| B. SSH + nvidia-smi only | Most direct data; but depends on SSH being reachable |
| **Chosen: A first, B to double-check** | When the API says "busy", trust it (saves an SSH call); when the API says "idle" or has no data, an SSH check is required before the idle timer starts, so a single snapshot cannot kill a run that is downloading or preprocessing |

> Stage 1 originally only used B, because runpodctl cannot report utilization. After switching to the v2 API both methods are available.

## SSH

Pod SSH public keys are injected via the env var `PUBLIC_KEY`: this server's dedicated `~/.ssh/atlas_runpod.pub` plus the public keys already registered on the RunPod account (Joey's own computer can still log in). The account's SSH key settings are not changed.

## Tests

```bash
guard/tests/run_tests.sh   # fake API v2 + fake runpodctl / ssh, 77 test cases, never touches the real account
gpu-up --dry-run           # read-only preview against the real account, does not start a pod
```

**Not yet verified on a real pod** (check on the first real pod start):
- Fields actually returned by the v2 API: `runtime.gpus[].util`, `ssh.direct.command`, `startedAt`, `cost`
- Whether `PUBLIC_KEY` injection works
- Whether a 200 GB container disk can be started in every region

## Runtime files (none of them in the repo)

- `~/.config/atlas-gpu/runpod_api_key` (600): restricted API key
- `~/.config/atlas-gpu/config.json` (**required**): `{"global_volume_id": "...", "global_volume_label": "..."}`. The Global volume is identified by ID only; the name is for display only
- `~/.ssh/atlas_runpod`: pod SSH private key
- `~/.local/state/atlas-gpu/<podID>.json`: time limit, idle timer
- `~/logs/gpu-guard.log`, `~/logs/gpu-guard.cron.log`
