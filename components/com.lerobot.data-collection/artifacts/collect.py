#!/usr/bin/env python3
import json, os, re, shlex, subprocess, sys, threading, time

# Force unbuffered output
os.environ["PYTHONUNBUFFERED"] = "1"
import builtins
_orig_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault("flush", True)
    _orig_print(*args, **kwargs)

THING_NAME    = os.environ.get("THING_NAME",    "thor-001")
DATASET_DIR   = os.environ.get("DATASET_DIR",   "/home/arobot/Desktop/physical-ai/so-101/outputs")
DATASET_NAME  = os.environ.get("DATASET_NAME",  "pick_orange_demo")
S3_BUCKET     = os.environ.get("S3_BUCKET",     "") or "greengrass-datasets-<AWS_ACCOUNT_ID>"
S3_PREFIX     = os.environ.get("S3_PREFIX",     "datasets/")
AWS_REGION    = os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-2")

TOPIC_CMD    = f"lerobot/{THING_NAME}/collect/command"
TOPIC_STATUS = f"lerobot/{THING_NAME}/collect/status"
TOPIC_VIDEO  = f"lerobot/{THING_NAME}/collect/video"
TOPIC_FILES  = f"lerobot/{THING_NAME}/collect/files"
TOPIC_KVS    = f"lerobot/{THING_NAME}/collect/kvs"
# WebRTC-ingested color stream (com.groot.kvs-webrtc-ingest) used for per-episode
# on-demand HLS playback in the web UI (NOT the IR monitoring stream).
WEBRTC_STREAM = os.environ.get("WEBRTC_STREAM", "thor-001-webrtc")
# Named device shadow 'episodes' — persists the LATEST session's per-episode
# time windows so the web UI can play each episode's range from WEBRTC_STREAM.
SHADOW_EP_UPDATE = f"$aws/things/{THING_NAME}/shadow/name/episodes/update"

# KVS monitoring stream (com.groot.kvs-stream publishes the workspace camera here).
# Used to build HLS URLs for live monitoring + per-episode on-demand playback.
KVS_STREAM   = os.environ.get("KVS_STREAM", "") or "thor-001-camera"

CONTAINER_NAME = "lerobot-record"
DOCKER_IMAGE   = os.environ.get("DATA_IMAGE", "lerobot-data-collection:latest")


