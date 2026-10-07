#!/usr/bin/env python3
"""
Dub Studio — keyless dubbing worker (GitHub Actions).

Pipeline: queue se job uthao -> input download -> ffprobe (<=5 min)
  -> ffmpeg extract -> Demucs htdemucs CPU (vocals vs background)
  -> faster-whisper small (transcribe) -> NLLB-200-distilled-600M (translate)
  -> edge-tts per segment -> atempo duration-fit -> adelay+amix remix
  -> result catbox par upload -> queue me job "done".

Koi API key kahin nahi — poori pipeline keyless hai.
Queue: ntfy.sh (keyless pub/sub) — neeche QUEUE section dekho.
"""
import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import uuid

import httpx

# ---------------------------------------------------------------- config
BASE_DIR = os.environ.get("WORK_DIR", "/tmp/dubstudio")
MAX_SECONDS = 300  # 5 minute
MAX_BYTES = 100 * 1024 * 1024  # 100 MB

AUDIO_EXTS = {"mp3", "wav", "m4a", "ogg", "flac", "aac", "wma"}
VIDEO_EXTS = {"mp4", "mov", "mkv", "webm"}

# Queue: ntfy.sh (keyless pub/sub, message cache ~12h).
# Design: frontend job JSON ko JOBS topic par POST karta hai.
# Worker har job ke liye RESULT topic check karta hai:
#   - done/error mojood -> skip | taaza progress (<50 min) -> skip | warna process.
# Is me koi read-modify-write race nahi — append-only log hai.
# NOTE: npoint.io ka write API private beta me hai (500), is liye ntfy par pivot.
#   IDs public/obscure hain (keyless design); queue me koi PII nahi jata.
NTFY_BASE = os.environ.get("NTFY_BASE", "https://ntfy.sh")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "dsq_4f8a1c9e2b7d")
JOBS_SUFFIX = "_jobs"
RES_SUFFIX = "_r_"
# itne minute purani progress ko "abandoned" samjho (worker mar gaya) -> dobara process
STALE_PROGRESS_MIN = 50


def _jobs_topic():
    return NTFY_TOPIC + JOBS_SUFFIX


def _res_topic(job_id):
    return NTFY_TOPIC + RES_SUFFIX + job_id


def ntfy_post(topic, obj):
    """Ek JSON message publish karo."""
    with httpx.Client(timeout=30) as c:
        r = c.post(f"{NTFY_BASE}/{topic}", json=obj,
                   headers={"Content-Type": "application/json"})
        r.raise_for_status()
        return True


def ntfy_get(topic, since="12h"):
    """Cached messages parho -> [(ntfy_id, time, payload_dict), ...]."""
    msgs = []
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{NTFY_BASE}/{topic}/json", params={"since": since})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        for line in r.text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                m = json.loads(line)
            except Exception:
                continue
            if not isinstance(m, dict) or "message" not in m:
                continue
            try:
                payload = json.loads(m["message"])
            except Exception:
                continue
            msgs.append((m.get("id"), m.get("time"), payload))
    return msgs


def job_result_status(job_id):
    """Result topic ka aakhri status: (status, progress, time) ya (None, 0, 0)."""
    msgs = ntfy_get(_res_topic(job_id), since="24h")
    if not msgs:
        return None, 0, 0
    _, t, payload = msgs[-1]
    return payload.get("status"), payload.get("progress", 0), t or 0


def store_update_job(job_id, fields, retries=4):
    """Job ka status snapshot result topic par publish karo (append-only)."""
    payload = {"id": job_id}
    payload.update(fields)
    payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for attempt in range(retries):
        try:
            ntfy_post(_res_topic(job_id), payload)
            return True
        except Exception as e:
            print(f"[queue] update retry {attempt + 1}: {e}", flush=True)
        time.sleep(2)
    return False


