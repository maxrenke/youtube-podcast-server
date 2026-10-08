# AGENTS.md - Development Guidelines for youtube-podcast-server

## Project Overview

A small Python service that downloads YouTube videos as mp3 and serves them as a podcast RSS feed.
Two modules: `rss_downloader.py` (HTTP server, RSS, UI) and `tasks.py` (queue, worker, subscriptions, yt-dlp calls).
See `readme.md` for the API, configuration and metadata details, and `youtube-podcast-server-TODO.md` for audit findings and proposals.

## Tech Stack

- **Language**: Python 3.11+ (image uses 3.12); stdlib only, no requirements.txt
- **Dependencies**: yt-dlp (YouTube downloading), ffmpeg (audio processing)
- **Built-in modules**: http.server, socketserver, urllib, xml.sax.saxutils
- **Container**: Docker

---

## Build / Run Commands

### Local Development

```bash
# Needs yt-dlp and ffmpeg on PATH. Starts the server on 0.0.0.0:8080.
python rss_downloader.py

# Queue a download
curl -X POST http://localhost:8080/download -H "Content-Type: application/json" \n  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID"}'
```

### Docker

```bash
# Build Docker image
docker build -t youtube-podcast-server .

# Run container
docker run -p 5757:8080 -v $(pwd)/downloads:/app/downloads youtube-podcast-server

# Using docker-compose
docker-compose up -d
```

### Testing

There are currently **no formal tests** in this project. When adding tests:

```bash
# Run pytest (if tests are added)
pytest

# Run a single test file
pytest tests/test_rss_downloader.py

# Run a single test function
pytest tests/test_rss_downloader.py::test_function_name -v
```

### Linting / Type Checking

Install development dependencies if a requirements-dev.txt exists, otherwise:

```bash
# Install linting tools
pip install ruff mypy

# Run ruff linter
ruff check .

# Run ruff with auto-fix
ruff check --fix .

# Run mypy type checker
mypy .

# Format code with ruff
ruff format .
```

---

## Code Style Guidelines

### General Principles

- Write clean, readable, and simple code
- Keep functions focused and small (single responsibility)
- Use descriptive variable and function names
- Handle errors explicitly with try/except blocks

### Imports

```python
# Standard library imports first, then third-party, then local
import sys
import subprocess
import os
import http.server
import socketserver
import urllib.parse
from datetime import datetime
from xml.sax.saxutils import escape
import json

# Order: stdlib > third-party > local
# Alphabetical within each group
```

### Formatting

- **Line length**: Maximum 100 characters (soft limit at 120)
- **Indentation**: 4 spaces (no tabs)
- **Blank lines**: Two blank lines between top-level definitions
- **Trailing whitespace**: Remove at end of lines

### Naming Conventions

- **Variables/functions**: `snake_case` (e.g., `download_dir`, `generate_rss_feed`)
- **Constants**: `UPPER_SNAKE_CASE` (e.g., `DOWNLOAD_DIR`, `AUDIO_EXT`)
- **Classes**: `PascalCase` (e.g., `RSSRequestHandler`)
- **Private functions**: Prefix with underscore (e.g., `_internal_function`)

### Type Hints

Use type hints where beneficial for clarity:

```python
def download_audio(youtube_url: str) -> None:
    ...

def generate_rss_feed() -> str:
    ...
```

### Error Handling

```python
# Use specific exception types
try:
    subprocess.run(command, check=True)
except subprocess.CalledProcessError as e:
    print(f"Error: {e}")

# Handle JSON parsing explicitly
try:
    data = json.loads(post_data)
except json.JSONDecodeError:
    self.send_error(400, "Invalid JSON")
```

### Docstrings

Use docstrings for public functions and classes:

```python
def download_audio(youtube_url: str) -> None:
    """Download a YouTube video as MP3 audio.
    
    Args:
        youtube_url: The full YouTube video URL.
        
    Raises:
        subprocess.CalledProcessError: If yt-dlp fails.
    """
```

### File Structure

```
youtube-podcast-server/
|- rss_downloader.py      # HTTP server, RSS generation, inline UI
|- tasks.py               # task queue, worker, scheduler, yt-dlp invocations
|- auth.py                # admin account, sessions, two-step codes, throttling
|- artwork.jpg            # podcast cover served at /artwork.jpg
|- Dockerfile, docker-compose.yml, deploy.ps1
|- readme.md              # user and API documentation
|- youtube-podcast-server-TODO.md   # audit findings and proposals
|- downloads/, state/     # runtime data (git-ignored)
```

---

## API and configuration

Documented in `readme.md` (HTTP API, env vars, episode metadata). Keep that file in sync with any route or option change.

---

## Notes for Agents

1. **Tests**: `python -m pytest -q tests` (no network needed). Add tests for new logic.
2. **yt-dlp binary**: in Docker the standalone yt-dlp binary is used, fetched at build time.
3. **Pre-commit**: `ruff check`, `mypy` and `pytest` must pass; do not bypass the hook.
4. **Security**: no authentication. The live instance is public through a Cloudflare Tunnel, so treat every request field as hostile (URLs are validated in `tasks.is_valid_url` and passed to yt-dlp after `--`).
