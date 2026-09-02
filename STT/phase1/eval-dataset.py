#!/usr/bin/env python3
"""
Measure the spike model against real French speech that already has a reference
transcript, so no microphone is needed.

    python3 eval-dataset.py --dataset multimed --n 20 --medical-only
    python3 eval-dataset.py --dataset fleurs  --n 20

This is NOT a replacement for recording clinicians (see STT-PLAN.md Q-D). It uses
public French audio, so it says nothing about Algerian-accented French, about the
ward's acoustics, or about the ten command sentences the assistant actually has to
understand. What it CAN do is falsify the model: if word error rate here is bad,
stop and pick another model. If it is good, the model has cleared a floor, not the bar.

Standard library only, on purpose — the same "minimal dependency" rule the service
itself follows.

  multimed : leduckhai/MultiMed-ST, French, corrected.test
             real doctor-patient consultation audio scraped from YouTube. Roughly a
             quarter of it is clinical; the rest is channel intros and small talk.
             --medical-only keeps the clinical rows.
  fleurs   : google/fleurs, fr_fr, test
             clean read speech, studio-quiet. The easy baseline: a model that
             struggles here is broken.
"""

import argparse
import array
import json
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
import wave
from pathlib import Path

ROWS_API = "https://datasets-server.huggingface.co/rows"

DATASETS = {
    "multimed": ("leduckhai/MultiMed-ST", "French", "corrected.test"),
    "fleurs": ("google/fleurs", "fr_fr", "test"),
}

# Rows whose reference text mentions any of this are treated as clinical.
MEDICAL = re.compile(
    r"patient|m[ée]dec|docteur|sympt|trait|douleur|maladie|diagnost|h[oô]pital|"
    r"infirm|chirurg|cancer|tumeur|scanner|c[eé]r[ée]br|neuro|sang|cœur|cardi|"
    r"consultation|ordonnance|rendez-vous|temp[ée]rature|bronch|hospitalis",
    re.I,
)


def fetch_rows(dataset, config, split, n):
    url = f"{ROWS_API}?" + urllib.parse.urlencode(
        {"dataset": dataset, "config": config, "split": split, "offset": 0, "length": n}
    )
    with urllib.request.urlopen(url, timeout=120) as r:
        return [x["row"] for x in json.load(r).get("rows", [])]


def audio_url(row):
    a = row.get("audio")
    if isinstance(a, list) and a and isinstance(a[0], dict):
        return a[0].get("src")
    if isinstance(a, dict):
        return a.get("src")
    return None


def reference(row):
    for key in ("text", "transcription", "raw_transcription"):
        if row.get(key):
            return row[key]
    return ""


