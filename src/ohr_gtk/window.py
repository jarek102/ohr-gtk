"""The window: status, noise control, and a developer panel.

Four things the layout is trying to say.

**Connection is a state, not an error.** Nothing connected, another client holding the
lease, a device that will not answer — these are the normal cases for a headset, and
each gets a screen rather than a toast over an empty one.

**Nothing is claimed that was not read.** Unavailable fields say so, and the page is
timestamped, because a device has been seen to accept a setting and abandon it a second
later. What is shown is the last poll, not a model of the device.

**The device decides the layout.** Sections are built from the feature map the headset
reports at connect. The two models here differ by six features, and a row that reads
*unavailable* forever on one of them is worse than no row.

**Writes are separated from reads**, because some of them are audible in the wearer's
ears and none of them can be taken back by asking nicely.
"""

from __future__ import annotations

import time
from typing import Any

from gi.repository import Adw, GLib, Gtk

from ohr import Frame, MessageType, VENDOR_SENNHEISER, anc, control
from ohr.transport import SERVICE_UUID

from . import catalogue, snapshot
from .worker import Failure, Worker

LIVE_SECONDS = 3
SLOW_EVERY = 10  # ticks, so half a minute between settings reads


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


#: Wire labels are precise and unfriendly. The status page is read by a person; the
#: developer panel below it still shows the raw byte, so nothing is lost by being
#: legible here.
READABLE = {
    "keep_playing": "music keeps playing",
    "stop_while_active": "music stops",
    "tones_and_voice": "tones and voice",
    "tones_only": "tones only",
    "off": "silent",
    "no_stream": "nothing playing",
    "call_ongoing": "call in progress",
    "unknown": "device does not know",
    "aptx": "aptX",
    "aptx_hd": "aptX HD",
    "aptx_ll": "aptX Low Latency",
    "aptx_adaptive": "aptX Adaptive",
    "aptx_lossless": "aptX Lossless",
    "aptx_voice": "aptX Voice",
    "aptx_lite": "aptX Lite",
    "sbc": "SBC",
    "aac": "AAC",
    "mp3": "MP3",
    "lhdc": "LHDC",
    "lc3": "LC3",
    "lc3_gaming": "LC3 gaming",
    "faststream": "FastStream",
    "usb": "USB",
}


def _readable(pair: tuple | None) -> str:
    """Render a ``(value, name)`` decode for a human, keeping the number when unnamed."""
    if pair is None:
        return "—"
    value, name = pair
    if name is None:
        return f"unrecognised ({value})"
    return READABLE.get(name, name.replace("_", " "))


#: How the peer link state reads to someone who is not holding the specification.
LINK_STATES = {
    "disconnected": "paired, not connected",
    "classic": "connected",
    "ble": "connected over BLE",
    "both": "connected, Classic and BLE",
}


def _flag(value: bool | None) -> str:
    return "unavailable" if value is None else ("on" if value else "off")


