# com.lerobot.data-collection — Component Architecture

## Overview

This is the **data collection Greengrass component** for the SO-ARM101 teleoperation robot on Jetson AGX Thor.
It receives remote commands via AWS IoT Core (MQTT), launches lerobot recording inside a Docker container, and uploads episode data to S3 via boto3.

> **For an overview of features such as the multi-component setup, GPU variant, WebRTC/HLS live, and
> the episode window shadow, see the "Components & Features" section in `README.md`.** This document is a structural reference for the data-collection component.

- **Component name**: `com.lerobot.data-collection` (original) / `com.lerobot.data-collection.gpu` (NVENC variant)
- **Platform**: Linux aarch64 (Jetson AGX Thor / thor-001, JetPack 7 / CUDA 13)
- **Core script**: `collect.py` — downloaded from S3 **at runtime** (not packaged; fetched from the **version-matched** folder `collect/com.lerobot.data-collection/<version>/collect.py` — the recipe fetch version and the uploaded version must match)
- **Docker image**: `lerobot-data-collection[-gpu]:<tag>` — built **standalone on device by this component's own `recipe.yaml` install step** (public `nvidia/cuda:13.0.x-cudnn-devel` base; does not depend on the inference base / `com.groot.n16.docker-build`)

---

## 1. Overall Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                            AWS Cloud                                 │
│                                                                      │
│  ┌──────────────────┐     ┌────────────────────────────────────────┐ │
│  │  AWS IoT Core    │     │              Amazon S3                 │ │
│  │  (MQTT Broker)   │     │                                        │ │
│  │                  │     │  greengrass-datasets-<AWS_ACCOUNT_ID>   │ │
│  │  lerobot/+/       │     │  ├── collect/com.lerobot.data-collection│ │
│  │  collect/command │     │  │   └── 1.0.0/collect.py  ← source   │ │
│  │  lerobot/+/       │     │  └── datasets/                         │ │
│  │  collect/status  │     │      └── {date}/{session}/episode_N/   │ │
│  │  lerobot/+/       │     │                                        │ │
│  │  collect/video   │     └──────────────┬─────────────────────────┘ │
│  └────────┬─────────┘                    │ boto3 upload              │
└───────────┼──────────────────────────────┼───────────────────────────┘
            │ MQTT over TLS                │
┌───────────┼──────────────────────────────┼───────────────────────────┐
│           │     Greengrass Edge Device (thor-001, aarch64)           │
│           │                              │                           │
│  ┌────────▼──────────────────────────────▼─────────────────────────┐ │
│  │                    Greengrass Nucleus                           │ │
│  │  ┌──────────────────────────────────────────────────────────┐  │ │
│  │  │              com.lerobot.data-collection                  │  │ │
│  │  │                                                          │  │ │
│  │  │  recipe.yaml (run script)                                │  │ │
│  │  │  ┌───────────────────────────────────┐                  │  │ │
│  │  │  │ aws s3 cp .../collect.py → run    │                  │  │ │
│  │  │  │ python3 -u /opt/groot/collect.py  │                  │  │ │
│  │  │  └──────────────┬────────────────────┘                  │  │ │
│  │  │                 │                                        │  │ │
│  │  │  ┌──────────────▼─────────────────────────────────────┐ │  │ │
│  │  │  │  Controller (collect.py)                           │ │  │ │
│  │  │  │  - IPC subscribe  (MQTT command)                   │ │  │ │
│  │  │  │  - IPC publish    (status + video presigned URL)   │ │  │ │
│  │  │  │  - docker run/stop  (lerobot recording)            │ │  │ │
│  │  │  │  - boto3 upload   (S3)                             │ │  │ │
│  │  │  └────────────────────────────────────────────────────┘ │  │ │
│  │  └──────────────────────────────────────────────────────────┘  │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  Docker                                                              │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │  lerobot-data-collection:latest  (local image)            │    │
│  │  /opt/gr00t-venv/bin/lerobot-record                         │    │
│  │  -v /home/arobot/.../outputs → /root/.cache/huggingface/    │    │
│  │  -v calibration (read-only)                                 │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  Hardware                                                            │
│  ┌──────────────┐ ┌──────────────┐ ┌────────────────┐ ┌──────────┐  │
│  │ /dev/ttyACM0 │ │ /dev/ttyACM1 │ │ /dev/cam_front │ │/dev/cam_ │  │
│  │  (Leader)    │ │  (Follower)  │ │  (symlink)     │ │wrist     │  │
│  └──────────────┘ └──────────────┘ └────────────────┘ └──────────┘  │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. Component Lifecycle (recipe.yaml)

