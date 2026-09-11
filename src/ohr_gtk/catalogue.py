"""The command list the developer panel is built from.

One entry per thing a device can be asked. **Adding a row here is the whole job** — the
panel builds itself from this list, so a newly specified command becomes a button by
appending four fields, and removing one is a deletion rather than a hunt through
widget code.

Two properties are carried per entry rather than inferred, because getting either
wrong has a cost the UI cannot undo:

``writes``
    Whether it changes the device. Setters are visually separated and confirmed, and
    never fire by accident from a panel meant for reading.

``audible``
    Whether the wearer *hears* it — and **the two models disagree**, so this flag is
    marked true where either one makes a sound. The ANC flag plays a tone on both. The
    earbuds announce submode changes and say nothing about transparency; the over-ear
    model does the reverse, with a tone for transparency distinct from the ANC one, and
    a tone for level only at 0 and 100. Treat it as a warning, not a specification.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ohr import Frame, anc, audio, battery, connections, device, equaliser, features


@dataclass(frozen=True, slots=True)
class Entry:
    """One row in the developer panel."""

    label: str
    build: Callable[..., Frame]
    decode: Callable[[bytes], Any] | None = None
    #: Name of a single integer argument, when the command takes one.
    argument: str | None = None
    default: int = 0
    writes: bool = False
    audible: bool = False
    note: str = ""


READS: tuple[Entry, ...] = (
    Entry("Firmware version", device.request_version, device.decode_versions,
          note="Six bytes on both models: one version per three, per earbud."),
    Entry("Firmware version (wide)", device.request_version_wide, device.decode_version_wide,
          note="The same number, three 16-bit fields. A free cross-check."),
    Entry("Product name", device.request_product_name, device.decode_product_name),
    Entry("Charging-case serial", device.request_case_serial, device.decode_case_serial,
          note="Identifies one person's hardware. Shown as raw bytes; do not paste it around."),
    Entry("On-head detection", device.request_on_head_detection,
          device.decode_on_head_detection,
          note="Inverted on the wire: 0 is on. The setting, not whether it is being worn."),
    Entry("Codec", audio.request_codec, audio.decode_codec,
          note="Live state, not capability \u2014 255 with nothing playing. 240 means a call."),
    Entry("Tone and voice prompts", audio.request_prompts, audio.decode_prompts,
          note="Says whether this device will make a sound when something is written."),
    Entry("Prompt language", audio.request_prompt_language, audio.decode_prompt_language),
    Entry("Feature list", features.request, features.decode),
    Entry("Feature list continuation", features.request_continuation, features.decode,
          note="Only when the first reply sets the continuation flag."),
    Entry("Battery level", battery.request_level, battery.decode_level),
    Entry("Battery types", battery.request_types, battery.decode_types),
    Entry("Charger", battery.request_charger, battery.decode_charger,
          note="On the earbuds this is per bud, and tracks the charging case."),
    Entry("ANC enabled", anc.request_enabled, anc.decode_enabled),
    Entry("Transparency enabled", anc.request_transparency, anc.decode_transparency),
    Entry("Submodes", anc.request_submodes, anc.decode_submodes),
    Entry("Active level", anc.request_level, anc.decode_level,
          note="Depends on the flags: the ANC level, or full scale with transparency on."),
    Entry("Transparency level", anc.request_transparency_level, anc.decode_level,
          note="Unaffected by the mode. The level read that means one thing."),
    Entry("EQ mode", equaliser.request_mode, equaliser.decode_mode),
    Entry("EQ configuration", equaliser.request_configuration, equaliser.decode_configuration),
    Entry("EQ band gain", equaliser.request_band_gain, equaliser.decode_band_gain,
          argument="band", default=0),
    Entry("Bass boost", equaliser.request_bass_boost, equaliser.decode_bass_boost),
    Entry("Paired count", connections.request_paired_count, connections.decode_paired_count),
    Entry("Peer details", connections.request_peer, connections.decode_peer,
          argument="index", default=0),
    Entry("Own index", connections.request_own_index,
          lambda p: connections.decode_single_byte(p, "own index")),
    Entry("Max connections", connections.request_max_connections,
          lambda p: connections.decode_single_byte(p, "max connections")),
)

WRITES: tuple[Entry, ...] = (
    Entry("ANC on", lambda: anc.request_set_enabled(True), writes=True, audible=True,
          note="Also clears transparency."),
    Entry("ANC off", lambda: anc.request_set_enabled(False), writes=True, audible=True),
    Entry("Transparency on", lambda: anc.request_set_transparency(True), writes=True,
          audible=True, note="Audible on the over-ear model, silent on the earbuds."),
    Entry("Transparency off", lambda: anc.request_set_transparency(False), writes=True,
          audible=True),
    Entry("Set level", lambda pct: anc.request_set_level(pct / 100), writes=True,
          audible=True, argument="percent", default=50,
          note="Clears transparency and leaves ANC alone: from transparency you land on "
               "off, from both-on you land on ANC."),
    Entry("Set anti-wind", lambda state: anc.request_set_submode(1, state), writes=True,
          audible=True, argument="state", default=0,
          note="Takes more than two values; 0 and 1 are the confirmed ones."),
    Entry("Set comfort", lambda state: anc.request_set_submode(2, state), writes=True,
          audible=True, argument="state", default=0),
    Entry("Set adaptive", lambda state: anc.request_set_submode(3, state), writes=True,
          audible=True, argument="state", default=0),
)

#: Feature 10 operation 5 deletes a pairing. It is not in the lists above and the raw
#: sender refuses it: there is no undo, and the device it would most plausibly be
#: aimed at by accident is the one holding the control channel.
FORBIDDEN: dict[tuple[int, int], str] = {
    (10, 5): "deletes a pairing — there is no undo",
}


def forbidden_reason(feature: int, operation: int) -> str | None:
    return FORBIDDEN.get((feature, operation))
