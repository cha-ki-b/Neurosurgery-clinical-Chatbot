#!/usr/bin/env bash
# Ask a served MedGemma to describe the synthetic test image.
#
#   ./vision-probe.sh <container-on-server2_net> <served-model-name>
#   ./vision-probe.sh vllm medgemma-4b-it
#
# Runs from inside clinical-agent because the model containers deliberately publish
# no port (STT-PLAN.md §2.4) — so the only way in is over server2_net.
#
# A smoke test, not a benchmark: it checks the vision path still functions and still
# reads text out of an image. Text is the first thing a degraded encoder loses, which
# is why the prompt asks for it.
set -u
TARGET="${1:?container name on server2_net}"; MODEL="${2:?served model name}"
IMG="$(cd "$(dirname "$0")" && pwd)/results/vision-test.png"

docker cp "$IMG" clinical-agent:/tmp/vt.png >/dev/null
docker exec clinical-agent python3 -c "
import base64, json, urllib.request
b64 = base64.b64encode(open('/tmp/vt.png','rb').read()).decode()
body = json.dumps({'model': '$MODEL', 'max_tokens': 150, 'temperature': 0,
  'messages': [{'role':'user','content':[
    {'type':'image_url','image_url':{'url':'data:image/png;base64,'+b64}},
    {'type':'text','text':'List the shapes and their colours, then read any text in the image exactly.'}]}]}).encode()
r = urllib.request.Request('http://$TARGET:8000/v1/chat/completions', data=body,
                           headers={'Content-Type':'application/json'})
print(json.load(urllib.request.urlopen(r, timeout=180))['choices'][0]['message']['content'].strip())
"
