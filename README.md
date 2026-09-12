# ohr-gtk

A GTK4/libadwaita front end for [ohr](https://github.com/jarek102/ohr), the vendor
control protocol for Sennheiser Bluetooth headsets.

> **Status: pre-alpha.** Built as a surface for manual testing, and honest about being
> one. It reads status, selects the three noise-control modes, and exposes every
> command the library knows behind a developer panel.

## Why it is a separate repository

`ohr` holds the specification, the vectors and the codecs, and has **no dependencies**
— a property its tests enforce, because the same fixtures are meant to keep a second
implementation in another language honest. A GTK toolkit does not belong in there. The
dependency points one way, and only one way.

## Running it

```bash
PYTHONPATH=src:../ohr/src python3 -m ohr_gtk
```

PyGObject and libadwaita come from your distribution, not from pip — building
PyGObject from source against mismatched headers is a worse failure than an import
error. Everything else is `ohr` itself.

To skip the device picker, which you will want after the third time:

```bash
PYTHONPATH=src:../ohr/src python3 -m ohr_gtk --connect AA:BB:CC:DD:EE:FF
```

## What it shows

**Status**, in three tiers, because reads are not free and a window meant to be left
open would otherwise hammer the channel forever. Identity once per connection; battery,
charger, noise-control flags and codec every few seconds; settings and the peer list
every half-minute.

**The device decides the layout.** Sections are built from the feature map the headset
reports at connect — a row reading *unavailable* forever is worse than no row. A field
that could not be read says so rather than showing a zero: an unread flag and a flag
that is off are not the same thing, and only one of them would justify a write.

**Connections** shows which devices are paired, which hold a slot, and which one is this
computer. Peers routinely outnumber the slots available, which is the thing worth seeing.

**Noise control**: the three modes. Each is up to two writes, verified by reading back,
and planned from a reading taken in the same operation that writes — a plan built from
the status page's last poll would verify perfectly and select the wrong mode. A mode
already selected writes **nothing**, because a redundant ANC write is a tone in the
wearer's ears.

**Developer**: every command in the library, with raw hex in and out, and a raw frame
sender for probing features the specification does not yet cover. The status page above
is written for a person; this one keeps the bytes.

## Adding a command

Append a row to [`catalogue.py`](src/ohr_gtk/catalogue.py). The panel builds itself from
that list, so a newly specified command becomes a button without touching widget code,
and removing one is a deletion rather than a hunt.

Two fields are carried per entry rather than guessed: whether it **writes**, and whether
it is **audible**. Setters live in their own group; the audible ones are marked, because
repeating one costs something to whoever is wearing the device.

## Things it deliberately does not do

**Guess.** `0x1405` deletes a pairing and the raw sender refuses it outright. There is
no undo, and the device it would most plausibly be aimed at by accident is the one
holding the control channel.

**Hold quietly.** While connected it owns the lease, so the `ohr` command line cannot
reach the same device. That is what a lease is for. Disconnecting is one click, in the
header, rather than something you close the window to do.

**Pretend a read-back is permanent.** A device has been seen to accept a setting,
confirm it, and abandon it a second later. Hence the poll, and hence the timestamp.

## Licence

MIT — see [LICENSE](LICENSE).
