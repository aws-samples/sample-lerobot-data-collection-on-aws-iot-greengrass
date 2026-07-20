# com.lerobot.data-collection.gpu

Copy of [`com.lerobot.data-collection`](../com.lerobot.data-collection/) that attempts to accelerate
the (usually slowest) video-encoding step with **NVENC**, using a separate image tag so it never
conflicts with the original image. It produces the same **LeRobot v3.0 (packed)** dataset and reuses
the original `collect.py`.

> [!IMPORTANT]
> **The NVENC ffmpeg shim in this variant is effectively a no-op.** This lerobot version encodes video
> **in-process via PyAV**, not via the system `ffmpeg` CLI, so the shim is never invoked and the output
> is still **CPU AV1** — identical to the original component. For encoding that actually runs on the
> GPU, use [`com.lerobot.data-collection.v21.gpu`](../com.lerobot.data-collection.v21.gpu/) (it patches
> lerobot's encoder directly). See [`GPU_ENCODING.md`](../../GPU_ENCODING.md).

- **Platform**: `linux / aarch64` · `RequiresPrivilege: true`
- **Dataset format**: LeRobot v3.0 (packed)
- **Video codec**: intended `h264_nvenc`, **actual = CPU AV1** (see note above)

> ⚠️ Shares MQTT topics with the other data-collection variants — **run only one at a time**.

## How it differs from the original

The install step builds the same stack, plus:
1. Installs an **NVENC ffmpeg shim** at `/usr/local/bin/ffmpeg` (ahead of `/usr/bin/ffmpeg` on PATH)
   that rewrites `-vcodec libsvtav1` → `-vcodec $VIDEO_CODEC`, `-crf` → `-cq`, and strips SVT-AV1
   params; **falls back to the real ffmpeg with the original args if NVENC fails** (safe).
2. Bakes `ENV GPU_ENCODE` / `VIDEO_CODEC` and adds `video` to `NVIDIA_DRIVER_CAPABILITIES`.

Everything else (topics, control FIFO, upload, shadow) is identical to the original.

## Configuration

Same keys as [`com.lerobot.data-collection`](../com.lerobot.data-collection/README.md#configuration),
with these differences/additions:

| Key | Default | Description |
|---|---|---|
| `dataImage` | `lerobot-data-collection-gpu:1.0.0` | **Separate** image tag (no conflict with the original). Bump to force a rebuild. |
| `gpuEncode` | `1` | GPU-encode switch, baked into the image as ENV (set `0` to keep CPU/libsvtav1). |
| `videoCodec` | `h264_nvenc` | NVENC codec the shim rewrites to. |

Shared keys (hardware / session / storage / torch stack) — see the original component's README.

A copy of the original `collect.py` is included in this folder's `artifacts/` for a self-contained
layout, but it is **identical** to the original controller. At runtime the recipe still fetches from
the original S3 path (`collect/com.lerobot.data-collection/<ver>/collect.py`) — the encoding change
lives entirely in the image, so there is no behavioral difference in the script.

## MQTT topics & IPC access control

- **Publish**: `lerobot/+/collect/{status,video,files,kvs}` + `$aws/things/+/shadow/name/episodes/update`
- **Subscribe**: `lerobot/+/collect/command`

(Adds the episode-window Device Shadow publish topic vs. the reference recipe, enabling web-UI episode
playback. The device TES role needs `iot:UpdateThingShadow`.)

## Deploy notes

- Replace placeholders (root [`README.md`](../../README.md) table) and set `s3Bucket` / `thingName`.
- First deploy builds a **separate ~2h image**; changing only `collect.py` needs no rebuild.
- To revert to guaranteed-CPU behavior, deploy the original component or set `gpuEncode="0"`.
