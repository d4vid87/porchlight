# UI and draft-motion validation

Checked on 17 September 2026, before recording promotional demos.

- Refreshed navigation, overview, live view, recordings, rules, modes, people and grouped Settings screens.
- Dark and light appearances; narrow-screen layout checked at 390 × 844. Camera dialog keeps Apply accessible and has no horizontal page overflow.
- Camera editor exposes technical names and units, searchable fields, live preview and per-area motion thresholds.
- Camera drafts enable Apply, remain intact when discard is cancelled, and save only through Apply.
- Advanced settings synchronize common and search views; Discard restores both.
- Browser test of draft threshold 255 produced zero detections while the saved test threshold remained 20. Applying afterward changed the saved test value to 255.
- Actual isolated ZoneMinder replay checks passed with 1.36.33 and 1.38.4. Each analysed 100 frames; the lower threshold produced 99 and 87 detection frames respectively, and threshold 255 produced zero on both.
- Replay decoded successfully in the browser with a ten-second duration. HTTP byte-range seeking is covered by `test_motion.py`.
- Existing API checks, Python compilation, JavaScript syntax, whitespace checks and the Debian package build passed.

Run the checks from the repository root:

```sh
python3 test_api.py
python3 test_motion.py
python3 test_motion.py --engine
PORCHLIGHT_MOTION_IMAGE=porchlight-motion:1.38.4 python3 test_motion.py --engine
node --check web/app.js
git diff --check
```

The browser checks used an isolated API fixture and synthetic video, not a household's cameras. Engine tests ran in Docker on the development host. These checks do not establish native installation or live-camera compatibility on every Mint or Omarchy configuration. Remote viewer/admin access and the wider documentation/demo release remain separate work.
