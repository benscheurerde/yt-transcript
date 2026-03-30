# YouTube Transcript API

Simpler API-Service der YouTube-Transkripte abruft. Verfuegbar als REST API und MCP Server.

## Endpoints

- `GET /transcript` - Transkript eines Videos (text/segments/srt)
- `POST /transcript/batch` - Mehrere Videos auf einmal (max. 10)
- `GET /transcript/list` - Verfuegbare Sprachen
- `GET /health` - Health Check
- `GET /openapi.json` - OpenAPI Spec (via FastAPI)

## Environment Variables

| Variable | Required | Default | Beschreibung |
|----------|----------|---------|-------------|
| `API_TOKEN` | Nein | - | Bearer Token fuer Auth |
| `REDIS_URL` | Nein | - | Redis Connection URL fuer Caching |
| `CACHE_TTL` | Nein | 3600 | Cache TTL in Sekunden |

## Docker

```bash
# Lokal bauen und starten
docker compose up -d

# Oder von Docker Hub
docker pull benscheurerde/yt-transcript:latest
```

## MCP Server

Der MCP Server (`mcp_server.py`) spricht als stdio-Client mit der REST API.

### Claude Code Integration

In `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "yt-transcript": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/benscheurer/yt-transcript.git", "python", "-m", "mcp_server"],
      "env": {
        "YT_TRANSCRIPT_API_URL": "https://yt-transcript.sac.sh",
        "YT_TRANSCRIPT_API_TOKEN": "dein-token"
      }
    }
  }
}
```

Oder lokal:

```json
{
  "mcpServers": {
    "yt-transcript": {
      "command": "python3",
      "args": ["/pfad/zu/mcp_server.py"],
      "env": {
        "YT_TRANSCRIPT_API_URL": "https://yt-transcript.sac.sh",
        "YT_TRANSCRIPT_API_TOKEN": "dein-token"
      }
    }
  }
}
```
