<p align="center">
  <img src="porchlight.png" width="128" alt="Porchlight">
</p>

<h1 align="center">Porchlight</h1>

<p align="center"><strong>Security cameras for people who don't want to learn security cameras.</strong></p>

<p align="center">
  A Debian package that installs and configures <a href="https://zoneminder.com">ZoneMinder</a>
  for you, then puts a plain-language web app in front of it.
</p>

<p align="center">
  <img alt="Platform: Debian / Ubuntu" src="https://img.shields.io/badge/platform-Debian%20%7C%20Ubuntu-blue">
  <img alt="Install: .deb package" src="https://img.shields.io/badge/install-.deb%20package-orange">
  <img alt="Video: 100% local" src="https://img.shields.io/badge/video-100%25%20local-brightgreen">
</p>

![The Cameras page](docs/screenshots/cameras.png)

## Why Porchlight

- **Private by design.** Video is recorded, stored and watched on your own computer.
  No account, no cloud, no subscription — nothing leaves your house unless you send it.
- **Somebody at the door, not "motion detected".** A small person detector
  (NanoDet, Apache-2.0) runs on your own CPU once per recording. A person makes the
  alert urgent and puts them in the picture; wind and headlights don't. The headline
  feature of every camera subscription, running at home.
- **Works with cameras you already own.** Porchlight scans your network with ONVIF and
  fills in the technical details itself. RTSP addresses, USB webcams, MJPEG cameras and
  plain video files all work too.