```
Greengrass deployment trigger
        │
        ▼
┌───────────────────────────────────────────────────────┐
│  INSTALL (RequiresPrivilege: true, Timeout: 120s)     │
│                                                       │
│  1. mkdir -p /home/arobot/.../outputs                 │
│                                                       │
│  2. pip3 install awsiotsdk boto3                      │
│     (IPC SDK + S3 upload library)                     │
└───────────────┬───────────────────────────────────────┘
                │ Installation complete
                ▼
┌───────────────────────────────────────────────────────┐
│  RUN (RequiresPrivilege: true)                        │
│                                                       │
│  1. Export DefaultConfiguration as env vars:          │
│     THING_NAME, LEADER_PORT, FOLLOWER_PORT,           │
│     FRONT_CAMERA, WRIST_CAMERA, DATASET_NAME,         │
│     DATASET_DIR, S3_BUCKET, S3_PREFIX, ...            │
│                                                       │
│  2. aws s3 cp s3://.../collect.py /opt/groot/         │
│     ← Always fetches latest collect.py at startup     │
│       (no redeployment needed for script changes)     │
│                                                       │
│  3. python3 -u /opt/groot/collect.py                  │
│     └─ Controller.run() entry point                   │
│        ├─ Connect to Greengrass IPC                   │
│        ├─ Subscribe: lerobot/{thing}/collect/command   │
│        ├─ Publish initial status                      │
│        └─ while True: watch proc exit, sleep(1)       │
└───────────────┬───────────────────────────────────────┘
                │ Component stop command
                ▼
┌───────────────────────────────────────────────────────┐
│  SHUTDOWN (RequiresPrivilege: true, Timeout: 10s)     │
│                                                       │
│  docker stop lerobot-record                    │
│  docker rm   lerobot-record                    │
│  pkill -f "collect.py"                                │
└───────────────────────────────────────────────────────┘
```

---

## 3. collect.py Internal Structure

```
collect.py
│
├── Module-level constants (read from environment variables)
│   ├── THING_NAME   = os.environ["THING_NAME"]
│   ├── DATASET_DIR  = "/home/arobot/Desktop/physical-ai/so-101/outputs"
│   ├── S3_BUCKET    = os.environ["S3_BUCKET"]
│   ├── TOPIC_CMD    = "lerobot/{THING_NAME}/collect/command"
│   ├── TOPIC_STATUS = "lerobot/{THING_NAME}/collect/status"
│   └── TOPIC_VIDEO  = "lerobot/{THING_NAME}/collect/video"
│
└── class Controller
    ├── __init__()
    │   ├── self.state      = "idle"
    │   ├── self.episode    = 0
    │   ├── self._proc      = None   ← docker subprocess handle
    │   ├── self._repo_id   = None   ← lerobot dataset repo id
    │   └── self._session_id = f"{DATASET_NAME}_{timestamp}"
    │
    ├── run()                    ← entry point
    │   ├── _connect_ipc()
    │   ├── _publish_status()
    │   └── while True:
    │       ├── if _proc.poll() is not None:   ← container exited
    │       │       → auto-upload if S3_BUCKET set
    │       └── sleep(1)
    │
    ├── _connect_ipc()           ← Greengrass IPC + MQTT subscribe
    │   ├── awsiot.greengrasscoreipc.connect()
    │   ├── Handler.on_stream_event() → _handle(payload)
    │   └── Falls back to standalone mode on failure
    │
    ├── _handle(payload)         ← MQTT command dispatch
    │   ├── "start"   → _start_recording(payload)
    │   ├── "stop"    → _stop_recording()
    │   ├── "discard" → _stop_recording(discard=True)
    │   └── "upload"  → Thread(_upload) async
    │
    ├── _start_recording(payload)   ← launch Docker container
    │   ├── docker run --rm --runtime=nvidia
    │   │       --name lerobot-record
    │   │       --device /dev/cam_front
    │   │       --device /dev/cam_wrist
    │   │       --device /dev/ttyACM0  /dev/ttyACM1
    │   │       -v {DATASET_DIR}:/root/.cache/huggingface/lerobot
    │   │       -v calibration:/root/.cache/.../calibration:ro
    │   │       lerobot-data-collection:latest
    │   │       -c "/opt/gr00t-venv/bin/lerobot-record ..."
    │   └── self._proc = subprocess.Popen(cmd)
    │
    ├── _stop_recording(discard)    ← stop Docker container
    │   ├── docker stop -t 120 lerobot-record
    │   ├── _proc.wait()
    │   └── if not discard → Thread(_upload)
    │
    ├── _upload(bucket, prefix)     ← S3 upload via boto3
    │   ├── state = "uploading"
    │   ├── s3.upload_file() for each file under ep_dir
    │   │   dst: {prefix}{date}/{session_id}/episode_{N}/{rel_path}
    │   ├── generate presigned URLs for front/wrist .mp4
    │   ├── publish video URLs → TOPIC_VIDEO
    │   └── state = "done" → sleep(3) → "idle"
    │
    └── _publish_status()           ← publish to IoT Core
        └── JSON: { state, episode, totalEpisodes,
                    step, maxSteps, langInstruction, datasetName }
```

---

## 4. MQTT Message Flow

### Command Topic (`lerobot/{thingName}/collect/command`)

Commands **received** from Web UI:

