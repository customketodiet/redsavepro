#!/usr/bin/env python3
"""
RedSave Pro - Reddit Video Downloader Backend
Production-ready | v1.0
"""

import re, json, os, time, logging, urllib.parse, subprocess, random
from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
import requests

# ── App ──────────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder="templates", static_folder="static")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]
REDDIT_HEADERS = {
    "Accept": "application/json, text/html, */*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}
CDN_HEADERS = {
    "Accept": "*/*",
    "Accept-Encoding": "identity",
    "Origin": "https://www.reddit.com",
    "Referer": "https://www.reddit.com/",
}
VIDEO_QUALITIES = [1080, 720, 480, 360, 240, 96]
TIMEOUT = 18
MAX_BYTES = 5 * 1024 * 1024  # 5 MB for JSON response

# ── Check FFmpeg ──────────────────────────────────────────────────────────────
def _check_ffmpeg():
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            ver = r.stdout.decode().split("\n")[0]
            log.info(f"FFmpeg OK: {ver[:50]}")
            return True
    except Exception as e:
        log.warning(f"FFmpeg not found: {e}")
    return False

FFMPEG_OK = _check_ffmpeg()

# ── CORS ──────────────────────────────────────────────────────────────────────
@app.after_request
def add_cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type"
    r.headers["X-Content-Type-Options"]       = "nosniff"
    return r

@app.route("/api/<path:p>", methods=["OPTIONS"])
def opt_handler(p):
    return jsonify({}), 200

# ── URL Utilities ─────────────────────────────────────────────────────────────
# A small fixed set of legitimate Reddit hosts. All validation below checks
# the *parsed* hostname with exact/suffix matching — never substring or
# regex matching against the raw URL string, which is what allowed hosts
# like "reddit.com.evil.com" to slip through in earlier drafts of this code.
REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "np.reddit.com",
                "redd.it", "v.redd.it"}

def _hostname(url: str) -> str:
    try:
        return urllib.parse.urlparse(url.strip()).netloc.lower().split(":")[0]
    except Exception:
        return ""

def is_reddit_url(url: str) -> bool:
    if not url:
        return False
    u = url.strip()
    if not u.startswith("http"):
        u = "https://" + u
    host = _hostname(u)
    return any(host == h or host.endswith("." + h) for h in REDDIT_HOSTS)

def normalize_url(url: str) -> str:
    """Rewrite known Reddit host variants to www.reddit.com — but ONLY
    when the parsed hostname is an exact match for a known alias. This
    must not be done with regex/substring matching on the raw string,
    since that can be bypassed with hosts like 'reddit.com.evil.com'.
    """
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url

    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower().split(":")[0]

    if host in ("old.reddit.com", "np.reddit.com", "reddit.com"):
        parsed = parsed._replace(netloc="www.reddit.com")

    return urllib.parse.urlunparse(parsed).rstrip("/")

def extract_post_id(url: str):
    """Returns (kind, id) where kind is 'post_id', 'video_id', or 'share_id'"""
    url = normalize_url(url)

    # v.redd.it/VIDEO_ID or reddit.com/video/VIDEO_ID
    m = re.search(r"(?:v\.redd\.it|reddit\.com/video)/([a-zA-Z0-9_]+)", url)
    if m: return ("video_id", m.group(1))

    # standard post URL: /comments/POST_ID/
    m = re.search(r"/comments/([a-zA-Z0-9]+)", url)
    if m: return ("post_id", m.group(1))

    # new share format: /r/sub/s/SHARE_ID
    m = re.search(r"/s/([a-zA-Z0-9]+)", url)
    if m: return ("share_id", m.group(1))

    # redd.it/POST_ID short link
    m = re.search(r"redd\.it/([a-zA-Z0-9]+)$", url)
    if m: return ("post_id", m.group(1))

    return (None, None)

def get_json_url(kind: str, pid: str) -> str:
    if kind == "post_id":
        return f"https://www.reddit.com/comments/{pid}.json?raw_json=1&limit=1"
    if kind == "share_id":
        return f"https://www.reddit.com/r/reddit/s/{pid}.json?raw_json=1&limit=1"
    # video_id: need to resolve via oembed or direct page
    return f"https://www.reddit.com/video/{pid}.json?raw_json=1&limit=1"

