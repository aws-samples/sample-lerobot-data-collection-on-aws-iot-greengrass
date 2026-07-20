# com.lerobot.data-collection

Reference data-collection component. Records SO-ARM101 (leader/follower) teleoperation with dual
cameras in **LeRobot dataset format (codebase_version v3.0 — packed: multiple episodes per
parquet/mp4 file)** and uploads to Amazon S3, controlled remotely over MQTT.

Video is encoded on the **CPU with SVT-AV1** (lerobot's in-process PyAV encoder). For GPU-accelerated
encoding see [`com.lerobot.data-collection.v21.gpu`](../com.lerobot.data-collection.v21.gpu/); for the
per-episode dataset layout see [`com.lerobot.data-collection.v21`](../com.lerobot.data-collection.v21/).

- **Platform**: `linux / aarch64` (Jetson AGX Thor, JetPack 7 / CUDA 13)
- **Runs with**: `RequiresPrivilege: true` (needs `/dev` device + NVIDIA access)
- **Dataset format**: LeRobot v3.0 (packed)
- **Video codec**: CPU SVT-AV1

> ⚠️ The four data-collection variants (`com.lerobot.data-collection`, `.gpu`, `.v21`, `.v21.gpu`)
> share the same MQTT topics — **deploy/run only one at a time**.

## Lifecycle (what the recipe does)

1. **install** (`Timeout: 7200`) — builds a **standalone** Docker image (`dataImage` tag) on the device
   from the public `nvidia/cuda:13.0.x-cudnn-devel-ubuntu24.04` base: aarch64 torch (Jetson AI Lab
   index), torchcodec (FFmpeg source build), cuDSS, and lerobot (`--no-deps` + explicit deps). A FIFO
   external-control patch is appended to lerobot's `control_utils.py`. **Idempotent** — skipped if the
   `dataImage` tag already exists.
2. **run** — exports the configuration as environment variables, fetches `collect.py` from S3
   (`collect/com.lerobot.data-collection/<version>/collect.py`), and launches the controller.
3. **shutdown** — stops/removes the `lerobot-record` container and `collect.py`.

> **collect.py is fetched from S3 at runtime**, not packaged. Keep the recipe's fetch-path version
> **identical** to the recipe `ComponentVersion` and to the uploaded `collect.py` version.
> Changing only `collect.py` needs no rebuild; **changing the image requires bumping `dataImage`**
> (an existing tag is skipped) → triggers a ~2h rebuild.

## Configuration

### Robot / camera hardware
| Key | Default | Description |
|---|---|---|
| `robotType` | `so101` | Robot type passed to `lerobot-record`. |
| `leaderPort` | `/dev/ttyACM0` | SO-101 **leader** arm serial port. |
| `followerPort` | `/dev/ttyACM1` | SO-101 **follower** arm serial port. |
| `frontCameraIndex` | `/dev/cam_front` | Front camera device (udev symlink). |
| `wristCameraIndex` | `/dev/cam_wrist` | Wrist camera device (udev symlink). |
| `cameraWidth` | `640` | Camera capture width (px). |
| `cameraHeight` | `480` | Camera capture height (px). |
| `cameraFps` | `30` | Camera frame rate. |

### Recording session
| Key | Default | Description |
|---|---|---|
| `langInstruction` | `pick orange` | Default task instruction (`--dataset.single_task`). Overridable per `start` command. |
| `datasetName` | `pick_orange_demo` | Base name of the dataset folder / session. |
| `numEpisodes` | `50` | Target number of episodes for the session. |
| `episodeLength` | `300` | Max **seconds** per episode (`episode_time_s`); auto-advances on timeout (`[TIMEOUT]`). |
| `datasetDir` | `/home/arobot/Desktop/physical-ai/so-101/outputs` | Local lerobot output dir; also holds the control FIFO (`.control.fifo`). |

### Storage / cloud
| Key | Default | Description |
|---|---|---|
| `s3Bucket` | `""` | **Required** — upload target bucket. Must be in the **same region as the deployment** (otherwise presigned-URL region mismatch). |
| `s3Prefix` | `datasets/` | S3 key prefix. Layout: `{prefix}{date}/{instruction}/{session}/...`. |
| `thingName` | `lerobot-device` | IoT Thing name = MQTT topic prefix (`lerobot/{thingName}/collect/*`). |
| `region` | `ap-northeast-2` | AWS region. |
| `kvsStreamName` | `thor-001-camera` | KVS stream used for live HLS monitoring + per-episode on-demand HLS in the web UI. |

### Image build (advanced — change only if you know the stack)
| Key | Default | Description |
|---|---|---|
| `dataImage` | `lerobot-data-collection:1.2.9` | Built image tag. **Bump to force a rebuild** (existing tag is skipped). |
| `lerobotCommit` | `c75455a6...` | Pinned lerobot commit (includes `so101_follower`). This one produces the **v3.0** format. |
| `torchVersion` | `2.10.0` | aarch64 torch (Jetson AI Lab). |
| `torchvisionVersion` | `0.25.0` | torchvision. |
| `torchcodecVersion` | `0.10.0` | torchcodec (source build). |
| `tritonVersion` | `3.5.0` | triton. |
| `jetsonTorchIndex` | `https://pypi.jetson-ai-lab.io/sbsa/cu130/+simple` | Jetson AI Lab pip index for the torch stack. |

## MQTT topics & IPC access control

The recipe grants (via `accessControl` → `aws.greengrass.ipc.mqttproxy`):

- **Publish**: `lerobot/+/collect/{status,video,files,kvs}`
- **Subscribe**: `lerobot/+/collect/command`

Command actions: `start` / `stop` (save current episode → next) / `endSession` (finalize) /
`discard` (hard-stop, no upload) / `upload` / `list` / `uploadFiles` / `kvsLive` / `kvsEpisodes`.

> This reference recipe does **not** grant the episode-window Device Shadow topic. The `.gpu` / `.v21`
> / `.v21.gpu` variants add `$aws/things/+/shadow/name/episodes/update` for episode-playback support.

## Deploy notes

- Replace placeholders first (see the root [`README.md`](../../README.md) substitution table): set
  `s3Bucket`, `thingName`, and the bucket name convention `greengrass-datasets-<AWS_ACCOUNT_ID>`.
- Upload `collect.py` to `s3://<bucket>/collect/com.lerobot.data-collection/1.0.0/collect.py`
  (version must match the recipe fetch path).
- See the root [`DEPLOYMENT_GUIDE.md`](../../DEPLOYMENT_GUIDE.md) for full commands and
  [`COMPONENT_ARCHITECTURE.md`](../../COMPONENT_ARCHITECTURE.md) for internals.
