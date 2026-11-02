#!/usr/bin/env python3
"""
DJI FPV Goggles V2 - USB Video Player
=====================================

Zeigt den echten USB-Video-Out-Stream der Goggles an (kein UVC, kein ADB).

Start:
    start_usb_player.bat
oder:
    .venv\\Scripts\\python.exe dji_usb_player.py

Diagnose (wenn kein Bild kommt):
    diagnose_usb.bat
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk

try:
    import av
except ImportError:
    raise SystemExit("PyAV fehlt. Bitte ausfuehren: pip install -r requirements.txt")

from dji_usb import (
    DJI_VID,
    GogglesError,
    GogglesNotFound,
    GogglesUSB,
    VideoStreamReader,
    default_record_dir,
    normalize_annexb_headers,
    remux_h264_to_mp4,
)


# --------------------------------------------------------------------------
# Capture-Worker (USB -> PyAV -> Frames)
# --------------------------------------------------------------------------

def choose_fps(measured: float, detected: float, default: float = 60.0) -> float:
    """
    Waehlt eine plausible Bildrate fuer den MP4-Remux.

    `detected` kommt aus dem SPS/VUI, `measured` aus der tatsaechlichen
    Decode-Rate. Weichen beide stark ab, ist der SPS-Wert meist die
    bessere Wahl (z.B. wenn die Dekodierung nicht mitkam); bei kleinen
    Abweichungen ist die gemessene Rate genauer.
    """
    measured = float(measured or 0.0)
    detected = float(detected or 0.0)
    measured_ok = 1.0 <= measured <= 240.0
    detected_ok = 1.0 <= detected <= 240.0

    common = (23.976, 24.0, 25.0, 29.97, 30.0, 48.0, 50.0, 59.94,
              60.0, 90.0, 100.0, 120.0, 144.0, 240.0)

    if measured_ok and detected_ok:
        if abs(measured - detected) <= detected * 0.10:
            return measured
        if any(abs(detected - c) < 0.5 for c in common):
            return detected
        return measured
    if detected_ok:
        return detected
    if measured_ok:
        return measured
    return default


class CaptureWorker(threading.Thread):
    def __init__(
        self,
        pid: Optional[int],
        codec: str,
        stop_event: threading.Event,
        frame_queue: "queue.Queue[np.ndarray]",
        on_status,
        record_dir: Optional[str] = None,
        verbose: bool = False,
    ):
        super().__init__(daemon=True, name="DJI-Capture")
        self.pid = pid
        self.codec = codec
        self.stop_event = stop_event
        self.frame_queue = frame_queue
        self.on_status = on_status
        self.record_dir = record_dir or default_record_dir()
        self.verbose = verbose

        self.goggles: Optional[GogglesUSB] = None
        self.reader: Optional[VideoStreamReader] = None

        self.frames_decoded = 0
        self.width = 0
        self.height = 0
        self.fps_hint = 60.0        # aus SPS/VUI, oft unzuverlaessig
        self.fps_measured = 0.0     # aus tatsaechlicher Decode-Rate
        self.extradata = b""        # SPS/PPS des Live-Streams (Annex-B)
        self.device_label = ""
        self.last_error: Optional[str] = None

        self._fps_samples: "deque[float]" = deque(maxlen=240)
        self._fps_first_frame = 0.0
        self._fps_last_status = 0.0

        self._rec_lock = threading.Lock()
        self._rec_fh = None
        self._rec_raw_path: Optional[str] = None

    # -- Status ------------------------------------------------------------
    def _status(self, text: str, error: bool = False) -> None:
        if error:
            self.last_error = text
        try:
            self.on_status(text, error)
        except Exception:
            pass

    # -- FPS-Messung -------------------------------------------------------
    def _track_fps(self) -> None:
        """
        Misst die reale Bilderrate aus den Ankunftszeiten der Frames.

        Es werden nur die letzten 2 s betrachtet. Ein eventueller
        Start-Burst (gepufferte Bilder) faellt dadurch schnell aus der
        Messung, statt die Rate dauerhaft zu verfaelschen.
        """
        now = time.monotonic()
        self._fps_samples.append(now)
        cutoff = now - 2.0
        while self._fps_samples and self._fps_samples[0] < cutoff:
            self._fps_samples.popleft()
        span = self._fps_samples[-1] - self._fps_samples[0]
        if span >= 0.5 and len(self._fps_samples) >= 8:
            self.fps_measured = (len(self._fps_samples) - 1) / span

    def recording_fps(self) -> float:
        """
        Bildrate fuer den MP4-Remux.

        Kombiniert die gemessene Decode-Rate mit dem SPS/VUI-Wert: kleine
        Abweichungen -> Messung, grosse Abweichung -> SPS (Dekodierung
        kam vermutlich nicht mit).
        """
        return choose_fps(self.fps_measured, self.fps_hint)

    # -- Frames ------------------------------------------------------------
    def _emit(self, frame: np.ndarray) -> None:
        if self.stop_event.is_set():
            return
        try:
            self.frame_queue.put_nowait(frame)
        except queue.Full:
            try:
                self.frame_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.frame_queue.put_nowait(frame)
            except queue.Full:
                pass

    # -- Aufnahme ----------------------------------------------------------
    def start_recording(self) -> str:
        with self._rec_lock:
            if self._rec_fh is not None:
                return self._rec_raw_path or ""
            os.makedirs(self.record_dir, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            raw = os.path.join(self.record_dir, f"dji_goggles_{stamp}.h264")
            fh = open(raw, "wb")
            # SPS/PPS voranstellen: eine Aufnahme, die mitten in einer GOP
            # startet, waere sonst nicht dekodierbar/konvertierbar.
            headers = normalize_annexb_headers(self.extradata)
            if headers:
                fh.write(headers)
                fh.flush()
            self._rec_fh = fh
            self._rec_raw_path = raw
            if self.reader is not None:
                self.reader.set_record_file(fh)
            return raw

    def stop_recording(self) -> tuple[Optional[str], Optional[str]]:
        with self._rec_lock:
            fh = self._rec_fh
            raw = self._rec_raw_path
            self._rec_fh = None
            self._rec_raw_path = None
        if fh is None or raw is None:
            return None, None

        if self.reader is not None:
            self.reader.set_record_file(None)
        try:
            fh.flush()
            fh.close()
        except Exception:
            pass

        if not os.path.exists(raw) or os.path.getsize(raw) < 1024:
            return raw, None

        mp4 = os.path.splitext(raw)[0] + ".mp4"
        fps = self.recording_fps()
        self._status(f"Konvertiere Aufnahme nach MP4 ({fps:.1f} fps) ...")
        ok = remux_h264_to_mp4(raw, mp4, fps, self.extradata)
        if ok:
            try:
                os.remove(raw)
            except Exception:
                pass
            return raw, mp4
        return raw, None

    def is_recording(self) -> bool:
        with self._rec_lock:
            return self._rec_fh is not None

    # -- Thread ------------------------------------------------------------
    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._session()
                break
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                msg = str(exc) or exc.__class__.__name__
                self._status(msg, error=True)
                # 2 s warten, dann erneut versuchen (Reconnect).
                if self.stop_event.wait(2.0):
                    break

    def _session(self) -> None:
        self._fps_samples.clear()
        self.fps_measured = 0.0
        self._fps_first_frame = 0.0
        self._fps_last_status = 0.0
        self._status("Suche Goggles ...")
        goggles = GogglesUSB(self.pid, verbose=self.verbose)
        goggles.open()
        self.goggles = goggles

        label = f"DJI {DJI_VID:04X}:{goggles.dev.idProduct:04X}"
        self.device_label = label
        self._status(f"Verbunden: {label} - starte Video-Out ...")

        try:
            goggles.send_magic()
        except Exception as exc:
            raise GogglesError(f"Magic-Paket konnte nicht gesendet werden.\n{exc}") from exc

        # Die Goggles puffern weiter, waehrend niemand liest. Ohne diesen
        # Schritt startet der Player mit ein paar Sekunden altem Material
        # (sichtbares Vorspulen am Anfang).
        self._status(f"Verbunden: {label} - synchronisiere Stream ...")
        fmt = "hevc" if self.codec == "hevc" else "h264"
        try:
            drained = goggles.drain()
            _, prefix = goggles.read_to_keyframe(codec=fmt)
        except Exception as exc:
            raise GogglesError(f"Video-Stream konnte nicht synchronisiert werden.\n{exc}") from exc
        if drained and self.verbose:
            print(f"[usb] Backlog verworfen: {drained / 1e6:.1f} MB", file=sys.stderr)

        reader = VideoStreamReader(
            goggles, self.stop_event, verbose=self.verbose, prefix=prefix
        )
        self.reader = reader

        with self._rec_lock:
            if self._rec_fh is not None:
                reader.set_record_file(self._rec_fh)

        options = {
            "flags": "low_delay",
            "probesize": "262144",
            "analyzeduration": "500000",
        }

        container = None
        try:
            # Die Goggles liefern (bei PID 0x001F) immer H.264. Der rohe
            # h264-Demuxer kommt auch mit einem Start mitten in der GOP
            # klar; die Auto-Erkennung scheitert dort oft, weil SPS/PPS
            # erst beim naechsten Keyframe auftauchen.
            container = av.open(reader, format=fmt, options=options)
            stream = container.streams.video[0]
            try:
                self.extradata = bytes(stream.codec_context.extradata or b"")
            except Exception:
                self.extradata = b""
            try:
                stream.thread_type = "AUTO"
            except Exception:
                pass

            try:
                # guessed_rate kommt bei rohem H.264 aus dem SPS/VUI und ist
                # meist genauer als average_rate (das oft nur 25/1 ist).
                rate = stream.guessed_rate or stream.average_rate
                if rate:
                    self.fps_hint = float(rate)
            except Exception:
                pass

            self._status(
                f"Stream aktiv ({'HEVC' if fmt == 'hevc' else 'H.264'}) "
                f"- warte auf Bilder ..."
            )

            for frame in container.decode(stream):
                if self.stop_event.is_set():
                    break
                self.frames_decoded += 1
                self.width = frame.width
                self.height = frame.height
                self._track_fps()

                if not self.extradata:
                    try:
                        ed = bytes(stream.codec_context.extradata or b"")
                        if ed:
                            self.extradata = ed
                    except Exception:
                        pass

                if self.frames_decoded == 1:
                    self._fps_first_frame = time.monotonic()
                    self._status(
                        f"Live: {self.width}x{self.height} "
                        f"(Bildrate wird gemessen, {label})"
                    )
                elif self._fps_first_frame:
                    # Erst nach 2,5 s veroeffentlichen: dann ist ein
                    # eventueller Start-Burst aus der Messung gefallen.
                    now = time.monotonic()
                    if (now - self._fps_first_frame >= 2.5
                            and now - self._fps_last_status >= 2.0
                            and 1.0 <= self.fps_measured <= 240.0):
                        self._fps_last_status = now
                        self._status(
                            f"Live: {self.width}x{self.height} @ "
                            f"{self.fps_measured:.1f} fps ({label})"
                        )

                self._emit(frame.to_ndarray(format="bgr24"))
        except Exception as exc:
            if reader.error is not None:
                raise reader.error
            raise GogglesError(f"Video-Stream konnte nicht gelesen werden.\n{exc}") from exc
        finally:
            try:
                if container is not None:
                    container.close()
            except Exception:
                pass
            try:
                reader.close()
            except Exception:
                pass
            self.reader = None
            try:
                goggles.close()
            except Exception:
                pass
            self.goggles = None


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class PlayerApp:
    def __init__(self, root: tk.Tk, args):
        self.root = root
        self.args = args
        self.root.title("DJI FPV Goggles V2 - USB Video Player")
        self.root.geometry("1200x750")
        self.root.minsize(720, 480)
        self.root.configure(bg="#101014")

        self.stop_event = threading.Event()
        self.frame_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=2)
        self.worker: Optional[CaptureWorker] = None

        self.last_frame: Optional[np.ndarray] = None
        self.last_frame_id = 0
        self.rendered_frame_id = 0
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._canvas_image = None
        self._render_size = (0, 0)

        self.recording = False
        self._stats_t = time.monotonic()
        self._stats_bytes = 0
        self._stats_frames = 0
        self._mbps = 0.0
        self._fps = 0.0
        self._status_text = "Bereit."
        self._status_error = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(15, self._tick_video)
        self.root.after(500, self._tick_stats)

        if args.autostart:
            self.root.after(300, self.start)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self) -> None:
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background="#18181f")
        style.configure("TButton", padding=(10, 5))
        style.configure("Accent.TButton", foreground="#ffffff", background="#d62828")
        style.map("Accent.TButton", background=[("active", "#b31f1f")])

        top = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        top.pack(side="top", fill="x")

        self.btn_connect = ttk.Button(top, text="▶  Verbinden", command=self.start)
        self.btn_connect.pack(side="left", padx=(0, 6))

        self.btn_disconnect = ttk.Button(top, text="■  Trennen", command=self.stop)
        self.btn_disconnect.pack(side="left", padx=(0, 6))
        self.btn_disconnect.state(["disabled"])

        self.btn_record = ttk.Button(top, text="⏺  Aufnahme", command=self.toggle_record)
        self.btn_record.pack(side="left", padx=(0, 6))
        self.btn_record.state(["disabled"])

        self.btn_snapshot = ttk.Button(top, text="📷  Snapshot", command=self.snapshot)
        self.btn_snapshot.pack(side="left", padx=(0, 6))

        self.btn_fullscreen = ttk.Button(top, text="⛶  Vollbild", command=self.toggle_fullscreen)
        self.btn_fullscreen.pack(side="left", padx=(0, 6))

        self.lbl_path = ttk.Label(top, text="", background="#18181f", foreground="#9aa0a6")
        self.lbl_path.pack(side="right")

        self.canvas = tk.Canvas(
            self.root, bg="#000000", highlightthickness=0, bd=0,
        )
        self.canvas.pack(side="top", fill="both", expand=True)
        self.canvas.bind("<Double-Button-1>", lambda _e: self.toggle_fullscreen())

        bottom = ttk.Frame(self.root, padding=(8, 5))
        bottom.pack(side="bottom", fill="x")

        self.lbl_status = tk.Label(
            bottom, text="Bereit.", anchor="w", bg="#18181f", fg="#d5d8dc",
            font=("Segoe UI", 9),
        )
        self.lbl_status.pack(side="left", fill="x", expand=True)

        self.lbl_stats = tk.Label(
            bottom, text="", anchor="e", bg="#18181f", fg="#8fd18f",
            font=("Consolas", 9),
        )
        self.lbl_stats.pack(side="right")

        self.canvas.create_text(
            400, 300, text="Goggles verbinden und auf 'Verbinden' klicken",
            fill="#555a63", font=("Segoe UI", 14), tags="hint",
        )

    def _set_status(self, text: str, error: bool = False) -> None:
        self._status_text = text
        self._status_error = error
        self.lbl_status.configure(text=text, fg="#ff7b72" if error else "#d5d8dc")

    def _ui_status(self, text: str, error: bool = False) -> None:
        # Wird aus dem Worker-Thread aufgerufen.
        self.root.after(0, lambda: self._set_status(text, error))

    # -- Steuerung ---------------------------------------------------------
    def start(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.last_frame = None
        self.last_frame_id = 0
        self.rendered_frame_id = 0
        self._photo = None
        self._render_size = (0, 0)
        self.canvas.delete("hint")

        self.worker = CaptureWorker(
            pid=self.args.pid,
            codec=self.args.codec,
            stop_event=self.stop_event,
            frame_queue=self.frame_queue,
            on_status=self._ui_status,
            record_dir=self.args.record_dir,
            verbose=self.args.verbose,
        )
        self.worker.start()

        self.btn_connect.state(["disabled"])
        self.btn_disconnect.state(["!disabled"])
        self.btn_record.state(["!disabled"])
        self._stats_t = time.monotonic()
        self._stats_bytes = 0
        self._stats_frames = 0

    def stop(self) -> None:
        was_recording = self.recording
        self.stop_event.set()
        if self.worker is not None:
            self.worker.join(timeout=3.0)

        if was_recording:
            self._finish_recording(popup=False)

        self.worker = None
        self.recording = False
        self.btn_connect.state(["!disabled"])
        self.btn_disconnect.state(["disabled"])
        self.btn_record.state(["disabled"])
        self.btn_record.configure(text="⏺  Aufnahme")
        self._set_status("Getrennt.")
        self.lbl_stats.configure(text="")
        self._show_hint("Goggles verbinden und auf 'Verbinden' klicken")

    def _show_hint(self, text: str) -> None:
        self.canvas.delete("hint")
        w = max(self.canvas.winfo_width(), 400)
        h = max(self.canvas.winfo_height(), 300)
        self.canvas.create_text(
            w // 2, h // 2, text=text, fill="#555a63",
            font=("Segoe UI", 14), tags="hint",
        )

    # -- Aufnahme ----------------------------------------------------------
    def toggle_record(self) -> None:
        if self.worker is None or not self.worker.is_alive():
            return
        if not self.recording:
            path = self.worker.start_recording()
            self.recording = True
            self.btn_record.configure(text="■  Aufnahme stoppen")
            self.lbl_path.configure(text=f"Aufnahme: {os.path.basename(path)}")
            self._set_status(f"Aufnahme laeuft -> {path}")
        else:
            self._finish_recording(popup=True)

    def _finish_recording(self, popup: bool) -> None:
        worker = self.worker
        self.recording = False
        self.btn_record.configure(text="⏺  Aufnahme")
        if worker is None:
            return
        raw, mp4 = worker.stop_recording()
        if mp4:
            self._set_status(f"Aufnahme gespeichert: {mp4}")
            self.lbl_path.configure(text=os.path.basename(mp4))
            if popup:
                messagebox.showinfo("Aufnahme", f"Gespeichert:\n{mp4}")
        elif raw:
            self._set_status(f"Aufnahme als Rohstream gespeichert: {raw}")
            if popup:
                messagebox.showwarning(
                    "Aufnahme",
                    "MP4-Konvertierung fehlgeschlagen.\n"
                    f"Der Rohstream liegt hier:\n{raw}\n\n"
                    "Du kannst ihn z.B. mit ffmpeg konvertieren.",
                )
        else:
            self._set_status("Keine Aufnahmedaten erhalten.", error=True)

    def snapshot(self) -> None:
        if self.last_frame is None:
            messagebox.showinfo("Snapshot", "Noch kein Bild vorhanden.")
            return
        os.makedirs(default_record_dir(), exist_ok=True)
        path = os.path.join(
            default_record_dir(),
            f"snapshot_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.png",
        )
        cv2.imwrite(path, self.last_frame)
        self._set_status(f"Snapshot gespeichert: {path}")

    def toggle_fullscreen(self) -> None:
        try:
            full = bool(self.root.attributes("-fullscreen"))
            self.root.attributes("-fullscreen", not full)
        except Exception:
            pass

    # -- Anzeige -----------------------------------------------------------
    def _tick_video(self) -> None:
        got_new = False
        while True:
            try:
                self.last_frame = self.frame_queue.get_nowait()
                self.last_frame_id += 1
                got_new = True
            except queue.Empty:
                break

        if got_new and self.last_frame is not None:
            self._render(self.last_frame)

        self.root.after(10, self._tick_video)

    def _render(self, frame: np.ndarray) -> None:
        cw = max(self.canvas.winfo_width(), 1)
        ch = max(self.canvas.winfo_height(), 1)
        if cw <= 2 or ch <= 2:
            return

        h, w = frame.shape[:2]
        if h <= 0 or w <= 0:
            return

        scale = min(cw / w, ch / h)
        tw, th = max(1, int(w * scale)), max(1, int(h * scale))

        if (tw, th) != (w, h):
            small = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)
        else:
            small = frame

        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)

        if self._photo is None or self._render_size != (tw, th):
            self._photo = ImageTk.PhotoImage(img)
            self._render_size = (tw, th)
            if self._canvas_image is None:
                self._canvas_image = self.canvas.create_image(
                    cw // 2, ch // 2, anchor="center", image=self._photo,
                )
            else:
                self.canvas.itemconfig(self._canvas_image, image=self._photo)
                self.canvas.coords(self._canvas_image, cw // 2, ch // 2)
        else:
            try:
                self._photo.paste(img)
            except Exception:
                self._photo = ImageTk.PhotoImage(img)
                self.canvas.itemconfig(self._canvas_image, image=self._photo)
            self.canvas.coords(self._canvas_image, cw // 2, ch // 2)

    # -- Statistik ---------------------------------------------------------
    def _tick_stats(self) -> None:
        now = time.monotonic()
        dt = now - self._stats_t
        w = self.worker

        if w is not None and w.is_alive() and dt > 0.2:
            try:
                total_bytes = w.reader.goggles.total_bytes if (w.reader and w.reader.goggles) else 0
            except Exception:
                total_bytes = self._stats_bytes
            d_bytes = max(0, total_bytes - self._stats_bytes)
            d_frames = max(0, w.frames_decoded - self._stats_frames)
            self._mbps = d_bytes * 8.0 / dt / 1_000_000.0
            self._fps = d_frames / dt
            self._stats_bytes = total_bytes
            self._stats_frames = w.frames_decoded
            self._stats_t = now

            res = f"{w.width}x{w.height}" if w.width else "---"
            rec = " | ● REC" if self.recording else ""
            self.lbl_stats.configure(
                text=f"{res}  {self._fps:5.1f} fps  {self._mbps:6.2f} Mbit/s{rec}"
            )
        elif w is None:
            self._stats_t = now

        self.root.after(500, self._tick_stats)

    # -- Ende --------------------------------------------------------------
    def on_close(self) -> None:
        self.stop_event.set()
        if self.recording and self.worker is not None:
            try:
                self._finish_recording(popup=False)
            except Exception:
                pass
        if self.worker is not None:
            self.worker.join(timeout=2.0)
        self.root.destroy()


# --------------------------------------------------------------------------
# Headless / CLI
# --------------------------------------------------------------------------

def _print_devices() -> None:
    from dji_usb import list_usb_devices

    print("USB-Geraete:")
    for d in list_usb_devices():
        marker = "  <-- DJI" if d.vid == DJI_VID else ""
        print(
            f"  {d.vid:04X}:{d.pid:04X}  {d.manufacturer} {d.product}".rstrip()
            + marker
        )


def run_headless(args) -> int:
    from dji_usb import (
        GogglesUSB,
        VideoStreamReader,
        normalize_annexb_headers,
        remux_h264_to_mp4,
    )

    stop = threading.Event()
    out_path = args.output
    raw_path = out_path
    fh = None
    if out_path:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
        if out_path.lower().endswith(".mp4"):
            raw_path = os.path.splitext(out_path)[0] + ".h264"
        fh = open(raw_path, "wb")

    goggles = GogglesUSB(args.pid, verbose=True)
    goggles.open()
    goggles.send_magic()

    drained = goggles.drain()
    _, prefix = goggles.read_to_keyframe(
        codec="hevc" if args.codec == "hevc" else "h264"
    )
    if drained:
        print(f"[usb] Backlog verworfen: {drained / 1e6:.1f} MB", file=sys.stderr)

    reader = VideoStreamReader(goggles, stop, verbose=True, prefix=prefix)
    if fh is not None:
        reader.set_record_file(fh)

    container = av.open(
        reader,
        format="hevc" if args.codec == "hevc" else "h264",
        options={
            "flags": "low_delay",
            "probesize": "262144",
            "analyzeduration": "500000",
        },
    )
    stream = container.streams.video[0]
    extradata = b""
    try:
        extradata = bytes(stream.codec_context.extradata or b"")
    except Exception:
        extradata = b""
    if fh is not None:
        try:
            if fh.tell() == 0:
                headers = normalize_annexb_headers(extradata)
                if headers:
                    fh.write(headers)
                    fh.flush()
        except Exception:
            pass

    print("Stream laeuft. Beenden mit Ctrl+C.", flush=True)
    start = time.monotonic()
    last = start
    frames = 0
    total_frames = 0
    last_bytes = 0
    fps_hint = 60.0
    fps_measured = 0.0
    fps_samples: "deque[float]" = deque(maxlen=240)
    try:
        try:
            # guessed_rate (SPS/VUI) ist bei rohem H.264 verlaesslicher.
            r = stream.guessed_rate or stream.average_rate
            if r:
                fps_hint = float(r)
        except Exception:
            pass

        for frame in container.decode(stream):
            frames += 1
            total_frames += 1
            now = time.monotonic()
            fps_samples.append(now)
            if len(fps_samples) >= 10:
                span = fps_samples[-1] - fps_samples[0]
                if span > 0.25:
                    fps_measured = (len(fps_samples) - 1) / span
            if now - last >= 1.0:
                dt = now - last
                inst_fps = frames / dt
                mbps = (goggles.total_bytes - last_bytes) * 8.0 / dt / 1e6
                print(
                    f"{frame.width}x{frame.height}  {inst_fps:5.1f} fps  "
                    f"{mbps:6.2f} Mbit/s",
                    flush=True,
                )
                frames = 0
                last = now
                last_bytes = goggles.total_bytes

            if args.seconds and now - start >= args.seconds:
                break
    except KeyboardInterrupt:
        print("\nAbbruch durch Benutzer.")
    finally:
        stop.set()
        try:
            container.close()
        except Exception:
            pass
        try:
            reader.close()
        except Exception:
            pass
        try:
            goggles.close()
        except Exception:
            pass
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass
        if reader.error:
            print(f"Fehler: {reader.error}", file=sys.stderr)

    if fh is not None and raw_path and os.path.exists(raw_path):
        size = os.path.getsize(raw_path)
        elapsed = max(time.monotonic() - start, 0.001)
        if fps_measured <= 0 and total_frames > 1:
            fps_measured = (total_frames - 1) / elapsed
        fps = choose_fps(fps_measured, fps_hint)
        print(
            f"Rohstream: {raw_path} ({size/1e6:.1f} MB, "
            f"{total_frames} Frames, {fps:.1f} fps)",
            flush=True,
        )
        if out_path.lower().endswith(".mp4"):
            print(f"Konvertiere nach MP4 ({fps:.1f} fps) ...")
            if remux_h264_to_mp4(raw_path, out_path, fps, extradata):
                print(f"MP4 gespeichert: {out_path}")
                try:
                    os.remove(raw_path)
                except Exception:
                    pass
            else:
                print(f"MP4-Konvertierung fehlgeschlagen, Rohstream bleibt: {raw_path}")

    return 0 if not reader.error else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="DJI FPV Goggles V2 - USB Video Player (Bulk/RMVT Protokoll)",
    )
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=None,
                        help="USB Product ID (Default: auto, 0x001F)")
    parser.add_argument("--codec", choices=["auto", "h264", "hevc"], default="auto",
                        help="Codec des USB-Streams (Default: auto)")
    parser.add_argument("--record-dir", default=None,
                        help="Zielordner fuer Aufnahmen")
    parser.add_argument("--autostart", action="store_true",
                        help="Direkt beim Start verbinden")
    parser.add_argument("--list", action="store_true", help="USB-Geraete auflisten")
    parser.add_argument("--headless", action="store_true",
                        help="Ohne GUI, Stream nur lesen/aufnehmen")
    parser.add_argument("-o", "--output", default=None,
                        help="Headless: Zieldatei (.h264 oder .mp4)")
    parser.add_argument("--seconds", type=float, default=0,
                        help="Headless: nach N Sekunden beenden (0 = endlos)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if args.list:
        _print_devices()
        return 0

    if args.headless:
        try:
            return run_headless(args)
        except GogglesNotFound as exc:
            print(f"\n{exc}", file=sys.stderr)
            return 2
        except GogglesError as exc:
            print(f"\nUSB-Problem:\n{exc}", file=sys.stderr)
            return 2

    root = tk.Tk()
    app = PlayerApp(root, args)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
