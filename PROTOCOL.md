# DJI FPV Goggles V2 – USB-Protokoll (Reverse-Engineering)

Stand: 2026-11-02 · Quelle: DJI Fly `1.21.18.1003` (APK) + verifiziertes
Community-Protokoll (fpv-wtf/voc-poc, fpvout-c, fpv-dvca)

---

## TL;DR

Die Goggles sind **kein UVC-Gerät** und liefern das Bild **nicht über ADB**.
Sie stellen ein **Vendor-Specific Bulk-Interface** (Interface 3) bereit:

1. USB-Configuration 1 setzen
2. Interface 3 claimen
3. `52 4D 56 54` (`"RMVT"`) an **Endpoint `0x03` OUT** senden
4. Von **Endpoint `0x84` IN** den rohen **H.264-Annex-B-Stream** lesen

Es gibt **kein ACK, keinen Längen-Header und keinen Handshake**. Wenn keine
Daten mehr kommen (z. B. nach Akkuwechsel), einfach `"RMVT"` erneut senden.

Damit ist der komplette Video-Pfad vier Bytes lang. Alles andere (DUML,
RNDIS, AOA) ist für die App-Steuerung/Telemetrie zuständig, nicht für das
Bild.

---

## 1. USB-Gerät

| Feld | Wert |
|---|---|
| VID | `0x2CA3` (DJI) |
| PID | `0x001F` (DJI FPV Goggles V1/V2) |
| Manufacturer | `DJI` |
| Product | `DJI FPV GOGGLES` |
| bcdUSB | 2.10 |
| Configuration | 1 |
| Config-String | `rndis,mass_storage,bulk,acm` |
| Interfaces | 6 |

### Interface-Layout

| # | Klasse | Zweck | Endpoints |
|---|---|---|---|
| 0 | `224/1/3` (RNDIS) | RNDIS Control | `0x82` Interrupt IN |
| 1 | `10/0/0` (CDC Data) | RNDIS Ethernet | `0x81` IN, `0x01` OUT |
| 2 | `8/6/80` (Mass Storage) | interner Speicher | `0x83` IN, `0x02` OUT |
| **3** | **`255/67/1` (Vendor)** | **„BULK Interface" = Video** | **`0x03` OUT, `0x84` IN** |
| 4 | `2/2/1` (CDC ACM) | seriell | `0x86` Interrupt IN |
| 5 | `10/0/0` (CDC Data) | seriell | `0x85` IN, `0x04` OUT |

Interface 3 hat `wMaxPacketSize = 512` (High Speed Bulk).

> Der USB-Dump stammt aus `Amzd/DJI-FPV-Goggles/USB_dump.txt` und passt
> exakt zu den Deskriptor-Strings in der APK (`#VID_2CA3&PID_001F&MI_03#`).
> Die Goggles können auch USB-Host sein – bei USB-C-auf-USB-C kann ein
> OTG-Adapter auf der Laptop-Seite nötig sein, sonst laden sie nur.

---

## 2. Video-Out starten

```
Host                                Goggles (Interface 3)
 |                                         |
 |-- SET_CONFIGURATION(1) ----------------->|
 |-- CLAIM_INTERFACE(3) ------------------->|
 |-- BULK OUT 0x03: "RMVT" ---------------->|
 |                                         |
 |<------------- BULK IN 0x84: H.264 ------|  (endlos)
```

* Magic: `52 4D 56 54` (ASCII `RMVT`, little-endian `0x524D5654`)
* Kein Response auf das Magic-Paket.
* Ab dem Moment liefert `0x84` kontinuierlich Daten.
* Kommt ein Read leer zurück, Magic erneut senden (ca. 1× pro Sekunde).

### Warum `RMVT`?

Der String `RMVT` ist weder in der APK noch im DJI-SDK zu finden – er wurde
von der Community (fpv-wtf/voc-poc) empirisch ermittelt. Die APK bestätigt
aber den Rest des Pfads: `uav::core::AoaServicePort`, `AOARead`/`AOAWrite`,
`JNIUsbAccessory`, `wlm_dev_mgr_state_change_for_usb_conn_glass` sowie die
Windows-Gerätepfade `#VID_2CA3&PID_001F&MI_03#`.

