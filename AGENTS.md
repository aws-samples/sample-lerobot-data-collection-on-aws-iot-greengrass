# AGENTS.md — LeRobot Data Collection operations guide

This document is operational context for AI agents/operators working with this bundle
(`lerobot-data-collection`). Sensitive values are placeholders (`<AWS_ACCOUNT_ID>`, `<IOT_ENDPOINT>`,
`<WEB_USERNAME>`, `<WEB_PASSWORD>`, `<DATA_BUCKET>`). Follow the substitution table in `README.md` when deploying.

## System overview
Records SO-ARM101 (leader/follower) teleoperation in the LeRobot format → uploads to S3, with
remote MQTT control from a web console + live and per-episode video via KVS (WebRTC/HLS).
Device: Jetson AGX Thor (JetPack 7 / CUDA 13, aarch64). The deployment target is a thing group.

## Components (keep all of them together when deploying)
- `com.lerobot.data-collection.gpu` — NVENC encoding variant. Fetches `collect.py` from the version folder at runtime.
- `com.lerobot.data-collection` — original (CPU/SVT-AV1) reference. **Do not run alongside .gpu** (same MQTT topics).
- `com.lerobot.data-collection.v21` — pins lerobot to **v0.3.3** → LeRobot dataset **v2.1 (per-episode files)**. Ships its own `collect.py` (component-namespaced fetch path `.../collect/com.lerobot.data-collection.v21/<ver>/collect.py`) where **Discard = re-record the current episode** (FIFO `rerecord`, not a full-session stop), plus a reset-window countdown (`resetRemaining` on `/status`) and a `recSeq` signal published on every actual recording start (ep1 + rerecord retakes included) for precise external orchestration. Docker build is identical to the original minimal image (no NVENC) — only the lerobot commit differs. **Do not run alongside the other data-collection components** (same MQTT topics).
- `com.lerobot.data-collection.v21.gpu` — v2.1 (per-episode) **with real GPU (NVENC) encoding**. Separate image tag `lerobot-data-collection-v21-gpu:1.0.0`. The image appends an `encode_video_frames` override (base64, same last-def-wins trick as the FIFO patch) that encodes the PNG frames via the **system `ffmpeg` CLI with `h264_nvenc`** when `GPU_ENCODE=1` (baked ENV), falling back to the original PyAV/CPU SVT-AV1 on any failure. `video` is added to `NVIDIA_DRIVER_CAPABILITIES` **at the END** of the Dockerfile (keep the top ENV identical to the original so the apt/torch/torchcodec layers stay cache-hits — putting `,video` at the top busts the cache and forces a full ~2h rebuild that can hit transient apt failures). Reuses the same controller as `.v21` — ships its own identical `collect.py` fetched from its own namespaced path (`.../collect/com.lerobot.data-collection.v21.gpu/<ver>/collect.py`). Verified on Jetson Thor: output is H.264, ~2.7x faster to encode than CPU AV1 but ~4x larger files (see GPU_ENCODING.md). NOTE: the `.gpu` variant's NVENC ffmpeg *shim* is a no-op (lerobot encodes via PyAV, not the ffmpeg CLI); `.v21.gpu` is the approach that actually engages the GPU.
- `com.groot.kvs-webrtc-ingest` — color camera → KVS WebRTC ingestion, clockoverlay + viewer STS credentials.
- Depending on your environment, AWS-managed Greengrass components such as `aws.greengrass.Cli`,
  `aws.greengrass.LogManager`, and `aws.greengrass.SecureTunneling` may also be part of the same deployment.

> **Deployment principle**: always fetch the current deployment with `get-deployment`, **preserve all
> components + each config**, and replace only the target component with a new version, then
> `create-deployment`. Omitting any one removes it. `failureHandlingPolicy=ROLLBACK` recommended.
> A concurrent operator may change the deployment, so **always re-fetch right before deploying**.

## collect.py deployment model (important)
- `collect.py` is not packaged; the recipe `run` step fetches it from S3 at runtime:
  `s3://greengrass-datasets-<AWS_ACCOUNT_ID>/collect/com.lerobot.data-collection/<ver>/collect.py`
- Always keep the **recipe fetch-path version == the uploaded collect.py version** identical.
- The Docker image is built on-device by the install step (inline Dockerfile). If the `dataImage` tag already exists it is skipped →
  **to change the image you must bump the dataImage tag to trigger a rebuild (~2h)**. Keep dataImage unchanged when changing only collect.py (no rebuild).

