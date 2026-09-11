#!/usr/bin/env bash

# Runs the RK3588 moving-ROI watchdog acceptance test.

set -euo pipefail

TEST_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_SCHEDULE="$TEST_DIR/watchdog-schedule.json"

usage() {
  cat <<'EOF'
Usage:
  run-watchdog-test.sh --ffmpeg PATH [options]

Required:
  --ffmpeg PATH           RK3588 ffmpeg-rockchip binary under test

Options:
  --analysis-ffmpeg PATH  Full FFmpeg for source generation and metrics
                          (default: /usr/bin/ffmpeg)
  --analysis-ffprobe PATH Full FFprobe for exact frame counts
                          (default: /usr/bin/ffprobe)
  --codec NAME            h264, hevc, or all (default: all)
  --schedule PATH         Frozen fake-YOLO schedule
                          (default: watchdog-schedule.json)
  --output-dir PATH       New evidence directory (default: timestamped)
  -h, --help              Show this help

The test is fixed at RK3588, 1920x1080@30, 900 frames, GOP 60 and CBR 4 Mbps.
It refuses an existing output directory and never claims frame-exact ROI
alignment; quality is measured in fixed intersections of the moving boxes.
EOF
}

fail() {
  printf 'ROI watchdog test failed: %s\n' "$1" >&2
  exit 1
}

note() {
  printf '[roi-watchdog] %s\n' "$1"
}

require_file() {
  local path=$1
  local description=$2
  [ -f "$path" ] || fail "$description does not exist: $path"
}

record_command() {
  local argument
  for argument in "$@"; do
    printf '%q ' "$argument" >>"$COMMAND_LOG"
  done
  printf '\n' >>"$COMMAND_LOG"
}

wait_for_pid_exit() {
  local pid=$1
  local attempts=${2:-500}
  local index
  for ((index = 0; index < attempts; index++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
    sleep 0.02
  done
  return 1
}

stop_process() {
  local pid=${1:-}
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    if ! wait_for_pid_exit "$pid" 250; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
  fi
}

DUT_FFMPEG=
ANALYSIS_FFMPEG=/usr/bin/ffmpeg
ANALYSIS_FFPROBE=/usr/bin/ffprobe
CODEC=all
SCHEDULE=$DEFAULT_SCHEDULE
OUTPUT_DIR=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --ffmpeg)
      [ "$#" -ge 2 ] || fail "--ffmpeg requires a value"
      DUT_FFMPEG=$2
      shift 2
      ;;
    --analysis-ffmpeg)
      [ "$#" -ge 2 ] || fail "--analysis-ffmpeg requires a value"
      ANALYSIS_FFMPEG=$2
      shift 2
      ;;
    --analysis-ffprobe)
      [ "$#" -ge 2 ] || fail "--analysis-ffprobe requires a value"
      ANALYSIS_FFPROBE=$2
      shift 2
      ;;
    --codec)
      [ "$#" -ge 2 ] || fail "--codec requires a value"
      CODEC=$2
      shift 2
      ;;
    --schedule)
      [ "$#" -ge 2 ] || fail "--schedule requires a value"
      SCHEDULE=$2
      shift 2
      ;;
    --output-dir)
      [ "$#" -ge 2 ] || fail "--output-dir requires a value"
      OUTPUT_DIR=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "unknown argument: $1"
      ;;
  esac
done

[ -n "$DUT_FFMPEG" ] || fail "--ffmpeg is required"
case "$CODEC" in
  h264)
    CODECS=(h264)
    ;;
  hevc)
    CODECS=(hevc)
    ;;
  all)
    CODECS=(h264 hevc)
    ;;
  *)
    fail "--codec must be h264, hevc, or all"
    ;;
esac

