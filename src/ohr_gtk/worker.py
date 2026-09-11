"""Everything that touches Bluetooth, on one background thread.

The rule this module exists to enforce: **no ohr call ever runs on the GTK main
thread.** A control-channel exchange takes anywhere from a few milliseconds to a
five-second timeout, and one of the devices here is known to time out on a first
attempt and answer a retry. A frozen window during that is not a cosmetic problem — it
is the difference between a tool you reach for and one you avoid.

So the thread owns three things for the lifetime of a connection: the lease, the
socket, and the :class:`ohr.session.Session`. Work arrives as callables on a queue and
results go back to the main thread through ``GLib.idle_add``. Widgets are touched only
there.

The lease matters as much as the socket. Holding it means the ``ohr`` command line
cannot talk to the same device — that is the point of a lease, not a bug — so the
window makes disconnecting an obvious, one-click thing rather than something you have
to close the application to do.
"""

from __future__ import annotations

import queue
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from gi.repository import GLib

from ohr.errors import ProtocolError
from ohr.session import Session


@dataclass(frozen=True, slots=True)
class Failure:
    """A job that raised, carried back as a value rather than thrown at a callback.

    Kept as data because most failures here are *expected*: the vendor application
    contends for the same channel, devices time out, leases are held elsewhere. A UI
    should render those as state, so they must survive the trip to the main thread.
    """

    label: str
    error: str
    detail: str = ""


class Worker:
    """A serial queue of device operations, running off the main thread."""

    def __init__(self) -> None:
        self._jobs: queue.Queue[tuple[str, Callable[[Session], Any], Callable[[Any], None]] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="ohr-worker", daemon=True)
        self._session: Session | None = None
        self._connection = None
        self._lease = None
        self._address: str | None = None
        self._thread.start()

    # --- main thread ---------------------------------------------------------

    @property
    def address(self) -> str | None:
        """The device currently held, or ``None``. Safe to read from either thread."""
        return self._address

    def open(self, address: str, channel: int | None, done: Callable[[Any], None]) -> None:
        self._jobs.put(("open", lambda _: self._open(address, channel), done))

    def close(self, done: Callable[[Any], None] = lambda _: None) -> None:
        self._jobs.put(("close", lambda _: self._close(), done))

    def submit(self, label: str, job: Callable[[Session], Any], done: Callable[[Any], None]) -> None:
        """Run ``job`` with the open session, then hand the result to ``done``.

        ``done`` is always called, on the main thread, with either the return value or
        a :class:`Failure`. A job that silently never reports back would leave whatever
        spinner it started running for ever.
        """
        self._jobs.put((label, job, done))

    def shutdown(self) -> None:
        self._jobs.put(None)

    # --- worker thread -------------------------------------------------------

    def _run(self) -> None:
        while True:
            item = self._jobs.get()
            if item is None:
                self._close()
                return
            label, job, done = item
            try:
                if label not in ("open", "close") and self._session is None:
                    result: Any = Failure(label, "not connected")
                else:
                    result = job(self._session)
            except ProtocolError as exc:
                result = Failure(label, f"{type(exc).__name__}: {exc}")
            except Exception as exc:  # noqa: BLE001 - a worker thread must not die
                # An unexpected error here would otherwise take the thread with it and
                # leave every later job queued for ever, with no visible cause.
                result = Failure(label, f"{type(exc).__name__}: {exc}", traceback.format_exc())
            GLib.idle_add(self._deliver, done, result)

    @staticmethod
    def _deliver(done: Callable[[Any], None], result: Any) -> bool:
        done(result)
        return GLib.SOURCE_REMOVE

    def _open(self, address: str, channel: int | None) -> str:
        from ohr.linux import FlockLease, LinuxTransport

        self._close()
        self._lease = FlockLease().acquire(address, timeout=2.0)
        try:
            self._connection = LinuxTransport().connect(address, timeout=5.0, channel=channel)
        except Exception:
            # Never keep a lease for a connection that did not open — it would lock
            # every other client out of a device this process is not even talking to.
            self._lease.release()
            self._lease = None
            raise
        self._session = Session(self._connection)
        self._address = address
        return f"channel {self._connection.channel}"

    def _close(self) -> str:
        for resource, release in ((self._connection, "close"), (self._lease, "release")):
            if resource is not None:
                try:
                    getattr(resource, release)()
                except Exception:  # noqa: BLE001
                    pass  # Releasing must not fail; the process may be exiting.
        self._connection = self._lease = self._session = None
        self._address = None
        return "disconnected"
