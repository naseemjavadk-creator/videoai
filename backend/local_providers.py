"""
Free, no-billing replacements used when MODE=local (ClipSafe running on your own Mac).

    Google Cloud service             Local / free replacement
    -------------------------------  ---------------------------------------------------
    Vertex AI Gemini                 Gemini API with a free Google AI Studio key
    Cloud Text-to-Speech             edge-tts (Microsoft Edge neural voices, no key)
    Speech-to-Text V2                faster-whisper running on your CPU
    Video Intelligence face tracks   OpenCV YuNet face detector (Haar cascade fallback)
    Imagen 3 B-roll                  Pexels free stock photos (free API key, no card)
    Cloud Storage                    a local data/ folder (handled in pipeline.py/main.py)

Heavy modules (torch, ctranslate2, cv2) are imported lazily so the API starts fast.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("clipsafe.local")

# --------------------------------------------------------------------------------------
# Gemini API (AI Studio key): upload the analysis proxy through the Files API
# --------------------------------------------------------------------------------------


async def gemini_upload_video(client, path: Path, timeout: float = 600) -> tuple[str, str]:
    """Upload a video to the Gemini Files API and wait until it is ACTIVE.

    Returns (file_uri, mime_type). Uploaded files expire automatically after 48 hours.
    """
    uploaded = await client.aio.files.upload(file=str(path), config={"mime_type": "video/mp4"})
    deadline = time.monotonic() + timeout
    while True:
        state = getattr(getattr(uploaded, "state", None), "name", str(getattr(uploaded, "state", "")))
        if state == "ACTIVE":
            return uploaded.uri, uploaded.mime_type or "video/mp4"
        if state == "FAILED":
            raise RuntimeError("Gemini could not process the uploaded video")
        if time.monotonic() > deadline:
            raise RuntimeError("Gemini took too long to process the uploaded video")
        await asyncio.sleep(3)
        uploaded = await client.aio.files.get(name=uploaded.name)


# --------------------------------------------------------------------------------------
# Text-to-speech: edge-tts
# --------------------------------------------------------------------------------------

# Dashboard voices are named after Google's Chirp 3 HD voices; map them to an Edge voice
# of the same gender for the selected language.
_FEMALE = {"Kore", "Aoede", "Leda", "Zephyr", "Callirrhoe", "Despina"}

EDGE_VOICES: dict[str, tuple[str, str]] = {
    # language: (male, female)
    "en-US": ("en-US-AndrewMultilingualNeural", "en-US-AvaMultilingualNeural"),
    "en-IN": ("en-IN-PrabhatNeural", "en-IN-NeerjaNeural"),
    "en-GB": ("en-GB-RyanNeural", "en-GB-SoniaNeural"),
    "ml-IN": ("ml-IN-MidhunNeural", "ml-IN-SobhanaNeural"),
    "hi-IN": ("hi-IN-MadhurNeural", "hi-IN-SwaraNeural"),
    "ta-IN": ("ta-IN-ValluvarNeural", "ta-IN-PallaviNeural"),
    "es-US": ("es-US-AlonsoNeural", "es-US-PalomaNeural"),
}


def edge_voice_for(voice_name: Optional[str], language_code: str) -> str:
    """Turn 'ml-IN-Chirp3-HD-Charon' (or a plain Edge voice name) into an Edge voice."""
    if voice_name and voice_name.endswith("Neural"):
        return voice_name  # already an Edge voice
    lang = language_code
    persona = ""
    if voice_name:
        parts = voice_name.split("-")
        if len(parts) >= 2:
            lang = f"{parts[0]}-{parts[1]}"
        persona = parts[-1]
    male, female = EDGE_VOICES.get(lang, EDGE_VOICES.get(language_code, EDGE_VOICES["en-US"]))
    return female if persona in _FEMALE else male


async def edge_tts_to_wav(text: str, voice: str, out_wav: Path, run_cmd) -> None:
    import edge_tts  # lazy

    mp3 = out_wav.with_suffix(".mp3")
    last_error: Optional[Exception] = None
    for attempt in range(3):
        try:
            await edge_tts.Communicate(text, voice, rate="+6%").save(str(mp3))
            if mp3.exists() and mp3.stat().st_size > 0:
                break
        except Exception as exc:  # network hiccups or a throttled request
            last_error = exc
            await asyncio.sleep(2 * (attempt + 1))
    else:
        raise RuntimeError(f"edge-tts failed for voice {voice}: {last_error}")
    await run_cmd(
        ["ffmpeg", "-hide_banner", "-y", "-i", str(mp3), "-ar", "48000", "-ac", "1", str(out_wav)],
        timeout=120,
    )
    mp3.unlink(missing_ok=True)


# --------------------------------------------------------------------------------------
# B-roll: Pexels free stock photos
# --------------------------------------------------------------------------------------


async def pexels_photo(api_key: str, query: str, orientation: str, out: Path) -> bool:
    """Download a relevant Pexels photo. orientation: 'square' | 'portrait'. Returns success."""
    if not api_key or not query.strip():
        return False
    headers = {"Authorization": api_key}
    params = {"query": query[:80], "orientation": orientation, "per_page": 15, "size": "large"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as http:
        res = await http.get("https://api.pexels.com/v1/search", headers=headers, params=params)
        if res.status_code != 200:
            logger.warning("Pexels search failed (%s): %s", res.status_code, res.text[:200])
            return False
        photos = res.json().get("photos") or []
        if not photos:
            return False
        photo = random.choice(photos[:6])
        src = photo.get("src", {})
        url = src.get("portrait") if orientation == "portrait" else src.get("large2x") or src.get("large")
        url = url or src.get("original")
        if not url:
            return False
        img = await http.get(url)
        if img.status_code != 200 or not img.content:
            return False
        out.write_bytes(img.content)
        logger.info("B-roll from Pexels: %s by %s", photo.get("url"), photo.get("photographer"))
        return True


# --------------------------------------------------------------------------------------
# Face tracking: OpenCV
# --------------------------------------------------------------------------------------


def detect_faces(video: Path, yunet_model: Optional[str], step: float = 0.2) -> list[tuple[float, float, float]]:
    """Sample a frame every `step` seconds and return (time, center_x_norm, area_norm)."""
    import cv2  # lazy

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        return []
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    every = max(1, int(round(fps * step)))
    detector = None
    cascade = None
    if yunet_model and Path(yunet_model).exists():
        try:
            detector = cv2.FaceDetectorYN.create(yunet_model, "", (320, 320), 0.7, 0.3, 50)
        except Exception as exc:
            logger.warning("YuNet unavailable (%s); using Haar cascade", exc)
    if detector is None:
        if not hasattr(cv2, "CascadeClassifier"):
            cap.release()
            logger.warning("No face detector available; using a centred crop")
            return []
        cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")

    samples: list[tuple[float, float, float]] = []
    index = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if index % every == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            h, w = frame.shape[:2]
            scale = 480.0 / max(w, h)
            small = cv2.resize(frame, (int(w * scale), int(h * scale)))
            sh, sw = small.shape[:2]
            t = index / fps
            if detector is not None:
                detector.setInputSize((sw, sh))
                _, faces = detector.detect(small)
                boxes = [] if faces is None else [(f[0], f[1], f[2], f[3]) for f in faces]
            else:
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                boxes = list(cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=(24, 24)))
            for (x, y, bw, bh) in boxes:
                samples.append((t, float(x + bw / 2) / sw, float(bw * bh) / (sw * sh)))
        index += 1
    cap.release()
    samples.sort(key=lambda s: s[0])
    return samples


# --------------------------------------------------------------------------------------
# Captions: faster-whisper
# --------------------------------------------------------------------------------------

_whisper_lock = threading.Lock()


@lru_cache(maxsize=2)
def _whisper_model(name: str):
    from faster_whisper import WhisperModel  # lazy

    return WhisperModel(name, device="cpu", compute_type="int8")


def whisper_words(audio: Path, language_code: str, model_name: str) -> list[tuple[str, float, float]]:
    """Word-level timestamps for a (vocals) audio file. Returns (word, start, end)."""
    lang = (language_code or "en").split("-")[0].lower()
    with _whisper_lock:  # one transcription at a time keeps RAM use predictable
        model = _whisper_model(model_name)
        segments, _info = model.transcribe(
            str(audio),
            language=lang,
            word_timestamps=True,
            vad_filter=True,
            beam_size=5,
            condition_on_previous_text=False,
        )
        words: list[tuple[str, float, float]] = []
        for seg in segments:
            for w in seg.words or []:
                text = (w.word or "").strip()
                if text:
                    words.append((text, float(w.start), float(max(w.end, w.start + 0.05))))
    return words
