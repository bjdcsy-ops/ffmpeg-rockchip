#!/usr/bin/env bash

# Runs a controlled RK3588 A/B test for runtime RKMPP ROI updates.

set -euo pipefail

TEST_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_SCHEDULE="$TEST_DIR/effect-schedule.json"

usage() {
  cat <<'EOF'
Usage:
  run-effect-test.sh --ffmpeg PATH [options]

Required:
  --ffmpeg PATH           RK3588 ffmpeg-rockchip binary under test

Options:
  --analysis-ffmpeg PATH  Full FFmpeg used for source generation and metrics
                          (default: /usr/bin/ffmpeg)
  --analysis-ffprobe PATH Full FFprobe used for frame counts
                          (default: /usr/bin/ffprobe)
  --codec NAME            h264, hevc, or all (default: all)
  --schedule PATH         Fake-YOLO schedule (default: effect-schedule.json)
  --output-dir PATH       New evidence directory (default: timestamped path)
  -h, --help              Show this help

The script refuses an existing output directory and runs only on RK3588.
EOF
}

fail() {
  printf 'ROI effect test failed: %s\n' "$1" >&2
  exit 1
}

note() {
  printf '[roi-effect] %s\n' "$1"
}

record_command() {
  local argument
  for argument in "$@"; do
    printf '%q ' "$argument" >>"$COMMAND_LOG"
  done
  printf '\n' >>"$COMMAND_LOG"
}