# ── Fetch Reddit Post JSON ────────────────────────────────────────────────────
def fetch_post(url: str) -> dict:
    kind, pid = extract_post_id(url)
    if not kind:
        raise ValueError("Could not parse Reddit URL. Paste a valid Reddit post or video link.")

    # Follow redirects for short links (redd.it)
    resolved_url = normalize_url(url)
    if kind in ("share_id",) or "redd.it" in url:
        # Defense-in-depth: re-validate the host right before making this
        # outbound request, even though is_reddit_url() already checked it
        # upstream in api_fetch(). This ensures a future change to the
        # validators can't silently reopen an SSRF path here.
        if not is_reddit_url(resolved_url):
            raise ValueError("Could not parse Reddit URL. Paste a valid Reddit post or video link.")
        try:
            sess = requests.Session()
            sess.headers.update({**REDDIT_HEADERS, "User-Agent": random.choice(UA_POOL)})
            r = sess.head(resolved_url, allow_redirects=True, timeout=10)
            if r.url != resolved_url and is_reddit_url(r.url):
                resolved_url = r.url
                kind2, pid2 = extract_post_id(resolved_url)
                if pid2:
                    kind, pid = kind2, pid2
        except:
            pass

    json_url = get_json_url(kind, pid)
    log.info(f"Fetching: {json_url}")

    sess = requests.Session()
    sess.headers.update({**REDDIT_HEADERS, "User-Agent": random.choice(UA_POOL)})

    for attempt in range(2):
        try:
            resp = sess.get(json_url, timeout=TIMEOUT, allow_redirects=True)
            log.info(f"Reddit API → {resp.status_code} | {len(resp.content)} bytes")
            if resp.status_code == 404:
                raise ValueError("Post not found. It may be deleted or private.")
            if resp.status_code == 403:
                raise ValueError("Reddit blocked the request. Try again in a few seconds.")
            if resp.status_code != 200:
                raise ValueError(f"Reddit returned status {resp.status_code}. Please try again.")
            return resp.json()
        except ValueError:
            raise
        except Exception as e:
            if attempt == 1:
                raise ValueError(f"Could not reach Reddit. Check your connection. ({str(e)[:60]})")
            time.sleep(1)

