# Data Collection System — Design Document

## Overview

A system that collects teleoperation data from the SO-ARM101 robot and lets you control/monitor it remotely from a web UI.

> **For the current component set and features (multi-component · GPU variant · WebRTC/HLS live ·
> episode window shadow · endSession · empty-episode recovery, etc.), see the "Components & Features"
> section in `README.md`.** This document describes the system's design background.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Web UI (CloudFront)                                            │
│  - IoT Core WebSocket (Custom Authorizer auth)                  │
│  - S3 video playback (Credential Provider)                      │
│  - Instruction input / start·stop recording / S3 upload         │
└──────────────────────────┬──────────────────────────────────────┘
                           │ MQTT (WSS)
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  AWS IoT Core                                                   │
│  - Custom Authorizer (token-based WebSocket auth)               │
│  - Topic: lerobot/{thingName}/collect/command                    │
│  - Topic: lerobot/{thingName}/collect/status                     │
└──────────────────────────┬──────────────────────────────────────┘
                           │ Greengrass IPC
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  Jetson Thor (Greengrass Core)                                  │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │ com.lerobot.data-collection                               │    │
│  │ - Receive MQTT commands (start/stop/discard/upload)       │    │
│  │ - Camera capture (front + wrist)                          │    │
│  │ - Record robot state (leader → follower)                  │    │
│  │ - Save episodes (LeRobot v3.0 format)                     │    │
│  │ - S3 upload (mp4 + metadata)                              │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  S3 (dataset storage)                                            │
│  - s3://{bucket}/datasets/{date}/{slug}/{session}/              │
│  - LeRobot v3.0: data/*.parquet + videos/*/*.mp4 + meta/*        │
└─────────────────────────────────────────────────────────────────┘
```

## Security design

### 1. Web UI → IoT Core (WebSocket)

**Custom Authorizer** approach:
- Enter ID/Password on the web UI login screen (set to `<WEB_USERNAME>`/`<WEB_PASSWORD>` at deploy time — do not use demo defaults)
- Encode `username:password` in base64 and pass it as the Custom Authorizer token
- The Lambda decodes the token → validates ID/Password → returns an IoT Policy
- Connection is refused on validation failure

> ⚠️ This authentication (a shared account + base64 token) is for the demo. In production, replace it
> with Amazon Cognito (Authorization Code + PKCE), and manage credentials via Secrets Manager /
> a CloudFormation `NoEcho` parameter instead of code constants. See Known Limitations in the README for details.

### 2. Web UI → S3 (video playback)

**IoT Credential Provider** approach:
- Obtain temporary AWS credentials based on an IoT Core certificate
- Or access S3 with the policy returned by the Custom Authorizer
- Actual implementation: the device generates a pre-signed URL and delivers it over MQTT

### 3. Device → S3 (upload)

**Greengrass TES (Token Exchange Service)**:
- Upload to S3 using the Greengrass Core IAM Role
- No separate credentials needed

## Device execution model (self-built container)

Data collection runs `lerobot-record` inside a **Docker image the component builds on the device
itself**. It does not depend on a separate Python venv/script on the host (the old manual host workflow is retired).

- Local output (container volume mount): `/home/arobot/Desktop/physical-ai/so-101/outputs`
- Calibration: mount the device's own `~/.cache/huggingface/lerobot/calibration` read-only
  (not included in the deployment bundle — device-specific value).
- Image build/stack details: see `Dockerfile.data-collection-minimal(.md)` and the install step of each `components/*/recipe.yaml`.

## Bundle layout

```
lerobot-data-collection/
├── README.md / AGENTS.md / COMPONENT_ARCHITECTURE.md / design.md   # docs
├── components/
│   ├── com.lerobot.data-collection.gpu/recipe.yaml   # NVENC variant
│   ├── com.lerobot.data-collection/recipe.yaml       # original/reference
│   └── com.groot.kvs-webrtc-ingest/recipe.yaml       # color WebRTC ingestion
├── components/com.lerobot.data-collection/artifacts/collect.py  # controller (MQTT·recording·S3·shadow)
├── web-ui/multiviewer.html                           # multi-viewer console (storage)
├── web-ui/live-p2p.html                              # low-latency P2P console
├── infra/cloudformation.yaml                         # Custom Authorizer + S3 + CloudFront
├── deploy.sh                                         # deployment script
└── Dockerfile.data-collection-minimal(.md)           # image reference
```

## MQTT topic design

| Topic | Direction | Payload |
|------|------|----------|
| `lerobot/{thing}/collect/command` | Web → Device | `{"action":"start","lang":"...","numEpisodes":50}` / `{"action":"list",...}` / `{"action":"uploadFiles","files":[...]}` |
| `lerobot/{thing}/collect/status` | Device → Web | `{"state":"recording","episode":3,"totalEpisodes":50,"step":150}` |
| `lerobot/{thing}/collect/video` | Device → Web | `{"episode":3,"urls":{"front":"...","wrist":"..."}}` |
| `lerobot/{thing}/collect/files` | Device → Web | `{"date":"...","slug":"...","files":[{"rel","key","size","uploaded","url"}]}` |

## Single-container multi-episode model

- Enter the instruction + **episode count** once → record **N episodes consecutively in a single
  Docker container** via `lerobot-record --dataset.num_episodes=N`. Because a new container is not
  spun up per episode, the heavy CUDA/LeRobot loading cost is paid only once.
- Parse container stdout to reflect episode/step progress on the status topic in real time.
- On full completion (container exits naturally) or user stop, the whole dataset is uploaded automatically.

## Docker image: standalone build in the install step

- The component **install step** builds a **minimal image** (`dataImage`) for data collection
  **from scratch, without depending on the inference base (groot-n16-inference)**
  (idempotent — skipped if it already exists).
- It installs none of the inference stack (GR00T model · ONNX · gr00t server), only what `lerobot-record` needs.
- **aarch64 torch source (verified)**: confirmed from Isaac-GR00T `scripts/deployment/thor`
  (commit 5dc80c4) — install `torch==2.10.0 / torchvision==0.25.0 / triton==3.5.0` from the
  Jetson AI Lab index `https://pypi.jetson-ai-lab.io/sbsa/cu130/+simple`,
  install the **NVPL LAPACK/BLAS** (libnvpl-*) system libraries the torch wheel requires, and
  build **torchcodec==0.10.0** from source against the system FFmpeg.
