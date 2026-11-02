# DJI FPV Goggles V2 – USB Video Player

Live-Bild der **DJI FPV Goggles V2** (die ursprünglichen FPV Goggles, **nicht**
Goggles 2 / Integra) auf dem Laptop – über den echten USB-Video-Out.

> **Warum der alte Ansatz nicht ging:** Die Goggles sind **kein UVC-Gerät** und
> liefern das Bild **nicht über ADB**. Sie stellen ein Vendor-Specific
> Bulk-Interface bereit. Nach dem Senden von `RMVT` an Endpoint `0x03` kommt
> auf Endpoint `0x84` ein roher H.264-Stream. Details:
> **[PROTOCOL.md](PROTOCOL.md)**

---

## Features

- 🎥 Live-Vorschau über USB-C (echter Bulk-/RMVT-Pfad, kein UVC)
- 🚀 **Auto-Reconnect** – sendet `RMVT` automatisch erneut, wenn der Stream abbricht
- ⏺ **Aufnahme** – roher H.264-Mitschnitt, danach automatisch als MP4 (ohne Re-Encoding)
- 📷 **Snapshot** als PNG
- ⛶ **Vollbild** (Button, Doppelklick oder F11)
- 📊 Live-Statistik: Auflösung, FPS, Mbit/s, REC-Status
- 🖥 **Headless-Modus** für Aufnahmen ohne GUI
- 🔍 **Diagnose-Tool** für USB-Deskriptoren, Windows-Treiber und RNDIS

---

## Voraussetzungen

- Windows 10/11, Python 3.10+
- DJI FPV Goggles V1/V2 per USB-C am Laptop
- **Drohne/Air Unit an und verbunden** – ohne Video-Signal liefern die
  Goggles kein Bild
- **DJI Assistant 2 / DJI Fly (Desktop) komplett geschlossen**
- Treiber für Interface 3: **libusb-win32** (kommt mit DJI Assistant 2) oder
  **WinUSB** (via Zadig) – der Player erkennt beides automatisch

---

## Schnellstart

### 1. Goggles verbinden

Goggles einschalten, Drohne einschalten, USB-C-Kabel an den Laptop.
Bei USB-C-auf-USB-C laden die Goggles oft nur – dann einen
**USB-A-auf-USB-C-Port/Adapter** oder ein **OTG-Adapter** auf der
Laptop-Seite verwenden.

### 2. Diagnose ausführen (einmalig)

```
diagnose_usb.bat
```

Das Tool zeigt:

- ob Windows die Goggles sieht (`VID 2CA3`)
- welche Interfaces/Endpoints vorhanden sind
- welcher Treiber auf Interface 3 läuft (**libusb-win32** oder **WinUSB**)
- ob RNDIS (`192.168.42.x`) aktiv ist

**Wenn Interface 3 noch keinen passenden Treiber hat** (also weder
`libusb-win32` noch `WinUSB`):

