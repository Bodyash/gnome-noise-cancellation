# Engineering guide

Authoritative guidance for this repository; user instructions take precedence.
Read README.md and docs/ARCHITECTURE.md before changing audio behavior.

## Objectives and non-goals

Ship a small GNOME 51 extension that exposes a CPU noise-suppressed virtual source named
**Clean Microphone**. Bundle the Python worker as source in the extension ZIP;
users manually install existing distro runtime packages. Maintain one extension
release, without separate custom native helper/plugin packages. No compiler,
runtime downloads, user audio configuration, standalone GUI application, EasyEffects,
GPU inference, recording, network audio, telemetry, effects suite, automatic
fallback or hotplug recovery. Arch is the inspected development host; Fedora and
Debian/Ubuntu are portability targets, not automatically verified platforms.
Do not claim RNNoise is packaged on every distro release.

## Architecture and ownership

- `extension/extension.js`: compact GNOME Quick Settings; microphone, state,
  refresh and a standard openPreferences action. Keep processing controls out.
- `extension/prefs.js` and `extension/preferences/window.js`: standard GNOME-hosted
  GTK4/libadwaita preferences, confined to the preferences process. Use a scrollable
  preferences page, native rows and runtime-only dependency probes, never audio.
- `extension/core/processingBinding.js`: sync saved cross-process options only
  when the controller accepts configuration; retain pending edits while busy.
- `extension/core/controller.js`: sole Shell-side lifecycle owner, generations,
  selection authorization, legal transitions and idempotent asynchronous cleanup.
- `extension/core/devices.js`: conservative physical-source discovery/identity.
- `extension/core/preferences.js`: stable preferred-device matching, never live
  authorization. `extension/schemas/` stores the preference with GSettings.
- `extension/core/settings.js`: strict backend/option allowlist and immutable
  settings snapshots. GNOME preferences render these, never auto-enable or substitute.
- `extension/core/presentation.js`: derive backend/activity labels from the current
  controller snapshot, never pending preferences.
- `extension/core/dependencies.js`: display-only manual package advice.
- `extension/audio/backend.js`: bounded runtime self-check/discovery and fresh
  authorization boundary. Resolve the bundled worker relative to this module.
- `extension/audio/session.js`: Gio.Subprocess-owned worker IPC, proofs, heartbeat,
  immediate unload termination and asynchronous reaping.
- `extension/audio/process.js`: bounded asynchronous short subprocesses.
- `extension/worker/main.py`: source-only process, protocol, parent supervision.
- `extension/worker/lifecycle.py`: GI-independent identity and routing decisions.
- `extension/worker/routing.py`: Wp 0.5 introspection, registry and owned exact link.
- `extension/worker/mixer.py` and `volume.py`: session-scoped volume/mute proxy,
  distro mixer API, serialized physical writes and bounded acknowledgments.
- `extension/worker/handoff.py`: bounded, explicit tile-off handoff of the current
  default input; never change the configured default, output devices or BT profiles.
- `extension/worker/streaming.py`: GStreamer capture/output and bounded filtered queue.
- `extension/worker/dsp.py`: typed ctypes RNNoise API, 480-sample framing/scaling.
- `extension/worker/engines.py`: RNNoise/SpeexDSP/WebRTC factory, strict config
  validation, framing/reset/revocation contracts and filtered output gain.
- `tests/`: deterministic failures/races plus synthetic real-library audio tests.
- `tools/check.py`: shared headless test runner; build/validate only after tests pass.
- `.github/workflows/release.yml`: tag-gated tests, packaging, Shexli and GitHub Release.
  `tools/check-shexli.py` must reject findings and malformed reports, not merely trust
  the analyzer's exit code. Never bundle CI tools or virtual environments in the ZIP.
  The sole documented exception is 0.2.1/spec 2026-04-02's EGO-M-004 for an actual
  ZIP targeting exactly stable Shell 51. Verify analyzer version, original report,
  exact evidence and ZIP metadata; preserve the raw finding and emit a CI warning.
  Any other/mixed finding blocks release. Remove this exception when updating the
  analyzer rather than expanding it to other rules or future Shell versions.

The worker runs outside Shell. Python, at least one engine, PyGObject, GStreamer
and WirePlumber must be installed system runtimes; no pip modules or new custom C bindings.

