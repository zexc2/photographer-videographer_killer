#!/usr/bin/env bash
# Run on the first pod start: checks mounts / GPU / disk and measures Global volume and container disk read/write speed.
RUN_DIR=$1
source "$(dirname "$0")/../lib/common.sh"
OUT=$RUN_DIR/out
speed() {  # speed <dir> <size MB>: prints "write_MB/s read_MB/s"
  local f=$1/.atlas_speedtest.bin n=$2 t0 t1 t2
  t0=$(date +%s.%N); dd if=/dev/urandom of="$f" bs=1M count="$n" status=none conv=fsync; t1=$(date +%s.%N)
  sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
  dd if="$f" of=/dev/null bs=4M status=none; t2=$(date +%s.%N)
  rm -f "$f"
  python3 -c "print(f'{$n/($t1-$t0):.0f} {$n/($t2-$t1):.0f}')"
}
read -r vw vr < <(speed "$ATLAS_VOL" 2048)
read -r lw lr < <(speed "$ATLAS_LOCAL" 2048)
# Small files: write 500 x 4KB files to Global volume
t0=$(date +%s.%N); mkdir -p "$ATLAS_VOL/.atlas_small"
for i in $(seq 500); do head -c 4096 /dev/urandom > "$ATLAS_VOL/.atlas_small/$i"; done; t1=$(date +%s.%N)
rm -rf "$ATLAS_VOL/.atlas_small"
small=$(python3 -c "print(f'{500/($t1-$t0):.0f}')")
python3 - "$OUT/metrics.json" <<PY
import json, sys, shutil, subprocess
smi = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"], capture_output=True, text=True).stdout.strip()
d = {
  "atlas_vol": "$ATLAS_VOL", "atlas_vol_is_mount": subprocess.run(["mountpoint", "-q", "$ATLAS_VOL"]).returncode == 0,
  "gpu": smi, "cuda_nvcc": subprocess.run("nvcc --version | tail -1", shell=True, capture_output=True, text=True).stdout.strip(),
  "container_disk_free_gb": round(shutil.disk_usage("$ATLAS_LOCAL").free / 1e9, 1),
  "global_volume_MBps": {"write": $vw, "read": $vr}, "container_disk_MBps": {"write": $lw, "read": $lr},
  "global_volume_small_files_per_s": $small,
  "public_key_in_pid1_env": b"PUBLIC_KEY=" in open("/proc/1/environ", "rb").read(),
}
json.dump(d, open(sys.argv[1], "w"), indent=2, ensure_ascii=False)
print(json.dumps(d, indent=2, ensure_ascii=False))
PY
