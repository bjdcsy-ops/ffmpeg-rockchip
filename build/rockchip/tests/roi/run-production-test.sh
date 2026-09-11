#!/usr/bin/env bash

# Runs the RK3588 all-negative q15/q25 0/1/2/8 ROI production matrix.

set -euo pipefail

TEST_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
DEFAULT_Q15_SCHEDULE="$TEST_DIR/production-q15-schedule.json"
DEFAULT_Q25_SCHEDULE="$TEST_DIR/production-q25-schedule.json"

usage() {
  cat <<'EOF'
Usage:
  run-production-test.sh --ffmpeg PATH [options]

Required:
  --ffmpeg PATH           RK3588 ffmpeg-rockchip binary under test

Options:
  --analysis-ffmpeg PATH  Full FFmpeg for source generation and metrics
                          (default: /usr/bin/ffmpeg)
  --analysis-ffprobe PATH Full FFprobe for frame counts
                          (default: /usr/bin/ffprobe)
  --codec NAME            h264, hevc, or all (default: all)
  --q15-schedule PATH     qoffset -0.15 schedule
  --q25-schedule PATH     qoffset -0.25 schedule
  --output-dir PATH       New evidence directory (default: timestamped path)
  -h, --help              Show this help

The script refuses an existing output directory and runs only on RK3588.
For each codec it encodes one force-empty baseline plus q15 and q25 variants.
EOF
}

fail() {
  printf 'ROI production test failed: %s\n' "$1" >&2
  exit 1
}

note() {
  printf '[roi-production] %s\n' "$1"
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
Q15_SCHEDULE=$DEFAULT_Q15_SCHEDULE
Q25_SCHEDULE=$DEFAULT_Q25_SCHEDULE
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
    --q15-schedule)
      [ "$#" -ge 2 ] || fail "--q15-schedule requires a value"
      Q15_SCHEDULE=$2
      shift 2
      ;;
    --q25-schedule)
      [ "$#" -ge 2 ] || fail "--q25-schedule requires a value"
      Q25_SCHEDULE=$2
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
require_file "$Q15_SCHEDULE" "q15 schedule"
require_file "$Q25_SCHEDULE" "q25 schedule"
require_file "$TEST_DIR/fake-yolo.py" "fake YOLO sender"
require_file "$TEST_DIR/analyze.py" "shared fixed-probe analyzer"
require_file "$TEST_DIR/analyze-production.py" "production analyzer"
command -v python3 >/dev/null || fail "python3 is required"
command -v timeout >/dev/null || fail "GNU timeout is required"

PYTHONDONTWRITEBYTECODE=1 \
  python3 "$TEST_DIR/fake-yolo.py" --validate-schedule "$Q15_SCHEDULE" \
  >/dev/null || fail "q15 fake YOLO schedule validation failed"
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$TEST_DIR/fake-yolo.py" --validate-schedule "$Q25_SCHEDULE" \
  >/dev/null || fail "q25 fake YOLO schedule validation failed"
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$TEST_DIR/analyze-production.py" --self-test \
  >/dev/null || fail "production analyzer self-test failed"
PYTHONDONTWRITEBYTECODE=1 \
  python3 "$TEST_DIR/analyze-production.py" \
  --validate-schedules "$Q15_SCHEDULE" "$Q25_SCHEDULE" \
  >/dev/null || fail "production schedule-pair validation failed"

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
  OUTPUT_DIR="$PWD/roi-production-results-$(date -u +%Y%m%dT%H%M%SZ)-$$"
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

cp -- "$Q15_SCHEDULE" "$OUTPUT_DIR/production-q15-schedule.json"
cp -- "$Q25_SCHEDULE" "$OUTPUT_DIR/production-q25-schedule.json"
Q15_SCHEDULE="$OUTPUT_DIR/production-q15-schedule.json"
Q25_SCHEDULE="$OUTPUT_DIR/production-q25-schedule.json"
sha256sum "$Q15_SCHEDULE" "$Q25_SCHEDULE" >"$OUTPUT_DIR/schedules.sha256"
sha256sum \
  "$TEST_DIR/run-production-test.sh" \
  "$TEST_DIR/fake-yolo.py" \
  "$TEST_DIR/analyze.py" \
  "$TEST_DIR/analyze-production.py" \
  "$Q15_SCHEDULE" \
  "$Q25_SCHEDULE" \
  >"$OUTPUT_DIR/test-assets.sha256"