## Development conventions and TDD

Use small ES modules and Python modules, four spaces, clear ownership, explicit
imports and bounded interfaces. Core lifecycle logic must not import GNOME/GI.
Keep `metadata.json` version at 1; do not bump it for edits, tests or builds.
GitHub releases use Git tags; extensions.gnome.org assigns submission numbers.
Keep the public UUID `noise-cancellation@bodyash.github.io` stable after publication.
Keep native GStreamer startup off the GLib control thread: negotiation may wait
for links that thread must create. Bound transition threads and guard cancellation.
Never use blocking sample pulls from streaming callbacks: a queued new-sample
signal can reach Python after appsink moves to PAUSED. Use try-pull-sample with
zero timeout; no sample during pause/flush is not EOS. Keep the EOS/error bus
watch authoritative. Capture appsink must not wait for asynchronous preroll;
readiness still requires the pinned route and fresh processed frames.
Do not destroy an active picker item during its synchronous activation callback.
Use existing GJS, Python unittest and synthetic fixtures without new frameworks.
Write a failing regression test before core changes, implement, rerun relevant
tests, then refactor. Cover every asynchronous failure/cancellation boundary.
Synthetic audio tests must inspect actual nonzero samples delivered downstream,
including after idle/resume; frame counters and buffer lengths alone cannot detect
an always-silent output path. Preserve silence while idle and revocation tests.
Mocks do not demonstrate hardware safety. Never add a test that connects to the
desktop audio server automatically.

## Dependency and permission restrictions

Never install/update/remove dependencies, use sudo, restart services, modify global
or user audio configuration, install/enable the extension, mutate the live graph,
or commit without explicit authorization. Only write within this repository.
Network/package research requires user authorization; no source/model downloads
as part of runtime, build or tests. Package guidance must be verified against local
metadata or official indexes, otherwise labeled unverified. Unsupported releases
stay unavailable; never recommend mixing stable/testing repositories.

The user-authorized GitHub Actions workflow may install its declared test/analyzer
dependencies inside disposable hosted runners/containers. This exception applies
only to remote CI definitions, never local agent execution or extension runtime.
Keep action revisions and analyzer dependencies pinned; grant release write access
only to the publish job, after every required check succeeds. Preserve analysis
reports on failure; do not hide findings or silently weaken release gates.

## Audio safety and device lifecycle

Fail closed. There is no unfiltered passthrough. ACTIVE means an enabled, validated
published session; controller-owned activity distinguishes processing, suspending,
idle and resuming. Initial publication requires an exact physical identity, the
owned unique active capture link and actually processed frames. Processing after
idle requires that same route active again and newly processed frames. Idle allows
the owned link to pause, never disappear or change endpoints. Keep monitoring
identity, providers, owned nodes/ports/links and graph leases while idle.
Capture and output must not autoconnect, fall back
or reconnect. Pin server cookie, source/device serials, stable physical properties,
port/link incarnations and relevant WirePlumber service providers. Auxiliary/export
clients and unrelated devices appearing/disappearing must not revoke the session.
Use session.services to identify the selected backend and linking policy providers;
pin their original incarnations, not a changing set of all WirePlumber clients.
Names and node IDs alone are unsafe.
Exclude virtual sources and monitors.

Idle suspension is within the same explicitly enabled session, not hotplug recovery.
Keep the virtual source registered under its stable node name with a per-session
tag. Never change the configured system default. The user-authorized exception is
normal active tile-off: revoke delivery, release capture, then hand the effective
default input from Clean Microphone to the still-validated selected physical mic
before removing the virtual source. Preserve the configured Clean preference and
skip handoff if another input is current/configured. Faults and extension unload
must never initiate this handoff or delay immediate termination.
An active consumer link demands capture;
default metadata and corked/paused links do not. The independent output pipeline
must negotiate/serve silence while capture sleeps. Coalesce PLAYING/PAUSED changes,
gate delivery on idle, allow at most 200 ms for consumer gaps to settle before
native pause, reset DSP history and flush buffered output, then require
fresh processed frames on resume. Delivery generations must reject callbacks
that span a closed gate even if it has reopened before they finish. Capture
generations must also reject stale completions as fresh resume evidence: frame
counts alone do not prove that processing began after the latest capture request.
Never clear permanent revocation during reset.
Resume must clear the old capture timestamp before enabling stall checks. Enforce
a bounded transition timeout. Never hide or spoof GNOME privacy indicators.