BRIDGE="$TEST_DIR/roi-watchdog-bridge.py"
PRODUCER="$TEST_DIR/fake-yolo-watchdog.py"
ANALYZER="$TEST_DIR/analyze-watchdog.py"
require_file "$DUT_FFMPEG" "ffmpeg-rockchip binary"
require_file "$ANALYSIS_FFMPEG" "analysis FFmpeg"
require_file "$ANALYSIS_FFPROBE" "analysis FFprobe"
require_file "$SCHEDULE" "watchdog schedule"
require_file "$BRIDGE" "ROI watchdog bridge"
require_file "$PRODUCER" "fake YOLO watchdog producer"
require_file "$ANALYZER" "watchdog analyzer"
command -v python3 >/dev/null || fail "python3 is required"
command -v timeout >/dev/null || fail "GNU timeout is required"

compatible_file=
for candidate in \
  /proc/device-tree/compatible \
  /sys/firmware/devicetree/base/compatible; do
  if [ -r "$candidate" ]; then
    compatible_file=$candidate
    break
  fi
done
[ -n "$compatible_file" ] || fail "cannot read the device-tree compatible list"
if ! tr '\0' '\n' <"$compatible_file" | grep -qi 'rk3588'; then
  fail "this acceptance test is restricted to RK3588"
fi

if [ -z "$OUTPUT_DIR" ]; then
  OUTPUT_DIR="$PWD/roi-watchdog-results-$(date -u +%Y%m%dT%H%M%SZ)-$$"
fi
case "$OUTPUT_DIR" in
  *[!A-Za-z0-9_./-]*)
    fail "output directory may contain only letters, digits, _, -, ., and /"
    ;;
esac
[ ! -e "$OUTPUT_DIR" ] || fail "output directory already exists: $OUTPUT_DIR"
mkdir -p -- "$OUTPUT_DIR"
OUTPUT_DIR=$(cd -- "$OUTPUT_DIR" && pwd)
COMMAND_LOG="$OUTPUT_DIR/commands.txt"
: >"$COMMAND_LOG"
cp -- "$SCHEDULE" "$OUTPUT_DIR/schedule.json"
SCHEDULE="$OUTPUT_DIR/schedule.json"
sha256sum "$SCHEDULE" >"$OUTPUT_DIR/schedule.sha256"
sha256sum \
  "$TEST_DIR/run-watchdog-test.sh" \
  "$BRIDGE" \
  "$PRODUCER" \
  "$ANALYZER" \
  "$SCHEDULE" \
  >"$OUTPUT_DIR/test-assets.sha256"

RUNTIME_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/rkmpp-roi-watchdog.XXXXXX")
BRIDGE_PID=
PRODUCER_PID=

cleanup() {
  stop_process "${PRODUCER_PID:-}"
  stop_process "${BRIDGE_PID:-}"
  if [ -n "${RUNTIME_ROOT:-}" ] && [ -d "$RUNTIME_ROOT" ]; then
    rm -rf -- "$RUNTIME_ROOT"
  fi
}
trap cleanup EXIT INT TERM