def claim_job(job_id):
    """Job ko 'processing' mark karo; pehle se done/progress ho to None."""
    status, progress, t = job_result_status(job_id)
    now = time.time()
    if status in ("done", "error"):
        return None
    if status == "processing" and (now - t) < STALE_PROGRESS_MIN * 60:
        return None  # koi worker (shayad) zinda hai
    # naye sire se claim karo
    store_update_job(job_id, {"status": "processing", "progress": 2,
                              "stage": STAGES["prepare"]})
    # dobara parh kar tasdeeq (do workers ki race me aakhri jeetta hai;
    # concurrency group ki waja se ek waqt me ek hi worker hota hai)
    return {"id": job_id}


def fetch_queued_jobs():
    """Jobs topic se saare job payloads (24h window), purane pehle."""
    seen = {}
    for _id, _t, payload in ntfy_get(_jobs_topic(), since="24h"):
        jid = payload.get("id")
        if jid and jid not in seen:
            seen[jid] = payload
    jobs = list(seen.values())
    jobs.sort(key=lambda j: j.get("created_at", ""))
    return jobs

VOICES = {
    "ur": {"mard": "ur-PK-AsadNeural", "aurat": "ur-PK-UzmaNeural", "name": "اردو", "en": "Urdu"},
    "en": {"mard": "en-US-GuyNeural", "aurat": "en-US-JennyNeural", "name": "انگریزی", "en": "English"},
    "hi": {"mard": "hi-IN-MadhurNeural", "aurat": "hi-IN-SwaraNeural", "name": "ہندی", "en": "Hindi"},
    "ar": {"mard": "ar-SA-HamedNeural", "aurat": "ar-SA-ZariyahNeural", "name": "عربی", "en": "Arabic"},
    "fa": {"mard": "fa-IR-FaridNeural", "aurat": "fa-IR-DilaraNeural", "name": "فارسی", "en": "Persian"},
    "tr": {"mard": "tr-TR-AhmetNeural", "aurat": "tr-TR-EmelNeural", "name": "ترکی", "en": "Turkish"},
    "fr": {"mard": "fr-FR-HenriNeural", "aurat": "fr-FR-DeniseNeural", "name": "فرانسیسی", "en": "French"},
    "de": {"mard": "de-DE-ConradNeural", "aurat": "de-DE-KatjaNeural", "name": "جرمن", "en": "German"},
    "es": {"mard": "es-ES-AlvaroNeural", "aurat": "es-ES-ElviraNeural", "name": "ہسپانوی", "en": "Spanish"},
    "ru": {"mard": "ru-RU-DmitryNeural", "aurat": "ru-RU-SvetlanaNeural", "name": "روسی", "en": "Russian"},
    "id": {"mard": "id-ID-ArdiNeural", "aurat": "id-ID-GadisNeural", "name": "انڈونیشیائی", "en": "Indonesian"},
    "ms": {"mard": "ms-MY-OsmanNeural", "aurat": "ms-MY-YasminNeural", "name": "مالے", "en": "Malay"},
}

# faster-whisper kabhi code ("en") kabhi poora naam ("English") deta hai.
LANG_ALIASES = {
    "english": "en", "urdu": "ur", "hindi": "hi", "arabic": "ar",
    "persian": "fa", "farsi": "fa", "turkish": "tr", "french": "fr",
    "german": "de", "spanish": "es", "russian": "ru",
    "indonesian": "id", "malay": "ms",
}

# NLLB-200 FLORES codes (translation ke liye)
NLLB_CODES = {
    "ur": "urd_Arab", "en": "eng_Latn", "hi": "hin_Deva", "ar": "arb_Arab",
    "fa": "pes_Arab", "tr": "tur_Latn", "fr": "fra_Latn", "de": "deu_Latn",
    "es": "spa_Latn", "ru": "rus_Cyrl", "id": "ind_Latn", "ms": "zsm_Latn",
}