---

## 3. Hardware-Verifikation (Windows, 2026-11-02)

Mit einer echten **DJI FPV Goggles V2** (`2CA3:001F`) verifiziert:

| Punkt | Ergebnis |
|---|---|
| USB-Deskriptoren | exakt wie oben: 6 Interfaces, Interface 3 = `255/67/1`, EP `0x03` OUT / `0x84` IN, 512 Byte |
| Windows-Treiber Interface 3 | **libusb-win32** → `Vision (Interface 3)`, Dienst `libusb0` |
| Zugriff mit libusb-1.0 / WinUSB | schlägt fehl: `USBError [Errno 2] Entity not found` |
| Zugriff mit pyusb `usb.backend.libusb0` | funktioniert mit **64-Bit** `libusb0.dll` |
| `set_configuration(1)` | OK |
| `claim_interface(3)` | OK |
| `RMVT` an `0x03` | 4 Bytes geschrieben |
| Daten von `0x84` | **0 Bytes**, solange keine Drohne/Air Unit verbunden ist |
| Daten mit verbundener Drohne | **16,36 MB in 8 s** (≈16,3 Mbit/s inkl. Start-Backlog) |
| Stream-Format | H.264 High, **1440×808**, yuv420p, SPS/PPS vor jedem IDR |
| Bildrate / Bitrate (steady) | **30 fps**, **≈11 Mbit/s** |
| Aufnahme (12 s) | 365 Frames, MP4 12,2 s, 29,93 fps |

Wichtige Erkenntnisse für die Implementierung:

* `pyusb` findet `libusb0.dll` unter Windows nicht selbst. Der Pfad muss über
  `find_library=` übergeben werden.
* Die DLLs in `C:\Windows\System32` und `SysWOW64` sind **32-Bit** und laden
  in 64-Bit-Python nicht (`WinError 193`). Die DJI-Treiberordner enthalten
  64-Bit-Versionen (Version 1.2.6.0, passend zum `libusb0.sys`-Treiber).
* Nach einem abgebrochenen Bulk-Transfer bleibt ein Endpoint hängen; erst
  `clear_halt()` macht den nächsten `RMVT`-Write wieder möglich.
* `libusb0` meldet Timeouts als generische `USBError` mit
  `"timeout error"` im Text (nicht als `USBTimeoutError`) – der Reader
  behandelt das als „keine Daten“.
* Die Goggles liefern **nur mit aktivem Video-Signal** Daten. Ohne Drohne/Air
  Unit ist der Bulk-IN-Endpoint leer, obwohl `RMVT` erfolgreich geschrieben
  wurde.
* Die Goggles **puffern weiter**, während der Host nicht liest. Beim Öffnen
  kommen daher oft mehrere MB alte Bilder in einem Schwall (im Test
  **4,8 MB in 0,5 s**). Der Player verwirft diesen Backlog (`drain`) und
  startet erst am nächsten SPS/PPS, sonst spult die Vorschau am Anfang
  sichtbar vor und die MP4-Dauer ist zu lang.

---

## 4. Stream-Format



Der Stream ist **rohes H.264 Annex B** – kein MP4, kein RTP, kein
Längen-Header.

* Access-Units beginnen mit einem AUD:
  `00 00 00 01 09 10` (H.264, NAL-Typ 9)
* HEVC-Variante: `00 00 00 01 46 01` (NAL-Typ 35)
* DJIs eigener Parser (`VideoFrameParser.java` aus `fpv_live`) prüft genau
  diese beiden Muster und schaltet danach automatisch zwischen H.264/HEVC um.
* Die Goggles liefern **keine Zeitstempel**. Beim Remuxen nach MP4 werden
  sie aus der Bildrate erzeugt: bevorzugt aus dem SPS/VUI (H.264), sonst aus
  der gemessenen Decode-Rate, sonst 60 fps.
* Aufnahmen, die mitten in einer GOP beginnen, bekommen die SPS/PPS des
  Live-Streams vorangestellt; der MP4-Start wird auf den ersten Keyframe
  gelegt. Ohne das waere eine solche Aufnahme nicht dekodierbar.