mapfile -t schedule_values < <(
  python3 - "$SCHEDULE" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    value = json.load(stream)

frame = value["frame"]
phases = value["phases"]
expected = [
    ("warmup-clear", 0, 120),
    ("moving-a", 120, 300),
    ("watchdog-silent", 300, 540),
    ("moving-b", 540, 780),
    ("final-clear", 780, 900),
]
actual = [(item["name"], item["start_frame"], item["end_frame"]) for item in phases]
if actual != expected:
    raise SystemExit("unexpected watchdog phase layout")
if value["schema_version"] != 1:
    raise SystemExit("schema_version must be 1")
if (frame["width"], frame["height"], float(frame["fps"])) != (1920, 1080, 30.0):
    raise SystemExit("watchdog test must use 1920x1080@30")
if value["gop_frames"] != 60 or value["target_bitrate_bps"] != 4_000_000:
    raise SystemExit("watchdog test must use GOP60 and CBR4M")
if value["watchdog"]["timeout_ms"] != 400:
    raise SystemExit("watchdog timeout must be 400 ms")
expected_thresholds = {
    "min_active_absolute_psnr_y_db": 0.25,
    "min_active_vs_neutral_psnr_y_db": 0.5,
    "min_active_ssim_y": 0.0005,
    "max_watchdog_spatial_residual_psnr_y_db": 0.5,
    "max_watchdog_spatial_residual_fraction": 0.20,
    "max_final_clear_spatial_residual_psnr_y_db": 0.5,
    "max_final_clear_spatial_residual_fraction": 0.20,
    "max_cbr_size_delta_ratio": 0.10,
    "max_cbr_target_delta_ratio": 0.10,
}
if value["thresholds"] != expected_thresholds:
    raise SystemExit("watchdog thresholds differ from the frozen contract")

offsets = ((0, 0), (16, 0), (16, 16), (0, 16))
motion = []
for phase_name, base_x in (("moving-a", 64), ("moving-b", 1344)):
    phase = next(item for item in phases if item["name"] == phase_name)
    expected_frames = list(range(phase["start_frame"], phase["end_frame"], 12))
    if [item["frame"] for item in phase["updates"]] != expected_frames:
        raise SystemExit(f"{phase_name} must update every 12 frames")
    for index, update in enumerate(phase["updates"]):
        if len(update["regions"]) != 1:
            raise SystemExit(f"{phase_name} must contain one region")
        region = update["regions"][0]
        expected_x = base_x + offsets[index % 4][0]
        expected_y = 256 + offsets[index % 4][1]
        if (
            region["x"], region["y"], region["w"], region["h"], float(region["qoffset"])
        ) != (expected_x, expected_y, 512, 512, -0.25):
            raise SystemExit(f"{phase_name} update {index} does not match frozen motion")
    motion.append((phase["start_frame"], base_x, 256))

print(frame["width"])
print(frame["height"])
print(frame["fps"])
print(phases[-1]["end_frame"])
print(value["gop_frames"])
print(value["target_bitrate_bps"])
print(value["watchdog"]["timeout_ms"])
print(motion[0][0])
print(motion[0][1])
print(motion[0][2])
print(motion[1][0])
print(motion[1][1])
print(motion[1][2])
PY
)
[ "${#schedule_values[@]}" -eq 13 ] || fail "cannot validate frozen watchdog schedule"
WIDTH=${schedule_values[0]}
HEIGHT=${schedule_values[1]}
FPS=${schedule_values[2]}
EXPECTED_FRAMES=${schedule_values[3]}
GOP_FRAMES=${schedule_values[4]}
TARGET_BITRATE_BPS=${schedule_values[5]}
WATCHDOG_TIMEOUT_MS=${schedule_values[6]}
A_PHASE_START=${schedule_values[7]}
A_BASE_X=${schedule_values[8]}
A_BASE_Y=${schedule_values[9]}
B_PHASE_START=${schedule_values[10]}
B_BASE_X=${schedule_values[11]}
B_BASE_Y=${schedule_values[12]}
TARGET_BUFFER_BITS=$((TARGET_BITRATE_BPS * 2))
DURATION=$(
  python3 - "$EXPECTED_FRAMES" "$FPS" <<'PY'
import sys
print(f"{int(sys.argv[1]) / float(sys.argv[2]):.9f}".rstrip("0").rstrip("."))
PY
)
CASE_TIMEOUT=$(
  python3 - "$DURATION" <<'PY'
import math
import sys
print(math.ceil(float(sys.argv[1])) + 60)
PY
)

note "Running local protocol self-tests before hardware work"
record_command python3 "$PRODUCER" --self-test
PYTHONDONTWRITEBYTECODE=1 python3 "$PRODUCER" --self-test \
  >"$OUTPUT_DIR/producer-self-test.txt" 2>&1
record_command python3 "$BRIDGE" --self-test
PYTHONDONTWRITEBYTECODE=1 python3 "$BRIDGE" --self-test \
  >"$OUTPUT_DIR/bridge-self-test.txt" 2>&1
record_command python3 "$ANALYZER" --self-test
PYTHONDONTWRITEBYTECODE=1 python3 "$ANALYZER" --self-test \
  >"$OUTPUT_DIR/analyzer-self-test.txt" 2>&1

note "Collecting binary and RK3588 host evidence"
record_command "$DUT_FFMPEG" -hide_banner -version
"$DUT_FFMPEG" -hide_banner -version >"$OUTPUT_DIR/dut-version.txt" 2>&1
record_command "$ANALYSIS_FFMPEG" -hide_banner -version
"$ANALYSIS_FFMPEG" -hide_banner -version \
  >"$OUTPUT_DIR/analysis-ffmpeg-version.txt" 2>&1