STAGES = {
    "prepare": "فائل تیار کی جا رہی ہے…",
    "separate": "آواز اور بیک گراؤنڈ الگ کیے جا رہے ہیں…",
    "transcribe": "بولی کو متن میں بدلا جا رہا ہے…",
    "translate": "ترجمہ ہو رہا ہے…",
    "tts": "نئی آواز بنائی جا رہی ہے…",
    "mix": "آواز اور بیک گراؤنڈ ملائے جا رہے ہیں…",
    "upload": "تیار فائل اپ لوڈ ہو رہی ہے…",
    "done": "مکمل ہو گیا ✓",
}

# ---------------------------------------------------------------- helpers
def log(msg):
    print(f"[dub] {msg}", flush=True)


def job_dir(job_id):
    d = os.path.join(BASE_DIR, job_id)
    os.makedirs(d, exist_ok=True)
    return d


def run(cmd, timeout=1800):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError("پروسیسنگ میں خرابی: " + err)
    return p


def probe_duration(path):
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    if p.returncode != 0:
        raise RuntimeError("فائل پڑھی نہیں جا سکی۔ آڈیو/ویڈیو فائل اپ لوڈ کریں۔")
    return float(p.stdout.decode().strip())


def download_file(url, dst):
    with httpx.Client(timeout=600, follow_redirects=True) as c:
        with c.stream("GET", url) as r:
            r.raise_for_status()
            total = 0
            with open(dst, "wb") as f:
                for chunk in r.iter_bytes(1024 * 256):
                    f.write(chunk)
                    total += len(chunk)
                    if total > MAX_BYTES + 1024:
                        raise RuntimeError("فائل 100MB سے بڑی ہے۔")
    if os.path.getsize(dst) == 0:
        raise RuntimeError("ڈاؤن لوڈ خالی آیا۔ دوبارہ کوشش کریں۔")


def upload_file(path):
    """Result upload — catbox -> 0x0.st -> tmpfiles.org (jo chale)."""
    name = os.path.basename(path)
    errors = []
    with httpx.Client(timeout=600, follow_redirects=True) as c:
        # 1. catbox.moe
        try:
            with open(path, "rb") as f:
                r = c.post("https://catbox.moe/user/api.php",
                           data={"reqtype": "fileupload"},
                           files={"fileToUpload": (name, f)})
            if r.status_code == 200 and r.text.strip().startswith("https://"):
                return r.text.strip()
            errors.append(f"catbox:{r.status_code}")
        except Exception as e:
            errors.append(f"catbox:{e}")
        # 2. 0x0.st
        try:
            with open(path, "rb") as f:
                r = c.post("https://0x0.st", files={"file": (name, f)})
            if r.status_code == 200 and r.text.strip().startswith("https://"):
                return r.text.strip()
            errors.append(f"0x0:{r.status_code}")
        except Exception as e:
            errors.append(f"0x0:{e}")
        # 3. tmpfiles.org
        try:
            with open(path, "rb") as f:
                r = c.post("https://tmpfiles.org/api/v1/upload", files={"file": (name, f)})
            if r.status_code == 200:
                url = r.json()["data"]["url"]  # https://tmpfiles.org/<id>/<name>
                m = re.match(r"https://tmpfiles\.org/(\d+)/(.*)", url)
                if m:
                    return f"https://tmpfiles.org/dl/{m.group(1)}/{m.group(2)}"
                return url
            errors.append(f"tmpfiles:{r.status_code}")
        except Exception as e:
            errors.append(f"tmpfiles:{e}")
    raise RuntimeError("نتیجہ اپ لوڈ نہیں ہو سکا: " + "; ".join(errors))


