import time
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from yt_dlp.utils import DownloadError

from app import main


class ServiceTests(unittest.TestCase):
    def setUp(self):
        main.cooldown = main.UpstreamCooldown()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        main.playlists = main.Cache(128)
        main.videos = main.Cache(1024)
        self.client = TestClient(main.app, base_url="https://example.onrender.com")

    def test_health(self):
        for path in ("/", "/health"):
            self.assertEqual(self.client.get(path).json(), {"status": "ok"})

    @patch.object(main, "YoutubeDL")
    def test_validation_does_not_call_youtube(self, ydl):
        self.assertEqual(self.client.get("/playlist/bad!.m3u").status_code, 400)
        self.assertEqual(self.client.get("/video/bad.mp4").status_code, 404)
        ydl.assert_not_called()

    @patch.object(main, "YoutubeDL")
    def test_playlist_order_sanitization_and_host(self, ydl):
        extractor = ydl.return_value.__enter__.return_value
        extractor.extract_info.return_value = {"entries": [
            {"id": "abcdefghijk", "title": "First\n#EXTM3U\r café"},
            None, {"id": "xxxxxxxxxxx", "title": "[Private video]"},
            {"id": "zzzzzzzzzzz", "title": "Secret", "availability": "private"},
            {"id": "bad", "title": "Invalid"},
            {"id": "12345678901", "title": "Second"},
        ]}
        response = self.client.get("/playlist/PL1234567890.m3u")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "audio/x-mpegurl; charset=utf-8")
        self.assertEqual(response.text, "#EXTM3U\n#EXTINF:-1,First #EXTM3U café\nhttps://example.onrender.com/video/abcdefghijk.mp4\n#EXTINF:-1,Second\nhttps://example.onrender.com/video/12345678901.mp4\n")
        other = TestClient(main.app, base_url="http://localhost:8000")
        self.assertIn("http://localhost:8000/video/", other.get("/playlist/PL1234567890.m3u").text)
        extractor.extract_info.assert_called_once_with("https://www.youtube.com/playlist?list=PL1234567890", download=False)
        self.assertEqual(ydl.call_args.args[0]["extract_flat"], "in_playlist")
        self.assertIs(ydl.call_args.args[0]["ignoreerrors"], False)
        with patch.object(main.time, "monotonic", return_value=time.monotonic() + 121):
            extractor.extract_info.return_value = {"entries": []}
            self.assertEqual(self.client.get("/playlist/PL1234567890.m3u").text, "#EXTM3U\n")
        self.assertEqual(extractor.extract_info.call_count, 2)

    @patch.object(main, "YoutubeDL")
    def test_redirect_and_head_cache(self, ydl):
        extractor = ydl.return_value.__enter__.return_value
        url = f"https://example.googlevideo.com/videoplayback?expire={int(time.time()) + 3600}"
        extractor.extract_info.return_value = {"url": url, "protocol": "https", "vcodec": "avc1", "acodec": "mp4a"}
        for method in (self.client.get, self.client.head):
            response = method("/video/abcdefghijk.mp4", follow_redirects=False)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(response.headers["location"], url)
            self.assertEqual(response.headers["cache-control"], "no-store")
        extractor.extract_info.assert_called_once_with("https://www.youtube.com/watch?v=abcdefghijk", download=False)
        self.assertEqual(ydl.call_args.args[0]["format"], main.FORMAT)

    def test_ttl_and_eviction(self):
        with patch.object(main.time, "time", return_value=1000):
            self.assertEqual(main.media_ttl("https://example.com/?expire=1180"), 120)
            self.assertEqual(main.media_ttl("https://example.com/?expire=9000"), 900)
            self.assertEqual(main.media_ttl("https://example.com/?expire=999"), 0)
            self.assertEqual(main.media_ttl("https://example.com/?expire=bad"), 0)
            self.assertEqual(main.media_ttl("https://example.com/"), 900)
        cache = main.Cache(1)
        cache.put("a", 1, 120)
        cache.put("b", 2, 120)
        self.assertIsNone(cache.get("a"))
        with patch.object(main.time, "monotonic", return_value=time.monotonic() + 121):
            self.assertIsNone(cache.get("b"))

    @patch.object(main, "extract")
    def test_reject_non_muxed_manifest_and_expired(self, extract):
        base = {"url": "https://example.com/media", "protocol": "https", "vcodec": "avc1", "acodec": "aac"}
        for changes in ({"acodec": "none"}, {"vcodec": "none"}, {"requested_formats": [{}, {}]}, {"protocol": "m3u8_native"}, {"url": "file:///etc/passwd"}, {"url": "https://example.com/?expire=1"}):
            extract.return_value = {**base, **changes}
            self.assertEqual(self.client.get("/video/abcdefghijk.mp4").status_code, 502)

    @patch.object(main, "YoutubeDL")
    def test_extraction_errors(self, ydl):
        extractor = ydl.return_value.__enter__.return_value
        for error, status in ((DownloadError("Private video"), 404), (DownloadError("upstream failed"), 502), (DownloadError("Requested format is not available"), 502), (RuntimeError("secret internal message"), 502)):
            extractor.extract_info.side_effect = error
            response = self.client.get("/video/abcdefghijk.mp4")
            self.assertEqual(response.status_code, status)
            self.assertNotIn("secret", response.text)

    @patch.object(main, "YoutubeDL")
    def test_block_pauses_other_ids_and_recovers(self, ydl):
        extractor = ydl.return_value.__enter__.return_value
        extractor.extract_info.side_effect = DownloadError("Sign in to confirm you’re not a bot")
        response = self.client.get("/video/abcdefghijk.mp4")
        self.assertEqual(response.status_code, 503)
        self.assertIn("sign-in verification", response.json()["detail"])
        self.assertGreater(int(response.headers["retry-after"]), 0)
        self.assertEqual(self.client.get("/video/12345678901.mp4").status_code, 503)
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(extractor.extract_info.call_count, 1)
        with patch.object(main.time, "monotonic", return_value=time.monotonic() + 301):
            extractor.extract_info.side_effect = None
            extractor.extract_info.return_value = {"entries": []}
            self.assertEqual(self.client.get("/playlist/PL1234567890.m3u").status_code, 200)

    @patch.object(main, "YoutubeDL")
    def test_warning_preserves_rate_limit_cause(self, ydl):
        def fail(*args, **kwargs):
            ydl.call_args.args[0]["logger"].warning("Unable to download webpage: HTTP Error 429: Too Many Requests")
            raise DownloadError("Failed to extract any player response")
        ydl.return_value.__enter__.return_value.extract_info.side_effect = fail
        response = self.client.get("/video/abcdefghijk.mp4")
        self.assertEqual(response.status_code, 503)
        self.assertIn("rate limiting", response.json()["detail"])

    def test_cached_urls_still_work_during_cooldown(self):
        main.videos.put("abcdefghijk", "https://example.com/media", 120)
        main.cooldown.trip("YouTube denied access")
        self.assertEqual(self.client.get("/video/abcdefghijk.mp4", follow_redirects=False).status_code, 302)

    def test_cookie_secret_is_copied_and_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "secret.txt"
            source.write_text("# Netscape HTTP Cookie File\n")
            with patch.dict(os.environ, {"YOUTUBE_COOKIE_FILE": str(source)}):
                with main.cookie_options() as options:
                    target = Path(options["cookiefile"])
                    self.assertNotEqual(target, source)
                    self.assertEqual(target.read_text(), source.read_text())
                    self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                    target.write_text("changed")
                self.assertFalse(target.exists())
                self.assertEqual(source.read_text(), "# Netscape HTTP Cookie File\n")
                with self.assertRaises(RuntimeError):
                    with main.cookie_options() as options:
                        target = Path(options["cookiefile"])
                        raise RuntimeError("extraction failed")
                self.assertFalse(target.exists())

    def test_busy(self):
        with patch.object(main, "extraction_slots") as slots:
            slots.acquire.return_value = False
            self.assertEqual(self.client.get("/video/abcdefghijk.mp4").status_code, 503)

    def test_real_ytdlp_format_selector(self):
        # Exercise yt-dlp's actual selector against synthetic, quality-sorted formats.
        formats = [
            {"format_id": "mp4", "ext": "mp4", "vcodec": "avc1", "acodec": "aac", "protocol": "https"},
            {"format_id": "webm", "ext": "webm", "vcodec": "vp9", "acodec": "opus", "protocol": "https"},
            {"format_id": "hls", "ext": "mp4", "vcodec": "avc1", "acodec": "aac", "protocol": "m3u8_native"},
            {"format_id": "dash", "ext": "mp4", "vcodec": "avc1", "acodec": "none", "protocol": "https"},
        ]
        with main.YoutubeDL({"quiet": True, "cachedir": False}) as ydl:
            selector = ydl.build_format_selector(main.FORMAT)
            for candidates, expected in ((formats, "mp4"), (formats[1:], "webm")):
                selected = list(selector({"formats": candidates, "has_merged_format": False, "incomplete_formats": False}))
                self.assertEqual([item["format_id"] for item in selected], [expected])


if __name__ == "__main__":
    unittest.main()
