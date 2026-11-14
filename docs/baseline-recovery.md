# Simulator conversation baseline

This records the local MuJoCo conversation baseline verified on 15 November 2026. The running simulator and conversation app were left in place. A restored checkout starts a new Hugging Face session.

The annotated tag is `baseline/simulator-hf-voice-2026-11-15` on the fork `jolinps/reachy_mini_conversation_app`. Recover it with a separate worktree. Do not reset or overwrite a checkout that already contains later work.

## Startup

Quit Reachy Mini Control before starting the simulator. On this Mac that application owns `127.0.0.1:8000` and `127.0.0.1:8443`.

```shell
osascript -e 'tell application "Reachy Mini Control" to quit'
```

Confirm nothing is listening on ports 8000, 8010, or 8443, and that `/tmp/reachymini_camera_socket` is absent, before launching the daemon.

The simulator virtual environment already exists at `/Users/jl/Documents/Reachy/.venvs/reachy-mini-sim`. It is not created by this repository. Start only one media-enabled daemon. Do not pass `--no-media`. `scripts/start-mujoco-sim.sh` in the Reachy repository passes `--no-media` and cannot establish this baseline.

```shell
cd /Users/jl/Documents/Reachy
PYTHONPATH=src .venvs/reachy-mini-sim/bin/python -c '
import os
import stat
from pathlib import Path
site = Path(".venvs/reachy-mini-sim/lib/python3.11/site-packages")
hidden = getattr(stat, "UF_HIDDEN", 0)
for pth in site.glob("*.pth"):
    flags = os.lstat(pth).st_flags
    if hidden and flags & hidden:
        os.chflags(pth, flags & ~hidden)
import gstreamer_libs
gstreamer_libs.setup_python_environment()
os.environ["PYTHONUNBUFFERED"] = "1"
mjpython = str(Path(".venvs/reachy-mini-sim/bin/mjpython").resolve())
os.execv(mjpython, [mjpython, "-m", "reachy_mini.daemon.app.main", "--sim", "--fastapi-host", "127.0.0.1", "--fastapi-port", "8010", "--robot-name", "reachy-mini-sim", "--no-preload-datasets", "--dataset-update-interval", "0"])
'
```

Wait until `http://127.0.0.1:8010/api/daemon/status` reports `robot_name` `reachy-mini-sim`, `simulation_enabled` true, and `no_media` false.

Then start the application from this repository:

```shell
cd /Users/jl/Documents/reachy_mini_conversation_app
REACHY_MINI_APP_TIMEOUT_MINUTES=0 .venv/bin/python scripts/run_local_simulator.py --debug
```

`scripts/run_local_simulator.py` builds `ReachyMini(robot_name="reachy-mini-sim", host="127.0.0.1", port=8010, connection_mode="localhost_only", media_backend="default")`, requires simulation and local media, and calls the existing `run(..., robot=...)`. It enables the UI, disables the camera tool, and uses `/tmp/reachy-conversation-instance`. The official `reachy-mini-conversation-app` command has no port or connection-mode flag, so it is not the baseline entry point.

The UI is `http://127.0.0.1:7860/`.

## Dependencies

Application environment, created with `/Users/jl/.local/bin/python3.12 -m venv .venv`:

| Component | Verified version |
|---|---|
| Python | 3.12.15 |
| reachy_mini_conversation_app | 1.2.0, editable from this checkout |
| reachy-mini | 1.14.0 |
| openai | 2.28.0 |
| reachy_mini_dances_library | 0.2.1 |
| huggingface_hub | 1.33.0 |
| mcp | 2.3.0 |
| gstreamer_bundle | 1.28.3 |
| gstreamer_python and gstreamer_libs | 1.28.9 |
| numpy | 2.5.4 |
| onnxruntime | 1.27.0 |

`pyproject.toml` allows `reachy-mini>=1.11.0`. A fresh `pip install -e .` can therefore resolve a different SDK release. After installing the project, install `reachy-mini==1.14.0` so the connection behavior matches this baseline. `uv` was not installed; pip was used. The verified interpreter is macOS arm64.

Simulator environment, created with Python 3.11.15 from `/Users/jl/.local/share/uv/python/cpython-3.11.15-macos-aarch64-none`:

| Component | Verified version |
|---|---|
| Python | 3.11.15 |
| reachy-mini | 1.14.0 |
| mujoco | 3.3.0 |
| gstreamer_bundle | 1.28.3 |

The MuJoCo window is the `mjpython` trampoline in that environment. Recreating this environment is outside this repository. If `.venvs/reachy-mini-sim` is removed, the tag alone cannot rebuild it.

### GStreamer on macOS

The wheels install `gstreamer_bundle.pth` and `gstreamer_ext_runtime.pth` with the macOS hidden flag set. This Python skips hidden `.pth` files, so `import reachy_mini` fails with `No module named 'gi'` until GStreamer is put on the path.