require_file() {
  local path=$1
  local description=$2
  [ -f "$path" ] || fail "$description does not exist: $path"
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

require_file "$DUT_FFMPEG" "ffmpeg-rockchip binary"
require_file "$ANALYSIS_FFMPEG" "analysis FFmpeg"
require_file "$ANALYSIS_FFPROBE" "analysis FFprobe"
require_file "$SCHEDULE" "schedule"
require_file "$TEST_DIR/fake-yolo.py" "fake YOLO sender"
require_file "$TEST_DIR/analyze.py" "quality analyzer"
command -v python3 >/dev/null || fail "python3 is required"
command -v timeout >/dev/null || fail "GNU timeout is required"
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$TEST_DIR/fake-yolo.py" --validate-schedule "$SCHEDULE" \
  >/dev/null || fail "fake YOLO schedule validation failed"

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
  OUTPUT_DIR="$PWD/roi-effect-results-$(date -u +%Y%m%dT%H%M%SZ)-$$"
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
  "$TEST_DIR/run-effect-test.sh" \
  "$TEST_DIR/fake-yolo.py" \
  "$TEST_DIR/analyze.py" \
  "$SCHEDULE" \
  >"$OUTPUT_DIR/test-assets.sha256"

RUNTIME_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/rkmpp-roi-effect.XXXXXX")
SENDER_PID=

cleanup() {
  if [ -n "${SENDER_PID:-}" ] && kill -0 "$SENDER_PID" 2>/dev/null; then
    kill "$SENDER_PID" 2>/dev/null || true
    wait "$SENDER_PID" 2>/dev/null || true
  fi
  rm -rf -- "$RUNTIME_ROOT"
}
trap cleanup EXIT INT TERM

mapfile -t schedule_values < <(
  python3 - "$SCHEDULE" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8") as source:
    schedule = json.load(source)
frame = schedule["frame"]
phases = schedule["phases"]
print(frame["width"])
print(frame["height"])
print(frame["fps"])
print(phases[-1]["end_frame"])
print(schedule.get("gop_frames", 60))
print(schedule["target_bitrate_bps"])
PY
)
[ "${#schedule_values[@]}" -eq 6 ] || fail "cannot read schedule geometry"
WIDTH=${schedule_values[0]}
HEIGHT=${schedule_values[1]}
FPS=${schedule_values[2]}
EXPECTED_FRAMES=${schedule_values[3]}
GOP_FRAMES=${schedule_values[4]}
TARGET_BITRATE_BPS=${schedule_values[5]}
TARGET_BUFFER_BITS=$((TARGET_BITRATE_BPS * 2))
DURATION=$(
  python3 - "$EXPECTED_FRAMES" "$FPS" <<'PY'
import sys
print(f"{int(sys.argv[1]) / float(sys.argv[2]):.9f}".rstrip("0").rstrip("."))
PY
)
CASE_TIMEOUT=$(
  python3 - "$DURATION" <<'PY'
import math, sys
print(math.ceil(float(sys.argv[1])) + 60)
PY
)

note "Collecting binary and host evidence"
record_command "$DUT_FFMPEG" -hide_banner -version
"$DUT_FFMPEG" -hide_banner -version \
  >"$OUTPUT_DIR/dut-version.txt" 2>&1
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
  grep -Fq 'roi_socket' "$help_file" || \
    fail "$encoder does not expose -roi_socket"
done

"$ANALYSIS_FFMPEG" -hide_banner -filters \
  >"$OUTPUT_DIR/analysis-filters.txt" 2>&1
for filter_name in testsrc2 noise drawbox crop psnr ssim; do
  grep -Eq "[[:space:]]${filter_name}[[:space:]]" \
    "$OUTPUT_DIR/analysis-filters.txt" || \
    fail "analysis FFmpeg is missing the $filter_name filter"
done
"$ANALYSIS_FFMPEG" -hide_banner -encoders \
  >"$OUTPUT_DIR/analysis-encoders.txt" 2>&1
grep -Eq '[[:space:]]libx264[[:space:]]' \
  "$OUTPUT_DIR/analysis-encoders.txt" || \
  fail "analysis FFmpeg is missing libx264"

SOURCE="$OUTPUT_DIR/source.mp4"
note "Generating frozen ${WIDTH}x${HEIGHT}@${FPS} reference source"
source_command=(
  "$ANALYSIS_FFMPEG"
  -hide_banner
  -y
  -f lavfi
  -i "testsrc2=size=${WIDTH}x${HEIGHT}:rate=${FPS}:duration=${DURATION}"
  -vf 'noise=alls=4:allf=t+u,drawbox=x=80:y=100:w=320:h=220:color=white@0.8:t=fill,drawbox=x=1100:y=650:w=240:h=180:color=red@0.8:t=fill'
  -frames:v "$EXPECTED_FRAMES"
  -c:v libx264
  -preset veryfast
  -crf 20
  -pix_fmt yuv420p
  -g "$GOP_FRAMES"
  -an
  -movflags +faststart
  "$SOURCE"
)
record_command "${source_command[@]}"
"${source_command[@]}" \
  >"$OUTPUT_DIR/source.stdout" 2>"$OUTPUT_DIR/source.log"

source_frames=$(
  "$ANALYSIS_FFPROBE" -v error -count_frames -select_streams v:0 \
    -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "$SOURCE"
)
[ "$source_frames" = "$EXPECTED_FRAMES" ] || \
  fail "source has $source_frames frames; expected $EXPECTED_FRAMES"
sha256sum "$SOURCE" >"$OUTPUT_DIR/source.sha256"

run_encode_case() {
  local codec=$1
  local mode=$2
  local force_empty=$3
  local case_dir="$OUTPUT_DIR/$codec/$mode"
  local runtime_dir="$RUNTIME_ROOT/$codec-$mode"
  local socket_dir="$runtime_dir/socket"
  local socket_path="$socket_dir/encoder.roi"
  local progress_file="$runtime_dir/progress.txt"
  local events="$case_dir/events.jsonl"
  local output="$case_dir/output.bin"
  local encoder="${codec}_rkmpp"
  local sender_rc
  local ffmpeg_rc
  local -a sender_command
  local -a encode_command

  mkdir -p -- "$case_dir" "$socket_dir"
  chmod 0700 "$runtime_dir" "$socket_dir"

  sender_command=(
    python3 "$TEST_DIR/fake-yolo.py"
    --socket "$socket_path"
    --schedule "$SCHEDULE"
    --progress "$progress_file"
    --events "$events"
  )
  if [ "$force_empty" = true ]; then
    sender_command+=(--force-empty)
  fi
  record_command "${sender_command[@]}"
  "${sender_command[@]}" \
    >"$case_dir/sender.stdout" 2>"$case_dir/sender.log" &
  SENDER_PID=$!

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
    -roi_socket "$socket_path"
    -f "$codec"
    "$output"
  )
  record_command timeout --signal=TERM --kill-after=10s \
    "${CASE_TIMEOUT}s" "${encode_command[@]}"
  note "Encoding $codec $mode with progress-driven fake YOLO"
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
    kill "$SENDER_PID" 2>/dev/null || true
    wait "$SENDER_PID" 2>/dev/null || true
    SENDER_PID=
    fail "$codec $mode encoder exited with $ffmpeg_rc"
  fi

  set +e
  wait "$SENDER_PID"
  sender_rc=$?
  set -e
  SENDER_PID=
  printf 'sender_rc=%s\n' "$sender_rc" >>"$case_dir/status.txt"
  [ "$sender_rc" -eq 0 ] || fail "$codec $mode sender exited with $sender_rc"
  [ ! -e "$socket_path" ] || fail "$codec $mode left its ROI socket behind"
  [ -s "$events" ] || fail "$codec $mode sender produced no event evidence"
  [ -s "$output" ] || fail "$codec $mode produced an empty bitstream"
  grep -Fq 'Listening for atomic ROI updates on Unix datagram socket' \
    "$case_dir/encode.log" || fail "$codec $mode did not bind the ROI socket"
  grep -Fq 'Applied ROI socket update #' "$case_dir/encode.log" || \
    fail "$codec $mode did not apply any ROI update"
  grep -Fq 'with 0 region(s)' "$case_dir/encode.log" || \
    fail "$codec $mode did not apply an empty ROI set"
  if [ "$force_empty" = false ]; then
    grep -Fq 'with 2 region(s)' "$case_dir/encode.log" || \
      fail "$codec $mode did not apply the active two-region set"
  fi
}

