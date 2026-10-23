# Runtime dependencies

The extension ships its worker as Python source. Users need no compiler, custom
helper package, LADSPA wrapper, NumPy, pip install or audio configuration files.

Required: GNOME Shell 51/GJS, a working PipeWire/WirePlumber desktop, pw-dump,
Python 3 with ctypes, one supported engine, PyGObject, Gst 1.0 and Wp 0.5 introspection,
and GStreamer PipeWire/app plugins. The worker checks the exact required API and
properties, including bounded appsrc queues. A distro name alone does not establish
compatible versions. Initially only little-endian audio is supported. Preferences use GNOME’s
standard GTK4/libadwaita extension preferences host, separate from Shell. Its
installed API is checked locally; no separate settings application is required.

## Arch

Package names and relevant typelib/plugin ownership were verified from installed
package metadata. Manual example for an existing PipeWire desktop:

```sh
sudo pacman -S python python-gobject gstreamer gst-plugins-base-libs gst-plugin-pipewire libwireplumber wireplumber
```

[RNNoise package](https://archlinux.org/packages/extra/x86_64/rnnoise/files/).

## Fedora

Names were verified against official package indexes; installation and exact
runtime compatibility were not tested. Manual example:

```sh
sudo dnf install python3 python3-gobject gstreamer1 gstreamer1-plugins-base pipewire-gstreamer wireplumber wireplumber-libs
```

Sources: [PyGObject](https://packages.fedoraproject.org/pkgs/pygobject3/python3-gobject/),
[RNNoise](https://packages.fedoraproject.org/pkgs/rnnoise/rnnoise/),
[GStreamer base](https://packages.fedoraproject.org/pkgs/gstreamer1-plugins-base/gstreamer1-plugins-base/),
[PipeWire plugin](https://packages.fedoraproject.org/pkgs/pipewire/pipewire-gstreamer/),
[WirePlumber libraries](https://packages.fedoraproject.org/pkgs/wireplumber/wireplumber-libs/).

## Debian / Ubuntu

**RNNoise is not verified as available across all stable releases.** Check first:

```sh
apt-cache policy librnnoise0 gir1.2-wp-0.5 gstreamer1.0-pipewire
```

Only on releases providing compatible candidates, manual example:

```sh
sudo apt install python3 python3-gi gir1.2-gstreamer-1.0 gir1.2-wp-0.5 gstreamer1.0-plugins-base gstreamer1.0-pipewire
```

This assumes PipeWire, WirePlumber and pw-dump already work on the desktop.
RNNoise indexes checked previously listed Debian forky/sid and Ubuntu stonking;
this is not a recommendation to add those repositories to a stable installation.
If your release lacks RNNoise, select SpeexDSP or WebRTC explicitly after installing
its runtime package. Each engine is checked independently. No processing engine
is substituted automatically. Common Gst/Wp compatibility is still required.

Verified name sources:
[Debian Python bindings](https://packages.debian.org/trixie/python3-gi),
[Gst introspection](https://packages.debian.org/trixie/gir1.2-gstreamer-1.0),
[PipeWire plugin](https://packages.debian.org/trixie/gstreamer1.0-pipewire),
[base plugins](https://packages.debian.org/trixie/gstreamer1.0-plugins-base),
[Wp introspection](https://packages.debian.org/sid/gir1.2-wp-0.5),
[Ubuntu Wp introspection](https://packages.ubuntu.com/en/resolute/gir1.2-wp-0.5),
[Debian RNNoise index](https://packages.debian.org/search?arch=amd64&keywords=rnnoise&searchon=sourcenames),
[Ubuntu RNNoise index](https://packages.ubuntu.com/librnnoise0).

Package availability does not imply GNOME 51 availability. No distro other than the
local Arch environment has been exercised. All commands above are user guidance;
the extension never executes a package manager.

## Choose an engine

Install at least one; installing all three is optional. The commands below are
manual user guidance, never executed by the extension. No development packages,
custom compiler, LADSPA wrapper or separate helper release is needed.

| Distro | RNNoise | SpeexDSP | WebRTC GStreamer plugin |
| --- | --- | --- | --- |
| Arch | `sudo pacman -S rnnoise` | `sudo pacman -S speexdsp` | `sudo pacman -S gst-plugins-bad` |
| Fedora | `sudo dnf install rnnoise` | `sudo dnf install speexdsp` | `sudo dnf install gstreamer1-plugins-bad-free` |
| Debian / Ubuntu | `sudo apt install librnnoise0` (candidate required) | `sudo apt install libspeexdsp1` | `sudo apt install gstreamer1.0-plugins-bad` |

SpeexDSP names verified: [Arch](https://archlinux.org/packages/extra/x86_64/speexdsp/),
[Fedora](https://packages.fedoraproject.org/pkgs/speexdsp/speexdsp/),
[Debian](https://packages.debian.org/stable/libs/libspeexdsp1),
[Ubuntu](https://packages.ubuntu.com/libspeexdsp1).
WebRTC plugin ownership verified in local Arch metadata and the
[Arch file list](https://archlinux.org/packages/extra/x86_64/gst-plugins-bad/files/),
and the [Fedora 44 file list](https://packages.fedoraproject.org/pkgs/gstreamer1-plugins-bad-free/gstreamer1-plugins-bad-free/fedora-44.html).
The [official GStreamer Linux guide](https://gstreamer.freedesktop.org/documentation/installing/on-linux.html)
lists the Debian/Ubuntu bad plugins package; its current file-list requests timed
out, so exact plugin presence there still needs runtime checking. Do not mix
stable/testing repositories to obtain optional engines.

Check without installing or connecting to PipeWire:

```sh
python3 -B -I extension/worker/main.py --check-all
```

Top-level `ready` validates common runtime APIs; inspect each `backends` entry's
`ready` value for actual engine availability. `--check` is retained as the
RNNoise-only diagnostic. An absent engine does not prevent inspecting microphones
or selecting another installed engine. No engine available means no processing.