mapfile -t schedule_values < <(
  python3 - "$Q15_SCHEDULE" <<'PY'
import json, sys

with open(sys.argv[1], encoding="utf-8") as source:
    schedule = json.load(source)
frame = schedule["frame"]
print(frame["width"])
print(frame["height"])
print(frame["fps"])
print(schedule["phases"][-1]["end_frame"])
print(schedule["gop_frames"])
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

RUNTIME_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/rkmpp-roi-production.XXXXXX")
SENDER_PID=

wait_sender_bounded() {
  local pid=$1
  local iteration

  for ((iteration = 0; iteration < 100; iteration += 1)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid"
      return $?
    fi
    sleep 0.1
  done
  kill -TERM "$pid" 2>/dev/null || true
  for ((iteration = 0; iteration < 20; iteration += 1)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    sleep 0.1
  done
  kill -KILL "$pid" 2>/dev/null || true
  wait "$pid" 2>/dev/null || true
  return 124
}

cleanup() {
  if [ -n "${SENDER_PID:-}" ] && kill -0 "$SENDER_PID" 2>/dev/null; then
    kill -TERM "$SENDER_PID" 2>/dev/null || true
    wait_sender_bounded "$SENDER_PID" >/dev/null 2>&1 || true
  fi
  rm -rf -- "$RUNTIME_ROOT"
}
trap cleanup EXIT INT TERM

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
SOURCE_FILTER='noise=alls=5:allf=t+u:all_seed=12345,drawbox=x=128:y=256:w=384:h=384:color=white@0.7:t=6,drawbox=x=176:y=304:w=80:h=80:color=red@0.8:t=fill,drawbox=x=400:y=528:w=64:h=64:color=blue@0.8:t=fill,drawbox=x=144:y=160:w=192:h=384:color=yellow@0.6:t=5,drawbox=x=1088:y=496:w=192:h=384:color=yellow@0.6:t=5,drawbox=x=112:y=128:w=48:h=80:color=red@0.8:t=fill,drawbox=x=400:y=192:w=48:h=80:color=blue@0.8:t=fill,drawbox=x=720:y=128:w=48:h=80:color=green@0.8:t=fill,drawbox=x=1040:y=208:w=48:h=80:color=magenta@0.8:t=fill,drawbox=x=1360:y=128:w=48:h=80:color=cyan@0.8:t=fill,drawbox=x=208:y=752:w=48:h=80:color=yellow@0.8:t=fill,drawbox=x=624:y=736:w=48:h=80:color=red@0.8:t=fill,drawbox=x=1136:y=752:w=48:h=80:color=blue@0.8:t=fill,drawbox=x=1536:y=688:w=320:h=320:color=white@0.7:t=6,drawbox=x=1584:y=736:w=80:h=80:color=red@0.8:t=fill,drawbox=x=1760:y=912:w=64:h=64:color=blue@0.8:t=fill'
note "Generating deterministic ${WIDTH}x${HEIGHT}@${FPS} production reference"
source_command=(
  "$ANALYSIS_FFMPEG"
  -hide_banner
  -y
  -f lavfi
  -i "testsrc2=size=${WIDTH}x${HEIGHT}:rate=${FPS}:duration=${DURATION}"
  -vf "$SOURCE_FILTER"
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
record_command timeout --signal=TERM --kill-after=10s 180s \
  "${source_command[@]}"
if ! timeout --signal=TERM --kill-after=10s 180s \
  "${source_command[@]}" \
  >"$OUTPUT_DIR/source.stdout" 2>"$OUTPUT_DIR/source.log"; then
  fail "reference source generation failed or exceeded 180 seconds"
fi

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
  local schedule=$3
  local force_empty=$4
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
  local expected_count
  local -a expected_counts
  local -a sender_command
  local -a encode_command

  mkdir -p -- "$case_dir" "$socket_dir"
  chmod 0700 "$runtime_dir" "$socket_dir"
  sender_command=(
    python3 "$TEST_DIR/fake-yolo.py"
    --socket "$socket_path"
    --schedule "$schedule"
    --progress "$progress_file"
    --events "$events"
  )
  if [ "$force_empty" = true ]; then
    sender_command+=(--force-empty)
  fi
  record_command "${sender_command[@]}"
  PYTHONDONTWRITEBYTECODE=1 "${sender_command[@]}" \
    >"$case_dir/sender.stdout" 2>"$case_dir/sender.log" &
  SENDER_PID=$!

  encode_command=(
    "$DUT_FFMPEG"
    -y
    -nostdin
    -hide_banner
    -loglevel debug
    -stats_period 0.02
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
    kill -TERM "$SENDER_PID" 2>/dev/null || true
    wait_sender_bounded "$SENDER_PID" >/dev/null 2>&1 || true
    SENDER_PID=
    fail "$codec $mode encoder exited with $ffmpeg_rc"
  fi

  set +e
  wait_sender_bounded "$SENDER_PID"
  sender_rc=$?
  set -e
  SENDER_PID=
  printf 'sender_rc=%s\n' "$sender_rc" >>"$case_dir/status.txt"
  [ "$sender_rc" -ne 124 ] || fail "$codec $mode sender did not exit within 10 seconds"
  [ "$sender_rc" -eq 0 ] || fail "$codec $mode sender exited with $sender_rc"
  [ ! -e "$socket_path" ] || fail "$codec $mode left its ROI socket behind"
  [ -s "$events" ] || fail "$codec $mode sender produced no event evidence"
  [ -s "$output" ] || fail "$codec $mode produced an empty bitstream"
  grep -Fq 'Listening for atomic ROI updates on Unix datagram socket' \
    "$case_dir/encode.log" || fail "$codec $mode did not bind the ROI socket"
  grep -Fq 'Applied ROI socket update #' "$case_dir/encode.log" || \
    fail "$codec $mode did not apply any ROI update"
  if [ "$force_empty" = true ]; then
    expected_counts=(0)
  else
    expected_counts=(0 1 2 8)
  fi
  for expected_count in "${expected_counts[@]}"; do
    grep -Fq "with ${expected_count} region(s)" "$case_dir/encode.log" || \
      fail "$codec $mode did not apply a ${expected_count}-region set"
  done
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
  "${decode_command[@]}" >"$case_dir/decode.stdout" 2>"$case_dir/decode.log"
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
  run_encode_case "$codec" baseline "$Q15_SCHEDULE" true
  run_encode_case "$codec" q15 "$Q15_SCHEDULE" false
  run_encode_case "$codec" q25 "$Q25_SCHEDULE" false
  for mode in baseline q15 q25; do
    decode_and_count "$codec" "$mode"
  done

  analysis_command=(
    python3 "$TEST_DIR/analyze-production.py"
    --ffmpeg "$ANALYSIS_FFMPEG"
    --source "$SOURCE"
    --baseline "$OUTPUT_DIR/$codec/baseline/output.bin"
    --q15 "$OUTPUT_DIR/$codec/q15/output.bin"
    --q25 "$OUTPUT_DIR/$codec/q25/output.bin"
    --baseline-events "$OUTPUT_DIR/$codec/baseline/events.jsonl"
    --q15-events "$OUTPUT_DIR/$codec/q15/events.jsonl"
    --q25-events "$OUTPUT_DIR/$codec/q25/events.jsonl"
    --baseline-progress "$OUTPUT_DIR/$codec/baseline/progress.txt"
    --q15-progress "$OUTPUT_DIR/$codec/q15/progress.txt"
    --q25-progress "$OUTPUT_DIR/$codec/q25/progress.txt"
    --baseline-encode-log "$OUTPUT_DIR/$codec/baseline/encode.log"
    --q15-encode-log "$OUTPUT_DIR/$codec/q15/encode.log"
    --q25-encode-log "$OUTPUT_DIR/$codec/q25/encode.log"
    --format "$codec"
    --q15-schedule "$Q15_SCHEDULE"
    --q25-schedule "$Q25_SCHEDULE"
    --output-dir "$OUTPUT_DIR/$codec/analysis"
  )
  record_command timeout --signal=TERM --kill-after=10s 900s \
    env PYTHONDONTWRITEBYTECODE=1 "${analysis_command[@]}"
  note "Analyzing $codec q15/q25 production matrix"
  timeout --signal=TERM --kill-after=10s 900s \
    env PYTHONDONTWRITEBYTECODE=1 "${analysis_command[@]}" 2>&1 | \
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
