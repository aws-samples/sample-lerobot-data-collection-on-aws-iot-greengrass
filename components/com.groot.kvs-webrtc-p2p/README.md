# com.groot.kvs-webrtc-p2p

**(Optional) low-latency live monitoring + recording, from one camera capture.** Streams the
**color camera** over **KVS WebRTC peer-to-peer (P2P)** for **sub-second live video** in the web UI,
**and** records the same feed to a KVS video stream for **per-episode HLS replay** — using a single
GStreamer capture `tee`'d to both sinks. This is a **monitoring** path, fully separate from the LeRobot
dataset S3 pipeline.

It is a low-latency alternative to [`com.groot.kvs-webrtc-ingest`](../com.groot.kvs-webrtc-ingest):
the storage-ingestion viewer relays media through the KVS cloud media server (several seconds of
buffering), whereas **P2P is device→browser directly (<1s)**. Recording/replay is preserved by teeing
a second branch into `kvssink`.

- **Platform**: `linux / aarch64` · `RequiresPrivilege: true`
- **Live channel**: `thor-001-p2p` (SINGLE_MASTER signaling channel, **no** `MediaStorageConfiguration` = pure P2P)
- **Recording stream**: `thor-001-webrtc` (KVS video stream, written by `kvssink`; browser replays per-episode segments over HLS)
- **Encoding**: GStreamer `v4l2src → videoconvert → clockoverlay → x264enc (zerolatency) → tee → { appsink (WebRTC P2P), kvssink (record) }`

## When to use this vs `com.groot.kvs-webrtc-ingest`

Both stream the color camera and both keep per-episode HLS replay (this one via a `kvssink` tee; the
storage one via `JoinStorageSession`). They differ on the live path:

| Dimension | `com.groot.kvs-webrtc-p2p` (this) | `com.groot.kvs-webrtc-ingest` (storage) |
|---|---|---|
| **Live latency** | **~sub-second** (device → browser directly) | **~2–5 s+** (device → KVS cloud media server → browser; storage/fragment buffering) |
| **Concurrent viewers** | Up to the KVS signaling-channel quota (**10 viewers per channel**), but in practice **bounded by the device's CPU/uplink** — the master sends RTP to every viewer, so the real limit depends on device resources | Up to the WebRTC-ingestion **multiviewer quota (3 concurrent viewers)**; the KVS cloud fans out, so it is **independent of device resources** |
| **Device encode load** | 1 encode (tee duplicates the H.264; no re-encode) | 1 encode |
| **Complexity** | Higher (WebRTC SDK + Producer SDK/`kvssink` + pipeline `tee`) | Lower (single storage sample) |

**Rule of thumb:** for the **lowest latency** (single / few-viewer operator monitor), prefer **this
(P2P)** — up to 10 viewers per channel, but each viewer adds device CPU/uplink (limited by device
resources). When you want the KVS cloud to fan out **independent of device resources** (up to the
3-viewer multiviewer quota), prefer **`kvs-webrtc-ingest` (storage)**. Both use the same MQTT topics
family but **distinct** viewer-creds topics (`.../webrtc/p2p-viewer` vs `.../webrtc/viewer`); the
browser page differs accordingly (`live-p2p.html` vs `multiviewer.html`).

## How it works

1. **install** (`Timeout: 5400`) — apt-installs GStreamer + build deps, clones and builds the KVS
   WebRTC C SDK **P2P master** sample (`kvsWebrtcClientMasterGstSample`) with
   `-DIOT_CORE_ENABLE_CREDENTIALS=ON` (auto-refreshing IoT credential provider = no restart-on-expiry
   gaps). It also builds the **KVS Producer SDK** GStreamer plugin (`kvssink`). It then patches the
   sample's `GstMedia.c` (from a pristine `git checkout` each deploy):
   - color (YUYV) `v4l2src` + a device **clock overlay** (HH:MM:SS, bottom-right),
   - low-latency `x264enc` (`tune=zerolatency bframes=0 key-int-max=30`),
   - a **`tee`** after the H.264 encoder: one branch to the WebRTC `appsink`, one branch to
     `kvssink` (recording). The patch is applied with a small Python rewrite that targets **only** the
     device-source video pipeline's `appsink` (robust to the multi-line C string literals / multiple
     pipeline variants in `GstMedia.c`).
