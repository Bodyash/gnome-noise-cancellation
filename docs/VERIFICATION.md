# Verification

Last reviewed: **2026-10-23**.

The [GNOME review audit](REVIEW.md) records source/packaging findings, the latest
published Shexli version and review decisions that require human judgment.

## Desktop report

The author reports a development build working flawlessly on Arch Linux / GNOME 51 after
exercising OBS, backend changes and idle/resume. Earlier desktop reports confirmed
Clean Microphone audio and the separate preferences window. This is user-reported
smoke testing; it is not a measured hotplug, latency or fault-injection certification.

The first public candidate uses source version **1** and UUID
`noise-cancellation@bodyash.github.io`; metadata includes the public repository URL.
This renumbers the previously internal development builds; audio behavior is
unchanged by publication preparation. The public UUID has not been installed or
tested on the desktop by the agent. Extension unload force-terminates owned
workers/probes through Gio.Subprocess,
Shell timers are cancelled immediately, and a reap barrier prevents a new lifecycle
from overlapping prior workers. The normal tile-off graceful path remains bounded.
It has automated coverage below and has not been installed or activated by the agent.

Earlier intermittent RNNoise silence and suspension reports have not recurred in
the user's latest report. Their exact desktop causes were not independently
reproduced; retain the regressions and routing diagnostic if they recur.

The user confirmed shared volume works and reported a brief Bluetooth call-profile
switch on normal tile-off while OBS follows the system default. Final selection
already returned to USB and restored Clean on the next enable. The conditional
handoff preserves that behavior and addresses the missing-default interval;
the actual headset symptom has not been independently reproduced or retested.

## Local environment

Arch Linux with GNOME Shell 51.1, GJS 1.90.1, PipeWire 1.6.9, WirePlumber 0.5.18,
Python 3.14.8, PyGObject 3.58.1, GStreamer 1.28.8, RNNoise 0.2 and SpeexDSP 1.2.1.
Gst/Wp introspection, PipeWire/app plugins and the WebRTC plugin are installed.
No runtime dependencies are missing locally. Tests use GJS and Python unittest;
Node.js and a Mermaid renderer are not installed.

## Automated evidence

| Check | Result / scope |
| --- | --- |
| JavaScript | 163 cases: lifecycle, processes, dependencies, protocol, discovery, preferences, settings, rapid changes, presentation and diagnostics |
| Python | 114 unittest methods plus table-driven scenarios: DSP, streaming, worker protocol, identity/routing, volume/mute, input handoff, cleanup, headless runner and analysis gate |
| Native synthetic audio | RNNoise, SpeexDSP and WebRTC produce nonzero downstream samples before and after idle; idle output is silent |
| Isolated native volume | Opt-in private PipeWire server: starts at 50%, virtual 80% updates a synthetic source, physical edits/mute mirror back, actual nonzero downstream samples retain unity virtual gain |
| Isolated native handoff | Real default metadata acknowledges physical input before virtual-source removal; configured Clean preference is preserved; no hardware or Bluetooth policy runs |
| Conversion | Synthetic GStreamer 44.1 → 48 kHz followed by real RNNoise |
| Runtime self-check | All three installed engines ready; capture/output constructed in NULL and registry ABI checked without server connection |
| Static APIs | 10 Shell imports checked against installed GNOME 51 resources; GTK4/libadwaita properties/methods checked via introspection |
| Packaging | Public UUID, fixed source version 1, repository URL, source-only ZIP, local schema, ES module parsing, Python AST, whitespace, license, no embedded home paths and exact archive/source parity |
| Relocation | Copied extension with spaces in its installation path; dynamic GJS imports and all-engine Python self-check run from an unrelated working directory |
| Release tooling | Shared headless runner passed locally; analysis gate tested with clean, warning/error/manual-review and malformed/inconsistent reports |
| Workflow static checks | YAML parsed with installed libyaml; job dependencies, tag-only publication, permissions, full action pins and 10 Bash scripts validated without executing CI installers |

GitHub Actions is defined as headless tests → source ZIP validation → Shexli →
tagged release with checksums. The user's first hosted run reached analysis after
tests/build passed. Its real Shexli report contained EGO-A-004 logging, EGO-L-003
tile/menu signals and EGO-M-004 rejecting Shell 51. Release publication was blocked.
The current fix adds explicit signal disconnects and bounded storage/settings
diagnostics; controller errors remain visible. The source version stays at 1.
New regressions cover signal cleanup failure/idempotence and late/repeated warnings.

