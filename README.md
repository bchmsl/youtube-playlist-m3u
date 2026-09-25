# YouTube playlist → CarTV M3U

Small Python 3.12 / FastAPI backend for public YouTube playlists. No frontend,
media downloads, transcoding, merging, storage, or media proxying.

- `GET /` and `GET /health`: `{"status":"ok"}`.
- `GET /playlist/PLAYLIST_ID.m3u`: UTF-8 M3U in YouTube order, containing this
  service's permanent `/video/VIDEO_ID.mp4` URLs. Unavailable entries are skipped.
- `GET /video/VIDEO_ID.mp4`: HTTP 302 to a freshly resolved single stream with
  video **and** audio. MP4 is preferred; another muxed container may be returned
  by the fallback, despite the permanent `.mp4` route suffix.
- Playlist and video routes also accept HEAD (including `curl -I`).

Only validated IDs are accepted, never arbitrary extraction URLs. Flat playlist
extraction avoids resolving every video's formats. Metadata is cached for 120
seconds; media URLs for at most 900 seconds, shortened to 60 seconds before the
URL's `expire` timestamp. Cache sizes are bounded, failed results are not
cached (upstream blocks activate a separate cooldown), and caches are per process and disappear on restart. Responses use
`Cache-Control: no-store` so client/intermediary caching does not freeze playlists
or redirects. A player must reload its M3U to see additions/removals after the
metadata cache expires; no redeployment or manual M3U generation is needed.

## Run locally with Docker

Install and start Docker, then run from this directory:

```bash
docker build -t youtube-m3u .
docker run --rm -p 8000:8000 youtube-m3u
```

Open `http://localhost:8000/health`, then replace `PLAYLIST_ID` in:

```text
http://localhost:8000/playlist/PLAYLIST_ID.m3u
```

Copy the playlist ID from the `list=` parameter of a public YouTube playlist.
Copy an 11-character video ID from a video's `v=` parameter.

```bash
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/playlist/PLAYLIST_ID.m3u
# Inspect the redirect without following it:
curl -I http://localhost:8000/video/VIDEO_ID.mp4
# Follow it and inspect upstream headers (does not download the video):
curl -I -L http://localhost:8000/video/VIDEO_ID.mp4
```

Some media servers handle HEAD differently from GET. Actual playback in CarTV is
the final check. A 302 alone verifies resolution, not playback from your network.

## Deploy to Render

1. Create an empty GitHub repository. In this project directory:
   ```bash
   git init
   git add .
   git commit -m "Add YouTube M3U backend"
   git branch -M master
   git remote add origin https://github.com/YOUR_USERNAME/youtube-playlist-m3u.git
   git push -u origin master
   ```
   If `origin` already exists, check `git remote -v` and use the intended existing
   remote, or change it with `git remote set-url origin YOUR_REPOSITORY_URL`.
2. Sign in to Render, connect your GitHub account, choose **New → Blueprint**,
   select the repository, and deploy the detected `render.yaml`.
   The blueprint creates one Docker web service on the **paid Starter plan**
   to avoid free-tier idle spin-down. No persistent disk or secrets are needed.
   Alternatively choose **New → Web Service**, connect the repo, select Docker,
   use `./Dockerfile`, choose your plan, and set health check path `/health`.
3. Wait for the deploy to become live. Copy the public `https://...onrender.com`
   URL from the service dashboard; the name may differ from `youtube-m3u`.
4. Open `https://YOUR_HOST.onrender.com/health`; expect `{"status":"ok"}`.
5. Test a real playlist and a video redirect using the curl commands above with
   that host. Add this URL to CarTV:
   ```text
   https://youtube-m3u.onrender.com/playlist/PLxxxx.m3u
   ```
   Replace the host with your assigned Render host and `PLxxxx` with the full ID.

The Docker command binds `0.0.0.0` and uses `$PORT` (default 8000). Uvicorn trusts
forwarded proxy headers so Render HTTPS requests generate HTTPS M3U links. This
container is intended to sit behind Render's trusted reverse proxy. When exposing
it directly on another server, restrict `--forwarded-allow-ips` to that proxy's
IP(s) instead of `*`. URLs use the incoming Host; no hostname is hardcoded.

## Tests without Docker

With Python 3.12 installed:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m compileall -q app tests
python -m unittest discover -s tests -v
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

For real video resolution, use Docker; the image includes the PO Token provider. Tests
mock YouTube and do not need JavaScript runtimes or network access. Docker includes Deno and
`yt-dlp[default]`, including its supported EJS challenge scripts.

## Operational limits

YouTube changes extraction frequently. Dependencies have lower bounds rather
than a frozen lock so clean rebuilds can pick up yt-dlp fixes. To update a deployed
service, use Render's **Clear build cache & deploy**; rerun tests after updates.
For locally refreshed dependencies: `docker build --pull --no-cache -t youtube-m3u .`.

YouTube may block Render/datacenter IPs, require sign-in or additional tokens,
restrict content geographically, or return signed URLs bound to the resolver's
IP or requiring headers the player does not send. A redirect cannot transfer the
server's IP, cookies, or headers to CarTV. Therefore this architecture cannot
guarantee playback for every public video or network. There is deliberately no
proxy/download fallback. Test from your actual player after deployment.

