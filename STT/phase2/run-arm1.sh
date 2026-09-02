#!/usr/bin/env bash
#
# Phase 2, arm 1 — MedGemma 1.5 + FP8, end to end.
#
#   ./run-arm1.sh
#
# Blocked until the Hugging Face account that owns ~/.cache/huggingface/token has
# been granted access to google/medgemma-1.5-4b-it. The script checks that first
# and stops with instructions rather than failing halfway through an 8 GB download.
#
# Everything here is reversible: it stops `vllm`, runs candidates in throwaway
# containers, and restores `vllm` on exit whatever happens. MedGemma 1's weights are
# never touched.

set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
RESULTS="$HERE/results"
MODEL_REPO="google/medgemma-1.5-4b-it"
HF_CACHE="/home/cerist/models/hf-cache"
CONTAINER="mg15-fp8"
UTIL="${UTIL:-0.50}"

mkdir -p "$RESULTS" "$HF_CACHE"

restore() {
    echo
    echo "── restoring production configuration ──"
    docker rm -f "$CONTAINER" >/dev/null 2>&1
    docker start vllm >/dev/null 2>&1
    for _ in $(seq 1 18); do
        [ "$(docker inspect vllm --format '{{.State.Health.Status}}' 2>/dev/null)" = "healthy" ] && break
        sleep 10
    done
    docker ps --filter name=vllm --format '  vllm: {{.Status}}'
}
trap restore EXIT

# ── 1. access check ─────────────────────────────────────────────────────────
TOKEN_FILE=~/.cache/huggingface/token
[ -s "$TOKEN_FILE" ] || { echo "✗ no token at $TOKEN_FILE"; exit 1; }
TOK=$(cat "$TOKEN_FILE")

WHO=$(curl -sS -H "Authorization: Bearer $TOK" https://huggingface.co/api/whoami-v2 \
      | python3 -c 'import sys,json;print(json.load(sys.stdin).get("name","?"))' 2>/dev/null)
CODE=$(curl -sS -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOK" \
       "https://huggingface.co/$MODEL_REPO/resolve/main/config.json")

echo "token belongs to : $WHO"
echo "access to $MODEL_REPO : HTTP $CODE"

if [ "$CODE" = "403" ]; then
    cat <<MSG

✗ BLOCKED — the account '$WHO' has not been granted access.

  The token is fine; the licence has not been accepted for THIS account.

  1. Sign in to huggingface.co as '$WHO' — not any other account you may have
     open. This is the usual cause: terms accepted while signed in as someone else.
  2. Open https://huggingface.co/$MODEL_REPO
  3. Accept the Health AI Developer Foundations terms.
     The repo is 'gated: auto', so approval is immediate — no waiting for a human.
  4. Re-run this script.

MSG
    exit 2
fi
[ "$CODE" = "200" ] || [ "$CODE" = "302" ] || [ "$CODE" = "307" ] || {
    echo "✗ unexpected HTTP $CODE — stopping rather than guessing"; exit 1; }
echo "✓ access granted"

# ── 2. free the card ────────────────────────────────────────────────────────
echo
echo "── stopping vllm (chat falls back to the rules engine) ──"
docker stop vllm >/dev/null 2>&1
docker rm -f "$CONTAINER" >/dev/null 2>&1
sleep 3

# ── 3. serve 1.5 at FP8 ─────────────────────────────────────────────────────
# Same image, same flags as arm 2. MedGemma 1.5 is Gemma3ForConditionalGeneration
# with an identical parameter count (4,300,079,472), so nothing else should change.
echo "── starting $MODEL_REPO at FP8, util $UTIL (first run downloads ~8 GB) ──"
docker run -d --name "$CONTAINER" --gpus all --network server2_net \
    -v "$HF_CACHE":/root/.cache/huggingface \
    -e HF_TOKEN="$TOK" \
    vllm/vllm-openai:v0.11.0 \
    --model "$MODEL_REPO" --served-model-name mg15fp8 \
    --quantization fp8 --max-model-len 4096 \
    --gpu-memory-utilization "$UTIL" --max-num-seqs 8 >/dev/null

echo -n "waiting for the engine"
for i in $(seq 1 120); do
    if docker exec clinical-agent python3 -c \
        "import urllib.request;urllib.request.urlopen('http://$CONTAINER:8000/v1/models',timeout=3)" >/dev/null 2>&1; then
        echo " — ready"; break
    fi
    docker ps --format '{{.Names}}' | grep -q "$CONTAINER" || {
        echo " — DIED"; docker logs --tail 30 "$CONTAINER" 2>&1 | tail -20; exit 1; }
    echo -n "."; sleep 15
done

docker logs "$CONTAINER" 2>&1 | grep -i "Available KV cache\|GPU KV cache size" | tee "$RESULTS/arm1-vram.txt"
nvidia-smi --query-compute-apps=used_memory --format=csv | tail -1 | tee -a "$RESULTS/arm1-vram.txt"

# ── 4. the gate ─────────────────────────────────────────────────────────────
cd /home/cerist/server2-stack
echo
echo "── eval_nlu (the gate: UNSAFE must be 0) ──"
docker compose -f docker-compose.yml -f docker-compose.vllm.yml exec -T \
    -e NLU_ENGINE=medgemma -e LLM_BASE_URL="http://$CONTAINER:8000/v1" -e LLM_MODEL=mg15fp8 \
    -w /srv/agent clinical-agent python3 -m tests.eval_nlu medgemma \
    > "$RESULTS/arm1-medgemma15-fp8-evalnlu.txt" 2>&1
tail -18 "$RESULTS/arm1-medgemma15-fp8-evalnlu.txt"

echo
echo "── explore.py (43 scenarios) ──"
timeout 1200 docker compose -f docker-compose.yml -f docker-compose.vllm.yml exec -T \
    -e NLU_ENGINE=medgemma -e LLM_BASE_URL="http://$CONTAINER:8000/v1" -e LLM_MODEL=mg15fp8 \
    -w /srv/agent clinical-agent python3 -m tests.explore \
    > "$RESULTS/arm1-medgemma15-fp8-explore.txt" 2>&1
echo "arm 1 states:"
grep -oE "^    \[[a-z_]+\]" "$RESULTS/arm1-medgemma15-fp8-explore.txt" | sort | uniq -c | sort -rn
echo "baseline states:"
grep -oE "^    \[[a-z_]+\]" "$RESULTS/baseline-medgemma1-bf16-explore.txt" | sort | uniq -c | sort -rn

echo
echo "── vision probe (must still read the text out of the image) ──"
"$HERE/vision-probe.sh" "$CONTAINER" mg15fp8 | tee "$RESULTS/vision-arm1-fp8.txt"
echo
echo "diff against the bf16 baseline:"
diff "$RESULTS/vision-baseline-bf16.txt" "$RESULTS/vision-arm1-fp8.txt" \
    && echo "  identical" || echo "  ^ differs — read it, do not assume it is a regression"

echo
echo "Done. Compare against results/BASELINE.md before promoting anything."