class Window(Adw.ApplicationWindow):
    def __init__(
        self, autoconnect: str | None = None, channel: int | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.set_title("ohr")
        self.set_default_size(620, 900)

        self._worker = Worker()
        self._devices: list = []
        self._identity = snapshot.Identity()
        self._live = snapshot.Live()
        self._slow = snapshot.Slow()
        self._busy = False
        self._ticks = 0
        self._last_read: str | None = None
        self._log_lines: list[str] = []

        self._toasts = Adw.ToastOverlay()
        self.set_content(self._toasts)

        self._stack = Adw.ViewStack()
        root = Adw.ToolbarView()
        root.add_top_bar(self._build_header())
        root.set_content(self._stack)
        self._toasts.set_child(root)

        self._stack.add_named(self._build_placeholder(), "nothing")
        self._stack.add_named(self._build_status_page(), "status")

        self._set_connected(False)
        self._refresh_devices()
        GLib.timeout_add_seconds(LIVE_SECONDS, self._on_tick)
        self.connect("close-request", self._on_close)

        if autoconnect:
            self._select(autoconnect)
            if channel:
                self._channel_row.set_value(channel)
            GLib.idle_add(self._on_connect_clicked, None)

    # --- chrome --------------------------------------------------------------

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()

        self._connect_button = Gtk.Button(label="Connect")
        self._connect_button.add_css_class("suggested-action")
        self._connect_button.connect("clicked", self._on_connect_clicked)
        header.pack_start(self._connect_button)

        self._device_button = Gtk.MenuButton(icon_name="bluetooth-symbolic")
        self._device_button.set_tooltip_text("Choose a headset")
        self._device_popover = Gtk.Popover()
        self._device_button.set_popover(self._device_popover)
        header.pack_start(self._device_button)

        self._spinner = Adw.Spinner()
        self._spinner.set_visible(False)
        header.pack_end(self._spinner)

        rescan = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan")
        rescan.connect("clicked", lambda _b: self._refresh_devices())
        header.pack_end(rescan)
        return header

    def _build_placeholder(self) -> Gtk.Widget:
        """The screen for having nothing to show, which is a normal state here."""
        self._placeholder = Adw.StatusPage(
            icon_name="audio-headphones-symbolic",
            title="No headset connected",
            description=(
                "Pair and connect a headset from your Bluetooth settings first — this "
                "application never brings up a link, it only talks to one that exists."
            ),
        )
        return self._placeholder

    # --- status page ---------------------------------------------------------

    def _build_status_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        self._rows: dict[str, Adw.ActionRow] = {}
        self._groups: dict[str, Adw.PreferencesGroup] = {}

        page.add(self._build_connection_group())
        page.add(self._build_charge_group())
        page.add(self._build_noise_group())
        page.add(self._build_peers_group())
        page.add(self._build_developer_group())
        return page

    def _value_row(self, group: Adw.PreferencesGroup, key: str, title: str,
                   subtitle: str = "") -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=subtitle or None)
        label = Gtk.Label(label="—", xalign=1, selectable=True)
        label.add_css_class("dim-label")
        row.add_suffix(label)
        row.value_label = label  # type: ignore[attr-defined]
        self._rows[key] = row
        group.add(row)
        return row

    def _build_connection_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup()
        self._groups["connection"] = group

        self._headline = Adw.ActionRow(title="—", subtitle="not connected")
        group.add(self._headline)

        self._channel_row = Adw.SpinRow.new_with_range(0, 30, 1)
        self._channel_row.set_title("RFCOMM channel")
        self._channel_row.set_subtitle(
            "0 to resolve normally. Service discovery answers with nothing on these "
            "devices, so a channel given once is remembered."
        )
        group.add(self._channel_row)

        self._poll_row = Adw.SwitchRow(
            title="Keep polling",
            subtitle="off freezes the values below; nothing is sent until you act",
        )
        self._poll_row.set_active(True)
        group.add(self._poll_row)

        self._value_row(group, "polled", "Last read",
                        "a report of the last poll, not a live model")
        return group

    def _build_charge_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Charge and health")
        self._groups["charge"] = group
        self._value_row(group, "battery", "Battery",
                        "per cell; a level does not mean that earbud joined")
        self._value_row(group, "charger", "Charger",
                        "on earbuds, per bud — this is what tracks the case")
        self._value_row(group, "protection", "Battery protection",
                        "when on, the device never charges to 100%")
        self._value_row(group, "eco", "Eco mode")
        self._value_row(group, "cells", "Cell type")
        return group

    def _build_noise_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Noise control",
            description=(
                "Each mode is up to two writes, verified by reading back. A mode "
                "already selected writes nothing — a redundant ANC write is a tone in "
                "the wearer's ears."
            ),
        )
        self._groups["noise"] = group

        row = Adw.ActionRow(title="Mode")
        box = Gtk.Box(spacing=0, valign=Gtk.Align.CENTER)
        box.add_css_class("linked")
        self._mode_buttons: dict[anc.Mode, Gtk.Button] = {}
        for mode, label in (
            (anc.Mode.ANC, "ANC"),
            (anc.Mode.OFF, "Off"),
            (anc.Mode.TRANSPARENCY, "Transparency"),
        ):
            button = Gtk.Button(label=label)
            button.connect("clicked", self._on_mode_clicked, mode)
            box.append(button)
            self._mode_buttons[mode] = button
        row.add_suffix(box)
        group.add(row)

        self._value_row(group, "flags", "ANC / transparency", "both can be set at once")
        self._value_row(group, "levels", "Levels",
                        "active, and the stored transparency level")
        self._value_row(group, "submodes", "Submodes",
                        "states are raw; ranges differ per submode")
        self._value_row(group, "autopause", "Auto-pause",
                        "what transparency does to whatever is playing")
        self._value_row(group, "codec", "Codec",
                        "live state — nothing playing reads as no stream")
        self._value_row(group, "prompts", "Prompts",
                        "whether this device makes a sound when something is written")
        return group

    def _build_peers_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Connections",
            description="Which devices the headset is paired to, and which hold it now.",
        )
        self._groups["peers"] = group
        self._value_row(group, "slots", "Slots",
                        "peers routinely outnumber the connections available")
        self._peer_rows: list[Adw.ActionRow] = []
        return group

    # --- developer -----------------------------------------------------------

    def _build_developer_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Developer",
            description="Every command the library knows. Built from catalogue.py — "
                        "add a row there and it appears here.",
        )

        reads = Adw.ExpanderRow(title="Reads", subtitle="non-mutating")
        for entry in catalogue.READS:
            reads.add_row(self._command_row(entry))
        group.add(reads)

        writes = Adw.ExpanderRow(
            title="Writes", subtitle="change the device; some are audible"
        )
        for entry in catalogue.WRITES:
            writes.add_row(self._command_row(entry))
        group.add(writes)

        group.add(self._build_raw_row())

        log = Adw.ExpanderRow(title="Log", subtitle="frames, newest first")
        self._log_label = Gtk.Label(
            label="nothing yet", xalign=0, selectable=True, wrap=True,
            margin_top=8, margin_bottom=8, margin_start=12, margin_end=12,
        )
        self._log_label.add_css_class("monospace")
        self._log_label.add_css_class("dim-label")
        log.add_row(self._log_label)
        group.add(log)
        return group

    def _command_row(self, entry: catalogue.Entry) -> Adw.ActionRow:
        subtitle = entry.note
        if entry.audible:
            subtitle = ("may be audible to the wearer. " + subtitle).strip()
        row = Adw.ActionRow(title=entry.label, subtitle=subtitle or None)

        argument = None
        if entry.argument:
            argument = Gtk.SpinButton.new_with_range(0, 255, 1)
            argument.set_value(entry.default)
            argument.set_valign(Gtk.Align.CENTER)
            argument.set_tooltip_text(entry.argument)
            row.add_suffix(argument)

        send = Gtk.Button(label="Send", valign=Gtk.Align.CENTER)
        if entry.writes:
            send.add_css_class("destructive-action" if entry.audible else "suggested-action")
        send.connect("clicked", self._on_command_clicked, entry, argument)
        row.add_suffix(send)
        return row

    def _build_raw_row(self) -> Adw.ExpanderRow:
        row = Adw.ExpanderRow(
            title="Raw frame",
            subtitle="for probing features this library does not specify",
        )
        self._raw = {}
        for key, title, upper, default in (
            ("feature", "Feature", 127, 9),
            ("operation", "Operation", 127, 2),
        ):
            spin = Adw.SpinRow.new_with_range(0, upper, 1)
            spin.set_title(title)
            spin.set_value(default)
            row.add_row(spin)
            self._raw[key] = spin

        payload = Adw.EntryRow(title="Payload (hex)")
        row.add_row(payload)
        self._raw["payload"] = payload

        hint = Adw.ActionRow(
            title="Parity does not tell you what is safe",
            subtitle=(
                "Setters sit one below their getters on features 12 and 13 only. "
                "Feature 3 reads at operations 2 and 14, and feature 10 disconnects at "
                "operation 3. An error reply carries a reason byte: 0 the feature is "
                "absent, 1 the operation is."
            ),
        )
        hint.add_css_class("dim-label")
        row.add_row(hint)

        send_row = Adw.ActionRow(title="Send")
        button = Gtk.Button(label="Send frame", valign=Gtk.Align.CENTER)
        button.connect("clicked", self._on_raw_clicked)
        send_row.add_suffix(button)
        row.add_row(send_row)
        return row

    # --- devices -------------------------------------------------------------

    def _refresh_devices(self) -> None:
        try:
            from ohr.linux import LinuxTransport

            self._devices = LinuxTransport().list_devices(service_uuid=SERVICE_UUID)
        except Exception as exc:  # noqa: BLE001
            self._devices = []
            self._toast(f"Could not list devices: {exc}")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        if not self._devices:
            box.append(Gtk.Label(label="Nothing advertising the service", margin_end=6))
        self._selected_index = 0
        for index, dev in enumerate(self._devices):
            state = "connected" if dev.connected else "disconnected"
            button = Gtk.Button(label=f"{dev.name}  ·  {state}", has_frame=False)
            button.get_child().set_xalign(0)
            button.connect("clicked", self._on_device_chosen, index)
            box.append(button)
        self._device_popover.set_child(box)

        self._device_button.set_sensitive(bool(self._devices))
        self._connect_button.set_sensitive(
            bool(self._devices) or self._worker.address is not None
        )
        if not self._worker.address:
            self._show_placeholder()

    def _on_device_chosen(self, _button: Gtk.Button, index: int) -> None:
        self._selected_index = index
        self._device_popover.popdown()
        self._headline.set_title(self._devices[index].name)
        self._on_connect_clicked(None)

    def _selected(self):
        i = getattr(self, "_selected_index", 0)
        return self._devices[i] if 0 <= i < len(self._devices) else None

    def _select(self, address: str) -> None:
        for index, dev in enumerate(self._devices):
            if dev.address.lower() == address.lower():
                self._selected_index = index
                return
        self._toast(f"{address} is not in the list — is it connected?")

    def _show_placeholder(self, title: str = "No headset connected",
                          description: str | None = None) -> None:
        self._placeholder.set_title(title)
        if description is not None:
            self._placeholder.set_description(description)
        self._stack.set_visible_child_name("nothing")

    # --- actions -------------------------------------------------------------

    def _on_connect_clicked(self, _button: Gtk.Button | None) -> None:
        if self._worker.address:
            self._worker.close(self._on_closed)
            return
        dev = self._selected()
        if dev is None:
            return
        channel = int(self._channel_row.get_value()) or None
        self._begin("connecting")
        self._worker.open(dev.address, channel, self._on_opened)

    def _on_opened(self, result: Any) -> None:
        self._end()
        if isinstance(result, Failure):
            # Contention is the ordinary case, not a crash: the vendor application is a
            # peer on this channel and cannot be coordinated with.
            busy = "busy" in result.error.lower() or "DeviceBusy" in result.error
            self._show_placeholder(
                "Headset is busy" if busy else "Could not connect",
                result.error + ("\n\nAnother client holds the control channel. Only one "
                                "can, by design." if busy else ""),
            )
            self._log(f"connect failed: {result.error}")
            return
        self._set_connected(True)
        self._stack.set_visible_child_name("status")
        self._log(f"connected: {result}")
        self._ticks = 0
        self._worker.submit("identity", snapshot.read_identity, self._on_identity)

    def _on_closed(self, _result: Any) -> None:
        self._set_connected(False)
        self._identity = snapshot.Identity()
        self._live = snapshot.Live()
        self._slow = snapshot.Slow()
        self._last_read = None
        self._show_placeholder()
        self._log("disconnected — the lease is free for other clients")

    def _on_identity(self, result: Any) -> None:
        if isinstance(result, Failure):
            self._toast(result.error)
            return
        self._identity = result
        self._apply_capabilities()
        self._poll_live()
        self._poll_slow()

    def _on_mode_clicked(self, _button: Gtk.Button, mode: anc.Mode) -> None:
        if not self._ready():
            return

        def job(session):
            # Read the flags in the same job that writes them. A plan built from the
            # page's last poll would verify perfectly and select the wrong mode.
            plan = anc.plan_mode(mode, snapshot.read_flags(session))
            if not plan:
                return "already"
            return control.apply(session, plan)

        self._begin(f"selecting {mode.value}")
        self._worker.submit(f"mode {mode.value}", job, self._on_mode_done)

    def _on_mode_done(self, result: Any) -> None:
        self._end()
        if isinstance(result, Failure):
            self._toast(result.error)
            self._log(f"mode failed: {result.error}")
        elif result == "already":
            self._toast("Already in that mode — nothing written")
            self._log("mode: already there, no writes sent")
        else:
            for outcome in result.outcomes:
                self._log(("ok   " if outcome.ok else "FAIL ") + outcome.describe())
            if not result.complete:
                failed = result.failed
                self._toast(f"Partly applied: {failed.describe() if failed else 'unknown'}")
        self._poll_live()

    def _on_command_clicked(
        self, _button: Gtk.Button, entry: catalogue.Entry, argument: Gtk.SpinButton | None
    ) -> None:
        if not self._ready():
            return
        value = int(argument.get_value()) if argument is not None else None
        try:
            request = entry.build(value) if entry.argument else entry.build()
        except Exception as exc:  # noqa: BLE001
            self._toast(f"{entry.label}: {exc}")
            return
        self._send(entry.label, request, entry.decode, refresh=entry.writes)

    def _on_raw_clicked(self, _button: Gtk.Button) -> None:
        if not self._ready():
            return
        feature = int(self._raw["feature"].get_value())
        operation = int(self._raw["operation"].get_value())

        reason = catalogue.forbidden_reason(feature, operation)
        if reason:
            self._toast(f"Refused: feature {feature} operation {operation} {reason}")
            self._log(f"refused {feature:02x}/{operation:02x}: {reason}")
            return

        try:
            payload = bytes.fromhex(self._raw["payload"].get_text().replace(" ", ""))
        except ValueError:
            self._toast("Payload is not hex")
            return

        request = Frame(VENDOR_SENNHEISER, feature, MessageType.COMMAND, operation, payload)
        self._send(f"raw {request.word:04x}", request, None, refresh=True)

    def _send(self, label: str, request: Frame, decode, *, refresh: bool) -> None:
        self._begin(label)
        self._log(f"→ {label}: {request.to_bytes().hex()}")

        def done(result: Any) -> None:
            self._end()
            if isinstance(result, Failure):
                self._log(f"← {label}: {result.error}")
                self._toast(result.error)
                return
            line = f"← {label}: {result.to_bytes().hex()}"
            if result.type is MessageType.ERROR:
                from ohr.frame import decode_error_reason

                try:
                    value, name = decode_error_reason(result.payload)
                    line += f"   [error {value}: {name or 'unknown reason'}]"
                except Exception:  # noqa: BLE001
                    line += "   [error frame]"
            elif decode is not None:
                try:
                    line += f"   {decode(result.payload)!r}"
                except Exception as exc:  # noqa: BLE001
                    line += f"   [did not decode: {exc}]"
            self._log(line)
            if refresh:
                self._poll_live()

        self._worker.submit(
            label, lambda session: session.request(request, timeout=snapshot.TIMEOUT), done
        )

    # --- polling -------------------------------------------------------------

    def _on_tick(self) -> bool:
        if self._worker.address and not self._busy and self._poll_row.get_active():
            self._ticks += 1
            self._poll_live()
            if self._ticks % SLOW_EVERY == 0:
                self._poll_slow()
        return GLib.SOURCE_CONTINUE

    def _poll_live(self) -> None:
        if self._worker.address:
            self._worker.submit(
                "live", lambda s: snapshot.read_live(s, self._identity), self._on_live
            )

    def _poll_slow(self) -> None:
        if self._worker.address:
            self._worker.submit(
                "slow", lambda s: snapshot.read_slow(s, self._identity), self._on_slow
            )

    def _on_live(self, result: Any) -> None:
        if isinstance(result, Failure):
            self._rows["polled"].value_label.set_label("read failed")  # type: ignore[attr-defined]
            return
        self._live = result
        self._last_read = time.strftime("%H:%M:%S")
        self._render_live()

    def _on_slow(self, result: Any) -> None:
        if isinstance(result, Failure):
            return
        self._slow = result
        self._render_slow()

    # --- rendering -----------------------------------------------------------

    def _apply_capabilities(self) -> None:
        """Show only the sections this headset actually has.

        A row that reads *unavailable* forever is worse than no row: it invites the
        reader to wonder what is broken. The two models here differ by six features, so
        this is not hypothetical.
        """
        identity = self._identity
        self._groups["charge"].set_visible(identity.has(snapshot.FEATURE_POWER))
        self._groups["noise"].set_visible(identity.has(snapshot.FEATURE_ANC))
        self._groups["peers"].set_visible(
            identity.has(snapshot.FEATURE_DEVICE_MANAGEMENT)
        )

        name = identity.name or identity.product or (self._selected().name if self._selected() else "—")
        self._headline.set_title(name)

        bits = []
        if identity.product and identity.product != name:
            bits.append(identity.product)
        versions = [str(v) for v in identity.versions if (v.major or v.minor or v.patch)]
        if versions:
            # The earbuds report one version per bud; showing both only helps when they
            # differ, which is the case worth noticing.
            bits.append("firmware " + (versions[0] if len(set(versions)) == 1
                                       else " / ".join(versions)))
        if identity.feature_map:
            bits.append(f"{len(identity.feature_map.features)} features")

        cells = identity.cells
        self._rows["cells"].value_label.set_label(  # type: ignore[attr-defined]
            "—" if cells is None else f"{cells.first} / {cells.second}"
        )
        self._headline.set_subtitle("  ·  ".join(bits) or "connected")

    def _render_live(self) -> None:
        live = self._live
        rows = self._rows

        if live.battery is None:
            text = "—"
        elif live.battery.shape == "scalar":
            text = _percent(live.battery.level)
        else:
            text = (f"L {_percent(live.battery.left)}   R {_percent(live.battery.right)}"
                    f"   case {_percent(live.battery.case)}")
        rows["battery"].value_label.set_label(text)  # type: ignore[attr-defined]

        charger = "—" if live.charger is None else f"{live.charger.first} / {live.charger.second}"
        rows["charger"].value_label.set_label(charger)  # type: ignore[attr-defined]

        rows["flags"].value_label.set_label(  # type: ignore[attr-defined]
            f"ANC {_flag(live.anc_on)}   ·   transparency {_flag(live.transparency_on)}"
        )
        rows["levels"].value_label.set_label(  # type: ignore[attr-defined]
            f"{_percent(live.active_level)} active   ·   "
            f"{_percent(live.transparency_level)} transparency"
        )
        rows["submodes"].value_label.set_label(  # type: ignore[attr-defined]
            "   ".join(f"{m.name or m.id} {m.state}" for m in live.submodes) or "—"
        )
        rows["codec"].value_label.set_label(_readable(live.codec))  # type: ignore[attr-defined]
        rows["polled"].value_label.set_label(self._last_read or "never")  # type: ignore[attr-defined]

        for target, button in self._mode_buttons.items():
            button.set_css_classes(["suggested-action"] if live.mode is target else [])

    def _render_slow(self) -> None:
        slow = self._slow
        rows = self._rows
        rows["protection"].value_label.set_label(_flag(slow.battery_protection))  # type: ignore[attr-defined]
        rows["eco"].value_label.set_label(_flag(slow.eco_mode))  # type: ignore[attr-defined]
        rows["prompts"].value_label.set_label(_readable(slow.prompts))  # type: ignore[attr-defined]
        rows["autopause"].value_label.set_label(_readable(slow.auto_pause))  # type: ignore[attr-defined]

        used = len(slow.peers)
        maximum = self._identity.max_connections
        rows["slots"].value_label.set_label(  # type: ignore[attr-defined]
            "—" if maximum is None else
            f"{slow.paired_count if slow.paired_count is not None else used} paired"
            f"   ·   {maximum} at once"
        )

        for row in self._peer_rows:
            self._groups["peers"].remove(row)
        self._peer_rows = []
        for peer in slow.peers:
            here = peer.index == slow.own_index
            title = peer.name or f"peer {peer.index}"
            if here:
                title += "  (this computer)"

            detail = LINK_STATES.get(peer.link, peer.link or "unknown")
            if peer.no_classic_pairing:
                # A pairing fact, not an activity one — the link state above is
                # unaffected by it, and conflating them misreports the connection.
                detail += ", not paired over Classic"
            row = Adw.ActionRow(title=title, subtitle=detail)

            if peer.link and peer.link != "disconnected":
                badge = Gtk.Label(label="holding a slot")
                badge.add_css_class("dim-label")
                row.add_suffix(badge)
            elif here:
                # We are talking to it right now, so a disconnected reading for our own
                # index is the device describing audio, not this control channel.
                badge = Gtk.Label(label="control only")
                badge.add_css_class("dim-label")
                row.add_suffix(badge)

            self._groups["peers"].add(row)
            self._peer_rows.append(row)

    # --- small helpers -------------------------------------------------------

    def _ready(self) -> bool:
        if self._worker.address:
            return True
        self._toast("Not connected")
        return False

    def _set_connected(self, connected: bool) -> None:
        self._connect_button.set_label("Disconnect" if connected else "Connect")
        self._connect_button.set_css_classes([] if connected else ["suggested-action"])
        self._channel_row.set_sensitive(not connected)
        self._device_button.set_sensitive(not connected and bool(self._devices))

    def _begin(self, what: str) -> None:
        self._busy = True
        self._spinner.set_visible(True)
        self._spinner.set_tooltip_text(what)

    def _end(self) -> None:
        self._busy = False
        self._spinner.set_visible(False)

    def _toast(self, message: str) -> None:
        self._toasts.add_toast(Adw.Toast(title=message, timeout=4))

    def _log(self, line: str) -> None:
        self._log_lines.insert(0, f"{time.strftime('%H:%M:%S')}  {line}")
        del self._log_lines[200:]
        self._log_label.set_label("\n".join(self._log_lines))

    def _on_close(self, _window: Adw.ApplicationWindow) -> bool:
        # Release the lease deliberately rather than leaving it to process exit, so a
        # window closed mid-request does not look like a stuck lock.
        self._worker.shutdown()
        return False