A muxed format may be lower resolution or absent; DASH-only and manifest results
return 502. Flat metadata cannot discover every playback restriction, so a listed
item can still fail when opened. Invalid playlist IDs return 400; invalid video
IDs and recognized unavailable items return 404; extraction failures return 502.
Busy resolution returns 503 with Retry-After. Error details stay in server logs.
Health reports process availability, not YouTube availability.

One extraction runs at a time, with bounded socket timeouts/retries.
These are not a hard total extraction deadline for very large playlists. Deploy
one worker for shared in-memory caches; multiple instances have independent
caches. For heavier public use, add rate limiting at your ingress.

References: [Render Docker](https://render.com/docs/docker),
[Render Blueprint specification](https://render.com/docs/blueprint-spec),
[yt-dlp JavaScript setup](https://github.com/yt-dlp/yt-dlp/wiki/EJS).


## Recovering from YouTube bot checks / HTTP 429

When extraction fails after a bot check, HTTP 429, or HTTP 403, the service pauses new
YouTube extractions for five minutes across all IDs. Requests during that pause
return 503 with a descriptive error and `Retry-After`. Valid cached media URLs
and health checks still work. This prevents a player cycling through the playlist
from continuously retrying YouTube. It does not remove a YouTube block. The
cooldown is per process and resets on restart; use one worker/instance.
Warnings that yt-dlp recovers from do not trigger the cooldown.

First stop the player while troubleshooting. After deploying this change, test
one video rather than loading the entire playlist. If the block persists, optional
cookies may help, but cookies do not guarantee acceptance from Render or playback
from a different network.

To try cookies on Render:

1. Export a YouTube-only Netscape-format cookies file following
   [yt-dlp's YouTube export instructions](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies).
   Treat it as an account credential. Do not paste it into chat or commit it.
   Prefer a dedicated account with no private content: this service is public and
   has no authentication, so anyone knowing an ID could request content accessible
   to the configured account. yt-dlp warns of possible account restrictions.
2. In Render, open **youtube-m3u → Environment → Secret Files → Add Secret File**.
   Name it `youtube-cookies.txt` and put the exported file contents there.
3. Add environment variable:
   ```text
   YOUTUBE_COOKIE_FILE=/etc/secrets/youtube-cookies.txt
   ```
4. Save and deploy. Test only:
   ```bash
   curl -I https://youtube-m3u.onrender.com/video/vN7auOEG00U.mp4
   ```
   A 302 means extraction succeeded. Then verify playback in the player.
5. If it still reports bot verification/429, stop retries. Test the same service
   on your home network to distinguish a deployment-network restriction. Do not
   assume repeated redeploys or additional cookies will remove the block.

The cookie option is disabled unless configured. Each extraction uses a private
temporary copy so yt-dlp cannot overwrite Render's mounted secret; the copy is
removed afterwards, including on errors. No media is downloaded or stored.
Replace expired cookies through Render; remove the environment variable to disable
authenticated extraction. [Render secret-file documentation](https://render.com/docs/configure-environment-variables#secret-files).


## mweb + automatic PO Tokens

The Docker image now includes the matching **bgutil provider and Python plugin
2.0.0**, with Node.js for token generation and Deno for yt-dlp's JS challenges.
Video extraction explicitly uses `mweb`. The provider runs on demand in the same
container in script mode; no public token server, extra Render service, or manual
token entry is required. Python still calls the yt-dlp API with `download=False`;
the plugin's JavaScript subprocess only generates tokens. Cookies remain optional.

Deploy the latest `master` commit on Render. Keep your existing cookie setting.
`BGUTIL_SERVER_HOME=/opt/bgutil` is set in the image; do not override it in Render.
No changes to `render.yaml` are needed. The Docker build checks that the provider
script and both runtimes can start. The provider and its Python plugin must be
updated together. Third-party provider code is GPL-3.0; retain its bundled notices
when distributing the image.

New extraction starts are at least **10 seconds apart**, shared across playlist
and video requests. One extraction runs at a time and internal extractor webpage
requests have a one-second delay. These controls are per process, not per account
or across replicas. Cache hits bypass pacing. A request may wait up to ten seconds;
one overlapping request can wait up to 45 seconds for the resolver. Further
concurrent requests return 503/Retry-After rather than building an unbounded queue.
For intermittent `Sign in to confirm you're not a bot` responses, video resolution
is retried once after three seconds with a fresh extractor and PO Token. The
five-minute failure cooldown starts only when that retry also fails.

For a controlled deployment test, stop CarTV's playlist scanning and request two
video IDs sequentially (allow the first request to finish before starting the next):

```bash
curl --max-time 90 -I https://youtube-m3u.onrender.com/video/vN7auOEG00U.mp4
# Replace SECOND_VIDEO_ID with another real 11-character ID:
curl --max-time 90 -I https://youtube-m3u.onrender.com/video/SECOND_VIDEO_ID.mp4
```

A 302 on each proves resolution only; play both from CarTV to verify audio/video
and access from the player's network. PO Tokens do not guarantee removal of IP
blocks, bot checks, or availability of a muxed format. No DASH merging or media
proxying fallback has been added. If the test fails, use the first extraction
failure in Render logs rather than repeated retries.

References: [yt-dlp PO Token guide](https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide),
[bgutil provider setup](https://github.com/Brainicism/bgutil-ytdlp-pot-provider).