record_command "$ANALYSIS_FFPROBE" -hide_banner -version
"$ANALYSIS_FFPROBE" -hide_banner -version \
  >"$OUTPUT_DIR/analysis-ffprobe-version.txt" 2>&1
tr '\0' '\n' <"$compatible_file" >"$OUTPUT_DIR/device-compatible.txt"
uname -a >"$OUTPUT_DIR/uname.txt"
date -Ins >"$OUTPUT_DIR/host-date.txt"
uptime >"$OUTPUT_DIR/host-uptime.txt"
free -h >"$OUTPUT_DIR/host-memory.txt"
ps -eo pid,ppid,etime,pcpu,pmem,comm >"$OUTPUT_DIR/host-processes.txt"
if command -v fuser >/dev/null && [ -e /dev/mpp_service ]; then
  fuser /dev/mpp_service >"$OUTPUT_DIR/mpp-clients.txt" 2>&1 || true
fi
sha256sum "$DUT_FFMPEG" >"$OUTPUT_DIR/dut-ffmpeg.sha256"
ldd "$DUT_FFMPEG" >"$OUTPUT_DIR/dut-ffmpeg-ldd.txt" 2>&1 || true

for codec in "${CODECS[@]}"; do
  encoder="${codec}_rkmpp"
  help_file="$OUTPUT_DIR/${encoder}-help.txt"
  record_command "$DUT_FFMPEG" -hide_banner -h "encoder=$encoder"
  "$DUT_FFMPEG" -hide_banner -h "encoder=$encoder" >"$help_file" 2>&1
  grep -Fq 'roi_socket' "$help_file" || fail "$encoder does not expose -roi_socket"
done

"$ANALYSIS_FFMPEG" -hide_banner -filters >"$OUTPUT_DIR/analysis-filters.txt" 2>&1
for filter_name in testsrc2 noise overlay hflip crop psnr ssim; do
  grep -Eq "[[:space:]]${filter_name}[[:space:]]" \
    "$OUTPUT_DIR/analysis-filters.txt" || \
    fail "analysis FFmpeg is missing the $filter_name filter"
done
"$ANALYSIS_FFMPEG" -hide_banner -encoders >"$OUTPUT_DIR/analysis-encoders.txt" 2>&1
grep -Eq '[[:space:]]libx264[[:space:]]' "$OUTPUT_DIR/analysis-encoders.txt" || \
  fail "analysis FFmpeg is missing libx264"

SOURCE="$OUTPUT_DIR/source.mp4"
A_STEP="mod(floor((n-${A_PHASE_START}+9600)/12),4)"
B_STEP="mod(floor((n-${B_PHASE_START}+9600)/12),4)"
A_X="${A_BASE_X}+16*between(${A_STEP},1,2)"
A_Y="${A_BASE_Y}+16*gte(${A_STEP},2)"
B_X="${B_BASE_X}+16*between(${B_STEP},1,2)"
B_Y="${B_BASE_Y}+16*gte(${B_STEP},2)"
SOURCE_FILTER="[0:v]noise=alls=4:allf=t+u:all_seed=101[bg];[1:v]noise=alls=10:allf=t+u:all_seed=202[a];[2:v]hflip,noise=alls=10:allf=t+u:all_seed=303[b];[bg][a]overlay=x='${A_X}':y='${A_Y}':eval=frame:shortest=1[ab];[ab][b]overlay=x='${B_X}':y='${B_Y}':eval=frame:shortest=1[outv]"
source_command=(
  "$ANALYSIS_FFMPEG"
  -hide_banner
  -y
  -f lavfi
  -i "testsrc2=size=${WIDTH}x${HEIGHT}:rate=${FPS}:duration=${DURATION}"
  -f lavfi
  -i "testsrc2=size=512x512:rate=${FPS}:duration=${DURATION}"
  -f lavfi
  -i "testsrc2=size=512x512:rate=${FPS}:duration=${DURATION}"
  -filter_complex "$SOURCE_FILTER"
  -map '[outv]'
  -frames:v "$EXPECTED_FRAMES"
  -c:v libx264
  -preset veryfast
  -crf 18
  -pix_fmt yuv420p
  -g "$GOP_FRAMES"
  -an
  -movflags +faststart
  "$SOURCE"
)
note "Generating frozen moving-target source"
record_command timeout --signal=TERM --kill-after=10s 180s "${source_command[@]}"
set +e
timeout --signal=TERM --kill-after=10s 180s "${source_command[@]}" \
  >"$OUTPUT_DIR/source.stdout" 2>"$OUTPUT_DIR/source.log"