# ---------------------------------------------------------------- pipeline steps
def separate_vocals(full_wav, sep_out):
    """Demucs htdemucs CPU — vocals.wav aur no_vocals.wav alag karo."""
    run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs",
         "--device", "cpu", "-o", sep_out, full_wav], timeout=1800)
    track = os.path.splitext(os.path.basename(full_wav))[0]
    d = os.path.join(sep_out, "htdemucs", track)
    vocals_wav = os.path.join(d, "vocals.wav")
    bg_wav = os.path.join(d, "no_vocals.wav")
    if not (os.path.exists(vocals_wav) and os.path.exists(bg_wav)):
        raise RuntimeError("آواز الگ کرنے میں ناکامی۔")
    return vocals_wav, bg_wav


_whisper_model = None


def transcribe_vocals(vocals_16k):
    """faster-whisper small (local, keyless) — segments + detected language."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log("whisper small model load ho raha hai…")
        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
    segments_gen, info = _whisper_model.transcribe(
        vocals_16k, beam_size=5, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500))
    lang = str(getattr(info, "language", "en") or "en").lower()
    lang = LANG_ALIASES.get(lang, lang)
    segments = []
    for s in segments_gen:
        text = (s.text or "").strip()
        if not text:
            continue
        start, end = float(s.start), float(s.end)
        if end - start < 0.2:
            continue
        segments.append({"start": start, "end": end, "text": text})
    return lang, segments


_nllb = None


def get_nllb():
    global _nllb
    if _nllb is None:
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
        log("NLLB-200-distilled-600M load ho raha hai…")
        tok = AutoTokenizer.from_pretrained("facebook/nllb-200-distilled-600M")
        model = AutoModelForSeq2SeqLM.from_pretrained("facebook/nllb-200-distilled-600M")
        model.eval()
        _nllb = (tok, model)
    return _nllb


def translate_segments(segments, src_lang, tgt_key):
    """NLLB se batch translation (keyless)."""
    if src_lang == tgt_key:
        return [s["text"] for s in segments]
    src_code = NLLB_CODES.get(src_lang)
    tgt_code = NLLB_CODES.get(tgt_key)
    if not src_code:
        raise RuntimeError(f"اس زبان ({src_lang}) کا ترجمہ سپورٹڈ نہیں۔")
    if not tgt_code:
        raise RuntimeError("منتخب زبان سپورٹڈ نہیں۔")
    tok, model = get_nllb()
    import torch
    tok.src_lang = src_code
    tgt_id = tok.convert_tokens_to_ids(tgt_code)
    out = []
    B = 8
    for i in range(0, len(segments), B):
        chunk = [s["text"] for s in segments[i:i + B]]
        enc = tok(chunk, return_tensors="pt", padding=True,
                  truncation=True, max_length=256)
        with torch.no_grad():
            gen = model.generate(**enc, forced_bos_token_id=tgt_id,
                                 max_length=256)
        out.extend(t.strip() for t in tok.batch_decode(gen, skip_special_tokens=True))
    return out


async def synth_segment(text, voice, out_mp3):
    import edge_tts
    comm = edge_tts.Communicate(text, voice)
    await comm.save(out_mp3)


async def tts_all(texts, voice, clip_dir, job_id, total):
    os.makedirs(clip_dir, exist_ok=True)
    sem = asyncio.Semaphore(4)
    done = {"n": 0}

    async def one(i, text):
        async with sem:
            out = os.path.join(clip_dir, f"clip_{i:04d}.mp3")
            await synth_segment(text, voice, out)
            done["n"] += 1
            store_update_job(job_id, {"progress": 70 + int(15 * done["n"] / total)})

    await asyncio.gather(*(one(i, t) for i, t in enumerate(texts)))


def fit_clip_to_duration(src_mp3, seg_dur, dst_wav):
    dur = probe_duration(src_mp3)
    tempo = dur / seg_dur
    filters = []
    if tempo > 1.02:
        s = min(tempo, 4.0)
        while s > 2.0 + 1e-9:
            filters.append("atempo=2.0")
            s /= 2.0
        filters.append(f"atempo={s:.4f}")
    filters.append(f"apad=whole_dur={seg_dur:.3f}")
    filters.append(f"atrim=0:{seg_dur:.3f}")
    filters.append("aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo")
    run(["ffmpeg", "-y", "-v", "error", "-i", src_mp3, "-af", ",".join(filters), dst_wav])


def remix(clips, bg_wav, out_mp3):
    n = len(clips)
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for c in clips:
        cmd += ["-i", c["wav"]]
    cmd += ["-i", bg_wav]
    fc = []
    for i, c in enumerate(clips):
        ms = int(c["start"] * 1000)
        fc.append(f"[{i}:a]adelay={ms}|{ms}[d{i}]")
    if n == 1:
        fc.append("[d0]anull[vox]")
    else:
        ins = "".join(f"[d{i}]" for i in range(n))
        fc.append(f"{ins}amix=inputs={n}:normalize=0[vox]")
    fc.append(f"[vox][{n}:a]amix=inputs=2:normalize=0,alimiter=limit=0.95[aout]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[aout]",
            "-c:a", "libmp3lame", "-b:a", "192k", out_mp3]
    run(cmd, timeout=600)


# ---------------------------------------------------------------- one job
_MANUAL = {}  # job_id -> jobs/manual.json path (progress file me, ntfy par nahi)


def update(job_id, **fields):
    if job_id in _MANUAL:
        try:
            with open(_MANUAL[job_id], encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            doc = {}
        doc.update(fields)
        doc["id"] = job_id
        with open(_MANUAL[job_id], "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        return
    store_update_job(job_id, fields)


def process_job(job):
    job_id = job["id"]
    d = job_dir(job_id)
    target_lang = job.get("target_lang", "ur")
    voice_gender = job.get("voice_gender", "aurat")
    is_video = bool(job.get("is_video"))
    file_url = job.get("file_url", "")
    if target_lang not in VOICES:
        raise RuntimeError("زبان منتخب کریں۔")
    if voice_gender not in ("mard", "aurat"):
        voice_gender = "aurat"
    if not file_url.startswith("https://"):
        raise RuntimeError("فائل کا لنک خراب ہے۔ دوبارہ اپ لوڈ کریں۔")

    # 1. download
    update(job_id, stage=STAGES["prepare"], progress=5)
    log(f"job {job_id}: download {file_url[:60]}…")
    ext = (job.get("file_name") or "").rsplit(".", 1)[-1].lower()
    if ext not in AUDIO_EXTS and ext not in VIDEO_EXTS:
        ext = "mp4" if is_video else "mp3"
    src_path = os.path.join(d, f"input.{ext}")
    download_file(file_url, src_path)
    dur_in = probe_duration(src_path)
    if dur_in > MAX_SECONDS:
        raise RuntimeError("فائل 5 منٹ سے لمبی ہے۔ چھوٹی فائل اپ لوڈ کریں۔")

    # 2. extract full-quality wav
    full_wav = os.path.join(d, "full.wav")
    run(["ffmpeg", "-y", "-v", "error", "-i", src_path,
         "-ar", "44100", "-ac", "2", full_wav])
    dur_total = probe_duration(full_wav)

    # 3. demucs: vocals vs background
    update(job_id, stage=STAGES["separate"], progress=12)
    log(f"job {job_id}: demucs…")
    vocals_wav, bg_wav = separate_vocals(full_wav, os.path.join(d, "separated"))
    update(job_id, progress=35)
    vocals_16k = os.path.join(d, "vocals_16k.wav")
    run(["ffmpeg", "-y", "-v", "error", "-i", vocals_wav,
         "-ar", "16000", "-ac", "1", vocals_16k])

    # 4. transcribe (faster-whisper, keyless)
    update(job_id, stage=STAGES["transcribe"], progress=42)
    log(f"job {job_id}: whisper…")
    src_lang, segments = transcribe_vocals(vocals_16k)
    if not segments:
        raise RuntimeError("آڈیو میں کوئی بولی نہیں ملی۔")
    log(f"job {job_id}: detected={src_lang}, segments={len(segments)}")
    update(job_id, progress=52)

    # 5. translate (NLLB, keyless)
    update(job_id, stage=STAGES["translate"], progress=56)
    log(f"job {job_id}: nllb {src_lang}->{target_lang}…")
    translations = translate_segments(segments, src_lang, target_lang)
    update(job_id, progress=66)

    # 6. tts (edge-tts, keyless)
    update(job_id, stage=STAGES["tts"], progress=70)
    voice = VOICES[target_lang][voice_gender]
    log(f"job {job_id}: tts voice={voice}…")
    clip_dir = os.path.join(d, "clips")
    asyncio.run(tts_all(translations, voice, clip_dir, job_id, len(translations)))
    update(job_id, progress=85)

    # 7. duration fit
    update(job_id, stage=STAGES["mix"], progress=88)
    clips = []
    for i, s in enumerate(segments):
        seg_dur = s["end"] - s["start"]
        src = os.path.join(clip_dir, f"clip_{i:04d}.mp3")
        dst = os.path.join(clip_dir, f"fit_{i:04d}.wav")
        fit_clip_to_duration(src, seg_dur, dst)
        clips.append({"start": s["start"], "wav": dst})

    # 8. remix over background
    dubbed_mp3 = os.path.join(d, "dubbed.mp3")
    remix(clips, bg_wav, dubbed_mp3)
    update(job_id, progress=94)

    # 9. video ho to mux
    final_path = dubbed_mp3
    final_name = "dubbed.mp3"
    if is_video:
        out_mp4 = os.path.join(d, "dubbed.mp4")
        run(["ffmpeg", "-y", "-v", "error", "-i", src_path, "-i", dubbed_mp3,
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
             "-map", "0:v:0", "-map", "1:a:0",
             "-movflags", "+faststart", out_mp4])
        final_path, final_name = out_mp4, "dubbed.mp4"

    # 10. result upload + done
    update(job_id, stage=STAGES["upload"], progress=97)
    log(f"job {job_id}: uploading result…")
    result_url = upload_file(final_path)
    update(job_id, status="done", progress=100, stage=STAGES["done"],
           result_url=result_url, duration=round(dur_total, 2),
           detected_lang=src_lang)
    log(f"job {job_id}: DONE -> {result_url}")


# ---------------------------------------------------------------- main loop
def main():
    # DEBUG-ENTRY: debug file likho + git push karo (ntfy runner se nahi jata)
    try:
        _dbg0 = {"debug_entry": True,
                 "debug_cwd": os.getcwd(),
                 "debug_jobs_dir": (sorted(os.listdir("jobs"))
                                    if os.path.isdir("jobs") else "NO-JOBS-DIR"),
                 "debug_manual_exists": os.path.exists("jobs/manual.json"),
                 "debug_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with open("jobs/debug_last.json", "w", encoding="utf-8") as _f0:
            json.dump(_dbg0, _f0, ensure_ascii=False, indent=1)
        _r1 = subprocess.run(["git", "add", "jobs/debug_last.json"],
                             capture_output=True, text=True)
        _r2 = subprocess.run(["git", "-c", "user.name=dub-worker",
                              "-c", "user.email=dub-worker@local",
                              "commit", "-m", "[skip ci] debug last run"],
                             capture_output=True, text=True)
        _r3 = subprocess.run(["git", "push"], capture_output=True, text=True)
        _dbg0["git_add_rc"] = _r1.returncode
        _dbg0["git_commit_rc"] = _r2.returncode
        _dbg0["git_commit_out"] = (_r2.stdout + _r2.stderr)[:200]
        _dbg0["git_push_rc"] = _r3.returncode
        _dbg0["git_push_out"] = (_r3.stdout + _r3.stderr)[:300]
        with open("jobs/debug_last.json", "w", encoding="utf-8") as _f0:
            json.dump(_dbg0, _f0, ensure_ascii=False, indent=1)
        subprocess.run(["git", "add", "jobs/debug_last.json"], check=False)
        subprocess.run(["git", "-c", "user.name=dub-worker",
                        "-c", "user.email=dub-worker@local",
                        "commit", "--amend", "-m", "[skip ci] debug last run"],
                       check=False)
        subprocess.run(["git", "push"], check=False)
    except Exception as _e0:
        pass
    ap = argparse.ArgumentParser(description="Dub Studio keyless worker (ntfy queue)")
    ap.add_argument("--ntfy-topic", default=None,
                    help="ntfy topic base (default: dub.py me hardcoded NTFY_TOPIC)")
    ap.add_argument("--max-jobs", type=int, default=5,
                    help="ek run me zyada se zyada kitne jobs (default 5)")
    args = ap.parse_args()
    global NTFY_TOPIC
    if args.ntfy_topic:
        NTFY_TOPIC = args.ntfy_topic

    log(f"queue read ntfy:{NTFY_TOPIC + JOBS_SUFFIX} …")
    try:
        jobs = fetch_queued_jobs()
    except Exception as e:
        log(f"queue read failed: {e} — ntfy ke baghair jari.")
        jobs = []
    log(f"jobs mile: {len(jobs)}")

    # manual job (testing / ntfy fallback): jobs/manual.json
    mpath = "jobs/manual.json"
    if os.path.exists(mpath):
        try:
            mdoc = json.load(open(mpath, encoding="utf-8"))
        except Exception as ex:
            log(f"manual.json parhna fail: {ex}")
            mdoc = {}
        if mdoc and not mdoc.get("processed"):
            mdoc["id"] = "manual"
            _MANUAL["manual"] = mpath
            # DEBUG: runner ka haal manual.json me likho (logs nahi parh sakte)
            try:
                _ls = sorted(os.listdir("jobs")) if os.path.isdir("jobs") else "NO-JOBS-DIR"
            except Exception as _e:
                _ls = f"ls-fail:{_e}"
            update("manual", debug_cwd=os.getcwd(), debug_jobs_ls=_ls,
                   debug_note="manual block entered")
            log("manual job mila — process ho raha hai …")
            try:
                process_job(mdoc)
                mdoc["processed"] = True
                if not mdoc.get("status"):
                    mdoc["status"] = "done"
            except Exception as e:
                mdoc["processed"] = True
                mdoc["status"] = "error"
                mdoc["error"] = (str(e) or "نامعلوم خرابی")[:300]
                log(f"manual job ERROR: {mdoc['error'][:200]}")
            mdoc["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                               time.gmtime())
            with open(mpath, "w", encoding="utf-8") as f:
                json.dump(mdoc, f, ensure_ascii=False, indent=1)
            subprocess.run(["git", "add", mpath], check=False)
            subprocess.run(["git", "-c", "user.name=dub-worker",
                            "-c", "user.email=dub-worker@local",
                            "commit", "-m", "[skip ci] manual job result"],
                           check=False)
            subprocess.run(["git", "push"], check=False)
            log("manual.json result commit ho gaya.")

    done_n = 0
    for job in jobs[:args.max_jobs]:
        job_id = job.get("id")
        if not job_id:
            continue
        claimed = claim_job(job_id)
        if not claimed:
            log(f"job {job_id}: pehle se done/processing — skip.")
            continue
        try:
            process_job(job)
            done_n += 1
        except Exception as e:
            msg = str(e) or "نامعلوم خرابی"
            log(f"job {job_id}: ERROR {msg[:200]}")
            update(job_id, status="error", error=msg, stage="خرابی ہو گئی")
    log(f"run khatam: {done_n} job(s) process huay.")


if __name__ == "__main__":
    main()
