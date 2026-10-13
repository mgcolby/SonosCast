# Third-party notices

SonosCast is MIT licensed (see `LICENSE`). It depends on the following
open-source packages, each under its own license. The prebuilt `SonosCast.exe`
bundles all of them.

| Package | Purpose | License |
| --- | --- | --- |
| [SoCo](https://github.com/SoCo/SoCo) | Sonos discovery and control (UPnP) | MIT |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | WASAPI loopback capture of Windows audio | Apache-2.0 |
| [lameenc](https://github.com/chrisstaite/lameenc) | MP3 encoding, statically linked [LAME](https://lame.sourceforge.io/) | LGPL-3.0-or-later (LAME itself LGPL-2.0-or-later) |
| [PyInstaller](https://pyinstaller.org) | Builds the single-file exe (build-time only; its bootloader ships in the exe) | GPL-2.0-or-later with the PyInstaller bootloader exception |

LGPL note: lameenc and LAME are used unmodified as a library. Their source is
available at the links above, and you can rebuild `SonosCast.exe` with a
different version by editing `requirements.txt` and running `build.bat`.

Sonos is a trademark of Sonos, Inc. This project is not affiliated with or
endorsed by Sonos.
