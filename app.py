import json
import os
import re
import subprocess
import tempfile
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

app = FastAPI(
    title="YouTube Transcript API",
    description="Holt YouTube-Transkripte als Plaintext, Segmente oder SRT. Nutzt yt-dlp als Backend.",
    version="2.0.0",
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


def _get_metadata(video_id: str) -> dict:
    """Metadaten via yt-dlp abrufen (kein API Key noetig)."""
    cache_key = f"meta:{video_id}"
    cached = _cache_get(cache_key)
    if cached:
        return json.loads(cached)

    try:
        result = subprocess.run(
            [
                "yt-dlp",
                "--dump-json",
                "--skip-download",
                "--no-warnings",
                f"https://www.youtube.com/watch?v={video_id}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        meta = {
            "title": data.get("title"),
            "author": data.get("uploader") or data.get("channel"),
            "author_url": data.get("uploader_url") or data.get("channel_url"),
            "thumbnail": data.get("thumbnail"),
            "duration": data.get("duration"),
            "upload_date": data.get("upload_date"),
            "view_count": data.get("view_count"),
        }
        _cache_set(cache_key, json.dumps(meta))
        return meta
    except Exception:
        return {}


def _parse_vtt(vtt_content: str) -> list[dict]:
    """Parst VTT-Untertitel in eine Liste von Segmenten."""
    segments = []
    lines = vtt_content.strip().split("\n")
    i = 0
    while i < len(lines):
        # Suche nach Timestamp-Zeilen (00:00:00.000 --> 00:00:01.000)
        if "-->" in lines[i]:
            time_parts = lines[i].split("-->")
            start = _parse_vtt_time(time_parts[0].strip())
            end = _parse_vtt_time(time_parts[1].strip())
            # Sammle Text-Zeilen bis zur naechsten Leerzeile
            text_lines = []
            i += 1
            while i < len(lines) and lines[i].strip():
                # VTT Tags entfernen
                clean = re.sub(r"<[^>]+>", "", lines[i].strip())
                if clean:
                    text_lines.append(clean)
                i += 1
            if text_lines:
                segments.append({
                    "start": start,
                    "duration": round(end - start, 3),
                    "text": " ".join(text_lines),
                })
        else:
            i += 1
    # Deduplizieren (VTT hat oft doppelte Zeilen)
    deduped = []
    seen_texts = set()
    for seg in segments:
        if seg["text"] not in seen_texts:
            deduped.append(seg)
            seen_texts.add(seg["text"])
    return deduped


def _parse_vtt_time(time_str: str) -> float:
    """Parst VTT Timestamp (00:00:00.000) in Sekunden."""
    time_str = time_str.strip()
    parts = time_str.replace(",", ".").split(":")
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    elif len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def _fetch_transcript(video_id: str, languages: list[str]) -> list[dict]:
    """Holt Transkript via yt-dlp."""
    url = f"https://www.youtube.com/watch?v={video_id}"

    with tempfile.TemporaryDirectory() as tmpdir:
        # Versuche erst manuell erstellte Untertitel, dann auto-generierte
        lang_str = ",".join(languages)
        output_path = os.path.join(tmpdir, "sub")

        result = subprocess.run(
            [
                "yt-dlp",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs", lang_str,
                "--sub-format", "vtt",
                "--skip-download",
                "--no-warnings",
                "-o", output_path,
                url,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "Video unavailable" in stderr or "Private video" in stderr:
                raise HTTPException(status_code=404, detail="Video nicht verfuegbar.")
            raise HTTPException(status_code=500, detail=f"yt-dlp Fehler: {stderr[:500]}")

        # Suche nach heruntergeladener VTT-Datei
        import glob
        vtt_files = glob.glob(os.path.join(tmpdir, "*.vtt"))

        if not vtt_files:
            raise HTTPException(
                status_code=404,
                detail=f"Kein Transkript in den Sprachen {languages} verfuegbar.",
            )

        # Bevorzuge manuell erstellte vor auto-generierten
        chosen_file = vtt_files[0]
        for f in vtt_files:
            # Dateien ohne ".auto." im Namen bevorzugen
            if not re.search(r"\.auto\.", os.path.basename(f)):
                chosen_file = f
                break

        with open(chosen_file, "r", encoding="utf-8") as f:
            vtt_content = f.read()

        return _parse_vtt(vtt_content)


def _list_available_subs(video_id: str) -> list[dict]:
    """Listet verfuegbare Untertitel-Sprachen via yt-dlp."""
    url = f"https://www.youtube.com/watch?v={video_id}"

    result = subprocess.run(
        [
            "yt-dlp",
            "--list-subs",
            "--skip-download",
            "--no-warnings",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "Video unavailable" in stderr or "Private video" in stderr:
            raise HTTPException(status_code=404, detail="Video nicht verfuegbar.")
        raise HTTPException(status_code=500, detail=f"yt-dlp Fehler: {stderr[:500]}")

    subs = []
    output = result.stdout
    in_manual = False
    in_auto = False

    for line in output.split("\n"):
        line = line.strip()
        if not line:
            continue
        if "Available subtitles" in line:
            in_manual = True
            in_auto = False
            continue
        if "Available automatic captions" in line:
            in_auto = True
            in_manual = False
            continue

        if in_manual or in_auto:
            # Format: "de       vtt, ..." oder "de  German    vtt, ..."
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) <= 10:
                lang_code = parts[0]
                # Versuche Sprachnamen zu extrahieren
                lang_name = ""
                for p in parts[1:]:
                    if p in ("vtt", "ttml", "srv1", "srv2", "srv3", "json3"):
                        break
                    lang_name += p + " "
                lang_name = lang_name.strip()

                subs.append({
                    "language": lang_name or lang_code,
                    "language_code": lang_code,
                    "is_generated": in_auto,
                    "is_translatable": in_auto,
                })

    return subs


@app.get("/")
def root():
    return {
        "service": "YouTube Transcript API",
        "version": "2.0.0",
        "backend": "yt-dlp",
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
    # Check yt-dlp version
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
        ytdlp_version = result.stdout.strip() if result.returncode == 0 else "error"
    except Exception:
        ytdlp_version = "not installed"
    return {"status": "ok", "redis": redis_status, "yt_dlp": ytdlp_version}


def _build_result(video_id: str, languages: list[str], segments: list[dict], metadata: dict, fmt: str, timestamps: bool, max_chars: Optional[int]):
    if fmt == "segments":
        return {
            "video_id": video_id,
            "language": languages,
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
            "language": languages,
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
            "language": languages,
            "metadata": metadata or None,
            "text": text,
        }


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

    segments = _fetch_transcript(video_id, languages)
    metadata = _get_metadata(video_id)
    result = _build_result(video_id, languages, segments, metadata, format, timestamps, max_chars)

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
            segments = _fetch_transcript(video_id, languages)
            metadata = _get_metadata(video_id)
            entry = _build_result(video_id, languages, segments, metadata, body.format, body.timestamps, body.max_chars)
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
    available = _list_available_subs(video_id)
    metadata = _get_metadata(video_id)

    return {
        "video_id": video_id,
        "metadata": metadata or None,
        "available_transcripts": available,
    }
