# GNOME review audit

Checked **2026-10-23**, against the current
[GNOME review guidelines](https://gjs.guide/extensions/review-guidelines/review-guidelines.html).
Scope: the working tree after the logging/signal fixes, including the packaged
extension and its transitive imports. This is an engineering audit, not GNOME approval.

## Why Shexli rejects 51

These metadata values serve different purposes:

```json
"shell-version": ["51"],
"version": 1
```

`51` declares GNOME Shell compatibility. `1` is the source package's submission
number, kept fixed at the author's request. Changing `version` cannot fix an error
about `shell-version`. GNOME's [51 porting guide](https://gjs.guide/extensions/upgrading/gnome-shell-51.html)
documents this released API target; installed GNOME 51 resources pass our checks.

[PyPI](https://pypi.org/project/shexli/) still lists **0.2.1, released April 10, 2026**,
as the newest published Shexli. Our pin matches that release; an unpinned upgrade
would currently select the same version. Its observed release validation rejects
the stable `51` target as implausibly future. This is consistent with stale
release knowledge, not an invalid version-string format. GNOME's own site
[approved a submission on October 10 with the same EGO-M-004 finding](https://extensions.gnome.org/review/76155).

The upstream rule source could not be retrieved in this environment, so this
audit does not claim to have verified its exact internal constant or whether an
unpublished Git revision fixes it. The conclusion above rests on the published
release metadata, actual CI output and GNOME's own approved submissions.

The CI compatibility exception is a **local release policy**, not a Shexli fix or
GNOME approval. It accepts only the sole EGO-M-004 error from installed Shexli
0.2.1/spec 2026-04-02, with matching report path/evidence, and the actual ZIP
declaring exactly `["51"]`. It preserves the original JSON and emits a visible CI
warning. Extra findings, other versions/targets, malformed reports and invalid
ZIPs still block release. Updating the analyzer requires revisiting this policy.
Never replace `51` with an untested Shell version to obtain a clean report.

## Source and packaging checks

| Area | Evidence and result |
| --- | --- |
| Initialization | Shell modules define classes/functions/static JS data. Settings, UI, timers and children are created through `enable()`. Preferences construct GTK only in their own host process. |
| Shell API | ES modules and native Quick Settings; `disable()` is synchronous, as required by GNOME 51. Ten installed Shell imports validate without constructing UI. |
| UI ownership | Owned Quick Settings items and indicator are destroyed; reference maps and warning history are released. Existing desktop controls and other extensions are not patched. |
| Signals | Saved settings/tile/menu IDs disconnect before actor destruction; dynamically created microphone menu items are destroyed with their section. Controller subscriptions and preferences connections are released. |
| Timers | Login discovery, child timeouts, heartbeat and kill escalation have explicit removal paths. Unload requests immediate child termination before fallible UI teardown. |
| Child processes | Gio.Subprocess uses argument arrays and bounded private pipes; no shell interpolation, root or unrestricted control service. Children are terminated and reaped; startup waits for prior reaping. |
| Process boundaries | Followed relative imports: 11 files reachable from Shell have no Gtk/Gdk/Adw imports; 8 reachable from preferences have no Shell/St/Meta/Clutter imports. No deprecated Lang/Mainloop/ByteArray or forced GObject disposal calls found. |
| Logging | Settings/storage warnings are bounded per category; controller state errors are deduplicated. No sample audio, authorization tokens or raw worker stderr are logged. |
| Metadata | Public UUID and repository URL, only Shell 51, matching schema key. Default-only session modes are omitted; no unused donations/clipboard declarations. See the version recommendation below. |
| GSettings | Schema ID, fixed path and XML filename agree. Tests use memory settings; the ZIP carries XML without the local compiled cache. |
| Archive | 23 files: 13 readable JS modules, 6 Python modules, metadata, stylesheet, schema and MIT license. No native libraries/executables, bytecode, tests, CI, AGENTS.md, icon or build caches. Exact source parity/CRC checks pass. |
| User/system boundaries | No runtime installer, downloaded code/models, recording, network audio, telemetry, clipboard access, privileged commands or global audio configuration. Dependency commands are display-only guidance. |
| Functionality | Author-reported Arch/GNOME 51/OBS smoke testing plus native synthetic audio. Current public-UUID ZIP still needs deliberate desktop testing; other distributions are not claimed verified. |
| Content/licensing | Readable source and included MIT license; no copied third-party implementation or bundled DSP model/library. The guidelines permit permissive source under GNOME-compatible distribution terms. |

No additional mandatory source/packaging violation was identified in this audit.
The following items prevent an unconditional claim that every review requirement
has already been satisfied.

## Human review decisions

**Python worker:** the scripts policy is conditional. The concrete justification
is access to RNNoise/SpeexDSP's C APIs through Python ctypes. The inspected distro
runtimes provide no RNNoise/Speex GI annotations for direct GJS calls. GStreamer
and WirePlumber themselves have introspection; their presence alone would not
justify Python. Processing runs outside Shell and ships only as readable source.
Acceptance of this language choice belongs to GNOME reviewers.

Suggested submission explanation:

> The bundled Python worker calls distro RNNoise/SpeexDSP libraries through typed
> ctypes bindings; these APIs are not exposed by our runtimes through GObject
> Introspection. It also owns GStreamer capture/output and exact WirePlumber
> routing, keeping audio outside GNOME Shell. No executable/library/model is
> bundled or downloaded. Gio.Subprocess starts it with isolated Python and private
> control pipes. Extension disable force-terminates owned children immediately,
> cancels timers/IPC and reaps asynchronously. Parent-death, heartbeat and graph
> leases provide additional fail-closed supervision. Dependencies are installed
> manually by users. No privileged operations, recording or network audio occur.

**Version field:** GNOME recommends leaving numeric `version` to the website.
The source retains `1` under the author's explicit instruction; it is an intentional
departure from that recommendation, not the cause of EGO-M-004. The site overrides
submission numbering. See [version guidance](https://gjs.guide/extensions/overview/anatomy.html#version).

**Author understanding:** the AI-use rule requires the publisher to understand and
justify the submitted implementation. Review the architecture, authorization,
worker lifecycle and teardown before submitting; automated checks cannot establish
that understanding or substitute for a reviewer decision. Engineering-agent
instructions are excluded from the ZIP.

## Verification and next step

The installed-tool headless runner passes **75 Python methods and 159 JavaScript
cases**, including real-library synthetic audio, lifecycle failures/races, cleanup
and strict analysis-policy negatives. Schema, Python/JS syntax, source-only ZIP
integrity/parity, process import boundaries and installed Shell/preferences APIs
were checked. Native GTK rendering and actual graph teardown remain desktop checks.
No dependencies were installed, desktop extension activated or live graph changed.

Commit/push the reviewed changes, then start a **new** manual Actions run on that
branch. The revised actual Shexli scan is still pending; do not describe the ZIP as
upstream-Shexli-clean based on policy tests. A GitHub experimental release and
extensions.gnome.org approval are separate milestones.
