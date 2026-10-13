"""
SonosCast - stream whatever your Windows laptop is playing to a Sonos speaker.

How it works
  1. Captures the laptop's default output device with WASAPI loopback.
  2. Encodes the PCM (raw WAV, or MP3 via lameenc).
  3. Serves it as a never-ending stream over HTTP on the LAN.
  4. Tells the chosen Sonos player (via its UPnP API) to tune into that stream.

Sonos players have no Bluetooth *receiver* (Ray/Beam/Five/One), so WiFi
streaming is the only way to get arbitrary laptop audio onto them.

Delay: our side (capture + encode + HTTP) is well under 100 ms. The delay you
hear is the Sonos player's own network-stream buffer, which it fills before it
starts playing and then holds for the life of the stream. The only lever we
have is the stream format (how many *seconds* of audio its byte-count buffer
holds) and prefilling that buffer with silence so playback starts right away.
"""

import os
import queue
import socket
import struct
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tkinter import ttk

import lameenc
import pyaudiowpatch as pa
import soco

APP_NAME = "SonosCast"
HTTP_PORT = 5555
SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAMES_PER_CHUNK = 1152          # one MP3 frame at 48 kHz (24 ms)
BITRATE_KBPS = 192
CLIENT_QUEUE_SECONDS = 0.75      # per-client backlog before we drop; also caps delay creep after a stall
DEBUG = bool(os.environ.get("SONOSCAST_DEBUG"))

# Measured on a Sonos Ray (S2 97.1), laptop silent, real-time paced stream:
#   WAV over http://            -> PLAYING after ~0.5 s, ~0.2 s of audio buffered
#   MP3 over x-rincon-mp3radio  -> PLAYING after ~5 s,   ~4.9 s of audio buffered
# The buffered seconds ARE the delay you hear. Prefilling MP3 with silence makes
# it start instantly without changing that delay; WAV needs none.


# --------------------------------------------------------------------------- #
# Codecs
# --------------------------------------------------------------------------- #
class WavCodec:
    """Raw 16-bit PCM in a WAV wrapper with a 'huge file' header. No encoder delay."""

    name = "wav"
    label = "Low delay (WAV)"
    path = "/stream.wav"
    content_type = "audio/wav"
    uri_scheme = "http"
    bytes_per_second = SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE
    prefill_seconds = 0.0

    def header(self):
        data_size = 0xFFFFFFFF - 36
        return b"RIFF" + struct.pack("<I", data_size + 36) + b"WAVE" + b"fmt " + struct.pack(
            "<IHHIIHH", 16, 1, CHANNELS, SAMPLE_RATE,
            SAMPLE_RATE * CHANNELS * BYTES_PER_SAMPLE, CHANNELS * BYTES_PER_SAMPLE, 16,
        ) + b"data" + struct.pack("<I", data_size)

    def encode(self, pcm):
        return pcm

    def silence(self, seconds):
        frames = int(seconds * SAMPLE_RATE)
        return b"\x00" * (frames * CHANNELS * BYTES_PER_SAMPLE)


class Mp3Codec:
    """MP3 via LAME. ~1/8 the bandwidth of WAV; a few frames of encoder delay."""

    name = "mp3"
    label = "Robust (MP3, ~5 s delay)"
    path = "/stream.mp3"
    content_type = "audio/mpeg"
    uri_scheme = "x-rincon-mp3radio"
    bytes_per_second = BITRATE_KBPS * 1000 // 8
    # Sonos radio mode gates on BOTH ~2.5 s of wall time and ~4.9 s of audio. A 2 s
    # prefill hits both together: PLAYING in ~3 s instead of ~5.5 s, same 4.9 s delay.
    # (4.5 s was tried and made the delay 7 s, since it over-filled the buffer.)
    prefill_seconds = 2.0

    def __init__(self):
        self._enc = self._new_encoder()

    @staticmethod
    def _new_encoder():
        enc = lameenc.Encoder()
        enc.set_bit_rate(BITRATE_KBPS)
        enc.set_in_sample_rate(SAMPLE_RATE)
        enc.set_channels(CHANNELS)
        enc.set_quality(7)   # fastest; transparent at 192 kbps
        return enc

    def header(self):
        return b""

    def encode(self, pcm):
        return self._enc.encode(pcm)

    def silence(self, seconds):
        # Encode with a throwaway encoder so the live stream's state is untouched.
        enc = self._new_encoder()
        chunk = b"\x00" * (FRAMES_PER_CHUNK * CHANNELS * BYTES_PER_SAMPLE)
        n = int(seconds * SAMPLE_RATE / FRAMES_PER_CHUNK)
        return b"".join(enc.encode(chunk) for _ in range(n)) + enc.flush()


