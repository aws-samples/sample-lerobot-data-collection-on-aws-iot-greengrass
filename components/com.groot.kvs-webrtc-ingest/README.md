# com.groot.kvs-webrtc-ingest

**(Optional) live video monitoring source.** Streams the **color camera → Amazon Kinesis Video Streams
(KVS) WebRTC** so the web UI can watch **sub-second live video while recording**, and replay
per-episode segments over HLS from the same stream. This is a **monitoring** path, fully separate from
the LeRobot dataset S3 pipeline.

- **Platform**: `linux / aarch64` · `RequiresPrivilege: true`
- **Stream/channel**: `thor-001-webrtc` (signaling channel with `MediaStorageConfiguration` ENABLED)
- **Encoding**: GStreamer `v4l2src → videoconvert → clockoverlay → H.264`

## How it works

1. **install** (`Timeout: 3600`) — apt-installs GStreamer + build deps, clones and builds the KVS
   WebRTC C SDK **storage** sample (`kvsWebrtcStorageAudioVideoMasterGstSample`). It patches the
   sample's `GstMedia.c` (from a pristine `git checkout` each deploy) to (a) use a **color (YUYV)**
   v4l2 source — auto-detected if `videoDevice` is empty, skipping IR/GREY/Depth nodes — and (b) burn
   a **device clock (HH:MM:SS) overlay** into the bottom-right via `clockoverlay`.
2. **run** — obtains device (TES) credentials, ensures the signaling channel exists, and runs the
   storage master. Because the channel has storage enabled, the SDK calls **`JoinStorageSession`**, so
   the H.264 media is **ingested and persisted** into the KVS stream (not just peer-to-peer). A
   **background publisher** mints short-lived (1h) **VIEWER-scoped STS credentials** (`AssumeRole
   KvsViewerRole`) and delivers them to the browser over MQTT (`lerobot/{thing}/webrtc/viewer`,
   retained) so the browser can `joinStorageSessionAsViewer`. A loop refreshes credentials and
   restarts the master.

## Configuration

| Key | Default | Description |
|---|---|---|
| `channelName` | `thor-001-webrtc` | KVS signaling channel name (must have `MediaStorageConfiguration` → same-named stream, storage ENABLED). |
| `region` | `ap-northeast-2` | AWS region. |
| `videoDevice` | `""` | v4l2 capture node. **Empty = auto-detect a color (YUYV) node**. Set explicitly (e.g. `/dev/video8`) if auto-detect picks the wrong device. |
| `videoWidth` | `640` | Capture width (px). |
| `videoHeight` | `480` | Capture height (px). |
| `videoFps` | `30` | Capture frame rate. |
| `credentialRefreshSec` | `2700` | Interval (s) to refresh credentials and restart the master (45 min). |
| `thingName` | `thor-001` | IoT Thing name — MQTT topic prefixes (`groot/{thing}/kvs-ingest/status`, `lerobot/{thing}/webrtc/viewer`). |
| `viewerRoleArn` | `arn:aws:iam::<AWS_ACCOUNT_ID>:role/KvsViewerRole` | Scoped IAM role the device AssumeRoles to mint VIEWER credentials. **Replace `<AWS_ACCOUNT_ID>`.** |

## MQTT topics & IPC access control

- **Publish**: `groot/+/kvs-ingest/status`, `lerobot/+/webrtc/viewer` (viewer STS creds, retained)

(Subscription is not needed — ingestion runs continuously.)

## Prerequisites (KVS)

1. Create the **signaling channel + `MediaStorageConfiguration`** (channel → same-named stream, storage
   ENABLED).
2. Create a **viewer IAM role** `KvsViewerRole` (viewer-only, scoped to that channel/stream) and set it
   as `viewerRoleArn`. Grant the device TES role `kinesisvideo:*` (or the minimal ingest actions) +
   `sts:AssumeRole` on `KvsViewerRole`.

## 🔐 Security notes

- Replace `<AWS_ACCOUNT_ID>` in `viewerRoleArn` with your own account.
- Viewer credentials are published as **MQTT retained** — narrow the IoT policy so only clients
  authorized via the Custom Authorizer can subscribe (the last message stays until it expires).
- Keep `KvsViewerRole` to **viewer read permissions only**, and the session duration to the minimum.
- This recipe contains **no hardcoded AWS keys** (runtime issuance via TES + STS). Do not add keys.

## Deploy notes

```bash
aws greengrassv2 create-component-version \
  --inline-recipe fileb://components/com.groot.kvs-webrtc-ingest/recipe.yaml --region <REGION>
# Example deploy config:
# {"channelName":"<channel>","thingName":"<thing>","videoDevice":"","viewerRoleArn":"arn:aws:iam::<AWS_ACCOUNT_ID>:role/KvsViewerRole"}
```

- The GStreamer pipeline is a C string literal — **no quotes/spaces in config values**; the install
  step re-applies patches idempotently (`git checkout` then `sed`) on every deploy.
- If the color camera is unstable over raw v4l2 (e.g. RealSense color nodes), set `videoDevice`
  explicitly. See the root [`README.md`](../../README.md) "(Optional) Live Video Monitoring" section.