# ── Extract Video Info ────────────────────────────────────────────────────────
def parse_post(data: list) -> dict:
    try:
        post = data[0]["data"]["children"][0]["data"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("Unexpected Reddit API response. Please try again.")

    if not post.get("is_video", False):
        hint = "This post contains an image, GIF, or text — not a hosted video."
        # Check for crosspost
        crosspost = post.get("crosspost_parent_list", [])
        if crosspost and crosspost[0].get("is_video"):
            post = crosspost[0]
        else:
            raise ValueError(f"No downloadable video found. {hint}")

    media = post.get("media") or post.get("secure_media") or {}
    rv    = media.get("reddit_video") or {}

    if not rv:
        raise ValueError("This post does not contain a Reddit-hosted video (v.redd.it). "
                         "Embedded YouTube/Twitch/external videos cannot be downloaded here.")

    fallback = rv.get("fallback_url", "")
    m = re.search(r"v\.redd\.it/([a-zA-Z0-9_]+)", fallback)
    if not m:
        raise ValueError("Could not extract video ID from this post.")

    video_id  = m.group(1)
    max_height = int(rv.get("height", 720))
    is_gif    = bool(rv.get("is_gif", False))
    has_audio = not is_gif
    duration  = int(rv.get("duration", 0))

    # Thumbnail: prefer preview image (higher res)
    thumbnail = ""
    try:
        raw = post["preview"]["images"][0]["source"]["url"]
        thumbnail = raw.replace("&amp;", "&")
    except:
        thumbnail = post.get("thumbnail", "")
        if thumbnail in ("self", "default", "nsfw", "spoiler", ""):
            thumbnail = ""

    # Title: clean up
    title = (post.get("title", "Reddit Video") or "Reddit Video").strip()
    title = re.sub(r"\s+", " ", title)

    # Quality options
    qualities = []
    for q in VIDEO_QUALITIES:
        if q <= max_height:
            qualities.append({
                "height": q,
                "label": ("1080p FHD" if q == 1080 else
                          "720p HD"   if q == 720  else
                          "480p SD"   if q == 480  else
                          "360p SD"   if q == 360  else
                          "240p Low"  if q == 240  else "96p Low"),
                "video_url": f"https://v.redd.it/{video_id}/DASH_{q}.mp4",
            })

    if not qualities:
        qualities.append({
            "height": max_height,
            "label": f"{max_height}p",
            "video_url": f"https://v.redd.it/{video_id}/DASH_{max_height}.mp4",
        })

    # Audio URL options (try both patterns)
    audio_urls = [
        f"https://v.redd.it/{video_id}/DASH_audio.mp4",
        f"https://v.redd.it/{video_id}/DASH_AUDIO_128.mp4",
        f"https://v.redd.it/{video_id}/DASH_AUDIO_64.mp4",
    ]

    def fmt_dur(s):
        if not s: return None
        m2, sec = divmod(int(s), 60)
        h, m2 = divmod(m2, 60)
        return f"{h}:{m2:02d}:{sec:02d}" if h else f"{m2}:{sec:02d}"

    return {
        "success":    True,
        "title":      title,
        "author":     f"u/{post.get('author','unknown')}",
        "subreddit":  f"r/{post.get('subreddit','unknown')}",
        "upvotes":    post.get("score", 0),
        "thumbnail":  thumbnail,
        "duration":   fmt_dur(duration),
        "is_gif":     is_gif,
        "has_audio":  has_audio,
        "video_id":   video_id,
        "audio_urls": audio_urls if has_audio else [],
        "qualities":  qualities,
        "ffmpeg_ok":  FFMPEG_OK,
    }

# ── Security: Only allow Reddit CDN ──────────────────────────────────────────
# IMPORTANT: must be exact-suffix matches, never substring ("in") checks.
# Substring matching would let "v.redd.it.evil.com" or "reddit.com.evil.net"
# bypass the filter, turning these endpoints into an open SSRF proxy.
ALLOWED_CDN = ["v.redd.it", "redd.it", "reddit.com", "redditmedia.com", "reddituploads.com"]

def is_allowed_cdn(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("https", "http"):
            return False
        host = parsed.netloc.lower().split(":")[0]  # strip port if present
        return any(host == d or host.endswith("." + d) for d in ALLOWED_CDN)
    except Exception:
        return False

# ── API Routes ────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("templates", "index.html")

@app.route("/robots.txt")
def robots():
    txt = ("User-agent: *\n"
           "Allow: /\n"
           "Disallow: /api/\n"
           "Disallow: /static/\n"
           "Sitemap: https://redsavepro.com/sitemap.xml\n")
    return Response(txt, mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap():
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://redsavepro.com/</loc>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
    <lastmod>2025-06-01</lastmod>
  </url>
</urlset>"""
    return Response(xml, mimetype="application/xml")

@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    try:
        body = request.get_json(force=True, silent=True) or {}
        url  = (body.get("url") or "").strip()

        if not url:
            return jsonify({"success": False, "error": "Please paste a Reddit video URL."}), 400
        if not is_reddit_url(url):
            return jsonify({"success": False,
                            "error": "Invalid URL. Please use a Reddit link "
                                     "(reddit.com, redd.it, or v.redd.it)."}), 400

        raw  = fetch_post(url)
        info = parse_post(raw)
        return jsonify(info)

    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 422
    except Exception as e:
        log.error(f"Unexpected error: {e}", exc_info=True)
        return jsonify({"success": False, "error": "Server error. Please try again."}), 500


@app.route("/api/merge")
def api_merge():
    """Stream FFmpeg-merged video+audio directly to client.
    If quality == 'audio', extracts audio-only (no video track) from audio_url.
    """
    video_url   = request.args.get("video", "").strip()
    audio_url   = request.args.get("audio", "").strip()
    quality     = request.args.get("quality", "720")
    audio_only  = (quality == "audio")
    filename    = "redsave_audio.m4a" if audio_only else f"redsave_{quality}p.mp4"

    if not audio_url or (not audio_only and not video_url):
        return jsonify({"error": "Missing video or audio URL."}), 400
    if not is_allowed_cdn(audio_url) or (not audio_only and not is_allowed_cdn(video_url)):
        return jsonify({"error": "Unauthorized source."}), 403
    if not FFMPEG_OK:
        return jsonify({"error": "FFmpeg not available on this server."}), 503

    # ── Pre-flight check: confirm CDN URLs are actually reachable ──
    # FFmpeg failures happen mid-stream, *after* HTTP headers are already
    # sent to the browser — by then we can no longer report a clean error.
    # A quick range-request here catches expired/broken links upfront.
    ua = random.choice(UA_POOL)
    preflight_hdrs = {**CDN_HEADERS, "User-Agent": ua, "Range": "bytes=0-1024"}
    targets = [("audio", audio_url)] if audio_only else [("video", video_url), ("audio", audio_url)]
    for label, u in targets:
        try:
            pre = requests.get(u, headers=preflight_hdrs, timeout=8, stream=True)
            pre.close()
            if pre.status_code not in (200, 206):
                return jsonify({
                    "error": f"The {label} source link has expired or is unavailable "
                             f"(HTTP {pre.status_code}). Please fetch the video again."
                }), 502
        except requests.exceptions.Timeout:
            return jsonify({"error": f"The {label} source timed out. Please try again."}), 504
        except requests.exceptions.ConnectionError:
            return jsonify({"error": f"Could not reach the {label} source. Please try again."}), 502

    def generate():
        proc = None
        try:
            if audio_only:
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-user_agent", ua,
                    "-headers", "Referer: https://www.reddit.com/\r\n",
                    "-i", audio_url,
                    "-vn",
                    "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "frag_keyframe+empty_moov+faststart",
                    "-f", "ipod",  # m4a container via ipod muxer (mp4-compatible, audio-only)
                    "pipe:1",
                ]
            else:
                cmd = [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-user_agent", ua,
                    "-headers", "Referer: https://www.reddit.com/\r\n",
                    "-i", video_url,
                    "-user_agent", ua,
                    "-headers", "Referer: https://www.reddit.com/\r\n",
                    "-i", audio_url,
                    "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "128k",
                    "-shortest",
                    "-movflags", "frag_keyframe+empty_moov+faststart",
                    "-f", "mp4",
                    "pipe:1",
                ]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            sent_any = False
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                sent_any = True
                yield chunk
            proc.wait(timeout=10)
            if proc.returncode not in (0, None):
                err = proc.stderr.read().decode("utf-8", errors="replace")[-300:]
                log.warning(f"FFmpeg exit {proc.returncode} (sent_any={sent_any}): {err}")
        except GeneratorExit:
            if proc:
                try: proc.kill()
                except Exception: pass
            raise
        except Exception as e:
            log.error(f"FFmpeg stream error: {e}")
        finally:
            if proc and proc.poll() is None:
                try: proc.kill()
                except Exception: pass

    hdrs = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type":        "audio/mp4" if audio_only else "video/mp4",
        "X-Accel-Buffering":   "no",
        "Cache-Control":       "no-cache",
    }
    return Response(stream_with_context(generate()), status=200, headers=hdrs)


@app.route("/api/proxy")
def api_proxy():
    """Proxy a v.redd.it CDN URL directly (video-only, no merge)."""
    vid_url  = request.args.get("url", "").strip()
    filename = request.args.get("filename", "redsave_video.mp4")

    if not vid_url:
        return jsonify({"error": "No URL."}), 400
    if not is_allowed_cdn(vid_url):
        return jsonify({"error": "Unauthorized source."}), 403

    try:
        hdrs = {**CDN_HEADERS, "User-Agent": random.choice(UA_POOL)}
        if "Range" in request.headers:
            hdrs["Range"] = request.headers["Range"]
        up = requests.get(vid_url, headers=hdrs, stream=True, timeout=30)

        # ── Validate upstream BEFORE committing to a streaming response ──
        # Without this check, a 404/403 from the CDN would be silently
        # forwarded as if it were valid video data with a fake 200 status.
        if up.status_code not in (200, 206):
            up.close()
            log.warning(f"Proxy upstream returned {up.status_code} for {vid_url[:80]}")
            return jsonify({
                "error": f"Video source returned an error (HTTP {up.status_code}). "
                         "The video link may have expired — try fetching the URL again."
            }), 502

        content_type = up.headers.get("Content-Type", "")
        if content_type and "video" not in content_type and "octet-stream" not in content_type:
            up.close()
            log.warning(f"Proxy upstream returned non-video content-type: {content_type}")
            return jsonify({"error": "Video source returned invalid content. Please try again."}), 502

        def gen():
            try:
                for chunk in up.iter_content(65536):
                    if chunk:
                        yield chunk
            finally:
                up.close()

        out_hdrs = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type":        up.headers.get("Content-Type", "video/mp4"),
            "Accept-Ranges":       "bytes",
        }
        if "Content-Length" in up.headers:
            out_hdrs["Content-Length"] = up.headers["Content-Length"]
        if "Content-Range" in up.headers:
            out_hdrs["Content-Range"] = up.headers["Content-Range"]

        return Response(
            stream_with_context(gen()),
            status=up.status_code,
            headers=out_hdrs,
        )
    except requests.exceptions.Timeout:
        return jsonify({"error": "Video source timed out. Please try again."}), 504
    except requests.exceptions.ConnectionError as e:
        log.error(f"Proxy connection error: {e}")
        return jsonify({"error": "Could not reach the video source. Please try again."}), 502
    except Exception as e:
        log.error(f"Proxy error: {e}")
        return jsonify({"error": "Unexpected error while downloading. Please try again."}), 500


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "ffmpeg": FFMPEG_OK, "v": "1.0.0", "t": int(time.time())})


# ── Entry ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"Starting RedSave Pro on port {port} | FFmpeg={FFMPEG_OK}")
    app.run(host="0.0.0.0", port=port, debug=False)
