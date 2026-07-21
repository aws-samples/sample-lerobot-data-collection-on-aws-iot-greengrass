<div align="center">
  <h1>LeRobot Data Collection — AWS IoT Greengrass Component</h1>
  <p>
    <a href="./README.md">English</a>
    ◆ <a href="./README-ko.md">한국어</a>
  </p>
</div>

> ⚠️ **IMPORTANT — Sample code, not for production.** This is sample code for
> educational and demonstration purposes only and is **NOT intended for
> production use**. Before any deployment, work with your security and legal
> teams to meet your organizational security, regulatory, and compliance
> requirements, and complete the hardening described under **Known Limitations
> (Demo/POC)** below. Licensed under MIT-0 (see `LICENSE`). Deploying this
> content may create AWS resources and incur charges.

An AWS IoT Greengrass v2 custom component (`com.lerobot.data-collection`) that records
teleoperation data from a SO-ARM101 (Leader/Follower) plus dual cameras in **LeRobot format**
and uploads it to **Amazon S3**. Controlled remotely over MQTT from a web UI.

Validated on Jetson AGX Thor (JetPack 7 / CUDA 13, aarch64).

> [!IMPORTANT]
> The examples provided in this repository are for experimental and educational purposes only.
> They demonstrate concepts and techniques but are not intended for direct use in production
> environments.

### Key Features
- 🎮 **Remote control** — Start recording / save & next / end session / discard / upload over MQTT from the web console
- 🎥 **Live monitoring** — Color-camera WebRTC live (+ firewall-safe HLS fallback), with a device clock (HH:MM:SS) overlay burned into the video
- 🎞 **Episode playback** — Replay per-session / per-episode segments from KVS over HLS (Device Shadow driven)
- 📦 **Auto upload** — LeRobot v3.0 datasets to S3 (date/instruction/session layout); completed files are played back / downloaded via presigned URLs
- 🗂 **File browser** — Check upload status · re-upload missing files · download
- 🧱 **Self-contained image** — The component install builds the data-collection Docker image from scratch on the device (no dependency on an inference base)

## Demo Video
  
https://github.com/user-attachments/assets/ffb4a431-67ce-44cd-9650-570edc4c581c
  

## Architecture

![Architecture — LeRobot data collection on AWS IoT Greengrass](docs/architecture.png)

<details>
<summary>Text diagram</summary>

```
┌───────────────────────────────────────────────────────────────────┐
│  🌐  Web console (CloudFront + MQTT over WSS, Custom Authorizer)    │
│      Control · Status · Live video · Episode playback · File browser│
└──────────────┬─────────────────────────────────┬──────────────────┘
      MQTT command·status·Shadow             WebRTC / HLS video
               │                                 │
┌──────────────▼──────────────┐     ┌────────────▼──────────────────┐
│  ☁️  AWS IoT Core            │     │  ☁️  Kinesis Video Streams     │
│     Custom Authorizer auth   │     │     Live (WebRTC) · Replay(HLS)│
│     Device Shadow: episodes  │     └────────────▲──────────────────┘
└──────────────┬──────────────┘                  │ color video ingest
               │ Greengrass IPC                   │
┌──────────────▼──────────────────────────────────┴─────────────────┐
│  🤖  Greengrass Core — Jetson AGX Thor                             │
│   ┌───────────────────────┐    ┌──────────────────────────────┐   │
│   │ collect.py            │    │ com.groot.kvs-webrtc-ingest  │   │
│   │  · MQTT control/status │    │  · color cam → H.264 → KVS   │   │
│   │  · docker run lerobot │    │  · device clock overlay       │   │
│   │  · S3 upload/presign   │    │  · viewer STS credentials     │   │
│   │  · episode shadow     │    └──────────────────────────────┘   │
│   └──────────┬────────────┘                                        │
│              │ docker      SO-101 arms (/dev/ttyACM0·1)            │
│   ┌──────────▼──────────────────────────┐  dual cameras(/dev/cam_*)│
│   │ 🐳 lerobot-record (self-built image) │                         │
│   └──────────────────────────────────────┘                        │
└───────────────────────────────┬───────────────────────────────────┘
                     boto3 upload (LeRobot v3.0)
                                 │
┌───────────────────────────────▼───────────────────────────────────┐
│  🪣  Amazon S3 — datasets/{date}/{slug}/{session}/                  │
│      data/*.parquet · videos/observation.images.*/*.mp4 · meta/*   │
└────────────────────────────────────────────────────────────────────┘
```

