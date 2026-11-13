# Spike S0.9: browser memory under 2 GB (D82)

Date: 2026-11-13. Machine: the development host (Ubuntu, podman). Story: S0.9, R-9.1.

## Setup

- The app image (`deploy/Dockerfile`, Playwright 1.64.1, Chromium headless shell 1248), run with `--memory 2g`. Postgres ran beside it in its own Compose container (34 MB in use at the time).
- One throwaway script in the container: Playwright launches the headless shell with spec 03 §8's flags (`--disable-dev-shm-usage --no-zygote --renderer-process-limit=1 --disable-gpu`, plus `--blink-settings=imagesEnabled=false` when images are off), one context with route interception that aborts fonts, media, analytics hosts and, with images off, images. It opens La Leña's lookup page (`networkidle`), waits 1 s, and closes.
- Each run is a fresh container, so its cgroup `memory.peak` covers exactly one helper: Python, the Playwright driver and every Chromium process, plus the page cache they touch.
- Three runs per mode.

## Results

| Mode                      | cgroup peak | Summed RSS peak | Cold start to page loaded |
| ------------------------- | ----------- | --------------- | ------------------------- |
| Images off (default)      | 223–224 MB  | 597–598 MB      | 3.4–4.2 s                 |
| Images on (captcha pages) | 318–319 MB  | 669–679 MB      | 4.4–5.9 s                 |

- **The cgroup peak is the number that counts.** Summed RSS counts the memory Chromium's processes share once per process, so it overstates the real use by more than half.
- The cold start includes launching Chromium and loading the page over the internet, so it is an upper bound for the launch cost D82 adds per fetch.

## Verdict

- **Gate passed:** 224 MB with images off and 319 MB with images on, under the plan's 450 MB limit (R-9.1's 800 MB app peak minus the 350 MB steady state).
- A helper per fetch costs about 4 s of cold start, which is small next to the portal's own response time and the queue's pace. D82 stands.
- `ops` (S12.1) repeats this measurement with the bot running, under the load test of 10 queued invoices.

## Live fetch on La Leña (M2, 2026-11-13)

`invoicebot portal-test facturactiva-la-lena` on the development host (not the container), with the owner's Oct 2026 invoice as the sample. Each run went in its own `systemd-run --user` scope, so the cgroup `memory.peak` covers the CLI, the browser helper and every Chromium process.

| Run                   | Result                             | cgroup peak | Wall time |
| --------------------- | ---------------------------------- | ----------- | --------- |
| Real invoice, 3 runs  | `ok`: PDF and XML, the XML matches | 315 MB      | 6.0–6.8 s |
| Made-up número, 1 run | `not_found_yet`                    | —           | 5.5 s     |

- The peak matches the spike's images-off number plus the CLI's own Python process, still under the 450 MB helper budget.
- The first runs found a bug: La Leña's PDF button fires its download twice, and the second one landed in the XML slot, so the XML was a copy of the PDF (`xml_invalid`). The helper now skips a download named for the other kind (`fetchers/browser/form.py`).
