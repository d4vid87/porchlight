# Test motion before applying

Open a camera's **Settings → Motion**. The technical fields show the saved values for each watched area. Change a threshold, then select **Test unsaved settings**. Testing does not save these edits.

Porchlight captures a fresh ten-second sample at ten frames per second. A disposable container replays that sample with the same ZoneMinder version as the recorder, using your draft detection thresholds and the saved watched-area geometry. Its database is separate, its network is disabled, and no alert/filter daemons run. The replay uses ZoneMinder's own analysis images to highlight detected motion.

Review the replay, adjust the thresholds and test again as needed. **Apply changes** saves the camera and watched-area edits. Cancel asks before discarding a draft. If one save fails, the remaining unsaved edits stay in the editor for retry; earlier successful writes are not rolled back.

This is a short, newly initialized replay, not a prediction of every future alarm. Capture is limited to 10 fps; camera connection, dimensions, recording mode and alert changes are excluded. Lighting changes, camera noise, analysis frame rate and reference-image history can affect results. When you edit during a test, the result identifies that the tested draft is older than the current controls.

## Install the isolated worker

Docker and ffmpeg must be available to the account running Porchlight. The supplied image packages ZoneMinder **1.36.33** from Ubuntu 24.04, suitable for a recorder running that exact version, including Linux Mint installations using that package:

```sh
docker build -f tools/motion/Dockerfile -t porchlight-motion:1.36.33 tools
python3 test_motion.py --engine
```

The check generates synthetic moving footage and verifies that two draft thresholds produce motion and no motion respectively. It never contacts a production camera or database.

For ZoneMinder **1.38.4**, build the second image after the base image:

```sh
docker build -f tools/motion/Dockerfile.1.38 -t porchlight-motion:1.38.4 tools
PORCHLIGHT_MOTION_IMAGE=porchlight-motion:1.38.4 python3 test_motion.py --engine
```

This image uses the [ZoneMinder project's 1.38 package repository](https://wiki.zoneminder.com/Ubuntu_Server_or_Desktop_Zoneminder_1.38.x). The Docker host can be Linux Mint or Omarchy; its distribution does not have to match the worker's Ubuntu base. Porchlight selects the locally built image tagged with the recorder's version automatically. Both 1.36.33 and 1.38.4 have passed the synthetic motion/no-motion checks.

For other versions, supply a compatible image containing the worker and set `PORCHLIGHT_MOTION_IMAGE`. Testing remains unavailable if the installed worker does not match the recorder. Porchlight never downloads a worker automatically.

The worker has a two-CPU, 2 GB memory limit and 512 MB shared-memory limit. Test inputs are limited to a primary analysis stream and a maximum of 3840 × 2160 pixels. The automated fixtures use 640 × 360 video; high-resolution performance depends on the host.

Only one test runs at a time. Sample media and copied configuration are removed when the job ends; the replay is deleted ten minutes later. All test output is temporary. Live camera settings, recordings and notifications are unaffected by the isolated analysis.