</details>

- **Control / data plane separation**: commands, status, and the episode window (Shadow) flow through IoT Core; datasets go to S3; live and replay video go through KVS.
- **Two video paths**: dataset mp4 (exact training data) and the KVS stream (monitoring / replay) are separate pipelines.

## Layout

```
.
├── components/com.lerobot.data-collection.gpu/recipe.yaml  # NVENC encoding variant recipe
├── components/com.lerobot.data-collection.v21/recipe.yaml  # LeRobot dataset v2.1 (per-episode) variant recipe
├── components/com.lerobot.data-collection.v21/artifacts/collect.py  # v21 collect.py (Discard=re-record, reset countdown, recSeq)
├── components/com.lerobot.data-collection.v21.gpu/recipe.yaml  # v2.1 + real GPU(NVENC) encoding variant recipe
├── components/com.lerobot.data-collection.v21.gpu/artifacts/collect.py  # v21.gpu collect.py (identical controller to .v21)
├── components/com.lerobot.data-collection/recipe.yaml   # original/reference recipe (image self-build)
├── components/com.lerobot.data-collection/artifacts/collect.py      # controller (MQTT control · recording · S3 upload · episode shadow)
├── components/com.lerobot.data-collection.gpu/artifacts/collect.py  # copy for the NVENC variant (identical controller)
├── components/com.groot.kvs-webrtc-ingest/recipe.yaml   # (optional) color camera → KVS WebRTC live/episode playback source
├── components/com.groot.kvs-webrtc-p2p/recipe.yaml      # (optional) low-latency P2P live + kvssink recording (tee) — <1s alternative to kvs-webrtc-ingest
├── web-ui/multiviewer.html                              # multi-viewer console (KVS storage viewer; cloud fan-out up to 3 viewers, device-independent)
├── web-ui/live-p2p.html                                 # low-latency P2P live console (default landing; sub-second)
├── infra/cloudformation.yaml                            # IoT Custom Authorizer + CloudFront + S3
├── Dockerfile.data-collection-minimal(.md)              # minimal data-collection image reference (mirrored by the recipe inline build)
├── deploy.sh                                            # full deployment script
└── README.md / DEPLOYMENT_GUIDE.md / AGENTS.md / COMPONENT_ARCHITECTURE.md / design.md  # docs
```

---

## ⚠️ Placeholders You Must Replace Before Deploying

For public release, this repository has **sensitive / environment-specific values replaced with
placeholders**. Replace the values below to match your environment before deploying.