source_rc=$?
set -e
printf 'source_rc=%s\n' "$source_rc" >"$OUTPUT_DIR/source-status.txt"
[ "$source_rc" -eq 0 ] || fail "source generation exited with $source_rc"
source_frames=$(
  "$ANALYSIS_FFPROBE" -v error -count_frames -select_streams v:0 \
    -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "$SOURCE"
)
[ "$source_frames" = "$EXPECTED_FRAMES" ] || \
  fail "source has $source_frames frames; expected $EXPECTED_FRAMES"
sha256sum "$SOURCE" >"$OUTPUT_DIR/source.sha256"

wait_for_bridge_drain() {
  local producer_events=$1
  local bridge_events=$2
  python3 - "$producer_events" "$bridge_events" <<'PY'
import json
import pathlib
import sys
import time

producer_path = pathlib.Path(sys.argv[1])
bridge_path = pathlib.Path(sys.argv[2])
deadline = time.monotonic() + 2.0
last = None
while time.monotonic() < deadline:
    try:
        producer = [json.loads(line) for line in producer_path.read_text().splitlines()]
        bridge = [json.loads(line) for line in bridge_path.read_text().splitlines()]
    except (OSError, json.JSONDecodeError):
        time.sleep(0.01)
        continue
    sends = sum(item.get("event") == "producer_send" and item.get("status") == "sent" for item in producer)
    forwards = sum(item.get("event") == "roi_forward" and item.get("status") == "sent" for item in bridge)
    last = (sends, forwards)
    if sends > 0 and sends == forwards:
        raise SystemExit(0)
    time.sleep(0.01)
print("bridge drain timed out; producer sends/bridge forwards={!r}".format(last), file=sys.stderr)
raise SystemExit(1)
PY
}