* Community-Beobachtung: manche Frames haben vor den NAL-Daten einen
  6-Byte-Zähler und `C8 67 FF`. FFmpeg/PyAV tolerieren das; für eine
  saubere Aufnahme wird der Rohstream 1:1 gespeichert und danach ohne
  Re-Encoding nach MP4 gemuxt.

### Auflösung/Bitrate

Gemessen mit DJI FPV Goggles V2 + verbundener Drohne (USB-Bulk-Pfad):

| Wert | Messung |
|---|---|
| Auflösung | **1440×808** |
| Codec / Profil | H.264 High, Level 5.0, yuv420p |
| Bildrate | **30 fps** (SPS/VUI und Decode-Rate stimmen überein) |
| Bitrate | **≈11 Mbit/s** (steady), Start-Backlog kurzzeitig höher |
| Keyframe-Intervall | SPS/PPS + IDR ca. alle 0,7 s |

Die Werte hängen vom Goggles-Modus und vom Drohnen-Video ab. Der Player
zeigt die echten Werte aus dem H.264-SPS an.

---

## 5. Was nicht funktioniert (und warum)

| Ansatz | Ergebnis |
|---|---|
| **UVC / OpenCV** | Die Goggles sind kein UVC-Gerät. Windows zeigt keine Webcam. |
| **ADB / screenrecord** | USB-Debugging existiert, aber DJI Fly nutzt es nicht für Video. Sobald DJI Assistant/Fly den Port hat, ist ADB blockiert. |
| **RNDIS + RTSP** | RNDIS (`192.168.42.x`) wird für die App-Steuerung/Telemetrie geöffnet, nicht für den Latenz-kritischen Video-Out. |
| **AOA (Android Open Accessory)** | Das DJI-SDK hat einen `AoaServicePort`, aber der PC-Video-Out läuft über Interface 3 – kein AOA-Handshake nötig. |
| **HDMI** | Die V2 hat nur HDMI-**In**, keinen HDMI-Out. |

---

## 6. DUML (nur Steuerung, nicht Video)

Die APK enthält **447 DUML-Befehle** (extrahiert aus den Template-Symbolen
`uav_cmd_base_req<Lh set, Lh id, Lh subid>` in `libsdk_jni.so`).
Tabelle: [`dji_commands.txt`](dji_commands.txt)

### DUML-v1-Paket (11 Byte Header + Payload + CRC16)

| Offset | Größe | Feld |
|---|---|---|
| 0 | 1 | `0x55` (SOF) |
| 1 | 2 | Länge (Bits 0–9) \| Protokollversion (Bits 10–15) |
| 3 | 1 | Header-CRC8 über Byte 0–2 (Seed `0x77`) |
| 4 | 1 | Sender |
| 5 | 1 | Empfänger |
| 6 | 2 | Sequence Counter (LE) |
| 8 | 1 | Flags: Encrypt (0–2), ACK (5–6), Typ (7) |
| 9 | 1 | Command Set |
| 10 | 1 | Command ID |
| 11 | n | Payload |
| 11+n | 2 | CRC16 über alle Bytes davor (Seed `0x3692`, LE) |

Sender `2` = App, Empfänger `21` = Glass/Goggles.

Relevante Befehle aus der APK:

```
0x01 0x00 0x91   uav_general_set_rndis_status_req
0x01 0x06 0x3A   uav_rc_set_usb_mode_2014rc_req
0x01 0x15 0x35   uav_goggles_app_to_glass_push_data_push
0x01 0x02 0x4C   uav_camera_set_video_out_para_req
0x01 0x02 0x4D   uav_camera_get_video_out_para_req
0x01 0x02 0xB3   uav_camera_get_app_request_i_frame_req
0x01 0x02 0xAB   uav_camera_set_video_coding_standard_req
```

`dji_duml.py` kann Frames bauen/parsen und die Tabelle durchsuchen:

```bash
python dji_duml.py --demo
python dji_duml.py --grep goggle
```

> Für den Video-Player wird DUML **nicht** benötigt. Es ist nur dokumentiert,
> weil es Teil der ursprünglichen Reverse-Engineering-Frage war.

---

## 7. APK-Analyse (wie die Erkenntnisse entstanden)