# (MP3 over plain http:// was tried: Sonos still buffers ~5 s for it, so no gain.)
CODECS = {c.name: c for c in (WavCodec, Mp3Codec)}
for _c in CODECS.values():
    _c.file_mode = _c.uri_scheme == "http"   # file mode: Sonos buffer is tiny, so WE add the cushion
DEFAULT_BUFFER_SECONDS = 1.0                 # cushion prefilled on connect for file-mode codecs
BUFFER_CHOICES = [0.3, 0.5, 1.0, 1.5, 2.0, 3.0]


# --------------------------------------------------------------------------- #
# Audio capture + encode + fan-out
# --------------------------------------------------------------------------- #
class Client:
    __slots__ = ("queue", "bytes_sent", "connected_at")

    def __init__(self, maxsize):
        self.queue = queue.Queue(maxsize=maxsize)
        self.bytes_sent = 0
        self.connected_at = time.monotonic()


class AudioBroadcaster:
    """Captures loopback audio, encodes it, and fans the bytes out to HTTP clients."""

    def __init__(self, on_status, codec, queue_seconds=CLIENT_QUEUE_SECONDS):
        self.on_status = on_status
        self.codec = codec
        self.queue_seconds = queue_seconds
        self._clients = {}
        self._clients_lock = threading.Lock()
        self._pcm_queue = queue.Queue(maxsize=200)
        self._stop = threading.Event()
        self._threads = []
        self.running = False

    # -- client plumbing -----------------------------------------------------
    def add_client(self):
        chunk_bytes = max(1, self.codec.bytes_per_second * FRAMES_PER_CHUNK // SAMPLE_RATE)
        maxsize = int(self.queue_seconds * self.codec.bytes_per_second / chunk_bytes)
        c = Client(maxsize)
        with self._clients_lock:
            self._clients[id(c)] = c
        return c

    def remove_client(self, c):
        with self._clients_lock:
            self._clients.pop(id(c), None)

    def clients(self):
        with self._clients_lock:
            return list(self._clients.values())

    def client_count(self):
        return len(self.clients())

    def _broadcast(self, data):
        if not data:
            return
        for c in self.clients():
            q = c.queue
            try:
                q.put_nowait(data)
            except queue.Full:
                # Slow consumer: drop the oldest chunk so latency never grows unbounded.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        if self.running:
            return
        self._stop.clear()
        self.running = True
        self._threads = [
            threading.Thread(target=self._capture_loop, name="capture", daemon=True),
            threading.Thread(target=self._encode_loop, name="encode", daemon=True),
        ]
        for t in self._threads:
            t.start()

    def stop(self):
        self._stop.set()
        self.running = False
        for t in self._threads:
            t.join(timeout=3)
        self._threads = []
        while not self._pcm_queue.empty():
            try:
                self._pcm_queue.get_nowait()
            except queue.Empty:
                break

    # -- capture -------------------------------------------------------------
    def _find_loopback(self, p):
        """Loopback device that mirrors the current default output device."""
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        default_out = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        for lb in p.get_loopback_device_info_generator():
            if default_out["name"] in lb["name"]:
                return lb
        for lb in p.get_loopback_device_info_generator():
            return lb
        raise RuntimeError("No WASAPI loopback device found")

    def _capture_loop(self):
        p = pa.PyAudio()
        stream = None
        try:
            lb = self._find_loopback(p)
            self.on_status(f"Capturing: {lb['name']}")
            native_rate = int(lb["defaultSampleRate"])
            native_ch = int(lb["maxInputChannels"]) or CHANNELS

            def callback(in_data, frame_count, time_info, status):
                if self._stop.is_set():
                    return (None, pa.paComplete)
                pcm = self._to_stream_format(in_data, native_ch, native_rate)
                try:
                    self._pcm_queue.put_nowait(pcm)
                except queue.Full:
                    pass
                return (None, pa.paContinue)

            stream = p.open(
                format=pa.paInt16,
                channels=native_ch,
                rate=native_rate,
                input=True,
                input_device_index=lb["index"],
                frames_per_buffer=FRAMES_PER_CHUNK,
                stream_callback=callback,
            )
            stream.start_stream()
            while not self._stop.is_set() and stream.is_active():
                time.sleep(0.1)
        except Exception as exc:  # noqa: BLE001
            self.on_status(f"Capture error: {exc}")
            self.running = False
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            p.terminate()

    @staticmethod
    def _to_stream_format(data, channels, rate):
        """Coerce captured PCM to 48 kHz stereo int16. Loopback devices are 48k/2ch
        in practice; this guards the odd mono or >2ch endpoint."""
        if channels == CHANNELS and rate == SAMPLE_RATE:
            return data
        n = len(data) // 2
        samples = struct.unpack(f"<{n}h", data)
        frames = [samples[i : i + channels] for i in range(0, n, channels)]
        if channels == 1:
            frames = [(f[0], f[0]) for f in frames]
        else:
            frames = [(f[0], f[1]) for f in frames]
        if rate != SAMPLE_RATE:
            out, ratio = [], rate / SAMPLE_RATE
            total = int(len(frames) / ratio)
            for i in range(total):
                out.append(frames[min(int(i * ratio), len(frames) - 1)])
            frames = out
        flat = [s for f in frames for s in f]
        return struct.pack(f"<{len(flat)}h", *flat)

    # -- encode --------------------------------------------------------------
    def _encode_loop(self):
        """Emit audio at exactly real time.

        WASAPI loopback delivers nothing while no app is rendering, so we must
        synthesise silence, and it must be paced by the wall clock: Sonos plays at
        48 kHz regardless and will stall and rebuffer if we under-deliver. While
        real audio flows we follow the capture clock and re-anchor the schedule;
        only once capture has been quiet for SLACK chunks do we start paying the
        schedule down with silence.
        """
        silence = b"\x00" * (FRAMES_PER_CHUNK * CHANNELS * BYTES_PER_SAMPLE)
        chunk_s = FRAMES_PER_CHUNK / SAMPLE_RATE
        slack = 3                                  # ~72 ms jitter allowance
        emitted = 0
        anchor = time.monotonic()
        while not self._stop.is_set():
            try:
                pcm = self._pcm_queue.get(timeout=chunk_s)
            except queue.Empty:
                pcm = None
            if pcm is not None:
                self._broadcast(self.codec.encode(pcm))
                emitted += 1
                # Real audio is flowing: clear any schedule debt (never run ahead of it).
                anchor = max(anchor, time.monotonic() - emitted * chunk_s)
                continue
            due = int((time.monotonic() - anchor) / chunk_s) - slack
            while emitted < due and not self._stop.is_set():
                self._broadcast(self.codec.encode(silence))
                emitted += 1


# --------------------------------------------------------------------------- #
# HTTP stream server
# --------------------------------------------------------------------------- #
def make_handler(broadcaster, prefill_seconds):
    codec = broadcaster.codec

    class StreamHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt, *args):
            if DEBUG:
                print(f"[http {self.client_address[0]}] " + (fmt % args), flush=True)

        def _debug_request(self):
            if DEBUG:
                hdrs = {k: v for k, v in self.headers.items()
                        if k.lower() in ("range", "user-agent", "icy-metadata", "connection")}
                print(f"[http {self.client_address[0]}] {self.command} {self.path} {hdrs}", flush=True)

        def _ok(self):
            return self.path.split("?")[0] == codec.path

        def _headers(self):
            self.send_response(200)
            self.send_header("Content-Type", codec.content_type)
            self.send_header("icy-name", f"{APP_NAME} - Laptop Audio")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "close")
            self.end_headers()

        def do_HEAD(self):
            self._debug_request()
            if not self._ok():
                self.send_error(404)
                return
            self._headers()

        def do_GET(self):
            self._debug_request()
            if not self._ok():
                self.send_error(404)
                return
            self._headers()
            c = broadcaster.add_client()
            try:
                head = codec.header()
                if prefill_seconds > 0:
                    head += codec.silence(prefill_seconds)
                if head:
                    self.wfile.write(head)
                    self.wfile.flush()
                    c.bytes_sent += len(head)
                while True:
                    try:
                        data = c.queue.get(timeout=5)
                    except queue.Empty:
                        if not broadcaster.running:
                            break
                        continue
                    self.wfile.write(data)
                    self.wfile.flush()
                    c.bytes_sent += len(data)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError, OSError) as exc:
                if DEBUG:
                    print(f"[http {self.client_address[0]}] closed: {exc!r}", flush=True)
            finally:
                broadcaster.remove_client(c)
                if DEBUG:
                    print(f"[http {self.client_address[0]}] disconnected after "
                          f"{time.monotonic() - c.connected_at:.1f}s, {c.bytes_sent} bytes", flush=True)

    return StreamHandler


