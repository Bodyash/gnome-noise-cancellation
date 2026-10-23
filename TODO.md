# Release checklist and roadmap

## Next patch release

- [ ] Retest OBS using system default with Bluetooth headphones: repeated tile-off
      must return to USB without a call-profile/noise-suppression interruption;
      tile-on must preserve the existing automatic Clean selection.
- [ ] Smoke-test shared volume/mute on GNOME 51: start at 50%, set Clean Microphone
      to 80%, edit the physical mic directly, test zero/mute, idle/resume and disable.
      Check other physical-mic consumers and hardware mixer rounding.
- [ ] Run Actions manually; resolve test, packaging and Shexli findings.
- [ ] Tag the patch after checks and desktop testing pass; confirm its Release ZIP
      and checksum download.

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