* APK: `1793008319309-DJIFly-1.21.18.1003(3132440)-android-release-261023-sec.apk` (744 MB)
* `classes.dex` ist nur ein 2,2-MB-Stub (AppGuard/Zeus), echte Logik liegt in
  den nativen Libs.
* `lib/arm64-v8a/libsdk_jni.so` (91 MB) enthält:
  * `uav::core::AoaServicePort`, `TcpServicePort`, `WifiServicePort`,
    `BleServicePort`
  * `JNI_LoadUsbAccessory`, `uav/jni/JNIUsbAccessory`, `AOARead`, `AOAWrite`
  * Windows-Pfade `#VID_2CA3&PID_001F&MI_03#`, `PID_1006`, `PID_1004`
  * die 447 DUML-Kommandos
* Weitere analysierte Libs: `libsdk_base.so` (RTSP-Client),
  `libsdk_common.so`, `libuav_video_jni.so`, `libupgrade_core.so`.

---

## 8. Implementierung in diesem Projekt

| Datei | Aufgabe |
|---|---|
| `dji_usb.py` | USB-Layer, `RMVT`, Bulk-Reader, H.264→MP4-Remux |
| `dji_usb_player.py` | GUI-Player + Headless-Modus |
| `diagnose_usb.py` | USB-Deskriptoren, Windows-Treiber, RNDIS, RMVT-Test |
| `dji_duml.py` | DUML-Framing, CRC8/CRC16, Befehlssuche |
| `dji_commands.txt` | 447 Befehle aus der APK |

### Windows-Treiber

Der Player unterstützt **beide** Treiber und wählt automatisch:

1. **libusb-win32** (`libusb0`) – wird von **DJI Assistant 2** für Interface 3
   installiert (`Vision (Interface 3)`, Dienst `libusb0`). Das ist der
   Normalfall, wenn DJI Assistant 2 installiert ist – **kein Zadig nötig**.
   Wichtig: pyusb braucht eine **64-Bit** `libusb0.dll`; die DLLs in
   `System32`/`SysWOW64` sind 32-Bit. Der Player sucht automatisch in den
   DJI-Treiberordnern, z. B.
   `…\DJI Assistant 2 (DJI FPV series)\Drivers\Drivers_Win10\Vision\vision_amd64\libusb0.dll`.
   Eigener Pfad per Umgebungsvariable `DJI_LIBUSB0_DLL`.
2. **WinUSB** (`libusb-1.0`) – falls Interface 3 auf WinUSB umgestellt wurde.

Nur wenn beides fehlt, Interface 3 auf WinUSB umstellen:

1. [Zadig](https://zadig.akeo.ie) starten (als Administrator)
2. `Options → List All Devices`
3. `DJI FPV GOGGLES` wählen
4. **`Interface 3` / `MI_03`** markieren – **nicht** das ganze Gerät
5. Treiber `WinUSB` → `Replace Driver`

RNDIS, Mass Storage und der serielle Port bleiben dabei unberührt.

> Nach einem abgebrochenen Bulk-Transfer bleibt der Endpoint unter
> libusb-win32 gern hängen. Der Player setzt ihn beim Öffnen und vor jedem
> erneuten `RMVT` mit `clear_halt` zurück.

---

## 9. Quellen / Danksagung

* [fpv-wtf/voc-poc](https://github.com/fpv-wtf/voc-poc) – `RMVT`-Magic und
  erster funktionierender PoC
* [fpvout/fpvout-c](https://github.com/fpvout/fpvout-c) – Endpoint-Layout,
  Interface-3-Nutzung
* [d4rken/fpv-dvca](https://github.com/d4rken/fpv-dvca) – Stream-Handling,
  Reconnect, Aufnahme
* [Amzd/DJI-FPV-Goggles](https://github.com/Amzd/DJI-FPV-Goggles) –
  USB-Deskriptor-Dump
* [o-gs/dji-firmware-tools](https://github.com/o-gs/dji-firmware-tools) –
  DUML-Header und CRC-Seeds
* [Oscar Liang](https://oscarliang.com/dji-fpv-goggles-video-out) –
  OTG-Hinweis bei USB-C-auf-USB-C
