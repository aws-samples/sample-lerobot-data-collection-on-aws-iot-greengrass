# Web UI

Single-page operator consoles for the LeRobot data-collection pipeline. Each page
is a self-contained HTML file (inline CSS/JS, no build step) that talks to AWS IoT
Core over MQTT (WSS) and plays the device camera through Kinesis Video Streams
(KVS). They are hosted as static objects on S3 and served through CloudFront.

Both pages share the same control panel, file browser, status badge, and
per-episode replay. **They differ only in how the *live* view is delivered** —
low-latency peer-to-peer vs. multi-viewer through KVS storage.

| File | Live view | Latency | Concurrent viewers | Default root object |
|---|---|---|---|---|
| [`live-p2p.html`](./live-p2p.html) | WebRTC **P2P** (signaling channel `thor-001-p2p`) | sub-second | limited (P2P master fan-out on the device) | ✅ yes |
| [`multiviewer.html`](./multiviewer.html) | WebRTC **storage** (`joinStorageSessionAsViewer` on `thor-001-webrtc`) | a few seconds (KVS relay/fragment buffering) | multiple (subject to the KVS storage-session viewer quota) | no |

Pick one as the CloudFront default root object. See the P2P-vs-storage trade-off
notes in the component READMEs (`components/com.groot.kvs-webrtc-p2p` and
`components/com.groot.kvs-webrtc-ingest`).

---

## `live-p2p.html` — low-latency live monitoring

- **Live**: joins the device's WebRTC **P2P master** on signaling channel
  `thor-001-p2p` (browser sends the offer; device answers). Because the media
  goes straight device→browser, latency is **sub-second**. Best for tight
  teleoperation feedback with a small number of viewers.
- **Episode replay**: still uses the recorded `thor-001-webrtc` stream over HLS
  (`GetHLSStreamingSessionURL`, `PRODUCER_TIMESTAMP`), so you keep DVR-style
  per-episode playback even though the live path is P2P.
- Use it as the CloudFront **default root object** for the day-to-day
  single-operator collection workflow.

## `multiviewer.html` — multi-viewer live monitoring

- **Live**: joins the recorded `thor-001-webrtc` stream as a **storage-session
  viewer** (`joinStorageSessionAsViewer`). Several people can watch at once, and
  the same stream feeds the episode HLS replay. The trade-off is a few seconds of
  latency from KVS storage/fragment buffering.
- **Episode replay**: identical to `live-p2p.html` (HLS on `thor-001-webrtc`).
- Use it when more than one person needs to watch the same session, or when P2P
  (UDP) is blocked and you want an all-through-KVS path.

---

## Shared features

- **Login** — connects to AWS IoT Core over MQTT/WSS, gated by an IoT **custom
  authorizer** (`OrangeWebAuthorizer`). Enter the username/password configured on
  the authorizer (defaults `<WEB_USERNAME>` / `<WEB_PASSWORD>`). No AWS
  credentials live in the page.
- **Recording controls** — Start (with target *Episodes* and *Reset (s)*),
  Stop & Save (advance to the next episode), Discard, End Session.
- **Status badge** — `REC` only while an episode is actually recording;
  `⚙️ SETUP` during warmup / save / the inter-episode reset gap. Falls back to a
  plain `REC` if the device's `collect.py` doesn't emit `recSeq`.
- **Reset countdown** — shows the remaining seconds of lerobot's inter-episode
  reset window (`resetRemaining`); blank if the device doesn't report it.
- **File browser** — lists uploaded dataset files for a date/task. The list is
  **paginated** and download URLs are **presigned lazily on click** (a `geturl`
  request per file) so the device→browser message never exceeds the AWS IoT Core
  128 KB MQTT payload limit. Supports re-uploading selected files.
- **Episode replay** — reads the latest session's episode windows from the device
  shadow (`shadow/name/episodes`) and plays each `[start, end]` window from
  `thor-001-webrtc` over HLS.

### MQTT topics (device thing name shown as `thor-001`)

| Topic | Direction | Purpose |
|---|---|---|
| `lerobot/thor-001/collect/command` | UI → device | start / stop / discard / endSession / list / geturl / uploadFiles |
| `lerobot/thor-001/collect/status` | device → UI | state, episode, `recSeq`, `resetRemaining`, error |
| `lerobot/thor-001/collect/files` | device → UI | paginated file list + lazy `{action:"url"}` responses |
| `lerobot/thor-001/webrtc/viewer` | device → UI (retained) | short-lived viewer STS credentials for `thor-001-webrtc` (storage live + episode HLS) |
| `lerobot/thor-001/webrtc/p2p-viewer` | device → UI (retained) | viewer credentials for the `thor-001-p2p` live path (`live-p2p.html` only) |
| `$aws/things/thor-001/shadow/name/episodes` | device → UI | latest session's per-episode time windows for replay |

---

## Configure before deploying

Replace the placeholders in the HTML with your environment's values:

| Placeholder | Meaning |
|---|---|
| `<IOT_ENDPOINT>` | AWS IoT Core ATS data endpoint (e.g. `xxxxxxxx-ats.iot.<region>.amazonaws.com`) |
| `<DATA_BUCKET>` | S3 bucket that holds the uploaded datasets (default value shown in the *Bucket* field) |
| `<WEB_USERNAME>` / `<WEB_PASSWORD>` | credentials checked by the `OrangeWebAuthorizer` custom authorizer |

`thor-001` (thing name), `OrangeWebAuthorizer` (authorizer name), and the KVS
channel/stream names (`thor-001-p2p`, `thor-001-webrtc`) are example values from
the reference deployment — change them to match your own thing and resources.

## Deploy

```bash
# upload one screen to the web UI bucket, serving it as HTML
aws s3 cp live-p2p.html    s3://<WEB_UI_BUCKET>/live-p2p.html    --content-type text/html
aws s3 cp multiviewer.html s3://<WEB_UI_BUCKET>/multiviewer.html --content-type text/html

# after an update, invalidate the CloudFront cache
aws cloudfront create-invalidation --distribution-id <DIST_ID> --paths "/live-p2p.html" "/multiviewer.html" "/"
```

Set the CloudFront **default root object** to whichever screen you want at `/`
(the reference deployment uses `live-p2p.html`). `deploy.sh` at the repo root
automates the bucket upload, placeholder substitution, and invalidation.
