# GPU (NVENC) Video Encoding

By default, LeRobot encodes each episode's video **on the CPU** using PyAV's bundled
`libsvtav1` (AV1) encoder. On a device with an NVIDIA hardware encoder (e.g. Jetson AGX
Thor), you can encode on the **GPU (NVENC)** instead. The
`com.lerobot.data-collection.v21.gpu` component does this and produces H.264 video
significantly faster.

## Why the `.gpu` (ffmpeg shim) variant does NOT actually use the GPU

The `com.lerobot.data-collection.gpu` component installs an "NVENC ffmpeg shim" — a
wrapper placed at `/usr/local/bin/ffmpeg` that rewrites `libsvtav1` -> `h264_nvenc` when
the `ffmpeg` **CLI** is invoked.

The problem: this LeRobot version encodes video **in-process via PyAV**
(`av.open(...).add_stream("libsvtav1")`), and never shells out to the `ffmpeg` CLI. So the
shim is never invoked, and `.gpu` still produces CPU-encoded AV1. The shim is effectively a
no-op.

Verified on-device: PyAV's bundled libavcodec has **no** NVENC encoder compiled in
(`av.codec.Codec("h264_nvenc", "w")` -> `UnknownCodecError`), while the **system** `ffmpeg`
CLI **does** expose `h264_nvenc` / `hevc_nvenc` / `av1_nvenc`, and `libnvidia-encode.so` is
present.

## How `.v21.gpu` gets real GPU encoding

Instead of a PATH shim, the `.v21.gpu` image **patches LeRobot's `encode_video_frames`**
(appended to `lerobot/datasets/video_utils.py`, the same last-definition-wins trick used for
the FIFO control patch). When `GPU_ENCODE=1`, the override encodes the on-disk PNG frames
via the **system `ffmpeg` CLI**:

```
ffmpeg -y -framerate <fps> -i frame_%06d.png -c:v h264_nvenc -pix_fmt yuv420p <out>.mp4
```

On **any** failure it falls back to the original PyAV/CPU SVT-AV1 path, so recording never
breaks. `GPU_ENCODE` / `VIDEO_CODEC` are baked into the image as ENV (config keys
`gpuEncode` / `videoCodec`), and the container inherits them.

### Requirements

- An NVIDIA hardware encoder + `libnvidia-encode.so` reachable in the container
  (`--runtime=nvidia`, which `collect.py` already uses).
- `video` in `NVIDIA_DRIVER_CAPABILITIES` (the recipe sets this).
- The system `ffmpeg` in the image built with NVENC (the base image's `ffmpeg` includes it).

> Jetson note: Thor exposes desktop-style NVENC (`h264_nvenc`) with `libnvidia-encode`.
> Older Tegra devices may only offer the V4L2 encoder (`nvv4l2h264enc`) — verify with
> `ffmpeg -hide_banner -encoders | grep -E 'nvenc|nvv4l2'` inside the container.

## Measured performance (Jetson AGX Thor)

Encoding an identical 900-frame (30 s @ 30 fps), 640x480 sequence, averaged over 3 runs:

| | GPU (`h264_nvenc`) | CPU (`libsvtav1`, crf 30) |
|---|---|---|
| Encode time | **~1.9 s** | **~5.1 s** |
| Throughput | ~445-499 fps | ~175-176 fps |
| Output size (30 s) | ~3.64 MB | ~0.91 MB |
| Codec | H.264 | AV1 |

- **~2.7x faster** encoding on the GPU. Each episode has two videos (front + wrist), so GPU
  shortens the inter-episode save/encode gap by roughly 6 s per episode.
- **Tradeoff:** the GPU output is ~4x larger, because `h264_nvenc`'s default rate control is
  less compression-efficient than `libsvtav1 -crf 30` (AV1). If storage/bandwidth matters,
  tune the override with a quality target (e.g. add `-cq 30` to the `h264_nvenc` args), or
  keep the CPU (AV1) variant.

## Choosing a variant

| Priority | Use |
|---|---|
| Faster encoding / shorter inter-episode gaps | `com.lerobot.data-collection.v21.gpu` (H.264, GPU) |
| Smaller files / best compression | `com.lerobot.data-collection.v21` (AV1, CPU) |

The GPU and CPU variants use the same MQTT topics — run only one at a time.

## Enable / configure

The `.v21.gpu` recipe defaults to GPU encoding:

```yaml
dataImage: "lerobot-data-collection-v21-gpu:1.0.0"   # separate image tag
gpuEncode: "1"
videoCodec: "h264_nvenc"                              # or hevc_nvenc
```

To fall back to CPU, deploy the CPU variant (`.v21`) instead — the shipped `.v21.gpu` image
bakes `GPU_ENCODE=1`, so flipping it off requires rebuilding the image (or extending
`collect.py`/the recipe to pass `-e GPU_ENCODE=0` to the recording container).

## Build / cache note (important)

Keep the **top** `ENV NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute` line identical to
the original image and add `,video` **only at the END** of the Dockerfile. `ENV` changes
invalidate the Docker layer cache for everything below them — putting `,video` near `FROM`
forces a full (~2 h) rebuild of the apt/torch/torchcodec layers (which can also hit transient
`apt` mirror failures). With the override + `video` ENV appended at the end, only the small
tail layers rebuild and the heavy layers stay cache-hits (build is a few minutes).

## Verify it actually used the GPU

The output codec is the proof — H.264 means `h264_nvenc` ran; AV1 means it fell back to CPU:

```bash
aws s3 cp "s3://<data-bucket>/datasets/<date>/<slug>/<session>/videos/chunk-000/observation.images.front/episode_000000.mp4" /tmp/e.mp4
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of default=nk=1:nw=1 /tmp/e.mp4
# -> h264   (GPU/NVENC)   |   av1   (CPU fallback)
```

`meta/info.json` also records `observation.images.front.info.video.codec`.
