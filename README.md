# Porchlight

![Porchlight](logo48.png)

Security cameras for people who don't want to learn security cameras.

Porchlight is a Debian package that installs and configures ZoneMinder for you,
then puts a plain-language web app in front of it. It finds cameras on your
network, fills in the technical details, shows every camera at once, plays back
recordings, and keeps every expert ZoneMinder option one click away.

## Install

```sh
sudo apt install ./porchlight_2.0_all.deb
porchlight
```

The package pulls in ZoneMinder and MySQL, sets up the database, fixes the
Ubuntu packaging gaps that otherwise leave ZoneMinder broken, and adds a
"Porchlight" launcher. Running `porchlight` starts the local service on
127.0.0.1:8321 and opens it in your browser.

## What it does

### Cameras

Add a camera and Porchlight scans the network with ONVIF, asks for the camera's
username and password, works out the video address, and saves it. USB webcams,
video files, MJPEG cameras and hand-typed RTSP addresses are all under
"More camera types".

No camera yet? The Cameras page offers a **sample video** so you can try the app
before the box arrives.

Every camera gets a card: a still that refreshes about once a minute, a badge
saying whether it is live, off or unreachable, its recording mode, and a ⋯ menu
for watching, watched areas, moving and removing it. Above them sit four tiles —
Home / Away, storage, today's recordings and system health — that each open the
page behind them. The whole app is dark by default; "Light" in the top bar
switches it and remembers your choice.

![Cameras](docs/screenshots/cameras.png)

### Live view

One, four, nine or sixteen cameras at once, at a quality you pick, optionally
cycling through them. Click a picture to fill the screen with it.

![Live view](docs/screenshots/live.png)

### Recordings

Filter by camera, day, motion or continuous, and kept-forever. Click to play,
save the clip, or keep it forever.

![Recordings](docs/screenshots/recordings.png)

### Rules

"When movement is recorded on the Front Door between 22:00 and 06:00, alert my
phone" — written as a sentence, stored as a real ZoneMinder filter.

**Phone alerts** need no account and no cloud video: install the free
[ntfy](https://ntfy.sh) app, subscribe to the random topic on this page, and
matching recordings ring your phone. Point it at your own ntfy server if you'd
rather. Email settings and a test button live on the same page.

![Rules](docs/screenshots/rules.png)

### Home / Away

Each mode says what every camera does; switch by hand or on a weekly schedule.

![Home / Away](docs/screenshots/modes.png)

### People

Turn on a password for the whole system and add viewer or manager accounts.

![People](docs/screenshots/people.png)

### System

Health, storage, restart, the common settings in plain English, and every
ZoneMinder option in a searchable expert table.

**Watch from your phone** opens the app to your home network. It is off by
default; turning it on requires a password, and every request that does not come
from this computer has to sign in with it. The page shows the address to type on
your phone, which can then be saved to the home screen as an app.

![System](docs/screenshots/system.png)

Per-camera **Settings** covers basics, connection, video, motion, PTZ control
and, on the last tab, every remaining ZoneMinder monitor field. **Areas** is a
polygon editor over a live snapshot for motion zones and privacy masks.

## Development

```sh
./build.sh                 # builds porchlight_2.0_all.deb
python3 test_api.py        # pure-logic checks, prints "ok"
python3 e2e_drive.py       # full end-to-end run, needs a working ZoneMinder
python3 shots.py OUTDIR    # demo data + the screenshots above (needs Xvfb)
```

- `server/zmapi.py` — everything that talks to ZoneMinder: REST API, token auth,
  ONVIF probing, and the request builders.
- `server/porchlight_server.py` — stdlib HTTP server, static files plus a JSON API.
- `web/` — vanilla HTML, CSS and JavaScript. No build step, no dependencies.
- `admin/porchlight-admin` — the only privileged code, reached through polkit with
  a fixed list of subcommands.
- `push.sh` — three lines of curl behind the phone alerts; ZoneMinder filters run
  it per event, with the topic in the command itself.

Filters and users are written straight to ZoneMinder's own database: the 1.36
API has no filters endpoint and refuses user writes.
