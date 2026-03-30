import json
import os
import re
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

app = FastAPI(
    title="YouTube Transcript API",
    description="Holt YouTube-Transkripte als Plaintext, Segmente oder SRT.",
    version="3.0.0",
)

security = HTTPBearer(auto_error=False)

API_TOKEN = os.environ.get("API_TOKEN")

# Optional Redis
REDIS_URL = os.environ.get("REDIS_URL")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "3600"))

_redis = None
_session = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
    return _session


def _get_redis():
    global _redis
    if _redis is not None:
        return _redis
    if not REDIS_URL:
        return None
    try:
        import redis as redis_lib
        _redis = redis_lib.from_url(REDIS_URL, decode_responses=True)
        _redis.ping()
        return _redis
    except Exception:
        _redis = False
        return None


def _cache_get(key: str) -> Optional[str]:
    r = _get_redis()
    if not r:
        return None
    try:
        return r.get(key)
    except Exception:
        return None


def _cache_set(key: str, value: str):
    r = _get_redis()
    if not r:
        return
    try:
        r.setex(key, CACHE_TTL, value)
    except Exception:
        pass


def _verify_token(credentials: Optional[HTTPAuthorizationCredentials]):
    if not API_TOKEN:
        return
    if not credentials or credentials.credentials != API_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


VIDEO_ID_PATTERNS = [
    r"(?:youtube\.com/watch\?.*v=)([\w-]{11})",
    r"(?:youtu\.be/)([\w-]{11})",
    r"(?:youtube\.com/embed/)([\w-]{11})",
    r"(?:youtube\.com/shorts/)([\w-]{11})",
    r"(?:youtube\.com/live/)([\w-]{11})",
    r"^([\w-]{11})$",
]


def _extract_video_id(video: str) -> str:
    video = video.strip()
    for pattern in VIDEO_ID_PATTERNS:
        match = re.search(pattern, video)
        if match:
            return match.group(1)
    raise HTTPException(status_code=400, detail=f"Ungueltige Video-URL oder ID: {video}")


def _format_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _get_video_data(video_id: str) -> dict:
    """Holt Video-Seite und extrahiert captions + metadata aus dem HTML."""
    cache_key = f"videodata:{video_id}"
    cached = _cache_get(cache_key)
    if cached:
        return json.loads(cached)

    session = _get_session()
    resp = session.get(
        f"https://www.youtube.com/watch?v={video_id}",
        timeout=15,
    )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"YouTube nicht erreichbar: {resp.status_code}")

    html = resp.text

    # Bot detection check
    if "Sign in to confirm" in html or "/sorry/" in html:
        raise HTTPException(status_code=503, detail="YouTube Bot-Detection aktiv. Bitte spaeter erneut versuchen.")

    # Extract captions
    captions_match = re.search(r'"captions":(.*?),"videoDetails"', html)
    caption_tracks = []
    if captions_match:
        try:
            captions = json.loads(captions_match.group(1))
            caption_tracks = captions.get("playerCaptionsTracklistRenderer", {}).get("captionTracks", [])
        except json.JSONDecodeError:
            pass

    # Extract metadata
    metadata = {}
    details_match = re.search(r'"videoDetails":(.*?),"playerConfig"', html)
    if not details_match:
        details_match = re.search(r'"videoDetails":(.*?),"annotations"', html)
    if details_match:
        try:
            details = json.loads(details_match.group(1))
            metadata = {
                "title": details.get("title"),
                "author": details.get("author"),
                "channel_id": details.get("channelId"),
                "duration": int(details.get("lengthSeconds", 0)),
                "view_count": int(details.get("viewCount", 0)),
                "thumbnail": details.get("thumbnail", {}).get("thumbnails", [{}])[-1].get("url"),
            }
        except (json.JSONDecodeError, ValueError):
            pass

    # Fallback metadata via oembed
    if not metadata.get("title"):
        try:
            oembed_resp = session.get(
                f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json",
                timeout=5,
            )
            if oembed_resp.status_code == 200:
                data = oembed_resp.json()
                metadata = {
                    "title": data.get("title"),
                    "author": data.get("author_name"),
                    "author_url": data.get("author_url"),
                    "thumbnail": data.get("thumbnail_url"),
                }
        except Exception:
            pass

    result = {
        "caption_tracks": caption_tracks,
        "metadata": metadata,
    }
    _cache_set(cache_key, json.dumps(result))
    return result


def _fetch_timedtext(base_url: str) -> list[dict]:
    """Holt Transkript-Segmente von der timedtext URL."""
    url = base_url
    if "fmt=" not in url:
        url += "&fmt=json3"

    session = _get_session()
    resp = session.get(url, timeout=15)

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Transkript konnte nicht geladen werden.")

    data = resp.json()
    events = data.get("events", [])
    segments = []

    for event in events:
        segs = event.get("segs")
        if not segs:
            continue
        start_ms = event.get("tStartMs", 0)
        duration_ms = event.get("dDurationMs", 0)
        text = "".join(s.get("utf8", "") for s in segs).strip()
        text = text.replace("\n", " ")
        if text:
            segments.append({
                "start": round(start_ms / 1000, 3),
                "duration": round(duration_ms / 1000, 3),
                "text": text,
            })

    return segments


