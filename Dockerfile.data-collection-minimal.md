# Minimal Docker image for data collection (fully standalone build)

## Background — why the inference image is not needed

The old `Dockerfile.data-collection` used the **inference image** (`groot-n16-inference`) as its base.
That base bundles, for inference, a CUDA devel (several GB) + the full Isaac-GR00T repo + git-lfs model
assets + ONNX export + the `gr00t.eval.run_gr00t_server` entrypoint.

But **data collection does not run neural-network inference.** It only records leader→follower motion
together with cameras/joints via `lerobot-record`. So all of the inference assets above are unnecessary.

`Dockerfile.data-collection-minimal` builds a minimal image from scratch (standalone), **without
depending on** that inference base.

## Core — where the aarch64 torch comes from (verified)

The only tricky part of "building from scratch" is Jetson (aarch64) torch. We inspected Isaac-GR00T's
`scripts/deployment/thor/{install_deps.sh, pyproject.toml}` (commit `5dc80c4`) directly and mirrored the following:

| Item | Value |
|------|-----|
| torch wheel index | `https://pypi.jetson-ai-lab.io/sbsa/cu130/+simple` (Jetson AI Lab) |
| torch pins | `torch==2.10.0`, `torchvision==0.25.0`, `triton==3.5.0` |
| torchcodec | `0.10.0` — **source build** against system FFmpeg (`--no-build-isolation`) |
| system libraries | **NVPL LAPACK/BLAS** (`libnvpl-lapack0 libnvpl-blas0`) — required by the torch wheel |
| FFmpeg dev | `libav*-dev` required by the torchcodec build |

Installing just these packages lets `lerobot-record` run **without cloning the entire GR00T repo**.

## lerobot pin (important)

- PyPI `lerobot==0.5.1` does **not** include `so101_follower` → install from the git commit
  `c75455a6...` pinned by Isaac-GR00T.
- To prevent torch from being overwritten (`Torch not compiled with CUDA`), lerobot and drivers are
  installed with `--no-deps`, and only the lightweight packages lerobot actually imports are installed normally.

## What gets installed

| Category | Packages |
|------|--------|
| GPU runtime | torch / torchvision / triton (Jetson AI Lab), torchcodec (source) |
| Recording engine | `lerobot` (git@c75455a) |
| Robot drivers | `feetech-servo-sdk`, `pyserial` |
| Camera | `opencv-python-headless` |
| Video encoding | `ffmpeg` (+ torchcodec) |
| Cloud/IPC | `awsiotsdk`, `boto3` |
| lerobot deps | `draccus`, `deepdiff`, `pyyaml`, `einops`, `numpy` |
| (optional) visualization | `rerun-sdk` |

Inference-only pieces (GR00T model, ONNX, gr00t server) are **not installed.**

## Build

> ⚠️ Must be built on **aarch64 (Jetson Thor)**. The torch wheel is sbsa/aarch64 only.

```bash
docker build -f Dockerfile.data-collection-minimal \
  -t lerobot-data-collection:latest .
```

Override pins with `--build-arg`:
```bash
docker build -f Dockerfile.data-collection-minimal \
  --build-arg TORCH_VERSION=2.10.0 \
  --build-arg TORCHCODEC_VERSION=0.10.0 \
  -t lerobot-data-collection:latest .
```

## Automatic build in the component install step

The **install step** of the `com.lerobot.data-collection` recipe builds the same content via an
inline heredoc. No separate docker-build component is needed.

- Idempotent: skipped if `dataImage` already exists
- Pins are exposed as recipe config: `torchVersion`, `torchvisionVersion`,
  `torchcodecVersion`, `tritonVersion`, `jetsonTorchIndex`, `lerobotCommit`
- install Timeout is 7200 seconds (includes the source build, which takes time)

## Integration with collect.py

`ENTRYPOINT ["/bin/bash"]`. The host's `collect.py` injects the command via
`docker run ... -c "/opt/gr00t-venv/bin/lerobot-record ..."` (same as before), so swapping the image
alone works without modifying `collect.py` or the run step. `collect.py` receives the image name via the `DATA_IMAGE` environment variable.

## Build verification

The final Dockerfile stage verifies that the `lerobot-record` entrypoint exists
(`test -x /opt/gr00t-venv/bin/lerobot-record`). If it is missing, the build fails to prevent silent breakage.