Unplug, replacement, uncertain identity, unexpected link changes, PipeWire or
WirePlumber restart, worker crash, malformed state and lost supervision permanently
revoke the current session. Stop output, remove owned resources, clear selection,
enter RECONFIGURATION_REQUIRED. A returning mic never restores authorization.
Fresh explicit selection AND enable are required after invalidation in the current
lifecycle. Keep the remembered device preference separate: processing faults and
logout must not erase it. Persist no enabled state or graph IDs. A fresh extension
lifecycle has a ten-second startup discovery window with at most five read-only
retries for late device registration. Only a unique hardware+endpoint match may
restore selection DISABLED. Invalid/ambiguous matching or invalidation cancels
restoration; an expired window cannot select a later hotplugged input. No later hotplug recovery,
automatic enable, label-only matching or ambiguity guessing. User preference writes
must never interrupt audio cleanup. Test GSettings using a memory backend only.

Users may save preferences at any time. Active session settings may change only
while stopped, after owned resources have been reaped. Queue external edits and
apply only the latest valid saved snapshot after cleanup, without starting audio.
Validate the same allowlist/bounds in JS and Python. Retain inactive
engine options; never persist enabled state. Freeze settings for every session and
require echoed canonical settings in processing readiness/activity proofs. Check
optional engines independently. Never silently substitute engines or disable
suppression. Output gain touches filtered audio only. AGC is opt-in. Echo
cancellation requires explicit synchronized playback-reference routing and lifecycle
tests; do not expose a nonfunctional switch. Never guess echo from microphone-only
input. Use the standard GNOME preferences host rather than expanding Quick
Settings with processing controls. GTK/Adwaita must never be imported into Shell.
Clean up preference widget/GSettings signals and owned dependency-check children
on close; Shell must disconnect its cross-process settings binding on unload.

Use operation generations in all Shell async paths. Late sessions are stopped;
stale callbacks cannot publish or affect newer sessions. STOPPING lasts until
the owned process has been reaped; failed cleanup blocks future enable.
The worker's session cannot reopen after revocation. Guard each output buffer
against revocation and an expired lease. Keep queues bounded and yield silence on
underrun. Do not claim Python is hard real-time or that already queued downstream
audio can be recalled. Preserve and document parent-death, heartbeat and watchdog
mitigations and their interpreter/scheduling limits.

## Compatibility, errors and cleanup

Target Shell 51 ES modules, QuickMenuToggle and SystemIndicator. Verify installed
APIs before declaring compatibility. No audio processing or blocking I/O in Shell.
All optional libraries/tools must be checked before use. Missing RNNoise, GI,
plugins, PipeWire or microphones must produce bounded actionable messages, never
a Shell crash or false success. No endless retries, noisy logs or unbounded children.
Log bounded distinct controller failures with the NoiseCancellation prefix;
never log audio, device serials or authorization keys.
Deduplicate repeated storage/settings warnings per category per extension lifecycle;
clear this diagnostic history on unload. Keep distinct controller failures visible.
Save and explicitly disconnect tile/menu signal IDs before destroying their owners,
including partial enable failure; one failed disconnect cannot skip the other.
Retain a bounded last-error message independently of the current UI state so a
missed fault can be inspected after selection or login. Storage errors must not
interrupt cleanup. Keep idle output clock-paced even when PipeWire drops buffers:
use a system pipeline clock and synchronized output sink, never a CPU-speed
appsrc loop. Include native stage, link state and fresh-frame status in timeouts.