## MQTT topics
- `lerobot/{thing}/collect/{command,status,video,files,kvs}`
- `lerobot/{thing}/webrtc/viewer` (viewer STS credentials, retained)
- `$aws/things/{thing}/shadow/name/episodes/{update,get,...}` (episode window; latest session only)
- The web UI must use the `lerobot/` prefix (a past `orange/` misconfiguration caused commands not to reach the component).

## Key invariants / gates (watch for regressions)
1. **Empty-episode (exit 133) guard**: `next`/`stop` FIFO writes happen only after the current episode has recorded ≥~1.5s,
   re-checked via `_ep_start_ts` right before each write inside the retry loop. (Prevents re-delivery at an episode boundary.)
2. **Empty last-episode recovery**: even if the last episode crashes with 0 frames (rc=133), saved episodes are uploaded + the shadow is updated.
3. **presign = frozen credentials + SigV4**. The data bucket must be **in the same region as the deployment** (otherwise presign region mismatch fails).
4. **Episode window shadow** is written via boto3 `iot-data.update_thing_shadow` (IPC fire-and-forget failed silently).
   The TES role needs `iot:UpdateThingShadow`.
5. Do not set a `Timeout` on long-running `run` steps (e.g. `com.groot.kvs-webrtc-ingest`); a `Timeout` of 0 makes the step exit immediately → rollback.
6. The KVS-webrtc-ingest pipeline (clockoverlay, etc.) is a C string literal → no quotes/spaces in values; `git checkout` then idempotent sed on every deploy.

## Web UI (Monitor tab)
- Top toggle (one line): `WebRTC Live` (UDP) / `HLS Live` (TCP/443 fallback) / `Episode Playback`.
- Episode Playback: episode list (shadow) on the left + segment playback on the right. Ranges extending into the future use `LIVE_REPLAY`, past-only uses `ON_DEMAND`.
- Files: ▶ next to a session folder → sets the full session range + selectable playback from the episode list. Next to an mp4 is download only.
- Live is WebRTC/HLS only (removed a past bug where the device `/kvs` live HLS attached to the same video element and overwrote color with grayscale).

## Deployment procedure (component change)
1. Upload `collect.py` to a new version folder (S3).
2. Bump the recipe `ComponentVersion` + fetch-path version → `create-component-version`.
3. `get-deployment` (LATEST) → change only the target component to the new version, preserve the rest + each config merge → `create-deployment`.
4. Confirm SUCCEEDED via `describe-job-execution` + check CloudWatch logs (`[OK] Controller running`, `[SHADOW]`, `[REC]`).

## Web UI deployment
- Replace S3 `index.html` (text/html) + CloudFront invalidation (`/index.html`, `/`).
- Verify on every deploy: `node --check` on the inline JS, no Korean (all English), balanced `<div>`/`</div>`.

## Verification/diagnostics
- Component logs: CloudWatch `/aws/greengrass/UserComponent/<region>/com.lerobot.data-collection.gpu`
  (`[CMD] [REC] [CTRL] [STATUS] [SHADOW] [DOCKER]`).
- After a change: `py_compile` (collect.py), YAML load (recipe), deployment job status, one real end-to-end recording.
- The physical arms (/dev/ttyACM*) and cameras (/dev/cam_*) are required — motor-not-detected and similar are hardware issues (distinct from the "empty episode" case of exit 133).

## Safety
- For production (real-robot) impacting actions (deploy/delete/IAM), explain the impact and rollback, and act only after confirmation. Reads (describe/list/logs) are free.
- The public bundle must contain **no real account IDs/endpoints/credentials/personal bucket names** — always keep placeholders.

## Public sample notes (aws-samples)
- **Repository**: `https://github.com/aws-samples/sample-lerobot-data-collection-on-aws-iot-greengrass`
- **License**: `LICENSE` = MIT-0.
- **Docs**: `README.md` (English) + `README-ko.md` (Korean); all other guides are English — keep new docs English-first.
- **Sanitization**: all environment-specific values are placeholders (`<AWS_ACCOUNT_ID>`, `<IOT_ENDPOINT>`,
  `<WEB_USERNAME>`, `<WEB_PASSWORD>`, `<DATA_BUCKET>`); the bucket convention is `greengrass-datasets-<AWS_ACCOUNT_ID>`.
  Never introduce real account IDs, endpoints, or credentials.
- **Security scanners**: some files carry inline suppressions (`# nosemgrep`, `# checkov:skip`, cfn_nag
  `rules_to_suppress`) with rationale for accepted sample/POC trade-offs — see `README.md` → *Known Limitations (Demo/POC)*
  for the hardening required before production.
