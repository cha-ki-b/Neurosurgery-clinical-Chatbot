#!/usr/bin/env bash
#
# Record one sentence for the phase-1 spike.
#
#   ./record.sh 3      records sentence 3 into audio/03.wav
#
# Records 16 kHz mono 16-bit PCM, which is exactly what the model wants, so
# nothing has to be converted later. Stop early with Ctrl+C, or let it run out.
#
# Running it again for the same number overwrites the file. That is deliberate:
# re-recording a bad take should be easy.

set -u

CARD="${CARD:-plughw:1,0}"     # override if arecord -l shows a different card
SECONDS_MAX="${SECONDS_MAX:-15}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="$HERE/audio"

if [ $# -ne 1 ]; then
    echo "usage: $0 <sentence number 1-10>" >&2
    exit 2
fi

case "$1" in
    ''|*[!0-9]*) echo "error: '$1' is not a number" >&2; exit 2 ;;
esac

if [ "$1" -lt 1 ] || [ "$1" -gt 10 ]; then
    echo "error: pick a number between 1 and 10" >&2
    exit 2
fi

N=$(printf '%02d' "$1")
OUT="$OUT_DIR/$N.wav"
mkdir -p "$OUT_DIR"

# Show the operator the sentence they are about to read, so they do not have to
# keep another window open.
echo
echo "─────────────────────────────────────────────────────────────"
grep -E "^  $N  " "$HERE/sentences-fr.txt" | head -1 | sed 's/^  [0-9]*  /  /' | grep . || \
    echo "  (sentence $N — see sentences-fr.txt)"
echo "─────────────────────────────────────────────────────────────"
echo
echo "Reading it once silently first helps. Recording starts in:"
for i in 3 2 1; do printf '  %d\n' "$i"; sleep 1; done
echo
echo "  ● RECORDING — speak now. Ctrl+C when you have finished the sentence."
echo

arecord -D "$CARD" -f S16_LE -r 16000 -c 1 -d "$SECONDS_MAX" "$OUT" 2>/dev/null

echo
if [ ! -s "$OUT" ]; then
    echo "  ✗ nothing was recorded. Is the microphone plugged in?" >&2
    exit 1
fi

BYTES=$(stat -c %s "$OUT")
if [ "$BYTES" -le 44 ]; then
    # 44 bytes is a WAV header with no audio after it.
    echo "  ✗ the file is empty ($BYTES bytes). Check the microphone and try again." >&2
    exit 1
fi

printf '  ✓ saved %s  (%s KB)\n' "$OUT" "$((BYTES / 1024))"
echo "    listen back with:  aplay $OUT"
echo "    not happy?  just run:  ./record.sh $1"
echo