The launcher calls `gstreamer_libs.setup_python_environment()` before importing the SDK. That is sufficient for `scripts/run_local_simulator.py`. The official console script imports the SDK immediately, so those two `.pth` files must be unhidden before using it:

```shell
chflags nohidden .venv/lib/python3.12/site-packages/gstreamer_bundle.pth .venv/lib/python3.12/site-packages/gstreamer_ext_runtime.pth
```

The first plugin scan can take about a minute. A warning that `libgstpython.dylib` cannot load `libpython3.12.dylib` was non-fatal. There is no Reachy Mini Audio device; local media uses the Mac default input and output.

The simulator process needs the same unhide and `setup_python_environment()` step in its own 3.11 environment before `mjpython` starts. That is what the startup command above does.

## Ports and sockets

One media-enabled daemon owns all of these. A second FastAPI port does not provide separate media.

| Resource | Owner for this baseline |
|---|---|
| `127.0.0.1:8010` | Simulator FastAPI daemon |
| TCP `*:8443` | That simulator's WebRTC signalling |
| UDP `5005` | That simulator's camera input |
| `/tmp/reachymini_camera_socket` | That simulator's camera IPC |
| `127.0.0.1:7860` | Conversation UI |
| `127.0.0.1:8000` | Must stay free |

`connection_mode="localhost_only"` with port 8010 and robot name `reachy-mini-sim` stays on that daemon. It does not use mDNS or the first WebRTC producer. `media_backend="default"` then selects `LOCAL` because the camera socket exists. `LOCAL` opens that socket even when the camera tool is off. A daemon reporting `no_media=true` forces the SDK to `NO_MEDIA` and cannot run this conversation.

## `prepare_simulator_python`

The conversation launcher does not import the Reachy repository. The simulator process was started with the equivalent of `prepare_simulator_python()` from `/Users/jl/Documents/Reachy/src/reachy_connector/simulation.py`.

That repository's committed HEAD is `5ddc96e05baefd8f44f6284f332002f22513c0b1` (`Checkpoint the Reachy Agent Bridge before Phase 1.1.`). `simulation.py` is an uncommitted local file. Its SHA-256 at the baseline was `fe8a64c1d9f0b1c3f3807563ec420adc83f717ec9ec234774992b68121fcec80`.

The function unhides hidden `.pth` files in the simulator site-packages and calls `gstreamer_libs.setup_python_environment()`. `simulator_command()` in the same file adds `--no-media`. The working baseline must not use that command. Use the media-enabled argument list in the startup section.

## Verified results

Checked against the live processes, without restarting them:

- The microphone path captured 16 kHz frames and Hugging Face completed a spoken transcript from `input_audio_transcription.completed`.
- Hugging Face allocated one deployed realtime session and returned spoken responses. The session identifier is not recorded here. A restored app allocates a new session.
- Assistant audio was submitted to playback. `conversation.level` events for the assistant are emitted only when samples are pushed to the player. One interrupted response pushed four frames, peak scaled level 0.598.
- The model called `move_head`. Direction `right` moved the MuJoCo head yaw from 0.000 to −0.487 radians. An earlier `move_head` left reached yaw 0.699 radians. Idle breathing later returned the head to center. These were model tool calls, not direct SDK movement.
- `conversation.interrupt` cleared the player queue, returned the UI to listening, and pushed no further speaker frames for the next four seconds. Later events for that response were discarded locally: the log has no `response completed` line and no assistant transcript after the flush.

## Limitations

Audible playback was not confirmed by a listener. Submission to the player is what was measured.

Local playback was suppressed. The realtime server still emitted `response.done` for the interrupted response, so cancellation of backend generation was not proven.

Hermes is not integrated. Cloud voice does not use a separate WebRTC transport in this baseline. WebRTC signalling on port 8443 belongs to the single simulator and was not the conversation media path.

`/tmp/reachy-conversation-instance` contained no settings, credentials, or conversation files when the baseline was saved. A private backup of that empty directory was stored outside this repository and is not listed here. A restored app creates a new Hugging Face session and does not resume the previous one.

## Recovery worktree

From a clone of the fork, with the live checkout left untouched:

```shell
git fetch origin tag baseline/simulator-hf-voice-2026-11-15
git worktree add ../reachy_mini_conversation_app-baseline baseline/simulator-hf-voice-2026-11-15
cd ../reachy_mini_conversation_app-baseline
/Users/jl/.local/bin/python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/pip install 'reachy-mini==1.14.0'
```

Recreate the application environment in that worktree. Point it at the existing simulator, or start the simulator from the command above if port 8010 is free. Do not run a second media-enabled daemon while the original simulator still owns port 8443 and the camera socket.

Subsequent application upgrades belong on the branch `upgrade/post-baseline`, which was created at this tag and was not checked out over the running tree.
