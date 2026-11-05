#!/usr/bin/env python3
"""BELABOX PiP-Mixer.

Holt den RTMP-Eingang (z. B. Larix -> nginx), dekodiert ihn auf eine kleine Fläche
(Standard 400x300 RGBA) und gibt sie mit festen 30 fps per Shared Memory (shmsink) an die
belacoder-Pipeline weiter, die sie per shmsrc als PiP einblendet.

Kommen kurz keine Bilder vom Handy, liefert der Mixer eine vollständig transparente Fläche
(alle Pixel (0,0,0,0)): Das PiP verschwindet, statt einzufrieren. Sobald wieder Bilder kommen,
ist es wieder da. Die Ausgabe läuft immer weiter, damit belacoder nie wegen des Handys abbricht.

Aufbau:
  Pipeline A (bei Fehler oder Stillstand neu gestartet, auch bei hängendem Decoder):
    rtmpsrc -> flvdemux -> h264parse -> nvv4l2decoder -> nvvidconv -> RGBA WxH -> appsink
  Pipeline B (läuft dauerhaft):
    appsrc (30 fps aus Python: letztes Handybild oder leer) -> shmsink SOCKET

Warum so und nicht mit compositor/input-selector (GStreamer 1.14 auf der BELABOX):
  - nvcompositor rechnet mit vormultipliziertem Alpha: unsichtbar ist nur exakt (0,0,0,0).
    compositor füllt "transparent" mit (255,255,255,0) -> weiße Fläche.
  - Mit compositor oder input-selector lief der Decoder-Zweig nur unzuverlässig an
    (Zeitstempel aus tsdemux bzw. blockierender inaktiver Eingang).
"""

import os
import signal
import sys
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

RTMP_URL = os.environ.get("PIP_RTMP_URL", "rtmp://127.0.0.1/publish/live")
SOCKET = os.environ.get("PIP_SOCKET", "/run/belabox-pip/pip.sock")
WIDTH = int(os.environ.get("PIP_WIDTH", "400"))
HEIGHT = int(os.environ.get("PIP_HEIGHT", "300"))
FPS = int(os.environ.get("PIP_FPS", "30"))
HIDE_AFTER = float(os.environ.get("PIP_HIDE_AFTER", "0.5"))     # s ohne Bild -> unsichtbar
RESTART_AFTER = float(os.environ.get("PIP_RESTART_AFTER", "3"))  # s ohne Bild -> A neu starten
RETRY = float(os.environ.get("PIP_RETRY", "2"))                  # s zwischen Verbindungsversuchen

FRAME_SIZE = WIDTH * HEIGHT * 4
CAPS = "video/x-raw,format=RGBA,width={},height={},framerate={}/1".format(WIDTH, HEIGHT, FPS)


def log(msg):
    # immer UTF-8 (Box läuft ohne passende Locale, Python 3.6 nimmt sonst ASCII)
    sys.stdout.buffer.write("{} {}\n".format(time.strftime("%H:%M:%S"), msg).encode("utf-8"))
    sys.stdout.buffer.flush()