def _fetch_transcript(video_id: str, languages: list[str]) -> tuple[list[dict], str, dict]:
    """Holt Transkript. Gibt (segments, language_code, metadata) zurueck."""
    video_data = _get_video_data(video_id)
    tracks = video_data.get("caption_tracks", [])
    metadata = video_data.get("metadata", {})

    if not tracks:
        raise HTTPException(
            status_code=404,
            detail="Keine Transkripte fuer dieses Video verfuegbar.",
        )

    # Suche Track in gewuenschter Sprache
    chosen_track = None
    chosen_lang = None

    for lang in languages:
        for track in tracks:
            if track.get("languageCode", "").startswith(lang):
                chosen_track = track
                chosen_lang = track.get("languageCode", lang)
                break
        if chosen_track:
            break

    # Fallback: erster verfuegbarer Track
    if not chosen_track:
        chosen_track = tracks[0]
        chosen_lang = chosen_track.get("languageCode", "unknown")

    base_url = chosen_track.get("baseUrl")
    if not base_url:
        raise HTTPException(status_code=500, detail="Keine Transkript-URL gefunden.")

    segments = _fetch_timedtext(base_url)

    if not segments:
        raise HTTPException(status_code=404, detail="Transkript ist leer.")

    return segments, chosen_lang, metadata


def _build_result(video_id: str, language: str, segments: list[dict], metadata: dict, fmt: str, timestamps: bool, max_chars: Optional[int]):
    if fmt == "segments":
        return {
            "video_id": video_id,
            "language": language,
            "metadata": metadata or None,
            "segments": segments,
        }
    elif fmt == "srt":
        srt_lines = []
        for i, s in enumerate(segments, 1):
            start_time = _format_srt_time(s["start"])
            end_time = _format_srt_time(s["start"] + s["duration"])
            srt_lines.append(f"{i}\n{start_time} --> {end_time}\n{s['text']}\n")
        return {
            "video_id": video_id,
            "language": language,
            "metadata": metadata or None,
            "srt": "\n".join(srt_lines),
        }
    else:
        if timestamps:
            lines = [f"[{_format_time(s['start'])}] {s['text']}" for s in segments]
        else:
            lines = [s["text"] for s in segments]
        text = " ".join(lines) if not timestamps else "\n".join(lines)
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "..."
        return {
            "video_id": video_id,
            "language": language,
            "metadata": metadata or None,
            "text": text,
        }


@app.get("/")
def root():
    return {
        "service": "YouTube Transcript API",
        "version": "3.0.0",
        "endpoints": {
            "GET /transcript": "Transkript eines Videos abrufen",
            "POST /transcript/batch": "Transkripte mehrerer Videos abrufen",
            "GET /transcript/list": "Verfuegbare Sprachen auflisten",
            "GET /health": "Health Check",
            "GET /openapi.json": "OpenAPI Spec",
        },
    }


@app.get("/health")
def health():
    redis_status = "connected" if _get_redis() else ("not configured" if not REDIS_URL else "error")
    return {"status": "ok", "redis": redis_status}


@app.get("/transcript")
def get_transcript(
    video: str = Query(..., description="YouTube-URL (alle Formate) oder Video-ID"),
    lang: str = Query("de,en", description="Kommaseparierte Sprachcodes in Prioritaetsreihenfolge"),
    timestamps: bool = Query(False, description="Timestamps im Plaintext mitliefern"),
    format: str = Query("text", description="Ausgabeformat: text, segments, srt"),
    max_chars: Optional[int] = Query(None, description="Maximale Zeichenanzahl (nur bei format=text)"),
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
):
    _verify_token(credentials)
    video_id = _extract_video_id(video)
    languages = [l.strip() for l in lang.split(",")]

    cache_key = f"transcript:{video_id}:{lang}:{timestamps}:{format}:{max_chars}"
    cached = _cache_get(cache_key)
    if cached:
        return json.loads(cached)

    segments, found_lang, metadata = _fetch_transcript(video_id, languages)
    result = _build_result(video_id, found_lang, segments, metadata, format, timestamps, max_chars)

    _cache_set(cache_key, json.dumps(result))
    return result


class BatchRequest(BaseModel):
    videos: list[str]
    lang: str = "de,en"
    timestamps: bool = False
    format: str = "text"
    max_chars: Optional[int] = None


@app.post("/transcript/batch")
def get_transcripts_batch(
    body: BatchRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
):
    _verify_token(credentials)
    if len(body.videos) > 10:
        raise HTTPException(status_code=400, detail="Maximal 10 Videos pro Batch-Request.")

    results = []
    languages = [l.strip() for l in body.lang.split(",")]

    for video in body.videos:
        try:
            video_id = _extract_video_id(video)
            segments, found_lang, metadata = _fetch_transcript(video_id, languages)
            entry = _build_result(video_id, found_lang, segments, metadata, body.format, body.timestamps, body.max_chars)
            results.append(entry)
        except HTTPException as e:
            results.append({"video_id": video, "error": e.detail})
        except Exception as e:
            results.append({"video_id": video, "error": str(e)})

    return {"results": results}


@app.get("/transcript/list")
def list_transcripts(
    video: str = Query(..., description="YouTube-URL oder Video-ID"),
    credentials: Optional[HTTPAuthorizationCredentials] = Security(security),
):
    _verify_token(credentials)
    video_id = _extract_video_id(video)
    video_data = _get_video_data(video_id)
    tracks = video_data.get("caption_tracks", [])
    metadata = video_data.get("metadata", {})

    available = []
    for t in tracks:
        available.append({
            "language": t.get("name", {}).get("simpleText", t.get("languageCode", "")),
            "language_code": t.get("languageCode", ""),
            "is_generated": t.get("kind") == "asr",
            "is_translatable": bool(t.get("isTranslatable")),
        })

    return {
        "video_id": video_id,
        "metadata": metadata or None,
        "available_transcripts": available,
    }
