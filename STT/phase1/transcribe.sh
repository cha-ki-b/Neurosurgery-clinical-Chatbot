#!/usr/bin/env bash
#
# Send every recording in audio/ to the speech model and print what it heard,
# next to what was supposed to be said.
#
#   ./transcribe.sh
#
# Writes the same thing to results.txt so you can read it again later without
# re-running, and so you can send it on.
#
# Talks to the throwaway spike container from PHASE-1-SPIKE.md step 3, which
# listens on 127.0.0.1:8100 and ONLY there — it has no authentication of any
# kind, so it must never be reachable from the network.

set -u

ENDPOINT="${ENDPOINT:-http://127.0.0.1:8100/v1/audio/transcriptions}"
MODEL="${MODEL:-qwen3-asr}"
LANGUAGE="${LANGUAGE:-fr}"
HERE="$(cd "$(dirname "$0")" && pwd)"
AUDIO_DIR="$HERE/audio"
RESULTS="$HERE/results.txt"

# The reference text, so the operator does not have to compare by memory.
expected_for() {
    grep -E "^  $1  " "$HERE/sentences-fr.txt" 2>/dev/null \
        | head -1 | sed 's/^  [0-9]*  //'
}

# --- is the model actually there? ------------------------------------------
if ! curl -sS --max-time 5 "${ENDPOINT%/audio/transcriptions}/models" >/dev/null 2>&1; then
    echo "✗ Cannot reach the model at $ENDPOINT" >&2
    echo "  Is the container running?   docker ps | grep qwen-asr-spike" >&2
    echo "  See PHASE-1-SPIKE.md step 3." >&2
    exit 1
fi

shopt -s nullglob
FILES=("$AUDIO_DIR"/*.wav)
if [ ${#FILES[@]} -eq 0 ]; then
    echo "✗ No recordings in $AUDIO_DIR — do step 4 first (./record.sh 1 … 10)." >&2
    exit 1
fi

{
    echo "Phase 1 spike — transcription results"
    echo "model:    $MODEL"
    echo "language: $LANGUAGE"
    echo "date:     $(date '+%Y-%m-%d %H:%M:%S')"
    echo
} > "$RESULTS"

for f in "${FILES[@]}"; do
    n=$(basename "$f" .wav)

    start=$(date +%s.%N)
    body=$(curl -sS --max-time 120 "$ENDPOINT" \
        -F "file=@$f" \
        -F "model=$MODEL" \
        -F "language=$LANGUAGE" \
        -F "response_format=json" 2>&1)
    end=$(date +%s.%N)
    elapsed=$(awk "BEGIN{printf \"%.2f\", $end - $start}")

    # Pull .text out of the JSON without needing jq installed.
    heard=$(printf '%s' "$body" | python3 -c '
import sys, json
raw = sys.stdin.read()
try:
    print(json.loads(raw).get("text", "").strip())
except Exception:
    print("!! unexpected reply: " + raw[:300].replace("\n", " "))
' 2>/dev/null)

    exp=$(expected_for "$n")

    block=$(printf '─── %s ───────────────────────────────────────────\n  expected : %s\n  heard    : %s\n  time     : %s s\n' \
        "$n" "${exp:-(not in sentences-fr.txt)}" "${heard:-(nothing)}" "$elapsed")

    echo "$block"
    echo "$block" >> "$RESULTS"
    echo >> "$RESULTS"
done

echo
echo "Saved to $RESULTS"
echo
echo "Now fill in SCORESHEET.md — and read step 6 of PHASE-1-SPIKE.md before you"
echo "judge anything. Sentence 08 is a deliberate trap; check it carefully."