- **Phone alerts without a vendor app.** Movement rules ring your phone through the
  free, open-source [ntfy](https://ntfy.sh) app: an unguessable topic name, no account,
  a snapshot of the moment in the notification, and snooze buttons for noisy evenings.
  Point it at your own ntfy server and even the snapshot never leaves home.
- **It tells you when it's broken.** A camera that stops answering rings your phone —
  and rings again when it's back. Old recordings make room before the disk fills.
- **Watch from your phone.** Open the app to your home Wi-Fi — off by default, always
  password-protected — and save it to your phone's home screen like an installed app.
- **Plain language, expert power.** Everyday tasks read like sentences; every ZoneMinder
  option stays one click away in a searchable expert table.
- **Try it before the camera arrives.** A bundled sample video sets up a working camera
  so you can explore the whole app with nothing to plug in.

## What you'd pay for elsewhere

| | Ring Protect | Nest Aware | Wyze Cam Plus | Porchlight |
|---|---|---|---|---|
| Monthly price | $4.99+ | $8+ | $2.99/camera | $0 |
| Snapshot in the alert | subscription | subscription | subscription | included |
| Person detection | subscription | subscription | subscription | included, on your CPU |
| Recording history | cloud, 180 days | cloud, 30–60 days | cloud, 14 days | your disk, your rules |
| Works with the internet down | no | no | no | yes |
| Footage leaves your house | always | always | always | never |

## A tour

### Cameras

Add a camera and Porchlight scans the network with ONVIF, asks for the camera's
username and password, works out the video address, and saves it. USB webcams, video
files, MJPEG cameras and hand-typed RTSP addresses are all under "More camera types".

Every camera gets a card: a still that refreshes about once a minute, a badge saying
whether it is live, off or unreachable, its recording mode, and a ⋯ menu for watching,
watched areas, moving and removing it. Above them sit four tiles — Home / Away,
storage, today's recordings and system health — that each open the page behind them.
The whole app is dark by default; "Light" in the top bar switches it and remembers
your choice. That's the page pictured at the top of this file.

### Live view

One, four, nine or sixteen cameras at once, at a quality you pick, optionally cycling
through them. Click a picture to fill the screen with it.

![Live view](docs/screenshots/live.png)

### Recordings

A 24-hour strip shows when things happened; click an hour to jump straight to it.
Filter by camera, day, motion or continuous, and kept-forever; sort newest first or
most movement first. Click to play, save the clip, keep it forever, or **Share** it —
an expiring three-day link anyone can watch without your password.

![Recordings](docs/screenshots/recordings.png)

### Rules and phone alerts

"When movement is recorded on the Front Door between 22:00 and 06:00 on weekdays,
alert my phone" — written as a sentence, stored as a real ZoneMinder filter. Rules can
ignore two-frame blips, and snooze chips on the Cameras page silence every alert for
30 minutes to 8 hours.

**Phone alerts** need no account: install the free [ntfy](https://ntfy.sh) app,
subscribe to the random topic on this page, and matching recordings ring your phone
with a snapshot of the moment attached. A person in the picture makes the alert
urgent; a per-camera setting can skip person-free alerts entirely. If a camera stops
answering, your phone hears about that too. Email settings and a test button live on
the same page.

![Rules](docs/screenshots/rules.png)

### Home / Away

Each mode says what every camera does; switch by hand or on a weekly schedule.

![Home / Away](docs/screenshots/modes.png)

### People

Turn on a password for the whole system and add viewer or manager accounts.

![People](docs/screenshots/people.png)

### System

Health, storage, restart, a one-file **backup** of cameras, rules and settings (and
the button that restores it on a fresh install), the common settings in plain English,
and every ZoneMinder option in a searchable expert table.

**Watch from your phone** opens the app to your home network. It is off by default;
turning it on requires a password, and every request that does not come from this
computer has to sign in with it. The page shows the address to type on your phone,
which can then be saved to the home screen as an app.

![System](docs/screenshots/system.png)

Per-camera **Settings** covers basics, connection, video, motion, PTZ control and, on
the last tab, every remaining ZoneMinder monitor field. **Areas** is a polygon editor
over a live snapshot for motion zones and privacy masks.

## Getting started

Porchlight runs on Debian and Ubuntu.

```sh
sudo apt install ./porchlight_2.1_all.deb
porchlight
```

The package pulls in ZoneMinder and MySQL, sets up the database, fixes the Ubuntu
packaging gaps that otherwise leave ZoneMinder broken, and adds a "Porchlight"
launcher. Running `porchlight` starts the local service on 127.0.0.1:8321 and opens it
in your browser. No camera yet? Click **Try it with a sample video** on the first
screen.

## Under the hood

- `server/zmapi.py` — everything that talks to ZoneMinder: REST API, token auth, ONVIF
  probing, and the request builders.
- `server/porchlight_server.py` — stdlib HTTP server, static files plus a JSON API.
- `server/detect.py` — the person detector: NanoDet-Plus-m-416 from the
  [OpenCV Zoo](https://github.com/opencv/opencv_zoo) (Apache-2.0) on onnxruntime's
  CPU provider, decoding JPEGs through the ffmpeg that is already a dependency.
  `python3-onnxruntime` is only Recommended; without it, alerts simply go out
  unfiltered.
- `web/` — vanilla HTML, CSS and JavaScript. No build step, no dependencies.
- `admin/porchlight-admin` — the only privileged code, reached through polkit with a
  fixed list of subcommands.
- `push.sh` — ZoneMinder filters run it per event; it hands the event to the local
  server, which applies cooldown, snooze and the person check before posting to ntfy.
  If the server isn't running it still sends a bare alert by itself.

Filters and users are written straight to ZoneMinder's own database: the 1.36 API has
no filters endpoint and refuses user writes. The timeline, event search grid, storage
areas and server groups stay in ZoneMinder's own pages — same cameras, same
recordings, same database, linked from the System page.

### Development

```sh
./build.sh                 # builds porchlight_2.1_all.deb
python3 test_api.py        # pure-logic checks, prints "ok"
python3 e2e_drive.py       # full end-to-end run, needs a working ZoneMinder
python3 shots.py OUTDIR    # demo data + the screenshots above (needs Xvfb)
python3 tools/make_icon.py porchlight.png logo48.png   # redraws the app icon
```

The camera footage in the screenshots is public-domain video from Wikimedia
Commons: a home security camera on Prince Edward Island, a snowy bird feeder,
a suburban driveway at night, and a US Fish & Wildlife trail camera.
