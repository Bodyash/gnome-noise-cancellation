# Release checklist and roadmap

## First GitHub release

- [ ] Push the source and workflow to the public GitHub repository with Issues enabled.
- [ ] Run Actions manually; resolve test, packaging and Shexli findings.
      The first run reached Shexli; rerun after the logging/signal fixes and the
      documented, ZIP-checked Shell 51 analyzer compatibility exception.
- [ ] Smoke-test the packaged public UUID on GNOME 51: each backend, idle/resume,
      rapid toggles and extension disable. Disable the old development UUID first.
- [ ] Push `v1.0.0` after checks pass; confirm the Release ZIP and checksum download.

## GNOME Extensions listing

- [ ] Export a listing copy of `icon.png` at most 512 × 512 pixels, under 2 MiB;
      the current repository artwork is 1254 × 1254. Capture a desktop screenshot.
- [ ] Upload the ZIP, icon and screenshot; explain why the bundled Python worker is
      necessary. Website review/approval remains separate from GitHub publication.
      Use [the review audit and worker rationale](docs/REVIEW.md); review the code
      yourself before submission, including its AI-assistance and language policies.

## Follow-up work

- [ ] Add an opt-in isolated PipeWire/WirePlumber integration fixture covering
      unplug/replacement, link loss, service restarts, worker death and unload.
- [ ] Measure fault-to-silence timing, idle CPU, resume latency and hardware rates.
- [ ] Expand desktop coverage: reboot preference matching, unrelated Bluetooth
      changes, multiple apps, Fedora and Debian/Ubuntu with compatible GNOME/APIs.
- [ ] Compare backend sound quality and CPU before adding more controls.
- [ ] Consider echo cancellation only with synchronized playback-reference routing
      and reference-device lifecycle tests.

Detailed manual checks and current evidence live in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)
and [docs/VERIFICATION.md](docs/VERIFICATION.md). These follow-up tasks do not block an
experimental GitHub release; advertise only the platforms and behavior actually tested.