run_encode_case() {
  local codec=$1
  local mode=$2
  local force_empty=$3
  local case_dir="$OUTPUT_DIR/$codec/$mode"
  local runtime_dir="$RUNTIME_ROOT/$codec-$mode"
  local listen_dir="$runtime_dir/listen"
  local forward_dir="$runtime_dir/forward"
  local listen_socket="$listen_dir/yolo.roi"
  local forward_socket="$forward_dir/encoder.roi"
  local progress_file="$runtime_dir/progress.txt"
  local producer_events="$case_dir/producer-events.jsonl"
  local bridge_events="$case_dir/bridge-events.jsonl"
  local output="$case_dir/output.bin"
  local encoder="${codec}_rkmpp"
  local producer_rc
  local bridge_rc
  local ffmpeg_rc
  local drain_rc
  local index
  local -a bridge_command
  local -a producer_command
  local -a encode_command

  mkdir -p -- "$case_dir" "$listen_dir" "$forward_dir"
  chmod 0700 "$runtime_dir" "$listen_dir" "$forward_dir"

  bridge_command=(
    python3 "$BRIDGE"
    --listen "$listen_socket"
    --forward "$forward_socket"
    --events "$bridge_events"
    --width "$WIDTH"
    --height "$HEIGHT"
    --timeout-ms "$WATCHDOG_TIMEOUT_MS"
    --poll-ms 10
    --wait-timeout 30
  )
  record_command "${bridge_command[@]}"
  PYTHONDONTWRITEBYTECODE=1 "${bridge_command[@]}" \
    >"$case_dir/bridge.stdout" 2>"$case_dir/bridge.log" &
  BRIDGE_PID=$!
  for ((index = 0; index < 500; index++)); do
    [ -S "$listen_socket" ] && break
    if ! kill -0 "$BRIDGE_PID" 2>/dev/null; then
      wait "$BRIDGE_PID" || true
      BRIDGE_PID=
      fail "$codec $mode bridge exited before binding"
    fi
    sleep 0.01
  done
  [ -S "$listen_socket" ] || fail "$codec $mode bridge did not bind its listen socket"

  producer_command=(
    python3 "$PRODUCER"
    --socket "$listen_socket"
    --progress "$progress_file"
    --schedule "$SCHEDULE"
    --events "$producer_events"
  )
  if [ "$force_empty" = true ]; then
    producer_command+=(--force-empty)
  fi
  record_command "${producer_command[@]}"
  PYTHONDONTWRITEBYTECODE=1 "${producer_command[@]}" \
    >"$case_dir/producer.stdout" 2>"$case_dir/producer.log" &
  PRODUCER_PID=$!

  encode_command=(
    "$DUT_FFMPEG"
    -y
    -nostdin
    -hide_banner
    -loglevel debug
    -stats_period 0.05
    -progress "$progress_file"
    -nostats
    -re
    -i "$SOURCE"
    -map 0:v:0
    -an
    -frames:v "$EXPECTED_FRAMES"
    -c:v "$encoder"
    -rc_mode CBR
    -b:v "$TARGET_BITRATE_BPS"
    -bufsize "$TARGET_BUFFER_BITS"
    -g "$GOP_FRAMES"
    -roi_socket "$forward_socket"
    -f "$codec"
    "$output"
  )
  record_command timeout --signal=TERM --kill-after=10s \
    "${CASE_TIMEOUT}s" "${encode_command[@]}"
  note "Encoding $codec $mode through the watchdog bridge"
  set +e
  timeout --signal=TERM --kill-after=10s "${CASE_TIMEOUT}s" \
    "${encode_command[@]}" \
    >"$case_dir/encode.stdout" 2>"$case_dir/encode.log"
  ffmpeg_rc=$?
  set -e
  printf 'ffmpeg_rc=%s\n' "$ffmpeg_rc" >"$case_dir/status.txt"
  if [ -f "$progress_file" ]; then
    cp -- "$progress_file" "$case_dir/progress.txt"
  fi
  if [ "$ffmpeg_rc" -ne 0 ]; then
    stop_process "$PRODUCER_PID"
    PRODUCER_PID=
    stop_process "$BRIDGE_PID"
    BRIDGE_PID=
    fail "$codec $mode encoder exited with $ffmpeg_rc"
  fi

  if ! wait_for_pid_exit "$PRODUCER_PID" 500; then
    stop_process "$PRODUCER_PID"
    PRODUCER_PID=
    stop_process "$BRIDGE_PID"
    BRIDGE_PID=
    fail "$codec $mode producer did not exit after progress=end"
  fi
  set +e
  wait "$PRODUCER_PID"
  producer_rc=$?
  set -e
  PRODUCER_PID=
  printf 'producer_rc=%s\n' "$producer_rc" >>"$case_dir/status.txt"
  if [ "$producer_rc" -ne 0 ]; then
    stop_process "$BRIDGE_PID"
    BRIDGE_PID=
    fail "$codec $mode producer exited with $producer_rc"
  fi

  set +e
  wait_for_bridge_drain "$producer_events" "$bridge_events" \
    >"$case_dir/bridge-drain.stdout" 2>"$case_dir/bridge-drain.log"
  drain_rc=$?
  set -e
  kill -TERM "$BRIDGE_PID" 2>/dev/null || true
  if ! wait_for_pid_exit "$BRIDGE_PID" 250; then
    kill -KILL "$BRIDGE_PID" 2>/dev/null || true
  fi
  set +e
  wait "$BRIDGE_PID"
  bridge_rc=$?
  set -e
  BRIDGE_PID=
  printf 'bridge_drain_rc=%s\nbridge_rc=%s\n' "$drain_rc" "$bridge_rc" \
    >>"$case_dir/status.txt"
  [ "$drain_rc" -eq 0 ] || fail "$codec $mode bridge did not drain producer updates"
  [ "$bridge_rc" -eq 0 ] || fail "$codec $mode bridge exited with $bridge_rc"

  [ ! -e "$listen_socket" ] || fail "$codec $mode left the bridge listen socket behind"
  [ ! -e "$forward_socket" ] || fail "$codec $mode left the encoder ROI socket behind"
  [ -s "$case_dir/progress.txt" ] || fail "$codec $mode progress evidence is empty"
  [ -s "$producer_events" ] || fail "$codec $mode producer event evidence is empty"
  [ -s "$bridge_events" ] || fail "$codec $mode bridge event evidence is empty"
  [ -s "$output" ] || fail "$codec $mode output bitstream is empty"
  grep -Fq 'Listening for atomic ROI updates on Unix datagram socket' \
    "$case_dir/encode.log" || fail "$codec $mode encoder did not bind roi_socket"
  grep -Fq 'with 0 region(s)' "$case_dir/encode.log" || \
    fail "$codec $mode encoder did not apply an empty set"
  if [ "$force_empty" = false ]; then
    grep -Fq 'with 1 region(s)' "$case_dir/encode.log" || \
      fail "$codec $mode encoder did not apply a one-region set"
  fi
  sha256sum "$output" >"$case_dir/output.sha256"
}

