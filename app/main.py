"""YouTube metadata to M3U; media bytes never pass through this service."""
import logging
import math
import os
import shutil
import tempfile
from contextlib import contextmanager
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
log = logging.getLogger("uvicorn.error")
VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
PLAYLIST_ID = re.compile(r"[A-Za-z0-9_-]{10,150}\Z")
# Restrict the requested muxed selector to direct HTTP(S), excluding manifests.
FORMAT = "best[ext=mp4][vcodec!=none][acodec!=none][protocol^=http]/best[vcodec!=none][acodec!=none][protocol^=http]"
# Fixed lock stripes coalesce identical requests without an unbounded lock map.
locks = [threading.Lock() for _ in range(64)]
extraction_slots = threading.BoundedSemaphore(1)
# Keep one active extraction and allow one overlapping player request to wait.
# This avoids transient 503 responses from players that probe several items at
# once without allowing an unbounded backlog of requests to YouTube.
extraction_queue_slots = threading.BoundedSemaphore(2)
EXTRACTION_WAIT_SECONDS = 45


class ExtractionPacer:
    # Called only while holding extraction_slots. Cached results bypass this.
    def __init__(self):
        self.next_start = 0

    def wait(self):
        delay = max(0, self.next_start - time.monotonic())
        if delay:
            time.sleep(delay)
        self.next_start = time.monotonic() + 10


pacer = ExtractionPacer()


def video_extractor_options():
    server_home = os.environ.get("BGUTIL_SERVER_HOME")
    if not server_home or not os.path.isfile(os.path.join(server_home, "build", "generate_once.js")):
        raise HTTPException(503, "PO Token provider is not installed; use the Docker image")
    return {
        "youtube": {"player_client": ["mweb"]},
        "youtubepot-bgutilscript": {"server_home": [server_home]},
    }


class UpstreamCooldown:
    """Pause new upstream requests across all IDs after a YouTube block."""
    def __init__(self):
        self.lock = threading.Lock()
        self.until = 0
        self.reason = "YouTube temporarily refused requests"

    def trip(self, reason):
        with self.lock:
            self.until = max(self.until, time.monotonic() + 300)
            self.reason = reason

    def check(self):
        with self.lock:
            remaining = math.ceil(self.until - time.monotonic())
            reason = self.reason
        if remaining > 0:
            raise HTTPException(503, reason + "; resolver paused temporarily", headers={
                "Retry-After": str(remaining), "Cache-Control": "no-store",
            })


cooldown = UpstreamCooldown()


class ExtractionLogger:
    # yt-dlp can log HTTP 429/403 as a warning and later raise a generic error.
    def __init__(self):
        self.block_reason = None

    def inspect(self, message):
        message = str(message).lower()
        if "not a bot" in message:
            self.block_reason = "YouTube requires sign-in verification from this server"
        elif "http error 429" in message or "too many requests" in message:
            self.block_reason = "YouTube is rate limiting this server"
        elif "http error 403" in message:
            self.block_reason = "YouTube denied access from this server"
        # Record warnings only: yt-dlp may recover using another player client.

    def debug(self, message):
        log.debug(message)

    def warning(self, message):
        self.inspect(message)
        log.warning(message)

    def error(self, message):
        self.inspect(message)
        log.error(message)


@contextmanager
def cookie_options():
    source = os.environ.get("YOUTUBE_COOKIE_FILE")
    if not source:
        yield {}
        return
    # yt-dlp writes its cookie jar on close. Never modify the mounted secret.
    with tempfile.TemporaryDirectory(prefix="youtube-cookies-") as directory:
        target = os.path.join(directory, "cookies.txt")
        try:
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
        except OSError:
            log.error("Unable to read configured YOUTUBE_COOKIE_FILE")
            raise HTTPException(503, "YouTube cookie configuration is unavailable") from None
        yield {"cookiefile": target}



class Cache:
    def __init__(self, capacity):
        self.capacity = capacity
        self.values = OrderedDict()
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            item = self.values.get(key)
            if item is None:
                return None
            value, deadline = item
            if time.monotonic() >= deadline:
                del self.values[key]
                return None
            self.values.move_to_end(key)
            return value

    def put(self, key, value, ttl):
        if ttl <= 0:
            return
        with self.lock:
            self.values[key] = (value, time.monotonic() + ttl)
            self.values.move_to_end(key)
            while len(self.values) > self.capacity:
                self.values.popitem(last=False)


playlists = Cache(128)
videos = Cache(1024)


def cached(cache, key, loader):
    value = cache.get(key)
    if value is not None:
        return value
    lock = locks[hash((id(cache), key)) % len(locks)]
    if not lock.acquire(timeout=1):
        raise HTTPException(503, "Resolution in progress; retry shortly", headers={"Retry-After": "5"})
    try:
        value = cache.get(key)
        if value is None:
            value, ttl = loader()
            cache.put(key, value, ttl)
        return value
    finally:
        lock.release()


