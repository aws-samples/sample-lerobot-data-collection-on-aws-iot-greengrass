# com.lerobot.data-collection.v21.gpu

[`com.lerobot.data-collection.v21`](../com.lerobot.data-collection.v21/) (LeRobot **v2.1 per-episode**,
lerobot v0.3.3) **with real GPU (NVENC) video encoding**. This is the variant whose GPU encoding
**actually engages** — unlike [`.gpu`](../com.lerobot.data-collection.gpu/), whose ffmpeg shim is a
no-op because lerobot encodes via PyAV.

Instead of shimming the `ffmpeg` CLI, the image **appends an override of lerobot's
`encode_video_frames`** (base64-embedded, same last-def-wins trick as the FIFO patch). When
`GPU_ENCODE=1`, it encodes the PNG frames via the **system `ffmpeg` CLI with `h264_nvenc`**, and
**falls back to the original PyAV / CPU SVT-AV1 on any failure**.

- **Platform**: `linux / aarch64` · `RequiresPrivilege: true`
- **Dataset format**: LeRobot **v2.1 (per-episode files)**
- **Video codec**: **H.264 (NVENC / GPU)**, CPU SVT-AV1 fallback
- **Measured on Jetson Thor**: ~2.7× faster encode than CPU AV1, but ~4× larger files (H.264 vs AV1).
  See [`GPU_ENCODING.md`](../../GPU_ENCODING.md).

> ⚠️ Shares MQTT topics with the other data-collection variants — **run only one at a time**.

## Configuration

Same keys as [`com.lerobot.data-collection.v21`](../com.lerobot.data-collection.v21/README.md#configuration),
plus the GPU switches:

| Key | Default | Description |
|---|---|---|
| `dataImage` | `lerobot-data-collection-v21-gpu:1.0.0` | Separate image tag (no conflict with v21/original/gpu). Bump to force a rebuild. |
| `gpuEncode` | `1` | GPU-encode switch, baked as ENV. Set `0` to fall back to CPU SVT-AV1. |
| `videoCodec` | `h264_nvenc` | NVENC codec used by the `encode_video_frames` override. |
| `lerobotCommit` | `b883328e...` (**lerobot v0.3.3**) | Same pin as v21 → produces the v2.1 per-episode format. |

Hardware / session / storage / torch-stack keys are the same as the original (see its README).

`collect.py` is **shipped in this component's `artifacts/`** and is **identical to `.v21`'s
controller** (the GPU change lives entirely in the image). At runtime the recipe fetches it from this
component's own namespaced path (`collect/com.lerobot.data-collection.v21.gpu/<ver>/collect.py`).

> **Build-cache caveat**: `video` is added to `NVIDIA_DRIVER_CAPABILITIES` at the **END** of the
> Dockerfile. Keep the top ENV identical to the original so the apt/torch/torchcodec layers stay
> cache-hits — putting `,video` at the top busts the cache and forces a full ~2h rebuild.

## MQTT topics & IPC access control

- **Publish**: `lerobot/+/collect/{status,video,files,kvs}` + `$aws/things/+/shadow/name/episodes/update`
- **Subscribe**: `lerobot/+/collect/command`

(Same as `.v21`, including the `resetRemaining` / `recSeq` status fields. TES role needs
`iot:UpdateThingShadow`.)

## Deploy notes

- Replace placeholders (root [`README.md`](../../README.md) table); set `s3Bucket` / `thingName`.
- Upload this component's `collect.py` (in `artifacts/`) to
  `s3://<bucket>/collect/com.lerobot.data-collection.v21.gpu/1.0.0/collect.py` (version must match the
  recipe fetch path).
- Verify GPU output: `ffprobe` the resulting mp4 → codec should be `h264`; `meta/info.json`
  `video.codec` = `h264`. If it shows `av1`, NVENC failed and the CPU fallback ran.
- See [`GPU_ENCODING.md`](../../GPU_ENCODING.md) for the CPU-vs-GPU benchmark and tuning (`-cq`).