- lerobot is installed from a commit that includes so101_follower, with `--no-deps` to prevent overwriting torch.
- For the full Dockerfile, see `Dockerfile.data-collection-minimal` (identical to the install script).
- `collect.py` receives the image name via the `DATA_IMAGE` environment variable and uses it in `docker run`.

> ⚠️ The build must run **on aarch64 (Jetson Thor)** (the torch wheel is sbsa/aarch64 only).
> The build takes a while, so the install Timeout is 7200 seconds.

## Data folder structure (by date · instruction)

```
{prefix}{YYYY-MM-DD}/{instruction_slug}/{session_id}/episode_*/...
  e.g.: datasets/2026-06-22/pick_orange/2026-06-22_pick_orange_1718000000/...
```
- Local: `{DATASET_DIR}/arobot/{date}_{slug}_{ts}/` ↔ S3: the path above (structure preserved)

## Web UI features

1. **Connect**: enter IoT Endpoint + token → Custom Authorizer WebSocket connection
2. **Control**: enter instruction + **episode count**, start/stop/discard recording, S3 upload
3. **Monitoring**: status display (episode/step), logs
4. **Video playback**: play the front/wrist camera video of an uploaded episode (S3 pre-signed URL)
5. **S3 settings**: specify bucket/prefix
6. **File browser** (new): list files in a date/instruction folder, **upload-complete badge**,
   select unuploaded files and **re-upload**, **download (⬇️) / play video (▶️)** for uploaded files