def extract(url, *, flat=False):
    cooldown.check()
    if not extraction_queue_slots.acquire(blocking=False):
        raise HTTPException(503, "YouTube resolver queue is full; retry shortly", headers={"Retry-After": "15"})
    slot_acquired = False
    extraction_log = ExtractionLogger()
    try:
        if not extraction_slots.acquire(timeout=EXTRACTION_WAIT_SECONDS):
            raise HTTPException(503, "YouTube resolver wait timed out; retry shortly", headers={"Retry-After": "15"})
        slot_acquired = True
        cooldown.check()
        extractor_args = {} if flat else video_extractor_options()
        pacer.wait()
        cooldown.check()
        options = {
            "quiet": True, "no_warnings": False, "logger": extraction_log,
            "cachedir": False, "skip_download": True,
            "socket_timeout": 15, "retries": 1, "extractor_retries": 1,
            "sleep_interval_requests": 1,
            "extractor_args": extractor_args,
            "extract_flat": "in_playlist" if flat else False,
            # Flat extraction does not resolve individual videos. Filter unavailable
            # entries below, but propagate playlist/network extraction failures.
            "ignoreerrors": False,
            "noplaylist": not flat,
        }
        if not flat:
            options["format"] = FORMAT
        attempts = 1 if flat else 2
        for attempt in range(attempts):
            extraction_log = ExtractionLogger()
            options["logger"] = extraction_log
            try:
                with cookie_options() as cookies:
                    with YoutubeDL({**options, **cookies}) as ydl:
                        result = ydl.extract_info(url, download=False)
                break
            except DownloadError as exc:
                extraction_log.inspect(exc)
                if attempt == 0 and extraction_log.block_reason == "YouTube requires sign-in verification from this server":
                    log.warning("YouTube requested sign-in for %s; retrying once with a fresh PO Token", url)
                    time.sleep(3)
                    cooldown.check()
                    continue
                raise
        if result is None:
            raise HTTPException(404, "YouTube item unavailable")
        return result
    except DownloadError as exc:
        log.warning("YouTube extraction failed for %s: %s", url, exc)
        extraction_log.inspect(exc)
        if extraction_log.block_reason:
            cooldown.trip(extraction_log.block_reason)
            cooldown.check()
        message = str(exc).lower()
        if "requested format is not available" in message:
            raise HTTPException(502, "No playable single stream with both video and audio is available") from None
        if any(part in message for part in ("private video", "video has been removed", "video unavailable", "playlist does not exist", "playlist is private")):
            raise HTTPException(404, "YouTube item unavailable or private") from None
        raise HTTPException(502, "YouTube extraction failed; try again later") from None
    except HTTPException:
        raise
    except Exception:
        log.exception("Unexpected YouTube extraction failure for %s", url)
        raise HTTPException(502, "YouTube extraction failed; try again later") from None
    finally:
        if slot_acquired:
            extraction_slots.release()
        extraction_queue_slots.release()


def clean_title(title):
    return " ".join("".join(" " if unicodedata.category(c).startswith("C") else c for c in str(title)).split())


def load_playlist(playlist_id):
    info = extract(f"https://www.youtube.com/playlist?list={playlist_id}", flat=True)
    entries = []
    for entry in info.get("entries") or []:
        if not entry or not VIDEO_ID.fullmatch(str(entry.get("id", ""))):
            continue
        title = entry.get("title")
        if not title or title in ("[Deleted video]", "[Private video]"):
            continue
        if entry.get("availability") in ("private", "needs_auth", "premium_only", "subscriber_only"):
            continue
        entries.append((entry["id"], clean_title(title)))
    return entries, 120


def media_ttl(url):
    # A minute's margin leaves time for the player to follow the redirect.
    expiries = parse_qs(urlsplit(url).query).get("expire", [])
    if not expiries:
        return 900
    try:
        expiry = min(int(value) for value in expiries)
        return max(0, min(900, expiry - time.time() - 60))
    except (ValueError, OverflowError):
        return 0  # Do not cache ambiguous expiry values.


def load_video(video_id):
    info = extract(f"https://www.youtube.com/watch?v={video_id}")
    url = info.get("url")
    if (not url or info.get("requested_formats") or info.get("has_drm")
            or info.get("vcodec") in (None, "none")
            or info.get("acodec") in (None, "none")
            or info.get("protocol") not in ("http", "https")):
        raise HTTPException(502, "No playable direct HTTP stream with both video and audio is available")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(502, "YouTube returned an invalid media URL")
    ttl = media_ttl(url)
    if parse_qs(parsed.query).get("expire") and ttl <= 0:
        raise HTTPException(502, "YouTube returned an expired or imminently expiring media URL; retry shortly")
    return url, ttl


@app.get("/")
@app.get("/health")
def health():
    return {"status": "ok"}


@app.api_route("/playlist/{playlist_id}.m3u", methods=["GET", "HEAD"])
def playlist(playlist_id: str, request: Request):
    if not PLAYLIST_ID.fullmatch(playlist_id):
        raise HTTPException(400, "Invalid playlist ID")
    entries = cached(playlists, playlist_id, lambda: load_playlist(playlist_id))
    lines = ["#EXTM3U"]
    for video_id, title in entries:
        lines.extend((f"#EXTINF:-1,{title}", str(request.url_for("video", video_id=video_id))))
    return Response("\n".join(lines) + "\n", media_type="audio/x-mpegurl; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.api_route("/video/{video_id}.mp4", methods=["GET", "HEAD"])
def video(video_id: str):
    if not VIDEO_ID.fullmatch(video_id):
        raise HTTPException(404, "Invalid video ID")
    url = cached(videos, video_id, lambda: load_video(video_id))
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store"})
