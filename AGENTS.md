# AGENTS.md - Development Guidelines for youtube-podcast-server

## Project Overview

This is a lightweight Python application that downloads YouTube videos as audio files and serves them via an RSS feed. The project consists of a single main file: `rss_downloader.py`.

## Tech Stack

- **Language**: Python 3.9+
- **Dependencies**: yt-dlp (YouTube downloading), ffmpeg (audio processing)
- **Built-in modules**: http.server, socketserver, urllib, xml.sax.saxutils
- **Container**: Docker

---

## Build / Run Commands

### Local Development

```bash
# Install dependencies
pip install -r requirements.txt

# Download a single YouTube video as audio
python rss_downloader.py "https://www.youtube.com/watch?v=VIDEO_ID"

# Start the HTTP server
python rss_downloader.py --server

# Install ffmpeg (required for audio processing)
# macOS: brew install ffmpeg
# Ubuntu/Debian: sudo apt-get install ffmpeg
# Windows: Download from https://ffmpeg.org/download.html
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
├── rss_downloader.py      # Main application
├── requirements.txt       # Python dependencies
├── Dockerfile            # Container configuration
├── docker-compose.yml    # Docker Compose config
├── downloads/            # Downloaded audio files (runtime-created)
└── AGENTS.md            # This file
```

---

## API Reference

### HTTP Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/rss` | RSS feed of all downloaded audio |
| GET | `/audio/<filename>` | Serve individual audio file |
| POST | `/download` | Download a YouTube URL |

### Download API Example

```bash
curl -X POST http://localhost:5757/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID"}'
```

---

## Configuration

Key variables at the top of `rss_downloader.py`:

- `DOWNLOAD_DIR`: Directory for downloaded files (default: `"downloads"`)
- `AUDIO_EXT`: Audio file extension (default: `".mp3"`)

The server port is set in `run_server()` (default: `8080`, exposed as `5757` in Docker).

---

## Notes for Agents

1. **No existing tests**: This project lacks automated tests. Consider adding pytest tests for any new functionality.
2. **yt-dlp binary**: In Docker, the project uses the standalone yt-dlp binary, not the pip package.
3. **RSS URL hardcoding**: The RSS feed generator has hardcoded URLs (`http://casaos.local:5757/`) - consider making this configurable.
4. **Security**: No authentication is implemented. The server is intended for local/personal use only.
