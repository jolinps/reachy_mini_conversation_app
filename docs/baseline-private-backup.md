# Private recovery backup

This note is on `upgrade/post-baseline`. The tag `baseline/simulator-hf-voice-2026-11-15` still points at `720ff5d702974448e56120b776e2c4c539088a36` and was not moved.

The private backup directory is named `simulator-hf-voice-2026-11-15`. Its absolute path is recorded only in that directory's `README.txt`. It is not a substitute for this git tag. A clean restoration from the backup into empty environments was not run.

## What the backup holds

| Path | Contents |
|---|---|
| `reachy-src/src/reachy_connector/simulation.py` | Working copy that defines `prepare_simulator_python` |
| `reachy-src/src/reachy_connector/robot.py` | Working copy imported by `simulation.py` |
| `reachy-src/src/reachy_connector/__init__.py` | Package marker |
| `reachy-src/src/reachy_hermes/__init__.py` | Package marker |
| `reachy-src/src/reachy_hermes/audio.py` | Imported by `robot.py` |
| `diffs/simulation.py.diff` | Uncommitted addition of `simulation.py` |
| `diffs/robot.py.diff` | Uncommitted diff of `robot.py` |
| `environments/conversation-pip-freeze.txt` | Application environment |
| `environments/simulator-pip-freeze.txt` | Simulator environment |
| `environments/SETUP.txt` | Python, GStreamer, and media setup for each environment |
| `launch-media-simulator.py` | Media-enabled daemon launcher |
| `CHECKSUMS.txt` | SHA-256 of the five source files |

Those five source checksums matched the files in `/Users/jl/Documents/Reachy` when the backup was verified. `simulation.py` is untracked there. Its SHA-256 is `fe8a64c1d9f0b1c3f3807563ec420adc83f717ec9ec234774992b68121fcec80`. The Reachy repository HEAD recorded with the backup is `5ddc96e05baefd8f44f6284f332002f22513c0b1`. `reachy_hermes/audio.py` and the package markers are the committed versions at that HEAD. No credentials were present in the copied sources or the freeze files.

The empty `/tmp/reachy-conversation-instance` backup from the baseline commit is still in this directory. The conversation debug log was not copied.

## Start the media-enabled simulator

Quit Reachy Mini Control, and do not run this while the existing simulator still owns ports 8010 and 8443. Use the simulator virtual environment so `prepare_simulator_python` sees that environment as `sys.prefix`:

```shell
/Users/jl/Documents/Reachy/.venvs/reachy-mini-sim/bin/python /path/to/simulator-hf-voice-2026-11-15/launch-media-simulator.py
```

The launcher loads `prepare_simulator_python` from the backed-up `simulation.py`, calls it, and replaces the process with:

```text
mjpython -m reachy_mini.daemon.app.main --sim --fastapi-host 127.0.0.1 --fastapi-port 8010 --robot-name reachy-mini-sim --no-preload-datasets --dataset-update-interval 0
```

It does not call `simulator_command()`. That function is still in the backed-up source and still includes `--no-media`. The launcher exits if its own argument list contains `--no-media`, and it exits if port 8010 or 8443 is already open.

`--print-only` loads the function and prints those arguments. It does not call the helper and does not start a daemon. That dry run was executed. `prepare_simulator_python()` itself was not called while the live daemon was running, because both would scan the same GStreamer registry.

Inside the helper, a second `.pth` search path is `parents[2] / ".venvs/reachy-mini-sim"`. From the backup layout that directory does not exist. Running the launcher with the simulator environment covers the path that does exist.

## Still required outside this backup

The GStreamer wheels and the two virtual environments are not inside the backup. Reinstalling them from the freeze files was not tested. Native GStreamer comes from those wheels: `gstreamer_bundle` 1.28.3 and the other `gstreamer_*` packages at 1.28.9, listed separately for each environment in `environments/SETUP.txt`. macOS hides `gstreamer_bundle.pth` and `gstreamer_ext_runtime.pth`, and CPython skips those files until the helper unhides them.

The conversation application source is this git tag, not the private backup. Restore it with a separate worktree, as `docs/baseline-recovery.md` describes. The application freeze still names editable commit `e901925`; the running baseline source is commit `720ff5d`.

The rest of the uncommitted Reachy tree, including the hub and Hermes adapter, is not in this backup and is not used by the media-enabled daemon command.