class Mixer:
    def __init__(self):
        self.loop = GLib.MainLoop()
        self.empty = Gst.Buffer.new_wrapped(bytes(FRAME_SIZE))  # (0,0,0,0) = unsichtbar
        self.latest = None        # letztes Handybild (Gst.Buffer)
        self.last_frame = 0.0     # Zeitpunkt des letzten Handybilds
        self.visible = False
        self.a = None
        self.a_started = 0.0
        self.a_restart_pending = False
        self.a_had_frames = False

        os.makedirs(os.path.dirname(SOCKET), exist_ok=True)
        if os.path.exists(SOCKET):
            os.unlink(SOCKET)

        self.b = Gst.parse_launch(
            "appsrc name=out is-live=true do-timestamp=true format=time block=false "
            "max-bytes={mb} caps=\"{caps}\" ! "
            "shmsink socket-path={sock} shm-size={shm} wait-for-connection=false sync=false"
            .format(caps=CAPS, sock=SOCKET, shm=FRAME_SIZE * 10, mb=FRAME_SIZE * 3))
        self.out = self.b.get_by_name("out")
        bus = self.b.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_b_message)

    # --- Pipeline A: RTMP -> dekodierte Bilder ---------------------------------------------
    def start_a(self):
        self.a_restart_pending = False
        self.a_had_frames = False
        self.a = Gst.parse_launch(
            "rtmpsrc location=\"{url} live=1\" timeout=5 ! flvdemux name=d "
            "d.video ! queue max-size-time=2000000000 leaky=downstream ! "
            "h264parse ! nvv4l2decoder ! nvvidconv ! "
            "video/x-raw,format=RGBA,width={w},height={h} ! "
            "appsink name=frames emit-signals=true max-buffers=1 drop=true sync=false"
            .format(url=RTMP_URL, w=WIDTH, h=HEIGHT))
        self.a.get_by_name("frames").connect("new-sample", self._on_sample)
        bus = self.a.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_a_message)
        self.a_started = time.monotonic()
        self.a.set_state(Gst.State.PLAYING)
        return False

    def stop_a(self):
        if self.a is not None:
            self.a.get_bus().remove_signal_watch()
            self.a.set_state(Gst.State.NULL)
            self.a = None

    def restart_a(self, reason):
        if self.a_restart_pending:
            return
        if self.a_had_frames:
            log("Handy-Bild weg ({})".format(reason))
        self.a_restart_pending = True
        self.stop_a()
        GLib.timeout_add(int(RETRY * 1000), self.start_a)

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer() if sample else None
        if buf is not None and buf.get_size() == FRAME_SIZE:
            self.latest = buf
            self.last_frame = time.monotonic()
            if not self.a_had_frames:
                self.a_had_frames = True
                GLib.idle_add(log, "Handy-Bild empfangen")
        return Gst.FlowReturn.OK

    def _on_a_message(self, bus, msg):
        if msg.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            reason = msg.parse_error()[0].message if msg.type == Gst.MessageType.ERROR else "EOS"
            self.restart_a(reason)

    # --- Pipeline B: Ausgabe ---------------------------------------------------------------
    def _on_b_message(self, bus, msg):
        if msg.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            err = msg.parse_error()[0].message if msg.type == Gst.MessageType.ERROR else "EOS"
            log("Ausgabe-Pipeline beendet: {} - Neustart durch systemd".format(err))
            self.loop.quit()

    def push_frame(self):
        now = time.monotonic()
        visible = self.latest is not None and (now - self.last_frame) < HIDE_AFTER
        if visible != self.visible:
            self.visible = visible
            log("PiP " + ("sichtbar" if visible else "ausgeblendet"))
        # copy(): neuer Puffer (gleicher Speicher), damit appsrc den Zeitstempel setzen kann
        self.out.emit("push-buffer", (self.latest if visible else self.empty).copy())
        return True

    def watchdog(self):
        # Stillstand von A erkennen (rtmpsrc meldet einen Abbruch nicht immer, Decoder kann hängen)
        if self.a is not None and not self.a_restart_pending:
            now = time.monotonic()
            ref = max(self.last_frame, self.a_started)
            if now - ref > RESTART_AFTER:
                self.restart_a("seit {:.0f} s kein Bild".format(now - ref))
        return True

    def run(self):
        self.b.set_state(Gst.State.PLAYING)
        self.start_a()
        GLib.timeout_add(int(1000 / FPS), self.push_frame)
        GLib.timeout_add(250, self.watchdog)
        for s in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, s, self.loop.quit)
        log("PiP-Mixer läuft: {} -> {} ({}x{}, {} fps)".format(RTMP_URL, SOCKET, WIDTH, HEIGHT, FPS))
        self.loop.run()
        self.stop_a()
        self.b.set_state(Gst.State.NULL)
        return 0


if __name__ == "__main__":
    Gst.init(None)
    sys.exit(Mixer().run())
