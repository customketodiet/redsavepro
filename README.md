# RedSave Pro — Reddit Video Downloader

Production-ready Reddit video downloader with automatic audio merging. Uses Reddit's public JSON API directly (no scraping) + FFmpeg for stream merging.

## Folder Structure

Flask is configured with `template_folder="templates"`, so the HTML pages **must** sit inside a `templates/` folder, not next to `server.py`:

```
redsavepro/
├── server.py
├── requirements.txt
├── Procfile
├── render.yaml
├── static/              ← empty is fine, just needs to exist
└── templates/
    ├── index.html
    ├── terms.html
    ├── privacy.html
    └── dmca.html
```

## Local Setup

```bash
pip install -r requirements.txt
python3 server.py
# Open http://localhost:5000
```

FFmpeg must be installed locally for the audio-merge feature to work:
- Mac: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Windows: download from ffmpeg.org and add to PATH

Without FFmpeg, the app still runs — it just falls back to video-only downloads automatically (`/api/health` reports `ffmpeg: false`).

## Deploy to Render (Free Tier)

1. Push this folder to a GitHub repo.
2. On Render: New → Web Service → connect the repo.
3. Render auto-detects `render.yaml` — just click **Create Web Service**.
4. **FFmpeg is pre-installed on Render's native Python environment** — no extra setup needed.

Manual settings (if not using render.yaml):
- Build Command: `pip install -r requirements.txt`
- Start Command: `gunicorn server:app --worker-class gthread --workers 2 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`

## How It Works

1. `/api/fetch` — takes a Reddit URL, resolves it to a post ID, calls Reddit's own `.json` API, and extracts video/audio CDN URLs + metadata (title, thumbnail, duration, quality list).
2. `/api/merge` — streams FFmpeg's output directly to the browser, merging the separate Reddit video + audio tracks into one MP4 in real time (no temp files, no disk writes).
3. `/api/proxy` — fallback video-only download (used if FFmpeg is unavailable, or for the "Video Only" button).
4. `/terms`, `/privacy`, `/dmca` — static legal pages served from `templates/`. **These are generic templates, not legal advice** — have them reviewed before relying on them, and replace the `dmca@redsavepro.com` / `support@redsavepro.com` addresses with inboxes you actually monitor.

## Security Notes

All CDN/Reddit host validation uses exact parsed-hostname matching (never substring/regex-on-raw-string matching), specifically to prevent SSRF via crafted hosts like `v.redd.it.evil.com`. Both `/api/proxy` and `/api/merge` pre-flight-check upstream URLs before committing to a streaming HTTP response, so expired/invalid CDN links return a clean JSON error instead of silently serving broken "video" data.

## Known Limitations

- Only works with Reddit-hosted videos (`v.redd.it`). Embedded YouTube/Twitch links inside Reddit posts are not supported.
- Free-tier hosting (Render free) sleeps after 15 minutes of inactivity — first request after sleep takes ~30s to wake up.
- `gthread` worker class is used (not `sync`) so one slow download doesn't block other users — this requires no extra dependency since `gthread` ships with gunicorn itself.
