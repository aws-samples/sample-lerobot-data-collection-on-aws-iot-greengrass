# com.lerobot.data-collection.v21

Same recording pipeline as [`com.lerobot.data-collection`](../com.lerobot.data-collection/) but pins
lerobot to **v0.3.3** — the last release whose `CODEBASE_VERSION` is **"v2.1"**, producing the
**per-episode** dataset layout instead of the v3.0 packed layout:

```
data/chunk-000/episode_000000.parquet          # one parquet per episode
videos/chunk-000/<video_key>/episode_000000.mp4 # one mp4 per episode
meta/{info.json (codebase_version v2.1), episodes.jsonl, tasks.jsonl, episodes_stats.jsonl}
```

Ships **its own `collect.py`** (component-namespaced fetch path) with three behavior changes vs. the
original controller. Video is still **CPU SVT-AV1** (PyAV); for the GPU version see
[`com.lerobot.data-collection.v21.gpu`](../com.lerobot.data-collection.v21.gpu/).

- **Platform**: `linux / aarch64` · `RequiresPrivilege: true`
- **Dataset format**: LeRobot **v2.1 (per-episode files)**
- **Video codec**: CPU SVT-AV1

> ⚠️ Shares MQTT topics with the other data-collection variants — **run only one at a time**.

## collect.py behavior differences (v21 only)

1. **Discard = re-record the current episode** — `discard` writes FIFO `rerecord` (lerobot clears the
   episode buffer and re-records the **same** number), instead of hard-stopping the whole session. The
   discarded take is not saved. Safe to press at 0 frames. (The old hard-stop is kept as `discardSession`.)
2. **Reset-window countdown** — while lerobot shows "Reset the environment", `resetRemaining` (10→1)
   is published on `/collect/status` once per second so the web UI can render a countdown.
3. **`recSeq` real-time recording-start signal** — a `recSeq` (+`recStart`) is published on **every**
   actual "Recording episode" start (including episode 1 and rerecord retakes), enabling precise
   external orchestration.

## Configuration

Identical keys to [`com.lerobot.data-collection`](../com.lerobot.data-collection/README.md#configuration),
with these differences:

| Key | Default | Description |
|---|---|---|
| `dataImage` | `lerobot-data-collection-v21:1.0.0` | Separate image tag. Docker build is the **same as the original minimal image** (no NVENC) — only `lerobotCommit` differs. |
| `lerobotCommit` | `b883328e...` (**lerobot v0.3.3**) | Pinned commit that produces the **v2.1 per-episode** format. |

All hardware / session / storage / torch-stack keys are the same as the original (see its README).
Note `episodeLength` is the auto-advance timeout in **seconds**.

`collect.py` fetch path is **component-namespaced**:
`s3://<bucket>/collect/com.lerobot.data-collection.v21/<version>/collect.py` (does not pollute the
original lineage). Keep this version identical to the recipe `ComponentVersion`.

## MQTT topics & IPC access control

- **Publish**: `lerobot/+/collect/{status,video,files,kvs}` + `$aws/things/+/shadow/name/episodes/update`
- **Subscribe**: `lerobot/+/collect/command`

`status` additionally carries `resetRemaining` (reset countdown) and `recSeq`/`recStart`
(recording-start signal). The device TES role needs `iot:UpdateThingShadow` for the episode-window shadow.

## Deploy notes

- Replace placeholders (root [`README.md`](../../README.md) table); set `s3Bucket` / `thingName`.
- Upload this component's `collect.py` (in `artifacts/`) to
  `s3://<bucket>/collect/com.lerobot.data-collection.v21/1.0.0/collect.py`.
- First deploy builds a **separate image** (cache-shares apt/torch/torchcodec layers with the original
  minimal image if present, so typically only the lerobot layer rebuilds).
- See [`DEPLOYMENT_GUIDE.md`](../../DEPLOYMENT_GUIDE.md) and [`COMPONENT_ARCHITECTURE.md`](../../COMPONENT_ARCHITECTURE.md).