class Controller:
    def __init__(self):
        self.state          = "idle"
        self.episode        = 0    # episodes captured so far in current session
        self.total_episodes = int(os.environ.get("NUM_EPISODES", "50"))
        self.step           = 0
        self.lang           = os.environ.get("LANG_INSTRUCTION", "pick orange")
        self._ipc           = None
        self._QOS           = None
        self._proc          = None
        self._repo_id       = None
        self._session_id    = None   # set per-session when a recording starts
        self._stopping      = False  # True while a stop/discard is in progress
        self._error         = ""     # last error message (surfaced to web UI)
        self._log_tail      = []     # rolling buffer of recent container stdout lines
        self._ep_start_ts   = None   # wall-clock when current episode began capturing frames
        self._episodes      = []     # per-episode KVS windows: [{episode,start,end}]
        self._last_manual_next = 0.0 # wall-clock of last manual 'stop'(=FIFO next) request

    @staticmethod
    def _slug(text):
        """Turn an instruction into a filesystem/S3-safe folder name.
        e.g. 'Pick orange!' -> 'pick_orange'."""
        s = re.sub(r"[^a-zA-Z0-9]+", "_", (text or "").strip().lower())
        return s.strip("_") or "task"

    def _session_paths(self, date, lang):
        """Local dataset dir + S3 base prefix for a given date+instruction.

        Layout (mirrored local ↔ S3):
            local : {DATASET_DIR}/arobot/{date}_{slug}_{ts}/
            s3    : {prefix}{date}/{slug}/{session_id}/
        Listing filters local dirs by the '{date}_{slug}_' name prefix."""
        slug = self._slug(lang)
        return slug

    def run(self):
        print(f"=== LeRobot Data Collection ===")
        print(f"[OK] THING_NAME={THING_NAME}")
        print(f"[OK] DATASET_DIR={DATASET_DIR}  DATASET_NAME={DATASET_NAME}")
        print(f"[OK] S3_BUCKET={S3_BUCKET!r}  S3_PREFIX={S3_PREFIX!r}")
        print(f"[OK] DOCKER_IMAGE={DOCKER_IMAGE}")
        print(f"[OK] TOPIC_CMD={TOPIC_CMD}")

        # Diagnose available devices
        self._log_devices()

        self._connect_ipc()
        self._publish_status()
        print(f"[OK] Controller running.")

        while True:
            # Detect docker container exit → all episodes finished → auto-upload.
            # In the persistent-container model the container stays up for the
            # WHOLE session (all N episodes); a natural exit here means the run
            # completed on its own. A user-driven stop/discard is handled in
            # _stop_recording (self._stopping guards against double handling).
            if self._proc and self._proc.poll() is not None:
                rc = self._proc.returncode
                self._proc = None
                if self._stopping:
                    # User-driven discard/stop is finalized in _stop_recording;
                    # do not double-handle here.
                    pass
                elif self.state == "recording" and rc != 0:
                    # lerobot-record exited non-zero. Distinguish two cases:
                    #  (a) EMPTY LAST EPISODE: the final episode ended with 0
                    #      frames (a Stop&Save or auto-timeout landed right at an
                    #      episode boundary) -> lerobot crashes on add_episode
                    #      ("add ... frames before add_episode"). Episodes
                    #      1..(N-1) ARE already saved on disk, so finalize the
                    #      session and UPLOAD them instead of discarding.
                    #  (b) Real failure (robot/camera): surface error, no upload.
                    detail = self._extract_error_detail()
                    empty_ep = ("add_episode" in detail) or ("add_frame" in detail)
                    completed = max(0, self.episode - 1)  # self.episode = failing episode
                    if empty_ep and completed >= 1:
                        print(f"[REC] Container exited rc={rc} — last episode empty; "
                              f"recovering {completed} completed episode(s): finalize + upload.")
                        if self._episodes:
                            self._episodes.pop()   # drop the empty (failed) trailing episode
                        self.episode = completed
                        self.state = "idle"
                        self._publish_status()
                        self._update_episode_shadow()
                        if S3_BUCKET:
                            threading.Thread(target=self._upload,
                                             args=(S3_BUCKET, S3_PREFIX), daemon=True).start()
                    else:
                        print(f"[REC] Container exited rc={rc} — recording FAILED "
                              f"(no clean completion). Skipping upload.")
                        self._error = (f"Recording failed (exit {rc}): " + detail) if detail else (
                            f"Recording failed (exit {rc}). Check the robot arm/camera connections "
                            f"(e.g. missing motor ID).")
                        print(f"[REC] error detail: {self._error}")
                        self.state = "error"
                        self._publish_status()
                        time.sleep(3)  # nosemgrep: arbitrary-sleep -- intentional polling/backoff
                        self._error = ""
                        self.state = "idle"
                        self._publish_status()
                elif self.state in ("recording", "saving"):
                    # Container exited on its own: either all N episodes finished
                    # (state=recording) or a graceful endSession finalized the
                    # session (state=saving, FIFO 'stop'). Both cases upload the
                    # completed episodes. Handling 'saving' here prevents the UI
                    # from getting stuck when endSession is used.
                    print(f"[REC] Container exited rc={rc} (state={self.state}) — "
                          f"session finished (captured={self.episode}). Uploading.")
                    self.state = "idle"
                    self._publish_status()
                    self._update_episode_shadow()
                    if S3_BUCKET:
                        threading.Thread(target=self._upload,
                                         args=(S3_BUCKET, S3_PREFIX), daemon=True).start()
            time.sleep(1)  # nosemgrep: arbitrary-sleep -- intentional polling/backoff

    def _log_devices(self):
        try:
            import glob
            video = sorted(glob.glob("/dev/video*") + glob.glob("/dev/cam_*"))
            serial = sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
            print(f"[DIAG] video devices : {video}")
            print(f"[DIAG] serial devices: {serial}")
        except Exception as e:
            print(f"[DIAG] device scan error: {e}")

        # Docker image availability
        try:
            r = subprocess.run(
                ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                capture_output=True, text=True, timeout=10)
            images = [l for l in r.stdout.strip().splitlines() if "groot" in l]
            print(f"[DIAG] docker groot images: {images}")
        except Exception as e:
            print(f"[DIAG] docker image check error: {e}")

    def _connect_ipc(self):
        try:
            import awsiot.greengrasscoreipc as ipc
            from awsiot.greengrasscoreipc.model import SubscribeToIoTCoreRequest, QOS
            import awsiot.greengrasscoreipc.client as client

            self._ipc = ipc.connect()
            self._QOS = QOS
            ctrl = self

            class H(client.SubscribeToIoTCoreStreamHandler):
                def on_stream_event(self, event):
                    try:
                        ctrl._handle(json.loads(event.message.payload.decode()))
                    except Exception as e:
                        print(f"[IPC] Parse error: {e}")
                def on_stream_error(self, error):
                    print(f"[IPC] Stream error: {error}")
                def on_stream_closed(self):
                    print(f"[IPC] Stream closed")

            req = SubscribeToIoTCoreRequest(topic_name=TOPIC_CMD, qos=QOS.AT_LEAST_ONCE)
            op = self._ipc.new_subscribe_to_iot_core(H())
            op.activate(req)
            op.get_response().result(timeout=10)
            print(f"[IPC] Connected. Subscribed: {TOPIC_CMD}")
        except Exception as e:
            print(f"[IPC] Connection failed: {e} — standalone mode")

    def _handle(self, payload):
        action = payload.get("action", "")
        print(f"[CMD] action={action!r} payload={payload}")
        if action == "start":
            self.lang = payload.get("lang", self.lang)
            self._start_recording(payload)
        elif action == "stop":
            # "Stop & Save" = end the CURRENT episode early and advance to the
            # next one (lerobot exit_early). The session keeps running. Sent via
            # the control FIFO; non-blocking. Record the time so the episode
            # transition is logged as MANUAL (not a timeout).
            self._last_manual_next = time.time()
            self._send_control("next")
            return
        elif action == "endSession":
            # "End Session" = finish the whole recording gracefully (lerobot
            # stop_recording). If the container is still running, ask lerobot to
            # finalize via the FIFO; the run() loop then auto-uploads on exit.
            # If the container has ALREADY exited (e.g., it finished/stopped on
            # its own), upload directly so the UI never sticks on "saving".
            self.state = "saving"
            self._publish_status()
            if self._proc and self._proc.poll() is None:
                self._send_control("stop")
            else:
                print(f"[CMD] endSession: no live container — uploading directly")
                self._update_episode_shadow()
                threading.Thread(target=self._upload,
                                 args=(S3_BUCKET, S3_PREFIX), daemon=True).start()
            return
        elif action == "discard":
            # Full-session discard: hard-stop the container, no upload.
            threading.Thread(target=self._stop_recording,
                             kwargs={"discard": True}, daemon=True).start()
            return
        elif action == "upload":
            bucket = payload.get("s3Bucket", "") or S3_BUCKET
            prefix = payload.get("s3Prefix", "") or S3_PREFIX
            print(f"[CMD] Manual upload: bucket={bucket!r} prefix={prefix!r}")
            threading.Thread(target=self._upload, args=(bucket, prefix), daemon=True).start()
            return
        elif action == "list":
            # List files in a date/instruction folder with per-file S3 status.
            bucket = payload.get("s3Bucket", "") or S3_BUCKET
            prefix = payload.get("s3Prefix", "") or S3_PREFIX
            date   = payload.get("date", "") or time.strftime("%Y-%m-%d")
            lang   = payload.get("lang", "") or self.lang
            threading.Thread(target=self._list_files,
                             args=(bucket, prefix, date, lang), daemon=True).start()
            return
        elif action == "uploadFiles":
            # Re-upload a user-selected subset of files.
            bucket = payload.get("s3Bucket", "") or S3_BUCKET
            prefix = payload.get("s3Prefix", "") or S3_PREFIX
            date   = payload.get("date", "") or time.strftime("%Y-%m-%d")
            lang   = payload.get("lang", "") or self.lang
            files  = payload.get("files", []) or []
            threading.Thread(target=self._upload_files,
                             args=(bucket, prefix, date, lang, files), daemon=True).start()
            return
        elif action == "geturl":
            # Lazily presign a single S3 key (keeps the file list small).
            bucket = payload.get("s3Bucket", "") or S3_BUCKET
            key    = payload.get("key", "")
            threading.Thread(target=self._get_url, args=(bucket, key), daemon=True).start()
            return
        elif action == "kvsLive":
            # Live HLS URL for monitoring the workspace camera (KVS).
            threading.Thread(target=self._publish_kvs_live, daemon=True).start()
            return
        elif action == "kvsEpisodes":
            # Per-episode on-demand HLS URLs for the current session's windows.
            threading.Thread(target=self._publish_kvs_episodes, daemon=True).start()
            return
        self._publish_status()

    def _send_control(self, cmd):
        """Send a control command to the running lerobot-record via the FIFO.
        cmd is one of: 'next' (exit_early -> next episode), 'stop'
        (stop_recording -> end session), 'rerecord'.

        lerobot only opens the FIFO for reading AFTER cameras/robot connect
        (~10-15s warmup) and re-opens it between episodes, so a single
        non-blocking write can hit ENXIO ('no reader') and be lost. To avoid
        dropping button presses we retry in a background thread until a reader
        is present (or the container is gone). Never blocks the IPC handler."""
        fifo = getattr(self, "_control_fifo_host", None)
        if not fifo:
            print(f"[CTRL] no control FIFO; ignoring {cmd!r}")
            return

        def _deliver():
            deadline = time.time() + 30.0
            attempts = 0
            # Episode-ending commands (next/stop) must NOT be delivered to a
            # <MIN_EP_SECS (possibly 0-frame) episode — lerobot then calls
            # add_episode() with no frames and crashes ("You must add ... frames
            # before add_episode", exit 133). This happens not only during
            # warmup but ALSO at an episode boundary: if Stop&Save is pressed
            # just as an episode auto-advances, the previous episode's reader is
            # gone (ENXIO) and the retry re-delivers 'next' to the FRESH next
            # episode's reader -> 0 frames -> crash. So re-check the guard
            # against the LATEST _ep_start_ts before EVERY write attempt, which
            # forces waiting until whichever episode ends up reading has been
            # recording >= MIN_EP_SECS.
            MIN_EP_SECS = 1.5
            while time.time() < deadline:
                if not (self._proc and self._proc.poll() is None):
                    print(f"[CTRL] container not running; giving up {cmd!r}")
                    return
                if cmd in ("next", "stop"):
                    ts = getattr(self, "_ep_start_ts", None)
                    if ts is None or (time.time() - ts) < MIN_EP_SECS:
                        time.sleep(0.1)  # nosemgrep: arbitrary-sleep -- intentional polling/backoff
                        continue
                if not os.path.exists(fifo):
                    time.sleep(0.3)  # nosemgrep: arbitrary-sleep -- intentional polling/backoff
                    attempts += 1
                    continue
                try:
                    fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
                    try:
                        os.write(fd, (cmd + "\n").encode())
                    finally:
                        os.close(fd)
                    suffix = f" (after {attempts} retries)" if attempts else ""
                    print(f"[CTRL] sent {cmd!r} to lerobot-record{suffix}")
                    return
                except OSError as e:
                    # ENXIO (errno 6): FIFO has no reader yet — retry shortly.
                    # (Loop top re-checks the MIN_EP_SECS guard so a boundary
                    #  re-delivery never lands on a 0-frame fresh episode.)
                    attempts += 1
                    time.sleep(0.3)  # nosemgrep: arbitrary-sleep -- intentional polling/backoff
            print(f"[CTRL] could not send {cmd!r}: no FIFO reader within 30s")

        threading.Thread(target=_deliver, daemon=True).start()

    def _start_recording(self, payload):
        if self._proc and self._proc.poll() is None:
            print(f"[REC] Already recording — ignoring start")
            return

        leader   = os.environ.get("LEADER_PORT",   "/dev/ttyACM0")
        follower = os.environ.get("FOLLOWER_PORT",  "/dev/ttyACM1")
        front    = os.environ.get("FRONT_CAMERA",   "/dev/cam_front")
        wrist    = os.environ.get("WRIST_CAMERA",   "/dev/cam_wrist")
        width    = os.environ.get("CAMERA_WIDTH",   "640")
        height   = os.environ.get("CAMERA_HEIGHT",  "480")
        fps      = os.environ.get("CAMERA_FPS",     "30")
        # Episode count drives a SINGLE persistent container that records all N
        # episodes back-to-back (lerobot handles per-episode transitions with
        # reset_time_s between them). Default to the deploy-time NUM_EPISODES.
        num_ep   = str(payload.get("numEpisodes") or os.environ.get("NUM_EPISODES", "1"))
        try:
            self.total_episodes = max(1, int(num_ep))
        except (TypeError, ValueError):
            self.total_episodes = int(os.environ.get("NUM_EPISODES", "1"))
        num_ep   = str(self.total_episodes)
        ep_time  = os.environ.get("EPISODE_LENGTH", "60")

        # New session: reset counters and stamp a fresh session/repo id keyed by
        # date + instruction so episodes land in a per-date, per-instruction folder.
        self._stopping   = False
        self.episode     = 0
        self.step        = 0
        self._log_tail   = []
        self._error      = ""
        self._ep_start_ts = None   # set when 'Recording episode' first appears
        self._episodes    = []     # reset per-episode KVS windows for new session
        ts               = int(time.time())
        date             = time.strftime("%Y-%m-%d")
        slug             = self._slug(self.lang)
        self._session_id = f"{date}_{slug}_{ts}"
        self._repo_id    = f"arobot/{self._session_id}"
        self._session_date = date
        self._session_slug = slug

        cameras_cfg = (
            f"{{front: {{type: opencv, index_or_path: {front}, "
            f"width: {width}, height: {height}, fps: {fps}}}, "
            f"wrist: {{type: opencv, index_or_path: {wrist}, "
            f"width: {width}, height: {height}, fps: {fps}}}}}"
        )

        record_args = " ".join([
            "--robot.type=so101_follower",
            f"--robot.port={follower}",
            "--robot.id=aws_so101_follower_arm",
            "--robot.calibration_dir=/root/.cache/huggingface/lerobot/calibration/robots/so_follower",
            f"'--robot.cameras={cameras_cfg}'",
            "--teleop.type=so101_leader",
            f"--teleop.port={leader}",
            "--teleop.id=aws_so101_leader_arm",
            "--teleop.calibration_dir=/root/.cache/huggingface/lerobot/calibration/teleoperators/so_leader",
            f"--dataset.repo_id={self._repo_id}",
            f"--dataset.single_task={shlex.quote(self.lang)}",
            f"--dataset.num_episodes={num_ep}",
            f"--dataset.fps={fps}",
            f"--dataset.episode_time_s={ep_time}",
            "--dataset.reset_time_s=10",
            "--dataset.video=true",
            "--dataset.push_to_hub=false",
            "--dataset.num_image_writer_threads_per_camera=4",
            "--display_data=false",
            "--play_sounds=false",
        ])

        # Remove stale container
        subprocess.run(["docker", "rm", "-f", CONTAINER_NAME],
                       capture_output=True)

        calibration_dir = os.environ.get(
            "CALIBRATION_DIR",
            "/home/arobot/.cache/huggingface/lerobot/calibration")

        # External control FIFO — placed inside DATASET_DIR (already bind-mounted
        # into the container), so no extra mount is needed. collect.py writes
        # 'next'/'stop'/'rerecord' here; the patched lerobot control_utils reads
        # it and sets exit_early / stop_recording. Recreated fresh per session.
        self._control_fifo_host = os.path.join(DATASET_DIR, ".control.fifo")
        control_fifo_container = "/root/.cache/huggingface/lerobot/.control.fifo"
        try:
            if os.path.exists(self._control_fifo_host):
                os.remove(self._control_fifo_host)
            os.mkfifo(self._control_fifo_host)
        except OSError as e:
            print(f"[CTRL] mkfifo warning: {e}")

        cmd = [
            "docker", "run", "--rm", "--runtime=nvidia", "--network=host",
            "--name", CONTAINER_NAME,
            "--entrypoint", "/bin/bash",
            "-e", f"LEROBOT_CONTROL_FIFO={control_fifo_container}",
            f"--device={leader}:{leader}",
            f"--device={follower}:{follower}",
            f"--device={front}:{front}",
            f"--device={wrist}:{wrist}",
            "-v", f"{DATASET_DIR}:/root/.cache/huggingface/lerobot",
            "-v", f"{calibration_dir}:/root/.cache/huggingface/lerobot/calibration:ro",
            DOCKER_IMAGE,
            "-c", f"/opt/gr00t-venv/bin/lerobot-record {record_args}",
        ]

        print(f"[REC] Starting session lang={self.lang!r} num_episodes={num_ep}")
        print(f"[REC] leader={leader} follower={follower} front={front} wrist={wrist}")
        print(f"[REC] repo_id={self._repo_id}")
        print(f"[REC] docker cmd: {' '.join(cmd)}")

        try:
            self._proc = subprocess.Popen(  # nosemgrep: dangerous-subprocess-use-audit -- static argv list, no shell=True
                cmd,  # nosemgrep: dangerous-subprocess-use-tainted-env-args -- argv list (no shell); the only MQTT-derived value (lang) is shlex.quoted before the container bash -c
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.state   = "recording"
            self.episode = 1   # recording the first episode; advanced by log parsing
            self._publish_status()
            print(f"[REC] Container started pid={self._proc.pid} (persistent for "
                  f"{self.total_episodes} episode(s))")

            # Stream container logs to CloudWatch
            threading.Thread(target=self._stream_logs, daemon=True).start()

            # Publish a live KVS HLS URL so the web UI can monitor the workspace.
            threading.Thread(target=self._publish_kvs_live, daemon=True).start()

        except Exception as e:
            print(f"[REC] Failed to start container: {e}")
            self.state = "error"
            self._publish_status()
            time.sleep(3)  # nosemgrep: arbitrary-sleep -- intentional polling/backoff
            self.state = "idle"
            self._publish_status()

    def _stream_logs(self):
        """Forward docker container stdout/stderr to this process stdout (CloudWatch)
        and parse lerobot-record progress so the UI tracks episode/step advance
        WITHIN the single persistent container."""
        if not self._proc:
            return
        try:
            for line in self._proc.stdout:
                line = line.rstrip()
                print(f"[DOCKER] {line}")
                self._log_tail.append(line)
                if len(self._log_tail) > 120:
                    self._log_tail = self._log_tail[-120:]
                self._parse_progress(line)
        except Exception as e:
            print(f"[DOCKER] log stream error: {e}")

    def _extract_error_detail(self):
        """Pull the most relevant error line(s) from recent container output so
        the web UI can show the actual cause (e.g. a missing motor ID) instead
        of a generic message."""
        keys = ("RuntimeError", "ValueError", "OSError", "SerialException",
                "Missing motor", "expected model", "Permission denied",
                "No such file", "could not open", "Could not open",
                "FileNotFoundError", "ConnectionError", "Error:", "Exception:")
        picked = []
        for ln in self._log_tail[-60:]:
            t = ln.strip()
            if t and any(k in t for k in keys) and t not in picked:
                picked.append(t)
        # keep the last few most-specific lines, cap length for MQTT/UI
        detail = " | ".join(picked[-4:])
        return detail[:500]

    def _parse_progress(self, line):
        """Parse lerobot-record output to advance episode within the single
        persistent container, then re-publish status for the web UI.

        Verified against real thor-001 CloudWatch logs (com.lerobot.data-collection):
            INFO 2026-05-25 06:45:48 ls/utils.py:227 Recording episode 0
            INFO ...               ls/utils.py:NNN Stop recording
            INFO ...               ls/utils.py:NNN Exiting
        lerobot's episode index is 0-BASED, so display = index + 1 (clamped to N).
        Note: lerobot also echoes its own source line
            log_say(f"Recording episode {dataset.num_episodes}", ...)
        which has no trailing digit and is safely ignored by the \\d+ match.
        There is no per-frame/step counter in lerobot's stdout, so `step` stays 0
        (episode count is the meaningful live signal)."""
        try:
            # Require the lerobot logger context to avoid matching the echoed
            # source line; fall back to a looser match if the logger prefix shifts.
            m = re.search(r"utils\.py:\d+\s+Recording episode\s+(\d+)", line) \
                or re.search(r"\bRecording episode\s+(\d+)\b", line)
            if m:
                now = time.time()
                prev_start = self._ep_start_ts
                had_prev = bool(self._episodes)
                # Close the previous episode's KVS window, open a new one.
                if self._episodes and self._episodes[-1].get("end") is None:
                    self._episodes[-1]["end"] = now
                # Mark when this episode actually began capturing frames. Used to
                # avoid delivering an early next/stop on a 0-frame episode (which
                # makes lerobot crash on add_episode).
                self._ep_start_ts = now
                ep = int(m.group(1)) + 1          # 0-based → 1-based display
                ep = max(1, min(ep, self.total_episodes))
                self._episodes.append({"episode": ep, "start": now, "end": None})
                # If this is a transition (not the very first episode), report
                # whether the previous episode ended by TIMEOUT (episode_time_s
                # elapsed) or by a MANUAL "Stop & Save". Manual = a 'next' was
                # requested during the previous episode.
                if had_prev and prev_start:
                    if self._last_manual_next >= prev_start:
                        print(f"[NEXT] episode {ep-1} saved by manual Stop&Save — advancing to episode {ep}")
                    else:
                        _lim = os.environ.get("EPISODE_LENGTH", "?")
                        print(f"[TIMEOUT] episode {ep-1} reached max time ({_lim}s) — "
                              f"auto-advancing to episode {ep}")
                # Next episode is ready and (re)recording has started (lerobot emits
                # 'Recording episode N' once per episode, after the reset gap).
                print(f"[START] episode {ep}/{self.total_episodes} ready — recording started")
                if ep != self.episode:
                    self.episode = ep
                    self.step = 0
                    self._publish_status()
                    print(f"[REC] Episode {ep}/{self.total_episodes} recording")
                return
            if re.search(r"utils\.py:\d+\s+Stop recording", line):
                print(f"[REC] lerobot: stop recording")
        except Exception:
            pass

    def _stop_recording(self, discard=False):
        """Stop the persistent container, ending the WHOLE session early.

        Unlike the old per-episode model, this stops a container that may have
        recorded several episodes already. On a normal stop we upload the entire
        dataset (all episodes captured so far); on discard we leave it on disk."""
        print(f"[REC] Stop requested discard={discard} (captured={self.episode})")
        self._stopping = True   # tell run() loop this exit is user-driven
        if not discard:
            # Immediately reflect the save phase in the UI. This runs in a
            # background thread, so docker stop (up to 120s) never blocks the
            # command handler; the UI updates via status events only.
            self.state = "saving"
            self._publish_status()
        try:
            subprocess.run(["docker", "stop", "-t", "120", CONTAINER_NAME],
                           capture_output=True, timeout=125)
            print(f"[REC] Container stopped: {CONTAINER_NAME}")
        except Exception as e:
            print(f"[REC] docker stop warning: {e}")

        if self._proc:
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

        if discard:
            self.state = "idle"
            self._publish_status()
            print(f"[REC] Session discarded")
        else:
            print(f"[REC] Session saved: {self.episode} episode(s)")
            if S3_BUCKET:
                threading.Thread(target=self._upload,
                                 args=(S3_BUCKET, S3_PREFIX), daemon=True).start()
            else:
                self.state = "idle"
                self._publish_status()

    def _upload(self, bucket, prefix):
        print(f"[Upload] Starting. episode={self.episode} repo_id={self._repo_id} bucket={bucket!r}")
        if not bucket:
            print(f"[Upload] ERROR: S3_BUCKET not set")
            return
        if not self._repo_id:
            print(f"[Upload] ERROR: no repo_id (no recording done)")
            return

        self.state = "uploading"
        self._publish_status()

        ep_dir = os.path.join(DATASET_DIR, self._repo_id)
        # Group by date/instruction: {prefix}{date}/{slug}/{session_id}/
        date    = getattr(self, "_session_date", time.strftime("%Y-%m-%d"))
        slug    = getattr(self, "_session_slug", self._slug(self.lang))
        s3_base = f"{prefix}{date}/{slug}/{self._session_id}"

        print(f"[Upload] src={ep_dir}")
        print(f"[Upload] dst=s3://{bucket}/{s3_base}/")

        if not os.path.isdir(ep_dir):
            print(f"[Upload] ERROR: source dir not found: {ep_dir}")
            # list parent dir for diagnosis
            parent = os.path.dirname(ep_dir)
            try:
                entries = os.listdir(parent) if os.path.isdir(parent) else []
                print(f"[Upload] parent dir {parent!r} contents: {entries}")
            except Exception as ex:
                print(f"[Upload] cannot list parent: {ex}")
            self.state = "error"
            self._publish_status()
            time.sleep(3)  # nosemgrep: arbitrary-sleep -- intentional polling/backoff
            self.state = "idle"
            self._publish_status()
            return

        # Verify the dataset actually contains recorded data, not just the
        # meta/ skeleton lerobot writes before recording. A crash at robot/
        # camera connect leaves only meta/info.json — skip uploading that.
        has_data = False
        for sub in ("data", "videos", "images"):
            d = os.path.join(ep_dir, sub)
            try:
                if os.path.isdir(d) and any(os.scandir(d)):
                    has_data = True
                    break
            except Exception:
                pass
        if not has_data:
            print(f"[Upload] SKIP: no recorded data (meta-only skeleton) in {ep_dir}")
            self._error = "Upload skipped: no recorded data (frames/video). Recording failed at startup."
            self.state = "error"
            self._publish_status()
            time.sleep(3)  # nosemgrep: arbitrary-sleep -- intentional polling/backoff
            self._error = ""
            self.state = "idle"
            self._publish_status()
            return

        try:
            import boto3
            s3 = self._s3()

            file_count = 0
            for root, dirs, files in os.walk(ep_dir):
                for f in files:
                    local = os.path.join(root, f)
                    rel   = os.path.relpath(local, ep_dir)
                    key   = f"{s3_base}/{rel}"
                    print(f"[Upload] uploading: {rel}")
                    s3.upload_file(local, bucket, key)
                    file_count += 1
            print(f"[Upload] Done: {file_count} files uploaded")

            # --- Episode manifest (episodes_index.json) ---------------------
            # LeRobot v3.0 aggregates multiple episodes into chunk files, so the
            # episode number is not in any filename. Write a manifest at the
            # session root that lists each episode (episode_index, length, task,
            # data/video files + frame ranges) so episodes are identifiable in
            # S3 without loading the dataset. Read from meta/episodes/*.parquet.
            try:
                import glob as _glob
                info = {}
                _ij = os.path.join(ep_dir, "meta", "info.json")
                if os.path.isfile(_ij):
                    with open(_ij, encoding="utf-8") as _f:
                        info = json.load(_f)
                episodes = []
                ep_parquets = sorted(_glob.glob(
                    os.path.join(ep_dir, "meta", "episodes", "**", "*.parquet"),
                    recursive=True))
                try:
                    import pyarrow.parquet as _pq
                    for _pf in ep_parquets:
                        episodes.extend(_pq.read_table(_pf).to_pylist())
                except Exception as _pe:
                    # Fallback: no parquet engine — at least list episode indices.
                    print(f"[Upload] manifest: parquet read unavailable ({_pe}); index-only")
                    _n = int(info.get("total_episodes") or self.episode or 0)
                    episodes = [{"episode_index": i} for i in range(_n)]
                manifest = {
                    "session_id": self._session_id,
                    "task": self.lang,
                    "date": date,
                    "codebase_version": info.get("codebase_version"),
                    "fps": info.get("fps"),
                    "total_episodes": info.get("total_episodes", len(episodes)),
                    "data_path": info.get("data_path"),
                    "video_path": info.get("video_path"),
                    "note": ("LeRobot v3.0 aggregates episodes into chunk files; "
                             "select an episode via the 'episode_index' field."),
                    "episodes": episodes,
                }
                _body = json.dumps(manifest, indent=2, ensure_ascii=False,
                                   default=str).encode()
                s3.put_object(Bucket=bucket, Key=f"{s3_base}/episodes_index.json",
                              Body=_body, ContentType="application/json")
                print(f"[Upload] episodes_index.json written ({len(episodes)} episode(s))")
            except Exception as _me:
                print(f"[Upload] manifest generation skipped: {type(_me).__name__}: {_me}")

            # Generate presigned URLs for video preview
            urls = {}
            import glob
            for cam in ["front", "wrist"]:
                vids = glob.glob(
                    f"{ep_dir}/videos/observation.images.{cam}/**/*.mp4",
                    recursive=True)
                if vids:
                    vid_key = f"{s3_base}/videos/observation.images.{cam}/chunk-000/{os.path.basename(vids[0])}"
                    urls[cam] = s3.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": bucket, "Key": vid_key},
                        ExpiresIn=900)
                    print(f"[Upload] presigned URL ({cam}): {urls[cam][:80]}...")

            if urls:
                self._publish(TOPIC_VIDEO, {"episode": self.episode, "urls": urls})

            self.state = "done"
            self._publish(TOPIC_STATUS, {
                "state": "done", "episode": self.episode,
                "totalEpisodes": self.total_episodes,
                "step": 0, "maxSteps": 0,
                "s3Path": f"s3://{bucket}/{s3_base}/",
            })
        except Exception as e:
            print(f"[Upload] ERROR: {e}")
            import traceback; traceback.print_exc()
            self.state = "error"
            self._publish_status()

        time.sleep(3)  # nosemgrep: arbitrary-sleep -- intentional polling/backoff
        self.state = "idle"
        self._publish_status()
        # Refresh the file browser for this date/instruction after uploading.
        self._list_files(bucket, prefix, date, self.lang)

    def _get_url(self, bucket, key):
        """Presign a single S3 key on demand and publish it (action:"url"), so
        the file list can omit per-file URLs and stay under the IoT 128KB limit."""
        url = None
        try:
            s3 = self._s3()
            url = s3.generate_presigned_url("get_object",
                Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)
        except Exception as e:
            print(f"[Files] geturl ERROR {key}: {e}")
        self._publish(TOPIC_FILES, {"action": "url", "key": key, "url": url})

    def _list_files(self, bucket, prefix, date, lang):
        """List local files for a date+instruction folder, marking which are
        already in S3. Publishes to TOPIC_FILES for the web file browser."""
        slug = self._slug(lang)
        print(f"[Files] list date={date} slug={slug!r} bucket={bucket!r}")
        try:
            import boto3
            s3 = self._s3()

            # 1) Collect every session dir on disk for this date+instruction.
            #    repo dirs are named arobot/{date}_{slug}_{ts}
            local_root = os.path.join(DATASET_DIR, "arobot")
            sessions = []
            if os.path.isdir(local_root):
                for d in sorted(os.listdir(local_root)):
                    if d.startswith(f"{date}_{slug}_"):
                        sessions.append(d)

            # 2) Index already-uploaded keys under this date/instruction prefix.
            s3_prefix = f"{prefix}{date}/{slug}/"
            uploaded = set()
            try:
                paginator = s3.get_paginator("list_objects_v2")
                for page in paginator.paginate(Bucket=bucket, Prefix=s3_prefix):
                    for obj in page.get("Contents", []):
                        uploaded.add(obj["Key"])
            except Exception as ex:
                print(f"[Files] S3 list warning: {ex}")

            # 3) Build a flat file list with per-file upload status + dl/play URL.
            files = []
            for sess in sessions:
                sess_dir  = os.path.join(local_root, sess)
                s3_base   = f"{prefix}{date}/{slug}/{sess}"
                for root, _dirs, fnames in os.walk(sess_dir):
                    for fn in fnames:
                        local = os.path.join(root, fn)
                        rel   = os.path.relpath(local, sess_dir)
                        key   = f"{s3_base}/{rel}"
                        is_up = key in uploaded
                        files.append({
                            "session": sess,
                            "rel": rel,
                            "key": key,
                            "size": os.path.getsize(local) if os.path.exists(local) else 0,
                            "uploaded": is_up,
                        })
            nup = sum(f['uploaded'] for f in files)
            print(f"[Files] {len(files)} file(s), {nup} uploaded")
            # No per-file presigned URLs here (fetched lazily via 'geturl' on
            # download) and the list is paginated so each MQTT message stays well
            # under the AWS IoT Core 128 KB payload limit.
            PAGE = 100
            pages = max(1, (len(files) + PAGE - 1) // PAGE)
            for pi in range(pages):
                self._publish(TOPIC_FILES, {
                    "date": date, "slug": slug, "lang": lang,
                    "bucket": bucket, "prefix": prefix,
                    "page": pi, "pages": pages, "total": len(files),
                    "files": files[pi*PAGE:(pi+1)*PAGE],
                })
        except Exception as e:
            print(f"[Files] ERROR: {e}")
            import traceback; traceback.print_exc()
            self._publish(TOPIC_FILES, {"date": date, "slug": slug, "files": [],
                                        "page": 0, "pages": 1, "error": str(e)})

    def _upload_files(self, bucket, prefix, date, lang, rel_keys):
        """Re-upload a user-selected subset of files (by their S3 key) then
        refresh the file browser."""
        slug = self._slug(lang)
        print(f"[Files] re-upload {len(rel_keys)} file(s) date={date} slug={slug!r}")
        if not bucket:
            print(f"[Files] ERROR: no bucket")
            return
        try:
            import boto3
            s3 = self._s3()
            local_root = os.path.join(DATASET_DIR, "arobot")
            done = 0
            for key in rel_keys:
                # key = {prefix}{date}/{slug}/{session}/{rel}; map back to local path
                tail = key[len(f"{prefix}{date}/{slug}/"):] if key.startswith(
                    f"{prefix}{date}/{slug}/") else None
                if not tail:
                    print(f"[Files] skip unrecognized key: {key}")
                    continue
                local = os.path.join(local_root, tail)
                if not os.path.isfile(local):
                    print(f"[Files] missing local file: {local}")
                    continue
                s3.upload_file(local, bucket, key)
                done += 1
                print(f"[Files] uploaded: {tail}")
            print(f"[Files] re-upload done: {done}/{len(rel_keys)}")
        except Exception as e:
            print(f"[Files] re-upload ERROR: {e}")
            import traceback; traceback.print_exc()
        # Refresh listing so the UI shows updated upload badges.
        self._list_files(bucket, prefix, date, lang)

    def _kvs_hls_url(self, mode, start=None, end=None, expires=3600):
        """Build a KVS HLS streaming session URL for the monitoring stream.
        mode='LIVE' for live monitoring; mode='ON_DEMAND' with start/end epoch
        seconds (producer timestamps) for per-episode playback. Uses the device
        TES role credentials (kinesisvideo:* already granted)."""
        import boto3
        kv = boto3.client("kinesisvideo", region_name=AWS_REGION)
        endpoint = kv.get_data_endpoint(
            StreamName=KVS_STREAM,
            APIName="GET_HLS_STREAMING_SESSION_URL")["DataEndpoint"]
        kva = boto3.client("kinesis-video-archived-media",
                           endpoint_url=endpoint, region_name=AWS_REGION)
        kwargs = {"StreamName": KVS_STREAM, "PlaybackMode": mode, "Expires": expires}
        if mode == "ON_DEMAND":
            kwargs["HLSFragmentSelector"] = {
                "FragmentSelectorType": "PRODUCER_TIMESTAMP",
                "TimestampRange": {"StartTimestamp": float(start),
                                   "EndTimestamp": float(end)},
            }
        return kva.get_hls_streaming_session_url(**kwargs)["HLSStreamingSessionURL"]

    def _publish_kvs_live(self):
        try:
            url = self._kvs_hls_url("LIVE", expires=3600)
            self._publish(TOPIC_KVS, {"type": "live", "stream": KVS_STREAM, "url": url})
            print(f"[KVS] live HLS url published")
        except Exception as e:
            print(f"[KVS] live url failed: {type(e).__name__}: {e}")
            self._publish(TOPIC_KVS, {"type": "live", "stream": KVS_STREAM, "error": str(e)})

    def _publish_kvs_episodes(self):
        eps = []
        for w in self._episodes:
            start = w["start"]
            end = w["end"] or time.time()
            item = {"episode": w["episode"], "start": start, "end": end}
            try:
                item["url"] = self._kvs_hls_url("ON_DEMAND", start=start, end=end, expires=3600)
            except Exception as e:
                print(f"[KVS] ep{w['episode']} url failed: {type(e).__name__}: {e}")
                item["error"] = str(e)
            eps.append(item)
        self._publish(TOPIC_KVS, {"type": "episodes", "stream": KVS_STREAM, "episodes": eps})
        print(f"[KVS] {len(eps)} episode HLS url(s) published")

    def _publish_status(self):
        status = {
            "state": self.state,
            "episode": self.episode,
            "totalEpisodes": self.total_episodes,
            "step": self.step,
            "maxSteps": int(os.environ.get("EPISODE_LENGTH", "300")),
            "langInstruction": self.lang,
            "datasetName": DATASET_NAME,
            "error": getattr(self, "_error", ""),
        }
        print(f"[STATUS] state={status['state']} episode={status['episode']}")
        self._publish(TOPIC_STATUS, status)

    def _s3(self):
        """S3 client for uploads/presign. Signs with FROZEN credentials + SigV4
        so presigned URLs aren't broken by a TES credential refresh happening
        mid-signing (which caused intermittent SignatureDoesNotMatch on
        download/play from the ap-northeast-2 bucket)."""
        import boto3
        from botocore.config import Config
        kw = {"region_name": AWS_REGION, "config": Config(signature_version="s3v4")}
        try:
            fc = boto3.Session().get_credentials().get_frozen_credentials()
            kw["aws_access_key_id"] = fc.access_key
            kw["aws_secret_access_key"] = fc.secret_key
            if fc.token:
                kw["aws_session_token"] = fc.token
        except Exception:
            pass
        return boto3.client("s3", **kw)

    def _update_episode_shadow(self):
        """Persist the LATEST session's per-episode time windows to the named
        device shadow 'episodes' (reported state). The web UI reads this to
        play each episode's window from WEBRTC_STREAM (thor-001-webrtc) via
        on-demand HLS. Overwrites every time, so only the most recent session
        is kept."""
        try:
            now = time.time()
            eps = []
            for e in self._episodes:
                if not e.get("start"):
                    continue
                end = e.get("end") or now   # finalize the trailing (last) episode
                eps.append({"episode": e.get("episode"),
                            "start": round(e["start"], 3),
                            "end": round(end, 3)})
            reported = {
                "session_id": self._session_id,
                "stream": WEBRTC_STREAM,
                "region": AWS_REGION,
                "episodes": eps,
                "total": len(eps),
                "updated": round(now, 3),
            }
            # Use the IoT Data plane REST API (boto3) rather than a
            # fire-and-forget IPC PublishToIoTCore — the latter silently
            # dropped shadow updates. This surfaces errors and reliably
            # writes the named shadow. Needs iot:UpdateThingShadow on the TES role.
            import boto3
            _c = boto3.client("iot-data", region_name=AWS_REGION)
            _c.update_thing_shadow(
                thingName=THING_NAME, shadowName="episodes",
                payload=json.dumps({"state": {"reported": reported}}).encode())
            print(f"[SHADOW] episodes updated (iot-data): session={self._session_id} n={len(eps)}")
        except Exception as e:
            print(f"[SHADOW] update error: {type(e).__name__}: {e!r}")

    def _publish(self, topic, payload):
        if not self._ipc:
            return
        try:
            from awsiot.greengrasscoreipc.model import PublishToIoTCoreRequest
            req = PublishToIoTCoreRequest(
                topic_name=topic,
                qos=self._QOS.AT_LEAST_ONCE,
                payload=json.dumps(payload).encode(),
            )
            op = self._ipc.new_publish_to_iot_core()
            op.activate(req)
            # Fire-and-forget: do NOT block on get_response().result(). Under
            # heavy recording load (docker + GPU encode + 30fps capture) the IPC
            # response could exceed the timeout and stall status updates, so the
            # web UI never saw "recording" and the stop button stayed disabled.
            # activate() hands the publish to the nucleus, which delivers it.
        except Exception as e:
            print(f"[IPC] Publish error topic={topic}: {type(e).__name__}: {e!r}")


if __name__ == "__main__":
    try:
        import awsiot.greengrasscoreipc
    except ImportError:
        print("[Init] Installing awsiotsdk + boto3...")
        subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit -- static package list
            [sys.executable, "-m", "pip", "install",
             "--break-system-packages", "-q", "awsiotsdk", "boto3"],
            check=False)

    try:
        Controller().run()
    except Exception as e:
        print(f"[FATAL] {e}")
        import traceback; traceback.print_exc()
        print("[FALLBACK] Keeping process alive...")
        while True:
            time.sleep(60)  # nosemgrep: arbitrary-sleep -- intentional polling/backoff
