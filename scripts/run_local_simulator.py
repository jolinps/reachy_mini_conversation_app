"""Launch the official conversation app against the local MuJoCo simulator.

The application entry point has no public port or connection-mode flag.
This script builds a ReachyMini with the supported parameters and passes
it through run(..., robot=...), leaving the application itself unchanged.
"""

from __future__ import annotations

import gstreamer_libs

gstreamer_libs.setup_python_environment()

from reachy_mini import ReachyMini
from reachy_mini.media.media_manager import MediaBackend
from reachy_mini_conversation_app.main import run
from reachy_mini_conversation_app.utils import parse_args

SIM_HOST = "127.0.0.1"
SIM_PORT = 8010
SIM_NAME = "reachy-mini-sim"


def main() -> None:
    """Connect to the verified simulator, then run the official application."""
    args, _unknown = parse_args()
    args.ui = True
    args.no_camera = True

    robot = ReachyMini(
        robot_name=SIM_NAME,
        host=SIM_HOST,
        port=SIM_PORT,
        connection_mode="localhost_only",
        media_backend="default",
        timeout=8,
        automatic_body_yaw=False,
    )
    status = robot.client.get_status()
    accepted = (
        robot.connection_mode == "localhost_only"
        and robot.client.host == SIM_HOST
        and robot.client.port == SIM_PORT
        and status.robot_name == SIM_NAME
        and status.simulation_enabled
        and not status.no_media
        and robot.media.backend == MediaBackend.LOCAL
    )
    if not accepted:
        robot.media.close()
        robot.client.disconnect()
        raise SystemExit(
            "Refusing to start: the connection is not the verified local simulator "
            f"(mode={robot.connection_mode}, endpoint={robot.client.host}:{robot.client.port}, "
            f"name={status.robot_name}, simulation_enabled={status.simulation_enabled}, "
            f"no_media={status.no_media}, media={robot.media.backend})."
        )

    print(
        f"Connected {status.robot_name} at {robot.client.host}:{robot.client.port} "
        f"mode={robot.connection_mode} simulation_enabled={status.simulation_enabled} "
        f"no_media={status.no_media} media={robot.media.backend.name}",
        flush=True,
    )
    run(args, robot=robot, instance_path="/tmp/reachy-conversation-instance")


if __name__ == "__main__":
    main()
