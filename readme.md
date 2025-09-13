# YouTube Podcast Server

A lightweight Python application that downloads YouTube videos as audio files and serves them via an RSS feed, effectively turning YouTube content into a personal podcast server.

## Features

- **YouTube Audio Download**: Extract high-quality MP3 audio from YouTube videos using yt-dlp
- **RSS Feed Generation**: Automatically generate RSS feeds from downloaded audio files
- **HTTP Server**: Built-in web server to serve audio files and RSS feeds
- **Metadata Preservation**: Embeds video descriptions, thumbnails, and metadata into audio files
- **RESTful API**: Simple API endpoint for triggering downloads
- **Docker Support**: Containerized deployment option

## Installation

### Prerequisites

- Python 3.9 or higher
- ffmpeg (for audio processing)

### Local Installation

1. Clone this repository:
   ```bash
   git clone <repository-url>
   cd youtube-podcast-server
   ```

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Install ffmpeg:
   - **Windows**: Download from [ffmpeg.org](https://ffmpeg.org/download.html)
   - **macOS**: `brew install ffmpeg`
   - **Ubuntu/Debian**: `sudo apt-get install ffmpeg`

### Docker Installation

```bash
docker build -t youtube-podcast-server .
docker run -p 8080:8080 -v $(pwd)/downloads:/app/downloads youtube-podcast-server
```

## Usage

### Command Line

Download a single video:
```bash
python rss_downloader.py "https://www.youtube.com/watch?v=VIDEO_ID"
```

Start the server:
```bash
python rss_downloader.py --server
```

### HTTP API

Start the server and use the following endpoints:

- **RSS Feed**: `GET http://localhost:8080/rss`
- **Audio Files**: `GET http://localhost:8080/audio/<filename>`
- **Download**: `POST http://localhost:8080/download`

#### Download API Example

```bash
curl -X POST http://localhost:8080/download \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.youtube.com/watch?v=VIDEO_ID"}'
```

### Podcast App Integration

1. Start the server: `python rss_downloader.py --server`
2. Add the RSS feed URL to your podcast app: `http://localhost:8080/rss`
3. Your podcast app will now show all downloaded audio files

## File Structure

```
youtube-podcast-server/
├── rss_downloader.py      # Main application
├── requirements.txt       # Python dependencies
├── Dockerfile            # Container configuration
├── downloads/            # Downloaded audio files (auto-created)
├── README.md            # This file
└── .gitignore           # Git ignore rules
```

## Configuration

### Download Directory

By default, files are downloaded to the `downloads/` directory. You can modify the `DOWNLOAD_DIR` variable in `rss_downloader.py` to change this location.

### Server Port

The server runs on port 8080 by default. Modify the `TCPServer` initialization in the `run_server()` function to change the port.

### Audio Quality

The application downloads the best available audio quality and converts to MP3. Audio settings can be modified in the `download_audio()` function:

- `--audio-quality 0`: Best quality (default)
- `--audio-format mp3`: Output format

## Features in Detail

### Metadata Handling

- **Video descriptions** are saved as separate `.description` files
- **Thumbnails** are embedded directly into MP3 files
- **Publication dates** in RSS feed reflect file modification times
- **Titles** are preserved from YouTube video titles

### RSS Feed Format

The generated RSS feed includes:
- Channel title: "Downloaded YouTube Audio"
- Individual items with titles, descriptions, and direct download links
- Proper MIME types for podcast app compatibility

## Troubleshooting

### Common Issues

1. **yt-dlp errors**: Make sure you have the latest version: `pip install --upgrade yt-dlp`
2. **ffmpeg not found**: Ensure ffmpeg is installed and in your system PATH
3. **Permission errors**: Check that the downloads directory is writable
4. **Port already in use**: Change the port number in the server configuration

### Dependencies

- `yt-dlp`: YouTube video/audio downloading
- `ffmpeg`: Audio processing and format conversion
- Built-in Python modules: `http.server`, `socketserver`, `urllib`, `xml.sax.saxutils`

## Security Considerations

- This server runs on localhost by default for security
- No authentication is implemented - intended for personal use
- Consider firewall rules if exposing to network
- Validate URLs when accepting external input

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Commit your changes: `git commit -am 'Add feature'`
4. Push to the branch: `git push origin feature-name`
5. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgments

- Built with [yt-dlp](https://github.com/yt-dlp/yt-dlp) for YouTube downloading
- Uses [ffmpeg](https://ffmpeg.org/) for audio processing
- Inspired by the need for a simple YouTube-to-podcast solution