The gate preserves raw findings and permits only the documented 0.2.1/spec
2026-04-02 version error after checking the exact report evidence and actual ZIP's
`["51"]` metadata. Tests reject mixed/other findings, malformed reports, different
targets, missing/bad ZIPs and changed analyzer versions. This is a declared tool
compatibility exception, not a claim of a clean upstream Shexli report. See
[release policy](DEVELOPMENT.md#github-actions-and-releases).
The revised ZIP still needs a hosted analyzer rerun. No container dependencies or
pip packages were installed locally.

Unload regressions first failed against the old graceful-only disposal path.
Real Gio.Subprocess protocol fakes now verify force-exit during active/startup,
escalation during an existing stop, timer cancellation, asynchronous reaping and
suppression of late messages. Eight source-level Shell wiring cases execute the
actual extension unload method with fake UI objects: termination precedes desktop
cleanup; signal/actor teardown exceptions cannot skip termination or remaining
cleanup; tile/menu disconnect failures remain isolated; warning history is cleared
and repeated storage diagnostics are bounded; failed termination retains a rejected
restart barrier. These development
fixtures do not evaluate Shell resources or enter the release ZIP.
Backend/controller tests verify immediate disposal,
idempotent cleanup completion, waiting across re-enable, cancellation while waiting
and blocking after unconfirmed prior termination. Discovery disposal also exposes
its reap completion. These fakes never connect to an audio server; actual virtual
source disappearance after unload remains a manual graph check.

Lifecycle tests cover missing dependencies, absent/invalid mic, failed/unproven
startup, unplug/replacement/replug, unrelated devices, service restarts, link loss,
worker crash, cancellation, stale callbacks and extension unload. Readiness proofs
must match the authorized route and frozen engine/options. Replug and saved settings
cannot recover invalidated authorization.

Native regressions cover nonblocking callbacks after appsink PAUSED, genuine EOS,
output clock pacing, bounded queues, idle reset/flush, brief consumer churn and
revocation during idle settling. Delivery/capture generations reject old frames
and prevent an old DSP completion from proving fresh resume readiness.

Volume regressions cover source-authoritative initialization, linear/cubic levels,
channel balance, zero/mute, hardware rounding, simultaneous edits, coalesced rapid
updates, late mirror echoes, rejected/missing controls, acknowledgment timeouts and
permanent close. Native GLib variants exercise the distro mixer's request format.
Lifecycle regressions require mixer readiness before capture/publication and reject
volume writes after identity loss. The runtime check verifies that the existing
WirePlumber mixer module is present without connecting to a server.

`python3 -B tools/test-volume.py` passed against an owned hardware-free PipeWire
server after authorization to bind its private Unix socket outside the sandbox.
Its source, consumer and known output signal are synthetic; no desktop server or
physical device was accessed. The installed PipeWire GStreamer plugin emitted one
`gst_caps_is_equal` negotiation assertion while the sample/volume/mute checks
passed. This fixture verifies adapter gain behavior and mixer API integration,
not hardware or GNOME slider behavior. The same private-server fixture now also
checks default metadata handoff and source-removal ordering. No extension was
installed or enabled.

Handoff regressions cover scoped explicit stop IPC, capture removal, effective
versus configured defaults, malformed/missing metadata, source/provider/output
replacement, duplicate source names, user changes during the server round trip,
write/sync failures, bounded timeout, cancellation and late callbacks. Teardown
tests keep output silent while it remains registered. Fault and unload paths never
request handoff; immediate unload cancels pending handoff IPC and force-terminates
the worker, and an unresponsive handoff hits the original stop deadline.

The settings stress test runs six sessions with 360 pending edits and repeated
enable/disable requests during delayed startup/teardown. It requires at most one
owned worker, frozen live settings, latest options after reaping, and no automatic
restart. Preference tests use an isolated memory GSettings backend. Routing reports
are tested with fake graphs and redact serials/session tags.

[Exact check commands](DEVELOPMENT.md#automated-checks).

## Remaining limits

- Live PipeWire and desktop D-Bus access are blocked in the agent sandbox. The
  agent has not changed the live audio graph or independently repeated the user's
  OBS session. Native synthetic tests do not demonstrate real graph ordering.
- Optional GTK widget tests need a private Broadway Unix socket; that bind is
  blocked here. Static API validation passed; the user confirmed the actual window.
- Mermaid diagrams use GitHub-compatible fenced blocks. A local renderer is
  unavailable; rendered layout has not been previewed here.
- Hardware rate/channel negotiation, reboot preference restoration, fault-to-silence
  timing, actual unplug/replacement, service restarts, latency and CPU under load
  still need systematic integration tests. General desktop success does not measure them.
- Shared volume/mute needs desktop testing with the actual GNOME controls and
  physical mixer, including hardware rounding and idle/resume. The existing desktop
  report predates this change.
- OBS default-input handoff needs a desktop retest with the user's Bluetooth
  headset, including unchanged call/media profile and headset noise suppression
  through repeated tile toggles. The isolated fixture verifies metadata ordering,
  not WirePlumber Bluetooth policy or headset firmware behavior.
- Fedora and Debian/Ubuntu package names have documented sources; dependency
  installation and desktop operation have not been validated on those distributions.
- Python worker acceptance by extensions.gnome.org remains unconfirmed.

No dependencies were installed, updated or removed. No service was restarted,
configuration changed, extension installed/enabled, or Git commit created by the
agent. Only repository source and local build artifacts were modified.
