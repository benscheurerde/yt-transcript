import os
import re
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

app = FastAPI(
    title="YouTube Transcript API",
    description="Holt YouTube-Transkripte als Plaintext, Segmente oder SRT. Ideal als Tool für KI-Assistenten.",
    version="1.0.0",
)

security = HTTPBearer(auto_error=False)

API_TOKEN = os.environ.get("API_TOKEN")

# Optional Redis
REDIS_URL = os.environ.get("REDIS_URL")
CACHE_TTL = int(os.environ.get("CACHE_TTL", "3600"))

_redis = None


def _get_redis():
    global _redis
    if _redis is not None:
        return _redis
    if not REDIS_URL:
        return None
    try:
        import redis

        _redis = redis.from_url(REDIS_URL, decode_responses=True)
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


def _fetch_transcript(video_id: str, languages: list[str]):
    try:
        ytt_api = YouTubeTranscriptApi()
        transcript = ytt_api.fetch(video_id, languages=languages)
        return transcript
    except NoTranscriptFound:
        raise HTTPException(
            status_code=404,
            detail=f"Kein Transkript in den Sprachen {languages} verfuegbar.",
        )
    except TranscriptsDisabled:
        raise HTTPException(
            status_code=404,
            detail="Transkripte sind fuer dieses Video deaktiviert.",
        )
    except VideoUnavailable:
        raise HTTPException(status_code=404, detail="Video nicht verfuegbar.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _get_metadata(video_id: str) -> dict:
    """Versucht Metadaten via oembed API abzurufen (kein API Key noetig)."""
    import json
    import urllib.request
    import urllib.error

    cache_key = f"meta:{video_id}"
    cached = _cache_get(cache_key)
    if cached:
        return json.loads(cached)

    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "yt-transcript-api/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            meta = {
                "title": data.get("title"),
                "author": data.get("author_name"),
                "author_url": data.get("author_url"),
                "thumbnail": data.get("thumbnail_url"),
            }
            _cache_set(cache_key, json.dumps(meta))
            return meta
    except Exception:
        return {}


@app.get("/")
def root():
    return {
        "service": "YouTube Transcript API",
        "version": "1.0.0",
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
        import json

        return json.loads(cached)

    transcript = _fetch_transcript(video_id, languages)
    snippets = [snippet for snippet in transcript]
    metadata = _get_metadata(video_id)

    if format == "segments":
        result = {
            "video_id": video_id,
            "language": languages,
            "metadata": metadata or None,
            "segments": [
                {"start": s.start, "duration": s.duration, "text": s.text}
                for s in snippets
            ],
        }
    elif format == "srt":
        srt_lines = []
        for i, s in enumerate(snippets, 1):
            start_time = _format_srt_time(s.start)
            end_time = _format_srt_time(s.start + s.duration)
            srt_lines.append(f"{i}\n{start_time} --> {end_time}\n{s.text}\n")
        result = {
            "video_id": video_id,
            "language": languages,
            "metadata": metadata or None,
            "srt": "\n".join(srt_lines),
        }
    else:
        if timestamps:
            lines = [f"[{_format_time(s.start)}] {s.text}" for s in snippets]
        else:
            lines = [s.text for s in snippets]
        text = " ".join(lines) if not timestamps else "\n".join(lines)
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "..."
        result = {
            "video_id": video_id,
            "language": languages,
            "metadata": metadata or None,
            "text": text,
        }

    import json

    _cache_set(cache_key, json.dumps(result))
    return result


from pydantic import BaseModel


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
    for video in body.videos:
        try:
            video_id = _extract_video_id(video)
            languages = [l.strip() for l in body.lang.split(",")]
            transcript = _fetch_transcript(video_id, languages)
            snippets = [snippet for snippet in transcript]
            metadata = _get_metadata(video_id)

            if body.format == "segments":
                entry = {
                    "video_id": video_id,
                    "metadata": metadata or None,
                    "segments": [
                        {"start": s.start, "duration": s.duration, "text": s.text}
                        for s in snippets
                    ],
                }
            elif body.format == "srt":
                srt_lines = []
                for i, s in enumerate(snippets, 1):
                    start_time = _format_srt_time(s.start)
                    end_time = _format_srt_time(s.start + s.duration)
                    srt_lines.append(f"{i}\n{start_time} --> {end_time}\n{s.text}\n")
                entry = {
                    "video_id": video_id,
                    "metadata": metadata or None,
                    "srt": "\n".join(srt_lines),
                }
            else:
                if body.timestamps:
                    lines = [f"[{_format_time(s.start)}] {s.text}" for s in snippets]
                else:
                    lines = [s.text for s in snippets]
                text = " ".join(lines) if not body.timestamps else "\n".join(lines)
                if body.max_chars and len(text) > body.max_chars:
                    text = text[:body.max_chars] + "..."
                entry = {
                    "video_id": video_id,
                    "metadata": metadata or None,
                    "text": text,
                }
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

    try:
        ytt_api = YouTubeTranscriptApi()
        transcript_list = ytt_api.list(video_id)
        available = []
        for t in transcript_list:
            available.append(
                {
                    "language": t.language,
                    "language_code": t.language_code,
                    "is_generated": t.is_generated,
                    "is_translatable": t.is_translatable,
                }
            )
        metadata = _get_metadata(video_id)
        return {
            "video_id": video_id,
            "metadata": metadata or None,
            "available_transcripts": available,
        }
    except TranscriptsDisabled:
        raise HTTPException(
            status_code=404,
            detail="Transkripte sind fuer dieses Video deaktiviert.",
        )
    except VideoUnavailable:
        raise HTTPException(status_code=404, detail="Video nicht verfuegbar.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
