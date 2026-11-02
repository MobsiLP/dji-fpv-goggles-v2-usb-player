#!/usr/bin/env python3
"""
DJI FPV Goggles V2 - ADB Video Player
Nutzt ADB (Android Debug Bridge) um den Video-Stream von den Goggles zu holen.
Die Goggles laufen auf Android - ADB ist der Weg!
"""

import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import cv2
import numpy as np
from PIL import Image, ImageTk
from datetime import datetime
import os
import sys
from pathlib import Path


class ADBGogglesPlayer:
    def __init__(self, root):
        self.root = root
        self.root.title("DJI FPV Goggles V2 - ADB Video Player")
        self.root.geometry("1000x700")
        self.root.minsize(800, 600)
        
        # State
        self.is_running = False
        self.is_recording = False
        self.recording_writer = None
        self.recording_path = None
        self.recording_start_time = None
        self.current_frame = None
        self.frame_lock = threading.Lock()
        self.adb_device = None
        self.screenrecord_proc = None
        self.ffmpeg_proc = None
        
        # GUI
        self.setup_ui()
        self.check_adb()
        self.refresh_devices()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
    
    def setup_ui(self):
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # Control Panel
        control_frame = ttk.LabelFrame(main_frame, text="Steuerung", padding="10")
        control_frame.pack(fill=tk.X, pady=(0, 10))
        
        # ADB Device selection
        device_row = ttk.Frame(control_frame)
        device_row.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(device_row, text="ADB Gerät:").pack(side=tk.LEFT, padx=(0, 10))
        
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(device_row, textvariable=self.device_var, 
                                          state="readonly", width=40)
        self.device_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_selected)
        
        ttk.Button(device_row, text="🔄 Aktualisieren", command=self.refresh_devices).pack(side=tk.LEFT, padx=5)
        ttk.Button(device_row, text="🔌 ADB neu starten", command=self.restart_adb).pack(side=tk.LEFT, padx=5)
        
        # Mode selection
        mode_row = ttk.Frame(control_frame)
        mode_row.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(mode_row, text="Modus:").pack(side=tk.LEFT, padx=(0, 10))
        
        self.mode_var = tk.StringVar(value="screenrecord")
        ttk.Radiobutton(mode_row, text="screenrecord (Bildschirm)", variable=self.mode_var, 
                        value="screenrecord").pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_row, text="scrcpy (niedrige Latenz)", variable=self.mode_var, 
                        value="scrcpy").pack(side=tk.LEFT, padx=5)
        
        # Buttons
        btn_row = ttk.Frame(control_frame)
        btn_row.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Button(btn_row, text="▶ Start", command=self.start_stream, 
                   style="Accent.TButton").pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_row, text="■ Stop", command=self.stop_stream).pack(side=tk.LEFT, padx=5)
        
        # Recording
        rec_row = ttk.Frame(control_frame)
        rec_row.pack(fill=tk.X)
        
        self.record_btn = ttk.Button(rec_row, text="⏺ Aufnahme starten", 
                                      command=self.toggle_recording, state=tk.DISABLED)
        self.record_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        self.record_status = ttk.Label(rec_row, text="Nicht aufgenommen", foreground="gray")
        self.record_status.pack(side=tk.LEFT)
        
        ttk.Label(rec_row, text="Dauer:").pack(side=tk.LEFT, padx=(20, 5))
        self.duration_label = ttk.Label(rec_row, text="00:00:00", font=("Consolas", 10))
        self.duration_label.pack(side=tk.LEFT)
        
        # Video Display
        video_frame = ttk.LabelFrame(main_frame, text="Live-Vorschau", padding="5")
        video_frame.pack(fill=tk.BOTH, expand=True)
        
        self.canvas = tk.Canvas(video_frame, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        
        self.placeholder_text = self.canvas.create_text(
            400, 300, text="ADB-Gerät wählen → Start klicken",
            fill="white", font=("Segoe UI", 14), tags="placeholder"
        )
        
        # Status
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(10, 0))
        
        self.status_var = tk.StringVar(value="Bereit - Goggles per USB-C verbinden, USB-Debugging aktivieren")
        ttk.Label(status_frame, textvariable=self.status_var, relief=tk.SUNKEN, 
                  anchor=tk.W).pack(fill=tk.X)
        
        # Info
        info_text = ("Voraussetzungen: 1) Goggles per USB-C an Laptop  2) USB-Debugging in Goggles-Einstellungen aktivieren "
                     "(7x auf Build-Number tippen → Entwickleroptionen → USB-Debugging)  3) 'USB-Debugging zulassen' auf Goggles bestätigen")
        ttk.Label(status_frame, text=info_text, foreground="gray", 
                  wraplength=900, justify=tk.LEFT).pack(fill=tk.X, pady=(5, 0))
    
    def check_adb(self):
        """Prüft ob ADB verfügbar ist"""
        try:
            result = subprocess.run(["adb", "version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                self.status_var.set(f"ADB gefunden: {result.stdout.split(chr(10))[0]}")
            else:
                self.status_var.set("ADB nicht gefunden - bitte Android Platform Tools installieren")
        except FileNotFoundError:
            self.status_var.set("⚠ ADB nicht im PATH - 'adb' Befehl nicht gefunden")
    
    def restart_adb(self):
        """Startet ADB Server neu"""
        self.status_var.set("Starte ADB Server neu...")
        subprocess.run(["adb", "kill-server"], capture_output=True)
        time.sleep(1)
        subprocess.run(["adb", "start-server"], capture_output=True)
        time.sleep(1)
        self.refresh_devices()
    
    def refresh_devices(self):
        """Listet ADB-Geräte auf"""
        self.status_var.set("Suche ADB-Geräte...")
        self.root.update()
        
        try:
            result = subprocess.run(["adb", "devices", "-l"], capture_output=True, text=True, timeout=10)
            lines = result.stdout.strip().split('\n')[1:]  # Skip header
            
            devices = []
            for line in lines:
                if line.strip() and '\tdevice' in line:
                    serial = line.split('\t')[0]
                    # Try to get model name
                    model_result = subprocess.run(
                        ["adb", "-s", serial, "shell", "getprop", "ro.product.model"],
                        capture_output=True, text=True, timeout=5
                    )
                    model = model_result.stdout.strip() if model_result.returncode == 0 else "Unknown"
                    devices.append((serial, f"{serial} - {model}"))
            
            if not devices:
                devices = [("", "Keine ADB-Geräte gefunden - USB-Debugging aktivieren?")]
            
            self.device_combo["values"] = [d[1] for d in devices]
            self.device_map = {d[1]: d[0] for d in devices}
            
            if devices and devices[0][0]:
                self.device_combo.current(0)
                self.on_device_selected(None)
            
            self.status_var.set(f"{len([d for d in devices if d[0]])} ADB-Gerät(e) gefunden")
            
        except Exception as e:
            self.status_var.set(f"Fehler: {e}")
    
    def on_device_selected(self, event):
        selection = self.device_var.get()
        if selection in self.device_map:
            self.adb_device = self.device_map[selection]
            self.status_var.set(f"Ausgewählt: {selection}")
    
    def start_stream(self):
        if not self.adb_device:
            messagebox.showwarning("Kein Gerät", "Bitte ADB-Gerät auswählen.")
            return
        
        if self.is_running:
            return
        
        mode = self.mode_var.get()
        self.status_var.set(f"Starte Stream ({mode})...")
        self.root.update()
        
        self.is_running = True
        self.canvas.delete("placeholder")
        self.record_btn.config(state=tk.NORMAL)
        
        if mode == "screenrecord":
            self.start_screenrecord()
        else:
            self.start_scrcpy()
        
        self.update_canvas()
    
    def start_screenrecord(self):
        """Nutzt Androids screenrecord über ADB"""
        # screenrecord output to stdout, dann per ffmpeg dekodieren
        cmd = [
            "adb", "-s", self.adb_device, "exec-out",
            "screenrecord", "--output-format=h264", "-",
        ]
        
        # FFmpeg zum Dekodieren des H.264 Streams
        ffmpeg_cmd = [
            "ffmpeg", "-i", "pipe:0", "-f", "rawvideo", 
            "-pix_fmt", "bgr24", "-vcodec", "rawvideo", "pipe:1"
        ]
        
        try:
            self.screenrecord_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**6
            )
            self.ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd, stdin=self.screenrecord_proc.stdout,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**6
            )
            
            # Thread zum Lesen der Frames
            self.stream_thread = threading.Thread(target=self.read_frames_raw, daemon=True)
            self.stream_thread.start()
            
            self.status_var.set("Stream läuft (screenrecord + ffmpeg)")
            
        except Exception as e:
            messagebox.showerror("Fehler", f"Konnte Stream nicht starten: {e}\n\nFFmpeg installiert?")
            self.stop_stream()
    
    def start_scrcpy(self):
        """Nutzt scrcpy für niedrige Latenz (falls installiert)"""
        # scrcpy muss installiert sein: https://github.com/Genymobile/scrcpy
        cmd = [
            "scrcpy", "-s", self.adb_device,
            "--no-display", "--render-driver=direct3d",
            "--video-codec=h264", "--video-bit-rate=8M",
            "--max-fps=60", "--lock-video-orientation=0",
            "--pipe-video", "-"
        ]
        
        ffmpeg_cmd = [
            "ffmpeg", "-i", "pipe:0", "-f", "rawvideo",
            "-pix_fmt", "bgr24", "-vcodec", "rawvideo", "pipe:1"
        ]
        
        try:
            self.scrcpy_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**6
            )
            self.ffmpeg_proc = subprocess.Popen(
                ffmpeg_cmd, stdin=self.scrcpy_proc.stdout,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=10**6
            )
            
            self.stream_thread = threading.Thread(target=self.read_frames_raw, daemon=True)
            self.stream_thread.start()
            
            self.status_var.set("Stream läuft (scrcpy + ffmpeg)")
            
        except FileNotFoundError:
            messagebox.showwarning("scrcpy nicht gefunden", 
                "scrcpy ist nicht installiert.\nInstalliere: https://github.com/Genymobile/scrcpy\n\nFallback: screenrecord Modus")
            self.mode_var.set("screenrecord")
            self.start_screenrecord()
        except Exception as e:
            messagebox.showerror("Fehler", f"Konnte scrcpy nicht starten: {e}")
            self.stop_stream()
    
    def read_frames_raw(self):
        """Liest rohe BGR-Frames von ffmpeg stdout"""
        # Erwarte 1920x1080 (Goggles Auflösung) - anpassen falls anders
        frame_size = 1920 * 1080 * 3  # BGR24
        
        while self.is_running and self.ffmpeg_proc and self.ffmpeg_proc.stdout:
            try:
                raw_data = self.ffmpeg_proc.stdout.read(frame_size)
                if len(raw_data) == frame_size:
                    frame = np.frombuffer(raw_data, dtype=np.uint8).reshape(1080, 1920, 3)
                    with self.frame_lock:
                        self.current_frame = frame.copy()
                elif len(raw_data) == 0:
                    time.sleep(0.01)
                else:
                    # Unvollständiger Frame - warten auf mehr Daten
                    remaining = frame_size - len(raw_data)
                    more_data = self.ffmpeg_proc.stdout.read(remaining)
                    raw_data += more_data
                    if len(raw_data) == frame_size:
                        frame = np.frombuffer(raw_data, dtype=np.uint8).reshape(1080, 1920, 3)
                        with self.frame_lock:
                            self.current_frame = frame.copy()
            except Exception as e:
                if self.is_running:
                    print(f"Frame read error: {e}")
                time.sleep(0.01)
    
    def stop_stream(self):
        self.is_running = False
        
        if self.is_recording:
            self.toggle_recording()
        
        # Prozesse beenden
        for proc_name in ['screenrecord_proc', 'scrcpy_proc', 'ffmpeg_proc']:
            proc = getattr(self, proc_name, None)
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except:
                    try:
                        proc.kill()
                    except:
                        pass
                setattr(self, proc_name, None)
        
        self.record_btn.config(state=tk.DISABLED)
        self.canvas.delete("video")
        self.placeholder_text = self.canvas.create_text(
            400, 300, text="Gestoppt - 'Start' für neuen Versuch",
            fill="white", font=("Segoe UI", 14), tags="placeholder"
        )
        self.status_var.set("Gestoppt")
        self.duration_label.config(text="00:00:00")
    
    def update_canvas(self):
        if not self.is_running:
            return
        
        frame = None
        with self.frame_lock:
            if self.current_frame is not None:
                frame = self.current_frame.copy()
        
        if frame is not None:
            self.draw_frame(frame)
            
            if self.is_recording and self.recording_writer:
                self.recording_writer.write(frame)
                self.update_recording_duration()
        
        self.root.after(16, self.update_canvas)  # ~60fps
    
    def draw_frame(self, frame):
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        
        if canvas_w <= 1 or canvas_h <= 1:
            return
        
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame_rgb.shape[:2]
        
        scale = min(canvas_w / w, canvas_h / h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        
        if scale != 1.0:
            frame_resized = cv2.resize(frame_rgb, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        else:
            frame_resized = frame_rgb
        
        img = Image.fromarray(frame_resized)
        photo = ImageTk.PhotoImage(img)
        
        x = (canvas_w - new_w) // 2
        y = (canvas_h - new_h) // 2
        
        self.canvas.delete("video")
        self.canvas.create_image(x, y, anchor=tk.NW, image=photo, tags="video")
        self.canvas.photo = photo
    
    def on_canvas_resize(self, event):
        pass
    
    def toggle_recording(self):
        if not self.is_recording:
            self.start_recording()
        else:
            self.stop_recording()
    
    def start_recording(self):
        if not self.is_running or self.current_frame is None:
            return
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"DJI_Goggles_V2_ADB_{timestamp}.mp4"
        
        path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4"), ("Alle Dateien", "*.*")],
            initialfile=default_name,
            title="Aufnahme speichern als..."
        )
        
        if not path:
            return
        
        h, w = self.current_frame.shape[:2]
        fps = 30
        
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        self.recording_writer = cv2.VideoWriter(path, fourcc, fps, (w, h))
        
        if not self.recording_writer.isOpened():
            messagebox.showerror("Fehler", "Konnte Videodatei nicht erstellen.")
            return
        
        self.recording_path = path
        self.is_recording = True
        self.recording_start_time = time.time()
        
        self.record_btn.config(text="⏹ Aufnahme stoppen")
        self.record_status.config(text=f"Aufnahme: {os.path.basename(path)}", foreground="red")
        self.status_var.set(f"Aufnahme läuft: {path}")
    
    def stop_recording(self):
        self.is_recording = False
        
        if self.recording_writer:
            self.recording_writer.release()
            self.recording_writer = None
        
        self.record_btn.config(text="⏺ Aufnahme starten")
        self.record_status.config(text=f"Gespeichert: {os.path.basename(self.recording_path) if self.recording_path else '?'}", 
                                  foreground="green")
        self.status_var.set(f"Aufnahme gespeichert: {self.recording_path}")
    
    def update_recording_duration(self):
        if self.is_recording and hasattr(self, 'recording_start_time'):
            elapsed = time.time() - self.recording_start_time
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            s = int(elapsed % 60)
            self.duration_label.config(text=f"{h:02d}:{m:02d}:{s:02d}")
    
    def on_closing(self):
        self.stop_stream()
        self.root.destroy()


def main():
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except:
        pass
    
    root = tk.Tk()
    app = ADBGogglesPlayer(root)
    root.mainloop()


if __name__ == "__main__":
    main()