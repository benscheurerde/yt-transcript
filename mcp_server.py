"""MCP Server fuer die YouTube Transcript API.

Exponiert die REST API als MCP Tools, damit Claude die Transkripte
direkt als Tool nutzen kann.

Laeuft als separater Prozess (stdio-basiert) und spricht mit der REST API.
"""

import json
import os
import urllib.error
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "YouTube Transcript",
    description="YouTube-Transkripte abrufen, durchsuchen und analysieren.",
)

API_BASE = os.environ.get("YT_TRANSCRIPT_API_URL", "https://yt-transcript.sac.sh")
API_TOKEN = os.environ.get("YT_TRANSCRIPT_API_TOKEN", "")


def _api_request(path: str, method: str = "GET", body: dict | None = None) -> dict[str, Any]:
    url = f"{API_BASE}{path}"
    headers = {"Content-Type": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"

    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        try:
            detail = json.loads(error_body).get("detail", error_body)
        except Exception:
            detail = error_body
        return {"error": True, "status": e.code, "detail": detail}
    except Exception as e:
        return {"error": True, "detail": str(e)}


@mcp.tool()
def get_transcript(
    video: str,
    lang: str = "de,en",
    timestamps: bool = False,
    format: str = "text",
    max_chars: int | None = None,
) -> str:
    """Holt das Transkript eines YouTube-Videos.

    Args:
        video: YouTube-URL (alle Formate) oder Video-ID
        lang: Kommaseparierte Sprachcodes in Prioritaetsreihenfolge (default: de,en)
        timestamps: Timestamps im Plaintext mitliefern
        format: Ausgabeformat - text (Fliesstext), segments (Array), srt (Untertitel)
        max_chars: Maximale Zeichenanzahl (nur bei format=text)
    """
    params = f"?video={video}&lang={lang}&timestamps={str(timestamps).lower()}&format={format}"
    if max_chars:
        params += f"&max_chars={max_chars}"
    result = _api_request(f"/transcript{params}")
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def get_transcripts_batch(
    videos: list[str],
    lang: str = "de,en",
    timestamps: bool = False,
    format: str = "text",
    max_chars: int | None = None,
) -> str:
    """Holt Transkripte fuer mehrere YouTube-Videos auf einmal (max. 10).

    Args:
        videos: Liste von YouTube-URLs oder Video-IDs
        lang: Kommaseparierte Sprachcodes (default: de,en)
        timestamps: Timestamps mitliefern
        format: Ausgabeformat - text, segments, srt
        max_chars: Maximale Zeichenanzahl pro Video
    """
    body = {
        "videos": videos,
        "lang": lang,
        "timestamps": timestamps,
        "format": format,
    }
    if max_chars:
        body["max_chars"] = max_chars
    result = _api_request("/transcript/batch", method="POST", body=body)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def list_transcript_languages(video: str) -> str:
    """Listet alle verfuegbaren Transkript-Sprachen fuer ein YouTube-Video.

    Args:
        video: YouTube-URL oder Video-ID
    """
    result = _api_request(f"/transcript/list?video={video}")
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
