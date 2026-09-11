"""The application object, and the small command line in front of it.

``--connect`` exists because this is a testing tool. Reaching for the same device
twenty times in a row through a dropdown is friction that discourages the testing the
window is for, and it makes a launch reproducible when something needs reporting.
"""

from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw  # noqa: E402

from .window import Window  # noqa: E402


class Application(Adw.Application):
    def __init__(self, connect: str | None = None, channel: int | None = None) -> None:
        super().__init__(application_id="io.github.jarek102.ohr")
        self._connect = connect
        self._channel = channel

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = Window(
                application=self, autoconnect=self._connect, channel=self._channel
            )
        window.present()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="ohr-gtk", description="Status, noise control and a developer panel."
    )
    parser.add_argument(
        "--connect",
        metavar="ADDRESS",
        help="open this device on start, instead of picking it from the list",
    )
    parser.add_argument(
        "--channel",
        type=int,
        help="RFCOMM channel, for a device that has not been reached before",
    )
    args, rest = parser.parse_known_args(argv[1:])
    return Application(args.connect, args.channel).run([argv[0], *rest])