for codec in "${CODECS[@]}"; do
  mkdir -p -- "$OUTPUT_DIR/$codec"
  run_encode_case "$codec" baseline true
  run_encode_case "$codec" dynamic false

  analysis_command=(
    python3 "$ANALYZER"
    --ffmpeg "$ANALYSIS_FFMPEG"
    --ffprobe "$ANALYSIS_FFPROBE"
    --source "$SOURCE"
    --baseline "$OUTPUT_DIR/$codec/baseline/output.bin"
    --dynamic "$OUTPUT_DIR/$codec/dynamic/output.bin"
    --baseline-progress "$OUTPUT_DIR/$codec/baseline/progress.txt"
    --dynamic-progress "$OUTPUT_DIR/$codec/dynamic/progress.txt"
    --baseline-producer-events "$OUTPUT_DIR/$codec/baseline/producer-events.jsonl"
    --dynamic-producer-events "$OUTPUT_DIR/$codec/dynamic/producer-events.jsonl"
    --baseline-bridge-events "$OUTPUT_DIR/$codec/baseline/bridge-events.jsonl"
    --dynamic-bridge-events "$OUTPUT_DIR/$codec/dynamic/bridge-events.jsonl"
    --baseline-encode-log "$OUTPUT_DIR/$codec/baseline/encode.log"
    --dynamic-encode-log "$OUTPUT_DIR/$codec/dynamic/encode.log"
    --format "$codec"
    --schedule "$SCHEDULE"
    --schedule-sha256 "$OUTPUT_DIR/schedule.sha256"
    --output-dir "$OUTPUT_DIR/$codec/analysis"
  )
  record_command timeout --signal=TERM --kill-after=30s 900s "${analysis_command[@]}"
  note "Analyzing $codec watchdog control and spatial effect"
  PYTHONDONTWRITEBYTECODE=1 timeout --signal=TERM --kill-after=30s 900s \
    "${analysis_command[@]}" 2>&1 | \
    tee "$OUTPUT_DIR/$codec/analysis-console.txt"
done

{
  printf 'result=PASS\n'
  printf 'device=RK3588\n'
  printf 'scenario=moving ROI plus 400 ms detector-stall watchdog\n'
  printf 'alignment=fixed intersection probes; frame-exact ROI is not claimed\n'
  printf 'source=%s\n' "$SOURCE"
  printf 'frames=%s\n' "$EXPECTED_FRAMES"
  printf 'target_bitrate_bps=%s\n' "$TARGET_BITRATE_BPS"
  printf 'codecs=%s\n' "${CODECS[*]}"
  for codec in "${CODECS[@]}"; do
    printf '\n[%s]\n' "$codec"
    cat "$OUTPUT_DIR/$codec/analysis/summary.txt"
  done
} >"$OUTPUT_DIR/summary.txt"

note "PASS; evidence saved in $OUTPUT_DIR"