| Placeholder | Meaning | Files it appears in |
|---|---|---|
| `<AWS_ACCOUNT_ID>` | 12-digit AWS account ID | `components/.../recipe.yaml` (data collection + `kvs-webrtc-ingest`'s `viewerRoleArn`), `components/.../artifacts/collect.py`, `infra/cloudformation.yaml`*, `web-ui/*.html`, `COMPONENT_ARCHITECTURE.md` |
| `<IOT_ENDPOINT>` | AWS IoT Core ATS data endpoint (`xxxx-ats.iot.<region>.amazonaws.com`) | `web-ui/*.html` |
| `<WEB_USERNAME>` | Web console login username | `web-ui/*.html`, `infra/cloudformation.yaml`, docs |
| `<WEB_PASSWORD>` | Web console login password | `web-ui/*.html`, `infra/cloudformation.yaml`, docs |
| `<DATA_BUCKET>` | Dataset upload S3 bucket (recommended to be in the **same region** as the deployment — avoids presign mismatch) | `web-ui/*.html` (#bk default), deployment config `s3Bucket` |

\* `<AWS_ACCOUNT_ID>` is used in S3 bucket names (`greengrass-datasets-<AWS_ACCOUNT_ID>`) etc.
   Adjust it to match your own bucket naming convention.

### How to find your endpoint
```bash
aws iot describe-endpoint --endpoint-type iot:Data-ATS \
  --query endpointAddress --output text --region <REGION>
```

### Bulk replacement example (locally, before deploying)
```bash
# macOS (on Linux use sed -i)
grep -rl '<AWS_ACCOUNT_ID>' . | xargs sed -i '' 's/<AWS_ACCOUNT_ID>/123456789012/g'
sed -i '' 's/<IOT_ENDPOINT>/xxxxxxxxxxxxx-ats.iot.ap-northeast-2.amazonaws.com/g' web-ui/*.html
# Web credentials must be kept identical between web-ui/*.html and infra/cloudformation.yaml.
```

### 🔐 Security notes
- `<WEB_USERNAME>` / `<WEB_PASSWORD>` must be set **identically in `web-ui/*.html` and
  `infra/cloudformation.yaml`** (the Custom Authorizer Lambda) for login to work.
- The original demo used `admin`/`admin` as defaults. **For production, always change these to
  strong values** and, if possible, manage them via CloudFormation parameters / Secrets Manager.
- The IoT Custom Authorizer validates a `base64(username:password)` token.

---

## Known Limitations (Demo/POC)

> ⚠️ This is sample code for demonstration/education only and is **not
> production-ready**. The items below are accepted trade-offs for the demo and
> should be addressed before any production deployment.

| Item | Current State | Production Action Required |
|------|---------------|----------------------------|
| WAF / CSP / logging | CloudFront has no WAF, access logging, or security response headers (CSP/HSTS) | Attach AWS WAF (`AWSManagedRulesCommonRuleSet`, us-east-1 for CLOUDFRONT scope), enable access logging + CloudTrail, add a `ResponseHeadersPolicy` (CSP/HSTS) |
| Container privileges | Components run with `RequiresPrivilege: true`; the recorder runs `docker run --network=host --runtime=nvidia` with device pass-through | Required for robot/camera hardware access. Expose only the ports/devices needed and review non-root execution |
| CDN scripts (SRI) | The web UI loads mqtt.js / hls.js / aws-sdk / kvs-webrtc from public CDNs without Subresource Integrity | Add `integrity` (SHA-384) + `crossorigin` to each `<script>`, or self-host the vetted assets |
| Dependency pinning | `uv` is installed via a piped install script (no checksum); the KVS WebRTC SDK is cloned unpinned; `collect.py` pip-installs at runtime unpinned (torch/lerobot **are** pinned) | Pin versions + verify checksums, pin the SDK to a tag/commit, and move the runtime `pip install` into the image build |
| Presigned / HLS URLs | Delivered over MQTT; expiry reduced to 1h (was 12h) | Reduce further to the minimum needed (e.g. ≤15 min) and restrict who can subscribe |
| Viewer STS credentials | Short-lived VIEWER STS credentials are published to a **retained** MQTT topic | Narrow the subscriber policy (see required hardening) and minimize the session duration |
| Camera footage | Color camera video is streamed to KVS and stored in S3 | If people appear in frame, obtain consent and comply with your privacy/retention obligations |

> ⚠️ **Required hardening before production (NOT waivable by a disclaimer):**
> - Restrict the IoT Custom Authorizer policy from `Resource: "*"` to specific client/topic ARNs
> - Remove the demo `admin/admin` default; manage credentials via CloudFormation `NoEcho` parameters / Secrets Manager (ideally Amazon Cognito with Authorization Code + PKCE)
> - Enable S3 Block Public Access, default encryption, a TLS-enforcing bucket policy, and versioning on the data bucket
> - Scope the device/viewer IAM roles from `kinesisvideo:*` to specific channel/stream ARNs and viewer-only actions

---

## Deployment Overview

The default region is `ap-northeast-2`. Change the region in the scripts/docs if needed.

1. **Replace placeholders** (see table above)
2. **Deploy CloudFormation** — `infra/cloudformation.yaml` (IoT Authorizer, S3, CloudFront)
3. **Upload collect.py** — `s3://<data-bucket>/collect/com.lerobot.data-collection/<version>/collect.py`
   (upload with the **same version** as the recipe's fetch path)
4. **Register the component** — `create-component-version` with `recipe.yaml`
5. **Deploy to Greengrass** — add `com.lerobot.data-collection` to the target thing / thing group
   (config: `thingName`, `s3Bucket`, `episodeLength`, `numEpisodes`, etc.)

**For detailed deployment commands and settings, see `DEPLOYMENT_GUIDE.md`.** For background and
structure, see `COMPONENT_ARCHITECTURE.md`, `design.md`, `AGENTS.md`.

> The component's install step builds the data-collection Docker image **from scratch** on the
> device (aarch64 torch + torchcodec (source build) + cuDSS + lerobot, etc.). The base image is the
> public `nvidia/cuda:13.0.x-cudnn-devel-ubuntu24.04` (validated on Jetson Thor).

## Usage Scenarios

### Scenario A — Multi-episode collection session (default flow)
1. **Log in** — Open the web console on CloudFront and connect with `<WEB_USERNAME>`/`<WEB_PASSWORD>`. Two live screens are available: **`live-p2p.html`** (default landing, sub-second P2P) and **`multiviewer.html`** (KVS storage viewer; cloud fan-out up to 3 viewers, device-independent). See "Which live screen?" below.
2. **Check the live view** — In the *Monitor* tab, `WebRTC Live` shows the workspace color video (device clock at bottom-right).
3. **Start recording** — In *Control*, enter an instruction (e.g. `pick orange`) + number of episodes → **⏺ Start**.
   The status badge turns `recording`, and the screen/log shows `start episode 1`.
4. **Progress through episodes** — When one demonstration ends, press **⏭ Save & Next** (saves the current episode, then advances).
   Or leave it and it **auto-advances** after `episodeLength` seconds (log `[TIMEOUT]`). Each transition shows `start episode N`.
5. **End the session** — Reach the target count, or press **🟥 End Session** (endSession) → status `saving`→`uploading`→`done`.
   The dataset lands under `s3://<DATA_BUCKET>/datasets/{date}/{instruction}/{session}/`.
6. **Replay** — *Monitor* → `Episode Playback` shows Episode 1·2·3… of the session you just recorded (Device Shadow).
   Clicking one replays that segment from `thor-001-webrtc` over HLS (color + timestamp).

### Scenario B — Watching live behind a firewall (corporate network)
- If WebRTC (UDP/STUN·TURN) is blocked and `WebRTC Live` won't appear, choose **`HLS Live`** from the toggle at the top of *Monitor*.
  It replays the same `thor-001-webrtc` stream over HLS (TCP/443) → visible behind a firewall (a few seconds of latency).

### Scenario C — Reviewing & downloading past data
1. In the *Files* tab, enter a **date + instruction** → **🔄 List** to browse sessions/files (upload badges shown).
2. Next to a session folder (📁), press **▶** → *Monitor* switches to `Episode Playback`, sets the full session range, and lists episodes.
3. Click **Episode N ▶** on the left → replays only that episode's time range.
4. Individual mp4s can be downloaded with **⬇️** (presigned URL; valid when the deployment region matches).

### Scenario D — Partial upload recovery / re-upload
- If some files did not upload (network issues, etc.), in *Files* **select the un-uploaded files → re-upload**.
- Even if the last episode fails with 0 frames (exit 133), already-saved episodes are **automatically finalized and uploaded**
  and reflected in the Shadow (the whole session is not discarded).

> All commands are published by `web-ui` over MQTT to `lerobot/<thing>/collect/command`. They can also be triggered via CLI (→ `DEPLOYMENT_GUIDE.md` §11).

## Data Format (LeRobot)
```
{repo_id}/
├── data/chunk-000/*.parquet      # observation.state, action, timestamp, index
├── videos/observation.images.{front,wrist}/chunk-000/*.mp4
└── meta/{info.json, stats.json, tasks.parquet, episodes/*}
```

---

## (Optional) Live Video Monitoring — `com.groot.kvs-webrtc-p2p` or `com.groot.kvs-webrtc-ingest`

Completed recordings are played back via S3 presigned URLs, but watching **video while recording**
that way has high latency. Two optional components stream the **color camera → Amazon Kinesis Video
Streams (KVS) WebRTC** for live monitoring (both keep per-episode HLS replay). Pick **one** (both bind
the same color camera):

- **`com.groot.kvs-webrtc-p2p`** — **peer-to-peer, sub-second** live (device → browser directly);
  recording preserved via a `kvssink` tee. Up to **10 viewers per channel**, but **bounded by device
  CPU/uplink**. Best for a **low-latency operator monitor**. Web page: **`live-p2p.html`** (the default
  deploy set uses this).
- **`com.groot.kvs-webrtc-ingest`** — **storage-session** live (device → KVS cloud → browser, ~2–5 s+);
  the KVS cloud fans out up to the **multiviewer quota (3 viewers)**, **independent of device
  resources**. Web page: **`multiviewer.html`**.

> **Scenario:** lowest-latency monitor for a robot up close → **`kvs-webrtc-p2p` (`live-p2p.html`)**;
> cloud fan-out independent of device load (up to 3 viewers) → **`kvs-webrtc-ingest`
> (`multiviewer.html`)**. Full trade-off tables are in each component's README.

The rest of this section covers the storage variant (`kvs-webrtc-ingest`); see
[`components/com.groot.kvs-webrtc-p2p/README.md`](components/com.groot.kvs-webrtc-p2p/README.md) for
the P2P variant.

- **How it works**: GStreamer H.264-encodes the color camera → a KVS WebRTC **STORAGE** master sample
  ingests media into the signaling channel via `JoinStorageSession` → the browser watches as a WebRTC **viewer**.
- **Camera auto-detection**: if `videoDevice` is empty, it auto-selects the v4l2 node that outputs
  **YUYV (color)** (excluding IR/GREY nodes). RealSense color nodes can be unstable for raw v4l2 capture,
  so depending on your environment you may need to set `videoDevice` explicitly or adjust the source node in the install script.
- **Web viewer credential delivery**: the browser has no AWS credentials, so the device issues
  **short-lived (1 hour), viewer-only STS credentials** (`AssumeRole KvsViewerRole`) and delivers them over MQTT
  (`lerobot/{thing}/webrtc/viewer`, retained). The browser uses these to call `joinStorageSessionAsViewer`.

### Prerequisites (KVS)
1. Create a **signaling channel + MediaStorageConfiguration** (channel → same-named stream, storage ENABLED).
2. Create a **viewer IAM role** `KvsViewerRole` — set it as `viewerRoleArn` in `recipe.yaml`
   (`arn:aws:iam::<AWS_ACCOUNT_ID>:role/KvsViewerRole`). Restrict it to **least privilege**
   (viewer scope only for that channel: `kinesisvideo:GetSignalingChannelEndpoint`, `ConnectAsViewer`,
   `DescribeSignalingChannel`, `GetIceServerConfig`, etc.).
3. Grant the device TES Role `kinesisvideo:*` (or the minimal actions needed for ingest) + `sts:AssumeRole` (KvsViewerRole) for that channel.

### 🔐 Security notes (for public release)
- The account ID in `viewerRoleArn` is replaced with the **`<AWS_ACCOUNT_ID>` placeholder** — replace it with your own account.
- Viewer credentials are published as **MQTT retained**. Narrow the IoT policy so that only clients
  **authorized via the Custom Authorizer** can subscribe to that topic (the last message stays on the topic until expiry).
- Keep `KvsViewerRole` limited to **read (viewer) permissions only**, and keep the session duration (default 3600s) to the necessary minimum.
- This recipe contains **no hardcoded AWS keys** (runtime issuance via TES + STS). Do not add keys even before committing.

### Register / deploy (summary)
```bash
# After preparing the channel/role
aws greengrassv2 create-component-version \
  --inline-recipe fileb://components/com.groot.kvs-webrtc-ingest/recipe.yaml --region <REGION>
# Example deploy config: {"channelName":"<channel>","thingName":"<thing>","videoDevice":"","viewerRoleArn":"arn:aws:iam::<AWS_ACCOUNT_ID>:role/KvsViewerRole"}
```

---

## What Was Excluded From This Folder (not uploaded)
For public release, the following are **not included** in this folder (they exist in the original `collection-web`):
- `test/` — downloaded dataset samples (large mp4/parquet)
- `artifacts/__pycache__/` — Python cache
- `deploy-history.md`, `progress.md` — internal operations logs containing account IDs / profiles / Job IDs

`.gitignore` prevents these categories (large data · caches · OS files) from recurring.

---

## Components & Features

The components and features included in this sample (sensitive values are placeholders):

### Components (recording + live monitoring)
| Component | Version | Role |
|---|---|---|
| `com.lerobot.data-collection` | 1.0.0 | Data-collection recipe with CPU (SVT-AV1) video encoding — reference variant |
| `com.lerobot.data-collection.gpu` | 1.0.0 | Data-collection recipe with an NVENC ffmpeg shim (attempts GPU encoding, falls back to CPU on failure); reuses the same `collect.py` from the version folder |
| `com.lerobot.data-collection.v21` | 1.0.0 | Data-collection recipe pinned to **lerobot v0.3.3** → produces **LeRobot dataset v2.1 (per-episode files:** `data/chunk-000/episode_000000.parquet`, `videos/chunk-000/<key>/episode_000000.mp4`**)**. Ships its own `collect.py` where **Discard re-records the current episode** (not a full-session stop), adds a reset-window countdown, and a `recSeq` real-time recording-start signal |
| `com.lerobot.data-collection.v21.gpu` | 1.0.0 | v2.1 (per-episode) **with real GPU (NVENC) video encoding**. The image patches lerobot's `encode_video_frames` to encode the PNG frames via the **system `ffmpeg` CLI with `h264_nvenc`** (falls back to the original PyAV/CPU SVT-AV1 on any failure). Measured **~2.7x faster** encoding than CPU on Jetson Thor (see [GPU_ENCODING.md](GPU_ENCODING.md)). Ships its own `collect.py` (identical to `.v21`'s controller) |
| `com.groot.kvs-webrtc-ingest` | 1.0.0 | Color camera → KVS WebRTC ingestion (live/episode-playback source `thor-001-webrtc`). Burns the device clock (HH:MM:SS) via clockoverlay + issues viewer STS credentials |
| `com.groot.kvs-webrtc-p2p` | 1.0.0 | Color camera → single capture tee'd to KVS WebRTC **P2P** master (`thor-001-p2p`, sub-second live) + `kvssink` recording (`thor-001-webrtc`, HLS replay). Low-latency alternative to `kvs-webrtc-ingest` |

> `.gpu` does not package `collect.py` separately; it fetches the original path
> `s3://greengrass-datasets-<AWS_ACCOUNT_ID>/collect/com.lerobot.data-collection/<ver>/collect.py`
> at runtime. `.v21` and `.v21.gpu` each fetch their own component-namespaced path
> (`s3://greengrass-datasets-<AWS_ACCOUNT_ID>/collect/com.lerobot.data-collection.v21/<ver>/collect.py`
> and `.../collect/com.lerobot.data-collection.v21.gpu/<ver>/collect.py`; the two scripts are identical).
> **Always keep the recipe's fetch path version and the uploaded collect.py version identical.**
> The four data-collection components use the same MQTT topics, so **do not run more than one at the same time** (pick one). `com.lerobot.data-collection` / `.gpu` produce the v3.0 (packed) format; `.v21` / `.v21.gpu` produce the v2.1 (per-episode) format.
>
> **On GPU encoding:** the `.gpu` variant's NVENC *ffmpeg shim* does **not** actually engage, because this lerobot version encodes in-process via **PyAV** (not the `ffmpeg` CLI), so `.gpu` still outputs CPU AV1. `.v21.gpu` fixes this by patching lerobot's encoder to call the system `ffmpeg` CLI (`h264_nvenc`) directly — that is the variant that produces real GPU-encoded (H.264) video. See [GPU_ENCODING.md](GPU_ENCODING.md).

### collect.py main features
- MQTT control: `start` / `stop` (save current episode then next) / `endSession` (end session) / `discard` / `upload` / `list` / `uploadFiles` / `kvsLive` / `kvsEpisodes`.
- **FIFO external control**: injects a FIFO watcher into lerobot's in-container `control_utils.py` to deliver `next`/`stop`/`rerecord` events.
- **Empty-episode (exit 133) guard**: `next`/`stop` are only delivered after the current episode has recorded at least ~1.5s (re-checked right before each write inside the FIFO retry loop → blocks 0-frame crashes caused by re-delivery at an episode boundary).
- **Empty last-episode recovery**: even if the last episode crashes with 0 frames (rc=133), if there are already saved episodes the session is finalized to **upload to S3 + update the shadow** (not discarded wholesale).
- **Episode transition logs**: `[START]` at each episode start, `[TIMEOUT]` for auto-timeout, `[NEXT]` for manual Stop&Save.
- **Episode window Device Shadow**: on session end, each episode's `[start,end]` (wall-clock) is published to the named shadow `episodes` (reported) via boto3 `iot-data.update_thing_shadow`. Only the latest session is kept. The web UI reads this to replay the corresponding segment of `thor-001-webrtc` over HLS.
- **Robust presigning**: presigned URLs are signed with **frozen credentials + SigV4** (avoids SignatureDoesNotMatch during TES credential rotation). The data bucket must be **in the same region as the deployment** for presigning to be valid.

### MQTT topics
- Command/status/video/files: `lerobot/{thing}/collect/{command,status,video,files,kvs}`
- Viewer STS credentials (retained): `lerobot/{thing}/webrtc/viewer`
- Episode window shadow: `$aws/things/{thing}/shadow/name/episodes/{update,get,...}`
  (device TES role needs `iot:UpdateThingShadow`; the web gets/subscribes to the shadow via the Custom Authorizer)

### Web UI (admin console)
- Login (Custom Authorizer token) → MQTT over WSS.
- Left: Login/Control/Status/**Logs**; Right: **Files** / **Monitor**.
- **Monitor** top toggle (one line): `WebRTC Live` / `HLS Live` / `Episode Playback`.
  - WebRTC Live: `thor-001-webrtc` color realtime (UDP) via `joinStorageSessionAsViewer`.
  - HLS Live: the same stream via `GetHLSStreamingSessionURL(LIVE)` (TCP/443, firewall-safe fallback).
  - Episode Playback: episode list (shadow) on the left + segment playback on the right (ON_DEMAND / LIVE_REPLAY for future ranges).
- Files: next to a session folder (📁), ▶ → sets the full session range + selectable playback from the episode list. Next to an mp4 is download (⬇️) only.
- Status badge idle/recording/saving/uploading/done/error + error display.

### Region / bucket note
- Keep the data upload bucket **in the same region as the deployment**. A bucket in another region (e.g. us-east-1)
  causes downloads/playback to fail with `AuthorizationQueryParametersError` due to presigned-URL signing-region mismatch.
