#!/usr/bin/env python3
"""
DJI FPV Goggles V2 Video Player
Zeigt Live-Video von den DJI FPV Goggles V2 über USB-C an.
Unterstützt Aufnahme als MP4.
"""

import cv2
import numpy as np
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import time
import os
from datetime import datetime
from pathlib import Path
import sys


class DJIGogglesPlayer:
    def __init__(self, root):
        self.root = root
        self.root.title("DJI FPV Goggles V2 - Video Player")
        self.root.geometry("1000x700")
        self.root.minsize(800, 600)
        
        # Video-Capture
        self.cap = None
        self.device_index = None
        self.is_running = False
        self.is_recording = False
        self.recording_writer = None
        self.recording_path = None
        
        # Frame-Buffer für Thread-Safety
        self.current_frame = None
        self.frame_lock = threading.Lock()
        
        # GUI aufbauen
        self.setup_ui()
        self.refresh_devices()
        
        # Cleanup beim Schließen
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
    
    def setup_ui(self):
        # Haupt-Container
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # --- Oberer Bereich: Device-Auswahl & Controls ---
        control_frame = ttk.LabelFrame(main_frame, text="Steuerung", padding="10")
        control_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Device-Dropdown
        device_row = ttk.Frame(control_frame)
        device_row.pack(fill=tk.X, pady=(0, 10))
        
        ttk.Label(device_row, text="Video-Gerät:").pack(side=tk.LEFT, padx=(0, 10))
        
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(device_row, textvariable=self.device_var, 
                                          state="readonly", width=50)
        self.device_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        self.device_combo.bind("<<ComboboxSelected>>", self.on_device_selected)
        
        ttk.Button(device_row, text="Aktualisieren", command=self.refresh_devices).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(device_row, text="Start", command=self.start_video).pack(side=tk.LEFT, padx=5)
        ttk.Button(device_row, text="Stop", command=self.stop_video).pack(side=tk.LEFT, padx=5)
        
        # Recording-Controls
        record_row = ttk.Frame(control_frame)
        record_row.pack(fill=tk.X)
        
        self.record_btn = ttk.Button(record_row, text="⏺ Aufnahme starten", 
                                      command=self.toggle_recording, state=tk.DISABLED)
        self.record_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        self.record_status = ttk.Label(record_row, text="Nicht aufgenommen", foreground="gray")
        self.record_status.pack(side=tk.LEFT)
        
        ttk.Label(record_row, text="Dauer:").pack(side=tk.LEFT, padx=(20, 5))
        self.duration_label = ttk.Label(record_row, text="00:00:00", font=("Consolas", 10))
        self.duration_label.pack(side=tk.LEFT)
        
        # --- Video-Display Bereich ---
        video_frame = ttk.LabelFrame(main_frame, text="Live-Vorschau", padding="5")
        video_frame.pack(fill=tk.BOTH, expand=True)
        
        # Canvas für Video
        self.canvas = tk.Canvas(video_frame, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self.on_canvas_resize)
        
        # Placeholder-Text
        self.placeholder_text = self.canvas.create_text(
            400, 300, text="Gerät auswählen und 'Start' klicken",
            fill="white", font=("Segoe UI", 14), tags="placeholder"
        )
        
        # --- Statusleiste ---
        status_frame = ttk.Frame(main_frame)
        status_frame.pack(fill=tk.X, pady=(10, 0))
        
        self.status_var = tk.StringVar(value="Bereit - DJI FPV Goggles V2 per USB-C verbinden")
        ttk.Label(status_frame, textvariable=self.status_var, relief=tk.SUNKEN, 
                  anchor=tk.W).pack(fill=tk.X)
        
        # Info-Label
        info_text = ("Hinweis: DJI FPV Goggles V2 per USB-C an Laptop anschließen. "
                     "Falls kein Gerät erscheint: DJI Assistant 2 schließen, "
                     "Goggles neu starten, anderes USB-Kabel/Port probieren.")
        ttk.Label(status_frame, text=info_text, foreground="gray", 
                  wraplength=900, justify=tk.LEFT).pack(fill=tk.X, pady=(5, 0))
    
    def refresh_devices(self):
        """Findet alle verfügbaren Video-Capture-Geräte"""
        self.status_var.set("Suche Video-Geräte...")
        self.root.update()
        
        devices = []
        # Teste Indizes 0-10 (meistens reicht 0-5)
        for i in range(10):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)  # DirectShow auf Windows
            if cap.isOpened():
                # Versuche Auflösung zu lesen
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                backend = cap.getBackendName()
                cap.release()
                
                name = f"Gerät {i}: {w}x{h} @ {fps:.0f}fps ({backend})"
                devices.append((i, name))
        
        if not devices:
            devices = [(-1, "Keine Video-Geräte gefunden")]
        
        self.device_combo["values"] = [d[1] for d in devices]
        self.device_map = {d[1]: d[0] for d in devices}
        
        if devices and devices[0][0] != -1:
            self.device_combo.current(0)
            self.on_device_selected(None)
        
        self.status_var.set(f"{len([d for d in devices if d[0] != -1])} Video-Gerät(e) gefunden")
    
    def on_device_selected(self, event):
        selection = self.device_var.get()
        if selection in self.device_map:
            self.device_index = self.device_map[selection]
            self.status_var.set(f"Ausgewählt: {selection}")
    
    def start_video(self):
        if self.device_index is None or self.device_index == -1:
            messagebox.showwarning("Kein Gerät", "Bitte zuerst ein Video-Gerät auswählen.")
            return
        
        if self.is_running:
            return
        
        self.status_var.set(f"Verbinde mit Gerät {self.device_index}...")
        self.root.update()
        
        # Capture starten
        self.cap = cv2.VideoCapture(self.device_index, cv2.CAP_DSHOW)
        
        if not self.cap.isOpened():
            messagebox.showerror("Fehler", f"Konnte Gerät {self.device_index} nicht öffnen.\n"
                                "Prüfen: USB-Kabel, DJI Assistant 2 geschlossen, Goggles an.")
            self.status_var.set("Fehler beim Öffnen des Geräts")
            return
        
        # Versuche hohe Auflösung einzustellen (Goggles V2 können 720p/1080p)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        self.cap.set(cv2.CAP_PROP_FPS, 60)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Niedrige Latenz
        
        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = self.cap.get(cv2.CAP_PROP_FPS)
        
        self.is_running = True
        self.canvas.delete("placeholder")
        self.record_btn.config(state=tk.NORMAL)
        
        self.status_var.set(f"Live: {actual_w}x{actual_h} @ {actual_fps:.0f}fps")
        
        # Video-Loop in separatem Thread
        self.video_thread = threading.Thread(target=self.video_loop, daemon=True)
        self.video_thread.start()
        
        # GUI-Update-Loop
        self.update_canvas()
    
    def stop_video(self):
        self.is_running = False
        if self.is_recording:
            self.toggle_recording()
        
        if self.cap:
            self.cap.release()
            self.cap = None
        
        self.record_btn.config(state=tk.DISABLED)
        self.canvas.delete("video")
        self.placeholder_text = self.canvas.create_text(
            400, 300, text="Gestoppt - 'Start' für neuen Versuch",
            fill="white", font=("Segoe UI", 14), tags="placeholder"
        )
        self.status_var.set("Gestoppt")
        self.duration_label.config(text="00:00:00")
    
    def video_loop(self):
        """Läuft im Background-Thread, liest Frames von der Kamera"""
        while self.is_running and self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret and frame is not None:
                with self.frame_lock:
                    self.current_frame = frame.copy()
            else:
                time.sleep(0.01)
    
    def update_canvas(self):
        """Aktualisiert Canvas im Haupt-Thread (tkinter ist nicht thread-safe)"""
        if not self.is_running:
            return
        
        frame = None
        with self.frame_lock:
            if self.current_frame is not None:
                frame = self.current_frame.copy()
        
        if frame is not None:
            self.draw_frame(frame)
            
            # Recording-Frame schreiben
            if self.is_recording and self.recording_writer:
                self.recording_writer.write(frame)
                self.update_recording_duration()
        
        # Nächster Frame in ~30fps (33ms)
        self.root.after(33, self.update_canvas)
    
    def draw_frame(self, frame):
        """Zeichnet Frame auf Canvas mit Skalierung"""
        canvas_w = self.canvas.winfo_width()
        canvas_h = self.canvas.winfo_height()
        
        if canvas_w <= 1 or canvas_h <= 1:
            return
        
        # BGR zu RGB
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = frame_rgb.shape[:2]
        
        # Skalierung berechnen (Aspect Ratio beibehalten)
        scale = min(canvas_w / w, canvas_h / h)
        new_w = int(w * scale)
        new_h = int(h * scale)
        
        # Resize
        if scale != 1.0:
            frame_resized = cv2.resize(frame_rgb, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        else:
            frame_resized = frame_rgb
        
        # Zu PhotoImage konvertieren
        from PIL import Image, ImageTk
        img = Image.fromarray(frame_resized)
        photo = ImageTk.PhotoImage(img)
        
        # Auf Canvas zeichnen (zentriert)
        x = (canvas_w - new_w) // 2
        y = (canvas_h - new_h) // 2
        
        self.canvas.delete("video")
        self.canvas.create_image(x, y, anchor=tk.NW, image=photo, tags="video")
        self.canvas.photo = photo  # Referenz halten
    
    def on_canvas_resize(self, event):
        """Canvas-Größe geändert - Frame wird beim nächsten Update neu gezeichnet"""
        pass
    
    def toggle_recording(self):
        if not self.is_recording:
            self.start_recording()
        else:
            self.stop_recording()
    
    def start_recording(self):
        if not self.is_running or self.current_frame is None:
            return
        
        # Speicherort wählen
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"DJI_Goggles_V2_{timestamp}.mp4"
        
        path = filedialog.asksaveasfilename(
            defaultextension=".mp4",
            filetypes=[("MP4 Video", "*.mp4"), ("Alle Dateien", "*.*")],
            initialfile=default_name,
            title="Aufnahme speichern als..."
        )
        
        if not path:
            return
        
        # VideoWriter einrichten
        h, w = self.current_frame.shape[:2]
        fps = 30  # Fixed für Aufnahme
        
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
        self.stop_video()
        self.root.destroy()


def main():
    # DPI Awareness für scharfe Darstellung auf Windows
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except:
        pass
    
    root = tk.Tk()
    app = DJIGogglesPlayer(root)
    root.mainloop()


if __name__ == "__main__":
    main()