2. **run** — resolves IoT credential settings from `effectiveConfig.yaml`, then runs the P2P master on
   the `thor-001-p2p` channel. `kvssink` records to `thor-001-webrtc` using the **IoT-certificate
   credential provider** (auto-refreshing, no downtime). A **background publisher** mints short-lived
   VIEWER-scoped STS credentials (`AssumeRole KvsViewerRole`) and delivers them to the browser over
   MQTT (`lerobot/{thing}/webrtc/p2p-viewer`, retained). The browser connects as a normal WebRTC
   **VIEWER** (sends the offer; the device master answers) — no `JoinStorageSession`.

## Configuration

| Key | Default | Description |
|---|---|---|
| `channelName` | `thor-001-p2p` | KVS **P2P** signaling channel (SINGLE_MASTER, **no** MediaStorageConfiguration). |
| `region` | `ap-northeast-2` | AWS region. |
| `videoDevice` | `/dev/video4` | Color v4l2 capture node (RealSense color). v4l2 numbering can shuffle across reboots — set to the actual color node. |
| `videoWidth` / `videoHeight` / `videoFps` | `640` / `480` / `30` | Capture format. |
| `credentialRefreshSec` | `86400` | Master restart interval (s). Kept high because both credential paths auto-refresh (no periodic restart needed). |
| `thingName` | `thor-001` | IoT Thing name — MQTT topic prefixes. |
| `viewerRoleArn` | `arn:aws:iam::<AWS_ACCOUNT_ID>:role/KvsViewerRole` | Scoped IAM role the device AssumeRoles to mint VIEWER credentials. **Replace `<AWS_ACCOUNT_ID>`.** |

## MQTT topics & IPC access control

- **Publish**: `groot/+/kvs-ingest/status`, `lerobot/+/webrtc/p2p-viewer` (viewer STS creds, retained)

> Uses a **distinct** viewer-creds topic (`.../webrtc/p2p-viewer`) so it does not collide with the
> storage-ingest component's `.../webrtc/viewer`. The web page connects as a P2P viewer (offer/answer)
> rather than `joinStorageSessionAsViewer`.

## Prerequisites (KVS)

1. Create the **P2P signaling channel** `thor-001-p2p` (SINGLE_MASTER, **no** MediaStorageConfiguration).
2. Ensure the **recording stream** `thor-001-webrtc` exists (a KVS video stream) for `kvssink` + HLS replay.
3. Create a **viewer IAM role** `KvsViewerRole` (viewer-only) and set `viewerRoleArn`. Grant the device
   TES role `kinesisvideo:*` (or minimal actions) + `sts:AssumeRole` on `KvsViewerRole`. `kvssink` uses
   the device's own IoT-certificate credentials (role alias) — no extra keys.

## 🔐 Security notes

- Replace `<AWS_ACCOUNT_ID>` in `viewerRoleArn` with your own account.
- Viewer credentials are published as **MQTT retained** — narrow the IoT policy so only clients
  authorized via the Custom Authorizer can subscribe.
- Keep `KvsViewerRole` to **viewer read permissions only**, with a minimal session duration.
- This recipe contains **no hardcoded AWS keys** (runtime issuance via TES + STS; `kvssink` via
  IoT-certificate). Do not add keys.

## Deploy notes

```bash
aws greengrassv2 create-component-version \
  --inline-recipe fileb://components/com.groot.kvs-webrtc-p2p/recipe.yaml --region <REGION>
# Example deploy config:
# {"channelName":"<p2p-channel>","thingName":"<thing>","videoDevice":"/dev/video4","viewerRoleArn":"arn:aws:iam::<AWS_ACCOUNT_ID>:role/KvsViewerRole"}
```

- The GStreamer pipeline is a C string literal — **no quotes/spaces in config values**; the install
  step re-applies patches idempotently (`git checkout` then Python rewrite) on every deploy.
- Pure P2P uses UDP (STUN/TURN); on restrictive firewalls the viewer may fall back to TURN relay over
  443. If P2P cannot connect at all, use the storage-ingest component + HLS instead.
- v4l2 `video*` numbers can change across reboots (RealSense vs USB cameras) — prefer a stable
  identifier (e.g. `/dev/v4l/by-path/...`) for the color node in production.
