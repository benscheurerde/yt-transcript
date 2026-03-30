"""MCP Server fuer YouTube Transkripte.

Laeuft als lokaler stdio-Prozess in Claude Code.
Nutzt youtube-transcript-api (funktioniert nur von Residential IPs).
"""

import json
import re
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

mcp = FastMCP(
    "YouTube Transcript",
    description="YouTube-Transkripte abrufen und analysieren.",
)

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
    return video


def _format_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _get_metadata(video_id: str) -> dict:
    url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "yt-transcript-mcp/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return {
                "title": data.get("title"),
                "author": data.get("author_name"),
                "author_url": data.get("author_url"),
                "thumbnail": data.get("thumbnail_url"),
            }
    except Exception:
        return {}


@mcp.tool()
def get_transcript(
    video: str,
    lang: str = "de,en",
    timestamps: bool = False,
    max_chars: int | None = None,
) -> str:
    """Holt das Transkript eines YouTube-Videos als Plaintext.

    Args:
        video: YouTube-URL (alle Formate) oder Video-ID
        lang: Kommaseparierte Sprachcodes in Prioritaetsreihenfolge (default: de,en)
        timestamps: Timestamps im Text mitliefern
        max_chars: Maximale Zeichenanzahl
    """
    video_id = _extract_video_id(video)
    languages = [l.strip() for l in lang.split(",")]
    metadata = _get_metadata(video_id)

    try:
        ytt = YouTubeTranscriptApi()
        transcript = ytt.fetch(video_id, languages=languages)
        snippets = list(transcript)
    except NoTranscriptFound:
        return json.dumps({
            "video_id": video_id,
            "metadata": metadata,
            "error": f"Kein Transkript in den Sprachen {languages} verfuegbar.",
        }, ensure_ascii=False, indent=2)
    except TranscriptsDisabled:
        return json.dumps({
            "video_id": video_id,
            "metadata": metadata,
            "error": "Transkripte sind fuer dieses Video deaktiviert.",
        }, ensure_ascii=False, indent=2)
    except VideoUnavailable:
        return json.dumps({
            "video_id": video_id,
            "error": "Video nicht verfuegbar.",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "video_id": video_id,
            "error": str(e),
        }, ensure_ascii=False, indent=2)

    if timestamps:
        lines = [f"[{_format_time(s.start)}] {s.text}" for s in snippets]
        text = "\n".join(lines)
    else:
        text = " ".join(s.text for s in snippets)

    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + "..."

    return json.dumps({
        "video_id": video_id,
        "language": languages[0],
        "metadata": metadata,
        "text": text,
    }, ensure_ascii=False, indent=2)


@mcp.tool()
def get_transcripts_batch(
    videos: list[str],
    lang: str = "de,en",
    timestamps: bool = False,
    max_chars: int | None = None,
) -> str:
    """Holt Transkripte fuer mehrere YouTube-Videos auf einmal (max. 10).

    Args:
        videos: Liste von YouTube-URLs oder Video-IDs
        lang: Kommaseparierte Sprachcodes (default: de,en)
        timestamps: Timestamps mitliefern
        max_chars: Maximale Zeichenanzahl pro Video
    """
    if len(videos) > 10:
        return json.dumps({"error": "Maximal 10 Videos pro Batch."}, ensure_ascii=False)

    results = []
    for video in videos:
        result_str = get_transcript(video, lang, timestamps, max_chars)
        results.append(json.loads(result_str))

    return json.dumps({"results": results}, ensure_ascii=False, indent=2)


@mcp.tool()
def list_transcript_languages(video: str) -> str:
    """Listet alle verfuegbaren Transkript-Sprachen fuer ein YouTube-Video.

    Args:
        video: YouTube-URL oder Video-ID
    """
    video_id = _extract_video_id(video)
    metadata = _get_metadata(video_id)

    try:
        ytt = YouTubeTranscriptApi()
        transcript_list = ytt.list(video_id)
        available = []
        for t in transcript_list:
            available.append({
                "language": t.language,
                "language_code": t.language_code,
                "is_generated": t.is_generated,
                "is_translatable": t.is_translatable,
            })
        return json.dumps({
            "video_id": video_id,
            "metadata": metadata,
            "available_transcripts": available,
        }, ensure_ascii=False, indent=2)
    except TranscriptsDisabled:
        return json.dumps({
            "video_id": video_id,
            "metadata": metadata,
            "error": "Transkripte sind deaktiviert.",
        }, ensure_ascii=False, indent=2)
    except VideoUnavailable:
        return json.dumps({
            "video_id": video_id,
            "error": "Video nicht verfuegbar.",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({
            "video_id": video_id,
            "error": str(e),
        }, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