Cleanup must be idempotent. Revoke first, stop output/capture, release only owned
links/connections, stop callbacks, disconnect signals, remove timers and reap the
worker. Stop streaming before destroying its processing state. A teardown stall must
not keep the worker alive indefinitely. Ignore late callbacks after unload.
Do not kill unrelated processes.
GNOME Extension.disable() must force-terminate owned workers and probes immediately
through Gio.Subprocess before fallible signal/widget teardown. Attempt every UI
cleanup independently so one exception cannot skip the rest. Remove heartbeat/escalation/discovery timers synchronously,
and cancel IPC. Normal tile-off may use bounded graceful cleanup; extension unload
must also escalate a stop already in progress. Keep an asynchronous reap barrier
across extension lifecycles; explicit re-enable cannot start audio before all prior
children are confirmed reaped. Failure to confirm termination blocks activation.
Never block Shell with synchronous wait, and never rely on GNOME awaiting disable().
Late worker messages cannot emit state after stop. Process exit closes owned,
non-lingering PipeWire connections; server-side node removal remains asynchronous.

## Security

Spawn argument arrays, never a shell. Use fixed bundled script paths and isolated
Python execution; disable bytecode generation. Resolve worker code relative to the
installed module URI and sibling Python files; discover executables through PATH
and libraries through the system loader. Never embed a developer home, username,
checkout location or temporary path in extension runtime code. Keep relocation
coverage with spaces in the installation path and an unrelated working directory. Strictly bound/validate IPC, tokens,
identities, frames, graph sizes and buffers. No unrestricted control socket/D-Bus
service. Private parent pipes and Linux parent-death termination scope the worker.
Do not record audio, leak serials in public logs, download models, interpolate
device data into executable code, or load user-specified plugins/libraries.
ctypes signatures must match the upstream ABI exactly.

## Verification commands

Run the complete headless sequence with `python3 -B tools/check.py`. It compiles the
local test schema, runs the suites below, then packages and validates the ZIP.
CI uses `--tests-only` to separate testing from packaging. This runner installs
nothing and points tests at nonexistent audio/session-bus endpoints.

```sh
glib-compile-schemas --strict extension/schemas
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
gjs -m tests/run.js
gjs -m tests/process.js
gjs -m tests/dependencies.js
gjs -m tests/session.js
gjs -m tests/extension-unload.js
gjs -m tests/backend.js
gjs -m tests/settings.js
gjs -m tests/processing-binding.js
gjs -m tests/presentation.js
gjs -m tests/routing-report.js
gjs -m tools/validate-preferences.js
python3 tools/pack.py
gjs -m tests/preferences.js
python3 -B -I extension/worker/main.py --check-all
python3 tools/validate.py
git diff --check
```

Optional native GTK tests: `python3 tools/test-preferences.py` uses a private
Unix-socket Broadway display and memory GSettings. It needs existing gtk4-broadwayd
and socket permission; report it as blocked when unavailable, never install
dependencies or fall back to the live desktop automatically.

These checks never mutate the live graph. tests/test_streaming.py uses synthetic
GStreamer audio. tools/diagnose.js adds read-only server discovery. GNOME static
resource validation is documented in docs/DEVELOPMENT.md. In restricted environments
keep GStreamer test registry caches under build/. Report missing tools/skipped
checks; never install dependencies to make tests pass.
Compile the local schema before individual preference tests; GNOME's
installer compiles the XML in the ZIP. Never add compiled schema caches to Git.

## Documentation and Definition of Done

Keep README short; detailed install, dependencies, safety and debugging belong in
docs/. Keep TODO.md and verification evidence current.
Keep TODO.md focused on actual release steps and future work, not completed history.
The root `icon.png` is repository/listing artwork, not a runtime dependency; do not
add it to the ZIP or introduce an unsupported metadata icon key. GNOME listing
images are uploaded separately and must meet the website's image limits.
Maintain the README overview and docs/ARCHITECTURE.md Mermaid diagrams alongside
changes to processes, audio flow, startup proofs or idle/session transitions.
Use standard GitHub Mermaid fences without external assets or runtime downloads.
Keep publication docs about the current design; retain regression rationale in
tests and the engineering guide rather than accumulating per-version narratives.
Clearly distinguish implemented, mock-tested, synthetic-library-tested and hardware-verified behavior.
Document extra dependencies and why reconfiguration is mandatory.

Done means relevant tests and source-only ZIP validation pass, including no stale
or extra archive members. Failure/race/cleanup behavior is covered, no unauthorized system changes occurred, and remaining
hardware/platform/review limits are explicit. A production release additionally
requires real graph, hotplug, latency and GNOME UI validation. EGO acceptance of the
Python worker must be confirmed; justification is not approval.
