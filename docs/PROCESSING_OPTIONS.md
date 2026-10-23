# Processing settings

Open Quick Settings → Noise Cancellation → **Settings…**, or use the extension’s
preferences button in the GNOME Extensions app. The standard GNOME preferences
window scrolls on smaller displays; changing controls keeps it open. Choose an
engine explicitly. Missing engines are marked unavailable; selecting one shows
its setup error rather than substituting another engine. Install dependencies
manually, refresh, then select and enable.

Choices are saved in GSettings, including options for inactive engines. You can
edit while processing; the current session stays frozen. Once disabled and fully
cleaned up, the latest saved choices apply to the next explicit enable.
Settings never authorize a microphone, restart processing or clear an invalidation.
All engines use the same supervised capture, idle, routing and cleanup path.

## Controls

| Engine | Option | Default / meaning |
| --- | --- | --- |
| All | Output level | 0 dB; −12 to +12 dB, applied after suppression with clipping protection |
| RNNoise | Built-in model | Already pretrained; no training or download required |
| SpeexDSP | Noise attenuation | −25 dB; more negative values permit stronger attenuation |
| SpeexDSP | Automatic gain | Off; optional speech-level adjustment, maximum gain capped at 12 dB |
| SpeexDSP | Gain target | Balanced: approximately −12 dBFS RMS; applies only with automatic gain |
| WebRTC | Suppression | High; choose Low, Moderate, High or Very high |
| WebRTC | Automatic gain | Off; adaptive digital mode with limiter |
| WebRTC | Gain target | −3 dBFS; configurable from 0 to 31 dB below full scale, applies only with automatic gain |
| WebRTC | High-pass filter | Off; optional low-frequency filtering |

Output gain is not a noise-suppression strength control. Positive gain can still
reduce headroom and sound distorted when the output clamp limits peaks. Automatic
gain can amplify residual noise; leave it off unless level variation is a problem.
Stronger suppression may damage quiet speech. Compare engines on your microphone;
no perceptual quality ranking has been established.

RNNoise has no native suppression-strength parameter. This iteration keeps its
installed default model and does not add a speech gate, raw/filtered blend or
custom model loader. SpeexDSP and WebRTC are separate selectable engines, not
additional processing layered on top of RNNoise. Noise suppression stays enabled.

## Echo cancellation

Not implemented. Both SpeexDSP and WebRTC need the audio sent to speakers as a
synchronized reference. The current graph captures one explicitly authorized
microphone only. A working echo option needs playback-device selection, reference
routing, synchronization, reference-device lifecycle handling and new safety tests.
There is no switch that claims echo cancellation without those components.

## Implementation and verification

RNNoise uses its typed ctypes API. SpeexDSP uses its typed preprocessing API on
480-sample S16 frames. WebRTC uses an internal bounded GStreamer
appsrc → webrtcdsp → appsink pipeline with S16 frames; it opens no audio device.
The shared capture/output interface remains 48 kHz mono F32 with 10 ms framing.
Only processed frames can reach Clean Microphone. Idle resets each processor's
history without clearing permanent revocation.

Locally installed libraries processed synthetic samples using all engines and all
four WebRTC levels. Pause/reset/resume passed with synthetic GStreamer capture
for every engine. The author reports the development build working on Arch/GNOME 51 after testing
OBS and backend changes. This is desktop smoke testing; comparative sound quality,
latency and CPU measurements remain future work. See [verification](VERIFICATION.md).

[Dependencies and package commands](DEPENDENCIES.md) · [Manual tests](DEVELOPMENT.md)

Primary references:
[RNNoise API](https://github.com/xiph/rnnoise/blob/main/include/rnnoise.h),
[SpeexDSP ABI](https://github.com/xiph/speexdsp/blob/master/include/speex/speex_preprocess.h),
[Speex preprocessing and echo reference](https://www.speex.org/docs/manual/speex-manual/node7.html),
[WebRTC plugin options](https://gstreamer.freedesktop.org/documentation/webrtcdsp/webrtcdsp.html).