class _StrictServer(ThreadingHTTPServer):
    # HTTPServer sets allow_reuse_address, which on Windows lets two processes bind the
    # same port and silently splits connections between them. We want bind() to fail.
    allow_reuse_address = False


class StreamServer:
    """Binds the preferred port, or the next free one if another instance holds it."""

    def __init__(self, broadcaster, port=HTTP_PORT, prefill_seconds=None):
        if prefill_seconds is None:
            prefill_seconds = broadcaster.codec.prefill_seconds
        handler = make_handler(broadcaster, prefill_seconds)
        self._server = None
        for candidate in list(range(port, port + 20)) + [0]:
            try:
                self._server = _StrictServer(("0.0.0.0", candidate), handler)
                break
            except OSError:
                continue
        if self._server is None:
            raise RuntimeError("No free port for the audio stream")
        self.port = self._server.server_address[1]
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()


def local_ip_for(target_ip):
    """The LAN address this machine uses to reach the speaker (works with VPNs/Tailscale)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 1400))
        return s.getsockname()[0]
    finally:
        s.close()


def stream_uri(codec, ip, port=HTTP_PORT):
    return f"{codec.uri_scheme}://{ip}:{port}{codec.path}"


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.resizable(False, False)
        self.zones = {}
        self.zone = None
        self.broadcaster = None
        self.server = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.discover)
        self.after(1000, self._poll)

    def _build(self):
        pad = {"padx": 10, "pady": 6}
        frame = ttk.Frame(self, padding=12)
        frame.grid()

        ttk.Label(frame, text="Speaker").grid(row=0, column=0, sticky="w", **pad)
        self.speaker_var = tk.StringVar()
        self.speaker_box = ttk.Combobox(frame, textvariable=self.speaker_var, state="readonly", width=34)
        self.speaker_box.grid(row=0, column=1, sticky="we", **pad)
        self.speaker_box.bind("<<ComboboxSelected>>", lambda _e: self._on_speaker_pick())
        ttk.Button(frame, text="Rescan", command=self.discover, width=8).grid(row=0, column=2, **pad)

        self.group_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Play on the whole Sonos group this speaker belongs to",
                        variable=self.group_var).grid(row=1, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(frame, text="Format").grid(row=2, column=0, sticky="w", **pad)
        self.codec_var = tk.StringVar(value=WavCodec.label)
        self.codec_box = ttk.Combobox(frame, textvariable=self.codec_var, state="readonly", width=34,
                                      values=[c.label for c in CODECS.values()])
        self.codec_box.grid(row=2, column=1, sticky="we", **pad)

        ttk.Label(frame, text="Buffer").grid(row=3, column=0, sticky="w", **pad)
        self.buffer_var = tk.StringVar(value=f"{DEFAULT_BUFFER_SECONDS:g} s")
        self.buffer_box = ttk.Combobox(frame, textvariable=self.buffer_var, state="readonly", width=34,
                                       values=[f"{b:g} s" for b in BUFFER_CHOICES])
        self.buffer_box.grid(row=3, column=1, sticky="we", **pad)
        ttk.Label(frame, text="= delay. Bigger survives longer WiFi hiccups.", foreground="#888").grid(
            row=3, column=2, sticky="w", padx=(0, 10))

        ttk.Label(frame, text="Volume").grid(row=4, column=0, sticky="w", **pad)
        self.volume = tk.IntVar(value=25)
        self.volume_scale = ttk.Scale(frame, from_=0, to=100, orient="horizontal",
                                      variable=self.volume, command=self._on_volume)
        self.volume_scale.grid(row=4, column=1, sticky="we", **pad)
        self.volume_label = ttk.Label(frame, text="25", width=4)
        self.volume_label.grid(row=4, column=2, sticky="w", **pad)

        self.toggle_btn = ttk.Button(frame, text="Start streaming", command=self.toggle, width=20)
        self.toggle_btn.grid(row=5, column=0, columnspan=3, pady=(10, 4))

        self.status = tk.StringVar(value="Looking for Sonos speakers...")
        ttk.Label(frame, textvariable=self.status, wraplength=420, foreground="#555").grid(
            row=6, column=0, columnspan=3, sticky="w", **pad)

        ttk.Label(frame, text="Laptop audio -> WiFi -> Sonos. Breaking up? Raise the buffer one step, or pick the Robust format.",
                  foreground="#888", wraplength=420).grid(row=7, column=0, columnspan=3, sticky="w", padx=10)

    # -- helpers -------------------------------------------------------------
    def set_status(self, text):
        if text != getattr(self, "_last_status", None):
            self._last_status = text
            log(f"status: {text}")
        self.after(0, lambda: self.status.set(text))

    def _codec(self):
        for c in CODECS.values():
            if c.label == self.codec_var.get():
                return c()
        return WavCodec()

    def _buffer_seconds(self):
        try:
            return float(self.buffer_var.get().split()[0])
        except (ValueError, IndexError):
            return DEFAULT_BUFFER_SECONDS

    @property
    def streaming(self):
        return self.broadcaster is not None and self.broadcaster.running

    def discover(self):
        self.set_status("Looking for Sonos speakers...")
        threading.Thread(target=self._discover_worker, daemon=True).start()

    def _discover_worker(self):
        found = soco.discover(timeout=6) or set()
        zones = {}
        for z in sorted(found, key=lambda z: z.player_name):
            try:
                model = z.get_speaker_info().get("model_name", "")
            except Exception:  # noqa: BLE001
                model = ""
            zones[f"{z.player_name}  ({model})"] = z

        def apply():
            self.zones = zones
            names = list(zones)
            self.speaker_box["values"] = names
            if names:
                current = self.speaker_var.get()
                self.speaker_var.set(current if current in names else names[0])
                self._on_speaker_pick()
                self.set_status(f"Found {len(names)} speaker(s). Pick one and press Start.")
            else:
                self.set_status("No Sonos speakers found. Is the laptop on the same WiFi?")
        self.after(0, apply)

    def _on_speaker_pick(self):
        self.zone = self.zones.get(self.speaker_var.get())
        if self.zone is not None:
            try:
                v = self.zone.volume
                self.volume.set(v)
                self.volume_label.config(text=str(v))
            except Exception:  # noqa: BLE001
                pass

    def _on_volume(self, _value):
        v = int(float(_value))
        self.volume_label.config(text=str(v))
        if self.zone is None:
            return
        target = self.zone.group.coordinator if self.group_var.get() else self.zone
        threading.Thread(target=self._set_volume, args=(target, v), daemon=True).start()

    @staticmethod
    def _set_volume(zone, v):
        try:
            zone.volume = v
        except Exception:  # noqa: BLE001
            pass

    def _target_zone(self):
        """Who to tell to play: the group coordinator or the speaker itself."""
        if self.zone is None:
            return None
        if self.group_var.get():
            return self.zone.group.coordinator
        if not self.zone.is_coordinator:
            self.zone.unjoin()
        return self.zone

    # -- start / stop --------------------------------------------------------
    def toggle(self):
        if self.streaming:
            self.stop_stream()
        else:
            self.start_stream()

    def start_stream(self):
        if self.zone is None:
            self.set_status("Pick a speaker first.")
            return
        self.toggle_btn.config(state="disabled")
        self.codec_box.config(state="disabled")
        self.buffer_box.config(state="disabled")
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self):
        try:
            target = self._target_zone()
            ip = local_ip_for(target.ip_address)
            codec = self._codec()
            prefill = self._buffer_seconds() if codec.file_mode else codec.prefill_seconds
            self.broadcaster = AudioBroadcaster(self.set_status, codec, queue_seconds=prefill + 0.75)
            self.server = StreamServer(self.broadcaster, prefill_seconds=prefill)
            self.server.start()
            self.broadcaster.start()
            time.sleep(0.3)
            port = self.server.port
            self._play = (target, stream_uri(codec, ip, port), codec.uri_scheme != "http")
            self._last_resume = 0.0
            log(f"start: {target.player_name} <- {self._play[1]} codec={codec.name} buffer={prefill:g}s")
            target.play_uri(self._play[1], title=f"{APP_NAME} - Laptop Audio", force_radio=self._play[2])
            self.after(0, lambda: self.toggle_btn.config(text="Stop streaming", state="normal"))
            self.set_status(f"Streaming to {target.player_name} ({codec.name.upper()}) from {ip}:{port}")
        except Exception as exc:  # noqa: BLE001
            self._teardown()
            self.after(0, lambda: self.toggle_btn.config(text="Start streaming", state="normal"))
            self.after(0, lambda: self.codec_box.config(state="readonly"))
            self.after(0, lambda: self.buffer_box.config(state="readonly"))
            self.set_status(f"Could not start: {exc}")

    def stop_stream(self):
        self.toggle_btn.config(state="disabled")
        threading.Thread(target=self._stop_worker, daemon=True).start()

    def _stop_worker(self):
        self._play = None
        try:
            if self.zone is not None:
                target = self.zone.group.coordinator if self.group_var.get() else self.zone
                try:
                    target.stop()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            self._teardown()
            self.after(0, lambda: self.toggle_btn.config(text="Start streaming", state="normal"))
            self.after(0, lambda: self.codec_box.config(state="readonly"))
            self.after(0, lambda: self.buffer_box.config(state="readonly"))
            self.set_status("Stopped.")

    def _teardown(self):
        if self.broadcaster is not None:
            self.broadcaster.stop()
            self.broadcaster = None
        if self.server is not None:
            try:
                self.server.stop()
            except Exception:  # noqa: BLE001
                pass
            self.server = None

    def _poll(self):
        """Watch for the Sonos app stopping the stream from its side."""
        if self.streaming and self.zone is not None:
            threading.Thread(target=self._poll_worker, daemon=True).start()
        self.after(3000, self._poll)

    def _poll_worker(self):
        try:
            bc = self.broadcaster
            play = getattr(self, "_play", None)
            if bc is None or play is None:
                return
            target, uri, radio = play
            info = target.get_current_transport_info()
            state = info.get("current_transport_state")
            clients = bc.client_count()
            if state != getattr(self, "_last_state", None):
                cur_uri = target.get_current_track_info().get("uri", "")
                log(f"sonos: {target.player_name} state={state} status={info.get('current_transport_status')} "
                    f"listeners={clients} uri={cur_uri[:90]}")
                self._last_state = state
            if state == "PLAYING":
                self.set_status(f"Streaming to {target.player_name} ({bc.codec.name.upper()})  "
                                f"state: PLAYING, {clients} listener(s)")
            elif state == "STOPPED" and bc.running:
                cur_uri = target.get_current_track_info().get("uri", "")
                if "htastream" in cur_uri or "line-in" in cur_uri or "x-rincon-stream" in cur_uri:
                    self.set_status(f"{target.player_name} switched to its TV/line input. "
                                    f"Turn the TV off and press Start again.")
                elif time.monotonic() - self._last_resume > 10:
                    self._last_resume = time.monotonic()
                    log(f"resume: {target.player_name} stopped on its own, re-issuing play")
                    self.set_status(f"{target.player_name} stopped; restarting the stream...")
                    target.play_uri(uri, title=f"{APP_NAME} - Laptop Audio", force_radio=radio)
            elif state == "PAUSED_PLAYBACK":
                self.set_status(f"{target.player_name} is paused. Press play in the Sonos app or Start again.")
        except Exception as exc:  # noqa: BLE001
            log(f"poll error: {exc!r}")

    def on_close(self):
        if self.streaming:
            self._stop_worker()
        self.destroy()


# --------------------------------------------------------------------------- #
# Headless self-test / latency probe
# --------------------------------------------------------------------------- #
def selftest(speaker_name, codec_name="wav", prefill=None, seconds=10):
    """Stream to a named speaker, report when it starts PLAYING and how many seconds
    of audio it had buffered at that moment (= the delay you will hear), then stop."""
    import urllib.request

    codec = CODECS[codec_name]()
    if prefill is None:
        prefill = DEFAULT_BUFFER_SECONDS if codec.file_mode else codec.prefill_seconds
    zones = soco.discover(timeout=6) or set()
    print("speakers:", ", ".join(sorted(z.player_name for z in zones)) or "none")
    zone = next((z for z in zones if z.player_name.lower() == speaker_name.lower()), None)
    if zone is None:
        print(f"speaker '{speaker_name}' not found")
        return 1
    if not zone.is_coordinator:
        zone.unjoin()
    bc = AudioBroadcaster(lambda s: print("status:", s), codec, queue_seconds=prefill + 0.75)
    srv = StreamServer(bc, prefill_seconds=prefill)
    srv.start()
    bc.start()
    time.sleep(0.5)
    ip = local_ip_for(zone.ip_address)
    print(f"serving on {ip}:{srv.port}")
    with urllib.request.urlopen(f"http://{ip}:{srv.port}{codec.path}", timeout=5) as r:
        print("local fetch ok, bytes:", len(r.read(4096)))
    time.sleep(0.5)
    prev_vol = zone.volume
    zone.volume = 15
    t0 = time.monotonic()
    zone.play_uri(stream_uri(codec, ip, srv.port), title=f"{APP_NAME} selftest",
                  force_radio=(codec.uri_scheme != "http"))
    state, started_at, buffered = None, None, None
    deadline = t0 + seconds
    while time.monotonic() < deadline:
        time.sleep(0.25)
        state = zone.get_current_transport_info()["current_transport_state"]
        if state == "PLAYING" and started_at is None:
            started_at = time.monotonic() - t0
            sent = max((c.bytes_sent for c in bc.clients()), default=0)
            buffered = (sent - len(codec.header())) / codec.bytes_per_second
            print(f"PLAYING after {started_at:.2f}s; Sonos had pulled {buffered:.2f}s of audio "
                  f"(prefill {prefill:.2f}s) -> expected delay ~{buffered:.2f}s")
            break
    if started_at is not None:
        # Each second: audio handed to the socket vs. audio that exists (prefill + wall time
        # since connect). 'ours' = stuck in our queue; 'inflight' = TCP buffers / not yet read.
        print("timeline after PLAYING (seconds of audio):")
        for _ in range(6):
            time.sleep(1)
            cl = bc.clients()
            if not cl:
                print("  client gone")
                break
            c = max(cl, key=lambda c: c.bytes_sent)
            sent_s = (c.bytes_sent - len(codec.header())) / codec.bytes_per_second
            exist_s = prefill + (time.monotonic() - c.connected_at)
            ours_s = c.queue.qsize() * FRAMES_PER_CHUNK / SAMPLE_RATE
            print(f"  exist={exist_s:5.2f}  sent={sent_s:5.2f}  not_sent={exist_s - sent_s:5.2f}  "
                  f"our_queue={ours_s:4.2f}  pos={zone.get_current_track_info().get('position')}")
    print(f"codec={codec.name} final state={state} listeners={bc.client_count()}")
    zone.stop()
    zone.volume = prev_vol
    bc.stop()
    srv.stop()
    print("PASS" if state == "PLAYING" else "FAIL")
    return 0 if state == "PLAYING" else 2


def _log_path():
    import sys
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    return os.path.join(base, "sonoscast.log")


def log(msg):
    """Timestamped line in sonoscast.log beside the exe (and on the console when there is one)."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    import sys
    if sys.stdout is not None and not _STDOUT_IS_LOG:
        print(line, flush=True)


_STDOUT_IS_LOG = False


def _ensure_console_output():
    """A --windowed PyInstaller exe has no stdout; send prints to the log beside the exe."""
    import sys
    global _STDOUT_IS_LOG
    if sys.stdout is None or sys.stderr is None:
        _STDOUT_IS_LOG = True
        logf = open(_log_path(), "a", buffering=1, encoding="utf-8")
        sys.stdout = sys.stdout or logf
        sys.stderr = sys.stderr or logf


if __name__ == "__main__":
    import sys

    _ensure_console_output()
    log(f"--- {APP_NAME} starting ({'exe' if getattr(sys, 'frozen', False) else 'script'}) ---")
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        # sonoscast.py --selftest <Speaker> [wav|mp3] [prefill_seconds]
        codec_name = sys.argv[3] if len(sys.argv) > 3 else "wav"
        prefill = float(sys.argv[4]) if len(sys.argv) > 4 else None
        sys.exit(selftest(sys.argv[2], codec_name, prefill))
    App().mainloop()
