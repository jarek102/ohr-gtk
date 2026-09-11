"""One reading of everything the status page shows.

Runs on the worker thread and returns plain data, so the main thread never holds a
session object or decides what a failed read means.

**Every field fails independently.** A device that declines one command should cost you
that row, not the window — and a field that could not be read is ``None``, which the
page renders as *unavailable* rather than as zero. The distinction is not pedantic
here: an unread ANC flag and an ANC flag that is off look identical if you collapse
them, and one of those justifies writing to the device.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ohr import MessageType, anc, battery
from ohr.errors import ProtocolError
from ohr.session import Session

TIMEOUT = 5.0


def _read(session: Session, request, decode) -> Any:
    try:
        reply = session.request(request, timeout=TIMEOUT)
    except ProtocolError:
        return None
    if reply.type is MessageType.ERROR:
        return None
    try:
        return decode(reply.payload)
    except ProtocolError:
        return None


@dataclass(frozen=True, slots=True)
class Snapshot:
    """What the device said, this time round."""

    battery: Any = None
    charger: Any = None
    anc_on: bool | None = None
    transparency_on: bool | None = None
    active_level: float | None = None
    transparency_level: float | None = None
    submodes: tuple = ()

    @property
    def flags(self) -> anc.State | None:
        """The two flags as the planner wants them, or ``None`` if ANC did not answer.

        Transparency stays ``None`` when unread, because :func:`ohr.anc.plan_mode`
        treats unknown and off differently — and it is right to.
        """
        if self.anc_on is None:
            return None
        return anc.State(anc=self.anc_on, transparency=self.transparency_on)

    @property
    def mode(self) -> anc.Mode | None:
        state = self.flags
        return state.mode if state else None


def read(session: Session) -> Snapshot:
    """Take one full reading. Never raises; unreadable fields come back as ``None``."""
    return Snapshot(
        battery=_read(session, battery.request_level(), battery.decode_level),
        charger=_read(session, battery.request_charger(), battery.decode_charger),
        anc_on=_read(session, anc.request_enabled(), anc.decode_enabled),
        transparency_on=_read(session, anc.request_transparency(), anc.decode_transparency),
        active_level=_read(session, anc.request_level(), anc.decode_level),
        transparency_level=_read(
            session, anc.request_transparency_level(), anc.decode_level
        ),
        submodes=_read(session, anc.request_submodes(), anc.decode_submodes) or (),
    )


def read_flags(session: Session) -> anc.State:
    """Just the two flags, for building a plan immediately before applying it.

    A plan built from the status page's last poll is a plan built from stale state, and
    a stale plan verifies perfectly while selecting the wrong thing. So a mode change
    re-reads here, on the worker thread, in the same job that writes.
    """
    enabled = _read(session, anc.request_enabled(), anc.decode_enabled)
    if enabled is None:
        raise ProtocolError("device did not report whether ANC is on")
    return anc.State(
        anc=enabled,
        transparency=_read(session, anc.request_transparency(), anc.decode_transparency),
    )