def to_mono16k(src: Path, dst: Path):
    """Convert any PCM WAV to mono 16 kHz 16-bit.

    Not an optimisation — a requirement. vLLM's /v1/audio/transcriptions rejects
    stereo and non-16 kHz uploads outright with "Invalid or unsupported audio file",
    even though librosa inside the same container decodes them happily. Dataset audio
    is 48 kHz stereo, so without this every request 400s.

    This is deliberately the same transformation the browser will do before sending
    (STT-PLAN.md §2.3): average the channels, box-filter down to 16 kHz. Stdlib only —
    `audioop` was removed in Python 3.13, and there is no ffmpeg on this host.
    """
    with wave.open(str(src), "rb") as w:
        ch, width, rate, n = (w.getnchannels(), w.getsampwidth(),
                              w.getframerate(), w.getnframes())
        raw = w.readframes(n)

    if width != 2:
        raise ValueError(f"expected 16-bit PCM, got {width * 8}-bit")

    samples = array.array("h")
    samples.frombytes(raw)
    if sys.byteorder == "big":
        samples.byteswap()

    if ch > 1:                                    # average the channels down to mono
        samples = array.array(
            "h", (sum(samples[i:i + ch]) // ch for i in range(0, len(samples) - ch + 1, ch))
        )

    if rate != 16000:
        ratio = rate / 16000.0
        out = array.array("h")
        if ratio >= 1:                            # downsample: mean over each window
            step, i, total = ratio, 0.0, len(samples)
            while i < total:
                a, b = int(i), min(int(i + step), total)
                if b <= a:
                    b = a + 1
                out.append(sum(samples[a:b]) // (b - a))
                i += step
        else:                                     # upsample: linear interpolation
            for k in range(int(len(samples) / ratio)):
                pos = k * ratio
                a = int(pos)
                b = min(a + 1, len(samples) - 1)
                frac = pos - a
                out.append(int(samples[a] * (1 - frac) + samples[b] * frac))
        samples = out

    with wave.open(str(dst), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(samples.tobytes())


def normalise(s):
    """Lowercase, strip punctuation and accents, collapse whitespace.

    The references are unpunctuated lowercase while the model returns properly
    written French, so comparing raw strings would score punctuation, not words.
    Accents are folded too: 'deite' vs 'deité' is not an error worth counting here.
    """
    s = s.lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^\w\s']", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def wer(ref, hyp):
    """Word error rate: edit distance over words, divided by reference length."""
    r, h = normalise(ref).split(), normalise(hyp).split()
    if not r:
        return None, 0
    prev = list(range(len(h) + 1))
    for i, rw in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hw in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rw != hw))
        prev = cur
    return prev[len(h)] / len(r), len(r)


def transcribe(endpoint, model, language, wav_path):
    """Multipart POST, hand-rolled so the script needs no requests/httpx."""
    boundary = "----spike" + str(int(time.time() * 1000))
    parts = []
    for name, value in (("model", model), ("language", language),
                        ("response_format", "json")):
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
            .encode()
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{wav_path.name}"\r\nContent-Type: audio/wav\r\n\r\n'.encode()
        + wav_path.read_bytes() + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)

    req = urllib.request.Request(
        endpoint, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as r:
        out = json.load(r)
    return out.get("text", ""), time.time() - t0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dataset", choices=DATASETS, default="multimed")
    p.add_argument("--n", type=int, default=20, help="samples to transcribe")
    p.add_argument("--pool", type=int, default=0,
                   help="rows to fetch before filtering (default: 4x --n)")
    p.add_argument("--medical-only", action="store_true",
                   help="keep only rows whose reference mentions clinical vocabulary")
    p.add_argument("--endpoint", default="http://127.0.0.1:8100/v1/audio/transcriptions")
    p.add_argument("--model", default="qwen3-asr")
    p.add_argument("--language", default="fr")
    args = p.parse_args()

    dataset, config, split = DATASETS[args.dataset]
    pool = args.pool or max(args.n * 4, 40)
    cache = Path(__file__).parent / "dataset-audio" / args.dataset
    cache.mkdir(parents=True, exist_ok=True)

    print(f"dataset  : {dataset} [{config}/{split}]")
    print(f"model    : {args.model}   language={args.language}")
    print(f"fetching : {pool} rows, keeping {args.n}"
          + (" (clinical only)" if args.medical_only else ""))
    print()

    try:
        rows = fetch_rows(dataset, config, split, pool)
    except Exception as e:
        sys.exit(f"could not reach the datasets server: {e}")

    if args.medical_only:
        rows = [r for r in rows if MEDICAL.search(reference(r))]
        print(f"{len(rows)} of {pool} rows are clinical\n")
    rows = [r for r in rows if audio_url(r) and reference(r)][: args.n]
    if not rows:
        sys.exit("no usable rows — try a bigger --pool, or drop --medical-only")

    results, out_lines = [], []
    for i, row in enumerate(rows, 1):
        raw_wav = cache / f"{i:03d}.orig.wav"
        wav = cache / f"{i:03d}.16k.wav"
        if not wav.exists():
            try:
                if not raw_wav.exists():
                    urllib.request.urlretrieve(audio_url(row), raw_wav)
                to_mono16k(raw_wav, wav)
            except Exception as e:
                print(f"{i:3d}  download/convert failed: {e}")
                continue
        ref = reference(row).strip()
        try:
            hyp, secs = transcribe(args.endpoint, args.model, args.language, wav)
        except Exception as e:
            print(f"{i:3d}  transcription failed: {e}")
            continue

        w, nwords = wer(ref, hyp)
        if w is None:
            continue
        results.append((w, nwords, secs, row.get("duration")))

        block = (f"─── {i:03d} ── WER {w*100:5.1f}%  ({nwords} words, {secs:.2f}s)\n"
                 f"  ref : {ref}\n  hyp : {hyp}")
        print(block + "\n")
        out_lines.append(block)

    if not results:
        sys.exit("nothing was transcribed")

    total_err = sum(w * n for w, n, _, _ in results)
    total_ref = sum(n for _, n, _, _ in results)
    audio_s = sum(d for *_, d in results if d) or 0
    proc_s = sum(s for *_, s, _ in results)

    summary = (
        "\n" + "=" * 60 + "\n"
        f"samples          : {len(results)}\n"
        f"aggregate WER    : {total_err / total_ref * 100:.1f}%   "
        f"(median {sorted(w for w, *_ in results)[len(results)//2]*100:.1f}%)\n"
        f"audio processed  : {audio_s:.0f}s in {proc_s:.1f}s  "
        f"(realtime factor {proc_s/audio_s:.3f}x)\n" if audio_s else ""
    )
    print(summary)
    print("Read this as a floor, not a verdict — see the docstring and STT-PLAN.md §4.")

    out = Path(__file__).parent / f"results-{args.dataset}.txt"
    out.write_text("\n\n".join(out_lines) + summary, encoding="utf-8")
    print(f"saved to {out}")


if __name__ == "__main__":
    main()