1. [Zadig](https://zadig.akeo.ie) herunterladen und als Administrator starten
2. `Options → List All Devices`
3. `DJI FPV GOGGLES` auswählen
4. **`Interface 3`** bzw. **`MI_03`** markieren
   (**nicht** das komplette Composite-Device!)
5. Ziel-Treiber `WinUSB` → `Replace Driver`
6. `diagnose_usb.bat` erneut ausführen

> Ist DJI Assistant 2 installiert, ist Interface 3 meist schon auf
> **libusb-win32** – dann ist **kein Zadig nötig**. Der Player nutzt
> automatisch die 64-Bit `libusb0.dll` aus den DJI-Treiberordnern.

Video-Test (sendet `RMVT` und liest 5 Sekunden mit):

```
diagnose_usb.bat --test --seconds 5
```

### 3. Player starten

```
start_player.bat
```

Beim ersten Start werden fehlende Pakete automatisch installiert.
Der Player verbindet sich direkt (`--autostart`).

---

## Nutzung

| Button | Funktion |
|---|---|
| **▶ Verbinden** | Goggles suchen, `RMVT` senden, Stream starten |
| **■ Trennen** | Stream beenden |
| **⏺ Aufnahme** | Start/Stop; speichert unter `%USERPROFILE%\Videos\DJI Goggles\` |
| **📷 Snapshot** | aktuelles Bild als PNG speichern |
| **⛶ Vollbild** | Vollbild an/aus (auch Doppelklick / F11) |

**Aufnahme:** Während der Aufnahme wird der Rohstream 1:1 mitgeschrieben
(kein Qualitätsverlust). Beim Stoppen wird er automatisch nach MP4
konvertiert. Dabei werden SPS/PPS vorangestellt und am ersten Keyframe
begonnen, damit auch Aufnahmen funktionieren, die mitten im Stream starten.
Die MP4-Zeitstempel richten sich nach der echten Bildrate. Falls die
Konvertierung fehlschlägt, bleibt die `.h264`-Datei erhalten.

---

## Kommandozeile

```bash
# Geräte auflisten
python dji_usb_player.py --list

# Normal starten
python dji_usb_player.py

# Direkt verbinden
python dji_usb_player.py --autostart

# Headless: 30 s aufnehmen (MP4)
python dji_usb_player.py --headless --seconds 30 -o test.mp4

# Headless: nur Rohstream
python dji_usb_player.py --headless --seconds 30 -o test.h264

# Anderes Codec erzwingen (Default: auto)
python dji_usb_player.py --codec h264
```

Weitere Optionen: `--pid 0x001F`, `--record-dir C:\...`, `-v`.

---

## Troubleshooting

| Problem | Lösung |
|---|---|
| `Keine DJI-Goggles gefunden` | Kabel/Port wechseln, Goggles einschalten, ggf. OTG-Adapter. `diagnose_usb.bat` prüfen. |
| `Windows hat für Interface 3 keinen WinUSB-Treiber` | Normalerweise nicht nötig: der Player nutzt automatisch `libusb-win32` von DJI Assistant 2. Sonst Zadig: **Interface 3 / MI_03** auf **WinUSB** stellen. |
| `Keine 64-Bit libusb0.dll gefunden` | DJI Assistant 2 installieren oder `DJI_LIBUSB0_DLL` auf eine 64-Bit `libusb0.dll` setzen. |
| `Zugriff verweigert` | DJI Assistant 2 / DJI Fly / DJI Studio schließen (auch im Task-Manager). |
| `Kein Videostream` / schwarzes Bild | **Drohne/Air Unit einschalten und verbinden.** Ohne aktives Video-Signal liefern die Goggles keine Daten – auch wenn USB und `RMVT` funktionieren. |
| Nach dem Video-Test kein Bild in der Brille | USB-Kabel einmal ab- und wieder anstecken (die Goggles bleiben im Video-Out-Modus). |
| Bild ruckelt / hohe Latenz | USB-2-Port statt Hub verwenden, andere Kabel probieren, `--codec h264` erzwingen. |
| MP4-Konvertierung schlägt fehl | Die `.h264`-Datei bleibt erhalten; mit `ffmpeg -i datei.h264 -c copy datei.mp4` konvertieren. |

---

## Dateien

```
dji_usb.py           # USB-Protokoll (RMVT, Bulk-Reader, MP4-Remux)
dji_usb_player.py    # GUI-Player + Headless-Modus
diagnose_usb.py      # USB-/Treiber-/RNDIS-Diagnose + Video-Test
dji_duml.py          # DUML-Framing/CRC + Befehlssuche
dji_commands.txt     # 447 DUML-Befehle aus der DJI-Fly-APK
PROTOCOL.md          # Reverse-Engineering-Dokumentation
start_player.bat     # Player starten
start_usb_player.bat # Player starten (gleicher Launcher)
diagnose_usb.bat     # Diagnose starten
requirements.txt     # Python-Abhängigkeiten
```

### Legacy (funktioniert nicht mit den Goggles)

- `dji_goggles_player.py` – alter UVC/OpenCV-Ansatz
- `dji_goggles_adb_player.py` – alter ADB/screenrecord-Ansatz
- `start_adb_player.bat`

Diese Dateien bleiben nur zur Historie liegen. Der neue Weg ist
`dji_usb_player.py`.

---

## Technischer Hintergrund

- **USB:** VID `0x2CA3`, PID `0x001F`, Configuration 1, Interface 3
- **Handshake:** `52 4D 56 54` (`RMVT`) an EP `0x03` (Bulk OUT)
- **Video:** roher H.264-Annex-B-Stream von EP `0x84` (Bulk IN)
- **Auflösung / Bildrate / Bitrate (gemessen):** **1440×808 @ 30 fps**, **≈11 Mbit/s** (H.264 High, yuv420p)
- **Start-Synchronisation:** Automatischer Backlog-Drain (verworfene gepufferte Frames) + Start am nächsten SPS/PPS – kein Vorspulen, korrekte MP4-Dauer
- **Dekodierung:** PyAV (FFmpeg) → NumPy → Tkinter
- **Aufnahme:** Rohstream → PyAV-Remux nach MP4 (kein Re-Encoding)

Vollständige Protokoll-Doku inkl. USB-Deskriptoren, DUML-Header und
APK-Analyse: **[PROTOCOL.md](PROTOCOL.md)**

---

## Lizenz

MIT – frei nutzbar und anpassbar.