| action    | Description                     | State Transition   |
|-----------|---------------------------------|--------------------|
| `start`   | Start episode recording         | idle → recording   |
| `stop`    | Stop and save episode           | recording → idle   |
| `discard` | Discard current episode         | recording → idle   |
| `upload`  | Manual S3 upload trigger        | idle → uploading   |

```json
// start example
{ "action": "start", "lang": "pick orange", "numEpisodes": "1" }

// stop example
{ "action": "stop" }

// upload example
{ "action": "upload", "s3Bucket": "greengrass-datasets-<AWS_ACCOUNT_ID>" }
```

### Status Topic (`lerobot/{thingName}/collect/status`)

State **published** by the component:

```json
{
  "state": "recording",
  "episode": 1,
  "totalEpisodes": 50,
  "step": 0,
  "maxSteps": 300,
  "langInstruction": "pick orange",
  "datasetName": "pick_orange_demo"
}
```

### Video Topic (`lerobot/{thingName}/collect/video`)

**Published after upload** — presigned S3 URLs for recorded videos:

```json
{
  "episode": 1,
  "urls": {
    "front": "https://s3.amazonaws.com/...presigned...episode_1/front.mp4",
    "wrist": "https://s3.amazonaws.com/...presigned...episode_1/wrist.mp4"
  }
}
```

---

## 5. State Machine

```
                    ┌─────────┐
          ┌─────────│  idle   │◄───────────────────────────┐
          │         └────┬────┘                             │
          │              │ action="start"                    │
          │              ▼                                   │
          │        ┌──────────────┐  action="discard"       │
          │        │  recording   │────────────────────────►│
          │        └──────┬───────┘                         │
          │               │ action="stop"                    │
          │               │ OR container exits normally      │
          │               ▼                                  │
          │         ┌──────────┐                             │
          │         │  saving  │  (docker stop → wait)       │
          │         └────┬─────┘                             │
          │              │ save complete → auto upload        │
          │              ▼                                    │
          │       ┌───────────┐    success    ┌──────────┐   │
          │       │ uploading │──────────────►│   done   │───┘
          │       └───────────┘               └──────────┘
          │              │ failure                           │
          │              ▼                                   │
          │         ┌──────────┐                             │
          └────────►│  error   │────────────────────────────►┘
                    └──────────┘   sleep(3) → idle
```

---

## 6. Key Difference: collect.py Deployment Method

Unlike v1.7 (which packages collect.py in a zip artifact), **v1.6 downloads collect.py directly from S3 at every startup**:

```
recipe.yaml run script
    │
    ├── aws s3 cp s3://greengrass-datasets-<AWS_ACCOUNT_ID>/
    │             collect/com.lerobot.data-collection/<version>/collect.py
    │             /opt/groot/collect.py
    │
    └── python3 -u /opt/groot/collect.py
```

**Advantage**: Update collect.py logic by uploading a new file to S3 — no Greengrass redeployment required.

---

## 7. Data Storage Structure

```
/home/arobot/Desktop/physical-ai/so-101/outputs/   (local, lerobot output)
└── {repo_id}/                    ← arobot/{DATASET_NAME}_{timestamp}
    ├── data/chunk-000/
    │   └── episode_000000.parquet
    └── videos/
        ├── observation.images.front/chunk-000/file-000.mp4
        └── observation.images.wrist/chunk-000/file-000.mp4

              ↓ boto3 upload

s3://greengrass-datasets-<AWS_ACCOUNT_ID>/
└── datasets/
    └── {YYYY-MM-DD}/
        └── {DATASET_NAME}_{session_timestamp}/
            └── episode_{N}/
                ├── data/chunk-000/episode_000000.parquet
                └── videos/observation.images.{front,wrist}/chunk-000/file-000.mp4
```

---

## 8. Configuration Parameters

| Parameter          | Default Value                                   | Description                          |
|--------------------|-------------------------------------------------|--------------------------------------|
| `thingName`        | lerobot-device                                   | IoT Thing name (MQTT topic prefix)   |
| `leaderPort`       | /dev/ttyACM0                                    | Leader robot serial port             |
| `followerPort`     | /dev/ttyACM1                                    | Follower robot serial port           |
| `frontCameraIndex` | /dev/cam_front                                  | Front camera (symlink)               |
| `wristCameraIndex` | /dev/cam_wrist                                  | Wrist camera (symlink)               |
| `cameraWidth`      | 640                                             | Camera resolution (width)            |
| `cameraHeight`     | 480                                             | Camera resolution (height)           |
| `cameraFps`        | 30                                              | Camera frame rate                    |
| `langInstruction`  | pick orange                                     | Default task instruction             |
| `datasetName`      | pick_orange_demo                                | Dataset folder name                  |
| `numEpisodes`      | 50                                              | Target episode count                 |
| `episodeLength`    | 300                                             | Max frames per episode               |
| `datasetDir`       | /home/arobot/Desktop/physical-ai/so-101/outputs | Local lerobot output path            |
| `s3Bucket`         | (empty — must be set in deployment)             | Target S3 bucket for upload          |
| `s3Prefix`         | datasets/                                       | S3 key prefix                        |
