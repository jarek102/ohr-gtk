"""The window: status, noise control, and a developer panel.

Three things the layout is trying to say.

**Nothing is claimed that was not read.** Unavailable fields say so. The status page is
a report of the last poll, not a model of the device, and it is timestamped so a stale
one is visibly stale rather than quietly wrong.

**Writes are separated from reads**, because one of them is audible in the wearer's
ears and the other is not. The developer panel keeps setters in their own group,
labelled, with the noisy ones marked.

**Contention is a state, not an error.** Another client holding the lease, or the
vendor application holding the channel, is the normal case rather than a crash.
"""

from __future__ import annotations

import time
from typing import Any

from gi.repository import Adw, GLib, Gtk

from ohr import Frame, MessageType, VENDOR_SENNHEISER, anc, control
from ohr.transport import SERVICE_UUID

from . import catalogue, snapshot
from .worker import Failure, Worker

POLL_SECONDS = 3


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


class Window(Adw.ApplicationWindow):
    def __init__(
        self, autoconnect: str | None = None, channel: int | None = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.set_title("ohr")
        self.set_default_size(560, 820)

        self._worker = Worker()
        self._devices: list = []
        self._snapshot = snapshot.Snapshot()
        self._busy = False
        self._log_lines: list[str] = []

        self._toasts = Adw.ToastOverlay()
        self.set_content(self._toasts)

        root = Adw.ToolbarView()
        self._toasts.set_child(root)
        root.add_top_bar(self._build_header())

        page = Adw.PreferencesPage()
        page.add(self._build_connection_group())
        page.add(self._build_status_group())
        page.add(self._build_noise_group())
        page.add(self._build_developer_group())
        root.set_content(page)

        self._set_connected(False)
        self._refresh_devices()
        GLib.timeout_add_seconds(POLL_SECONDS, self._on_tick)
        self.connect("close-request", self._on_close)

        if autoconnect:
            self._select(autoconnect)
            if channel:
                self._channel_row.set_value(channel)
            # After the first frame, so a connection that fails slowly still shows a
            # drawn window rather than an empty one.
            GLib.idle_add(self._on_connect_clicked, None)

    # --- chrome --------------------------------------------------------------

    def _build_header(self) -> Adw.HeaderBar:
        header = Adw.HeaderBar()

        self._connect_button = Gtk.Button(label="Connect")
        self._connect_button.add_css_class("suggested-action")
        self._connect_button.connect("clicked", self._on_connect_clicked)
        header.pack_start(self._connect_button)

        self._spinner = Adw.Spinner()
        self._spinner.set_visible(False)
        header.pack_end(self._spinner)

        rescan = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Rescan devices")
        rescan.connect("clicked", lambda _b: self._refresh_devices())
        header.pack_end(rescan)
        return header

    def _build_connection_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(title="Device")

        self._device_row = Adw.ComboRow(title="Headset")
        self._device_row.set_model(Gtk.StringList.new(["(none found)"]))
        group.add(self._device_row)

        self._channel_row = Adw.SpinRow.new_with_range(0, 30, 1)
        self._channel_row.set_title("RFCOMM channel")
        self._channel_row.set_subtitle(
            "0 to resolve normally. Service discovery answers with nothing on these "
            "devices, so a channel given once is remembered."
        )
        group.add(self._channel_row)

        self._status_row = Adw.ActionRow(title="Connection", subtitle="disconnected")
        group.add(self._status_row)
        return group

    # --- status --------------------------------------------------------------

    def _build_status_group(self) -> Adw.PreferencesGroup:
        group = Adw.PreferencesGroup(
            title="Status",
            description=(
                f"Polled every {POLL_SECONDS} seconds while connected. Control traffic "
                "shares a radio with the audio link — a long burst of it has been "
                "followed by an audible drop in quality — so polling can be stopped "
                "while you listen."
            ),
        )

        self._poll_row = Adw.SwitchRow(
            title="Keep polling",
            subtitle="off freezes the values below; nothing is sent until you act",
        )
        self._poll_row.set_active(True)
        group.add(self._poll_row)
        self._rows: dict[str, Adw.ActionRow] = {}
        for key, title, subtitle in (
            ("battery", "Battery", "per cell; a level does not mean that earbud joined"),
            ("charger", "Charger", "on the earbuds, per bud — tracks the case"),
            ("mode", "Mode", "a presentation of two independent flags"),
            ("flags", "ANC / transparency", "both can be set at once"),
            ("levels", "Levels", "active, and the stored transparency level"),
            ("submodes", "Submodes", "states are raw; ranges differ per submode"),
            ("polled", "Last read", "a report of the last poll, not a live model"),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            value = Gtk.Label(label="—", xalign=1)
            value.add_css_class("dim-label")
            value.set_selectable(True)
            row.add_suffix(value)
            self._rows[key] = row
            group.add(row)
            row.value_label = value  # type: ignore[attr-defined]
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
            title="Writes",
            subtitle="change the device; ANC is audible in both directions",
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
            subtitle = ("audible — plays a tone in the wearer's ears. " + subtitle).strip()
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
            ("feature", "Feature", 127, 14),
            ("operation", "Operation", 127, 1),
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

        labels = [
            f"{d.name}  ({d.address})  {'connected' if d.connected else 'disconnected'}"
            for d in self._devices
        ] or ["(none advertising the service)"]
        self._device_row.set_model(Gtk.StringList.new(labels))
        self._device_row.set_sensitive(bool(self._devices))
        self._connect_button.set_sensitive(bool(self._devices) or self._worker.address is not None)

    def _selected(self):
        index = self._device_row.get_selected()
        return self._devices[index] if 0 <= index < len(self._devices) else None

    def _select(self, address: str) -> None:
        """Point the dropdown at an address, matched case-insensitively."""
        for index, device in enumerate(self._devices):
            if device.address.lower() == address.lower():
                self._device_row.set_selected(index)
                return
        self._toast(f"{address} is not in the list — is it connected?")

    # --- actions -------------------------------------------------------------

    def _on_connect_clicked(self, _button: Gtk.Button) -> None:
        if self._worker.address:
            self._worker.close(self._on_closed)
            return
        device = self._selected()
        if device is None:
            return
        channel = int(self._channel_row.get_value()) or None
        self._begin("connecting")
        self._worker.open(device.address, channel, self._on_opened)

    def _on_opened(self, result: Any) -> None:
        self._end()
        if isinstance(result, Failure):
            self._status_row.set_subtitle(result.error)
            self._toast(result.error)
            self._log(f"connect failed: {result.error}")
            return
        self._set_connected(True)
        self._status_row.set_subtitle(f"{self._worker.address}  ·  {result}")
        self._log(f"connected: {result}")
        self._poll()

    def _on_closed(self, _result: Any) -> None:
        self._set_connected(False)
        self._status_row.set_subtitle("disconnected")
        self._snapshot = snapshot.Snapshot()
        self._render()
        self._log("disconnected — the lease is free for other clients")

    def _on_mode_clicked(self, _button: Gtk.Button, mode: anc.Mode) -> None:
        if not self._ready():
            return

        def job(session):
            # Read the flags in the same job that writes them. A plan built from the
            # status page's last poll would verify perfectly and select the wrong mode.
            current = snapshot.read_flags(session)
            plan = anc.plan_mode(mode, current)
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
        self._poll()

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

        text = self._raw["payload"].get_text().replace(" ", "")
        try:
            payload = bytes.fromhex(text)
        except ValueError:
            self._toast("Payload is not hex")
            return

        request = Frame(VENDOR_SENNHEISER, feature, MessageType.COMMAND, operation, payload)
        kind = "write" if operation % 2 == 0 else "read"
        self._send(f"raw {request.word:04x} ({kind})", request, None, refresh=kind == "write")

    def _send(self, label: str, request: Frame, decode, *, refresh: bool) -> None:
        self._begin(label)
        self._log(f"→ {label}: {request.to_bytes().hex()}")

        def job(session):
            return session.request(request, timeout=snapshot.TIMEOUT)

        def done(result: Any) -> None:
            self._end()
            if isinstance(result, Failure):
                self._log(f"← {label}: {result.error}")
                self._toast(result.error)
                return
            line = f"← {label}: {result.to_bytes().hex()}"
            if result.type is MessageType.ERROR:
                line += "   [error frame — an answer, so the setting is now unknown]"
            elif decode is not None:
                try:
                    line += f"   {decode(result.payload)!r}"
                except Exception as exc:  # noqa: BLE001
                    line += f"   [did not decode: {exc}]"
            self._log(line)
            if refresh:
                self._poll()

        self._worker.submit(label, job, done)

    # --- polling and rendering ----------------------------------------------

    def _on_tick(self) -> bool:
        if self._worker.address and not self._busy and self._poll_row.get_active():
            self._poll()
        return GLib.SOURCE_CONTINUE

    def _poll(self) -> None:
        if not self._worker.address:
            return
        self._worker.submit("status", snapshot.read, self._on_snapshot)

    def _on_snapshot(self, result: Any) -> None:
        if isinstance(result, Failure):
            self._rows["polled"].value_label.set_label("read failed")  # type: ignore[attr-defined]
            return
        self._snapshot = result
        self._render()

    def _render(self) -> None:
        s = self._snapshot

        if s.battery is None:
            battery_text = "—"
        elif s.battery.shape == "scalar":
            battery_text = _percent(s.battery.level)
        else:
            battery_text = (
                f"L {_percent(s.battery.left)}  R {_percent(s.battery.right)}  "
                f"case {_percent(s.battery.case)}"
            )

        charger = "—" if s.charger is None else f"{s.charger.first} / {s.charger.second}"
        mode = s.mode.value if s.mode else ("unknown" if s.anc_on is None else "—")

        def flag(value: bool | None) -> str:
            return "unavailable" if value is None else ("on" if value else "off")

        values = {
            "battery": battery_text,
            "charger": charger,
            "mode": mode,
            "flags": f"ANC {flag(s.anc_on)}  ·  transparency {flag(s.transparency_on)}",
            "levels": f"{_percent(s.active_level)} active  ·  {_percent(s.transparency_level)} transparency",
            "submodes": "  ".join(f"{m.name or m.id}={m.state}" for m in s.submodes) or "—",
            "polled": time.strftime("%H:%M:%S") if self._worker.address else "never",
        }
        for key, text in values.items():
            self._rows[key].value_label.set_label(text)  # type: ignore[attr-defined]

        for target, button in self._mode_buttons.items():
            selected = s.mode is target
            button.set_css_classes(["suggested-action"] if selected else [])

    # --- small helpers -------------------------------------------------------

    def _ready(self) -> bool:
        if self._worker.address:
            return True
        self._toast("Not connected")
        return False

    def _set_connected(self, connected: bool) -> None:
        self._connect_button.set_label("Disconnect" if connected else "Connect")
        self._connect_button.set_css_classes([] if connected else ["suggested-action"])
        self._device_row.set_sensitive(not connected and bool(self._devices))
        self._channel_row.set_sensitive(not connected)

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
        # window closed while a request is in flight does not look like a stuck lock.
        self._worker.shutdown()
        return False