decode_and_count() {
  local codec=$1
  local mode=$2
  local case_dir="$OUTPUT_DIR/$codec/$mode"
  local output="$case_dir/output.bin"
  local frame_count
  local -a decode_command

  decode_command=(
    "$ANALYSIS_FFMPEG"
    -nostdin
    -hide_banner
    -loglevel warning
    -xerror
    -err_detect explode
    -f "$codec"
    -i "$output"
    -map 0:v:0
    -f null
    -
  )
  record_command "${decode_command[@]}"
  "${decode_command[@]}" \
    >"$case_dir/decode.stdout" 2>"$case_dir/decode.log"

  frame_count=$(
    "$ANALYSIS_FFPROBE" -v error -f "$codec" -count_frames \
      -select_streams v:0 -show_entries stream=nb_read_frames \
      -of default=nw=1:nk=1 "$output"
  )
  printf '%s\n' "$frame_count" >"$case_dir/frame-count.txt"
  [ "$frame_count" = "$EXPECTED_FRAMES" ] || \
    fail "$codec $mode decoded $frame_count frames; expected $EXPECTED_FRAMES"
  sha256sum "$output" >"$case_dir/output.sha256"
}

for codec in "${CODECS[@]}"; do
  mkdir -p -- "$OUTPUT_DIR/$codec"
  run_encode_case "$codec" baseline true
  run_encode_case "$codec" dynamic false
  decode_and_count "$codec" baseline
  decode_and_count "$codec" dynamic

  analysis_command=(
    python3 "$TEST_DIR/analyze.py"
    --ffmpeg "$ANALYSIS_FFMPEG"
    --source "$SOURCE"
    --baseline "$OUTPUT_DIR/$codec/baseline/output.bin"
    --dynamic "$OUTPUT_DIR/$codec/dynamic/output.bin"
    --baseline-events "$OUTPUT_DIR/$codec/baseline/events.jsonl"
    --dynamic-events "$OUTPUT_DIR/$codec/dynamic/events.jsonl"
    --format "$codec"
    --schedule "$SCHEDULE"
    --output-dir "$OUTPUT_DIR/$codec/analysis"
  )
  record_command "${analysis_command[@]}"
  note "Analyzing $codec spatial quality deltas"
  "${analysis_command[@]}" 2>&1 | \
    tee "$OUTPUT_DIR/$codec/analysis-console.txt"
done

{
  printf 'result=PASS\n'
  printf 'device=RK3588\n'
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
