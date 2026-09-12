"""Reading a device, in three tiers.

Everything here runs on the worker thread and returns plain data, so the main thread
never holds a session and never decides what a failed read means.

**Three tiers, because reads are not free.** A window meant to be left open would
otherwise hammer the control channel forever, and heavy control traffic on these
devices is — at best — unproven to be harmless. So:

``identity``
    Once per connection. The feature map, product, firmware, connection limit. None of
    it changes while connected.

``live``
    Every few seconds. Battery, charger, the noise-control flags, the codec — the
    things that genuinely move.

``slow``
    Every half-minute. Settings someone might change from the phone: battery
    protection, eco mode, prompts. Real, but not worth asking about constantly.

**Every field fails independently**, and a field that could not be read is ``None``,
which the window renders as *unavailable* rather than as zero. An unread ANC flag and
an ANC flag that is off are not the same thing, and only one of them would justify
writing to the device.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ohr import MessageType, anc, audio, battery, connections, device, features
from ohr.errors import ProtocolError
from ohr.session import Session

TIMEOUT = 5.0

#: Features whose absence hides a whole section rather than one row.
FEATURE_POWER = 3
FEATURE_GENERIC_AUDIO = 4
FEATURE_VERSIONS = 9
FEATURE_DEVICE_MANAGEMENT = 10
FEATURE_TRANSPARENCY = 12
FEATURE_ANC = 13
FEATURE_LOCAL_NAME = 20


def _read(session: Session, request, decode) -> Any:
    """Issue one read, returning ``None`` for anything that is not a clean answer.

    An error frame and a timeout collapse to the same thing here on purpose: the
    window's only question is whether it has a value to show. What the *difference*
    means — absent feature, absent operation, wedged device — matters when probing, not
    when rendering.
    """
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
class Identity:
    """What this device is. Read once per connection."""

    feature_map: features.FeatureList | None = None
    product: str | None = None
    name: str | None = None
    versions: tuple = ()
    max_connections: int | None = None
    cells: Any = None

    def has(self, feature_id: int) -> bool:
        """Whether the device advertised a feature.

        Used to decide what the window shows at all. Note this is a statement about
        *features*: a device can advertise one and still refuse individual operations
        inside it, so a row can be legitimately empty in a section that belongs here.
        """
        return bool(self.feature_map and self.feature_map.supports(feature_id))


@dataclass(frozen=True, slots=True)
class Live:
    """The things that actually move."""

    battery: Any = None
    charger: Any = None
    anc_on: bool | None = None
    transparency_on: bool | None = None
    active_level: float | None = None
    transparency_level: float | None = None
    submodes: tuple = ()
    codec: tuple | None = None

    @property
    def flags(self) -> anc.State | None:
        """The two flags as the planner wants them, or ``None`` if ANC did not answer.

        Transparency stays ``None`` when unread, because the planner treats unknown and
        off differently — and is right to.
        """
        if self.anc_on is None:
            return None
        return anc.State(anc=self.anc_on, transparency=self.transparency_on)

    @property
    def mode(self) -> anc.Mode | None:
        state = self.flags
        return state.mode if state else None


@dataclass(frozen=True, slots=True)
class Slow:
    """Settings. Someone may change these from the phone, so they are not read-once."""

    battery_protection: bool | None = None
    eco_mode: bool | None = None
    prompts: tuple | None = None
    auto_pause: tuple | None = None
    on_head_detection: bool | None = None
    peers: tuple = field(default_factory=tuple)
    own_index: int | None = None
    paired_count: int | None = None


def read_identity(session: Session) -> Identity:
    """Read everything that cannot change while the connection lasts."""
    listing = _read(session, features.request(), features.decode)
    while listing is not None and listing.more:
        nxt = _read(session, features.request_continuation(), features.decode)
        if nxt is None:
            break
        listing = features.merge(listing, nxt)

    return Identity(
        feature_map=listing,
        product=_read(session, device.request_product_name(), device.decode_product_name),
        name=_read(session, device.request_local_name(), device.decode_local_name),
        versions=_read(session, device.request_version(), device.decode_versions) or (),
        max_connections=_read(
            session,
            connections.request_max_connections(),
            lambda p: connections.decode_single_byte(p, "max connections"),
        ),
        # Hardware battery chemistry. It is not going to change while connected.
        cells=_read(session, battery.request_types(), battery.decode_types),
    )


def read_live(session: Session, identity: Identity) -> Live:
    """Read the moving parts, skipping features this device does not have."""
    power = identity.has(FEATURE_POWER)
    noise = identity.has(FEATURE_ANC)

    return Live(
        battery=_read(session, battery.request_level(), battery.decode_level) if power else None,
        charger=_read(session, battery.request_charger(), battery.decode_charger) if power else None,
        anc_on=_read(session, anc.request_enabled(), anc.decode_enabled) if noise else None,
        transparency_on=(
            _read(session, anc.request_transparency(), anc.decode_transparency)
            if identity.has(FEATURE_TRANSPARENCY) else None
        ),
        active_level=_read(session, anc.request_level(), anc.decode_level) if noise else None,
        transparency_level=(
            _read(session, anc.request_transparency_level(), anc.decode_level)
            if identity.has(FEATURE_TRANSPARENCY) else None
        ),
        submodes=(_read(session, anc.request_submodes(), anc.decode_submodes) or ()) if noise else (),
        codec=(
            _read(session, audio.request_codec(), audio.decode_codec)
            if identity.has(FEATURE_GENERIC_AUDIO) else None
        ),
    )


def read_slow(session: Session, identity: Identity) -> Slow:
    """Read settings and the peer list."""
    power = identity.has(FEATURE_POWER)
    generic = identity.has(FEATURE_GENERIC_AUDIO)

    peers: list = []
    own = count = None
    if identity.has(FEATURE_DEVICE_MANAGEMENT):
        own = _read(
            session, connections.request_own_index(),
            lambda p: connections.decode_single_byte(p, "own index"),
        )
        count = _read(
            session, connections.request_paired_count(), connections.decode_paired_count
        )
        # Walk upward until the device says the slot is empty. The count is a hint, not
        # a guarantee the indices are contiguous, so the invalid entry is what stops it.
        for index in range(count if count is not None else 0):
            peer = _read(session, connections.request_peer(index), connections.decode_peer)
            if peer is None or not peer.valid:
                break
            peers.append(peer)

    return Slow(
        battery_protection=(
            _read(session, battery.request_battery_protection(),
                  battery.decode_battery_protection) if power else None
        ),
        eco_mode=(
            _read(session, battery.request_eco_mode(), battery.decode_eco_mode)
            if power else None
        ),
        prompts=(
            _read(session, audio.request_prompts(), audio.decode_prompts)
            if generic else None
        ),
        auto_pause=(
            _read(session, anc.request_auto_pause(), anc.decode_auto_pause)
            if identity.has(FEATURE_TRANSPARENCY) else None
        ),
        on_head_detection=_read(
            session, device.request_on_head_detection(), device.decode_on_head_detection
        ),
        peers=tuple(peers),
        own_index=own,
        paired_count=count,
    )


def read_flags(session: Session) -> anc.State:
    """Just the two flags, for building a plan immediately before applying it.

    A plan built from the window's last poll is a plan built from stale state, and a
    stale plan verifies perfectly while selecting the wrong thing. So a mode change
    re-reads here, on the worker thread, in the same job that writes.
    """
    enabled = _read(session, anc.request_enabled(), anc.decode_enabled)
    if enabled is None:
        raise ProtocolError("device did not report whether ANC is on")
    return anc.State(
        anc=enabled,
        transparency=_read(session, anc.request_transparency(), anc.decode_transparency),
    )
