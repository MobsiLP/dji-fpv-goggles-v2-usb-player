#!/usr/bin/env python3
"""
DJI FPV Goggles - USB Video-Out Protokoll
=========================================

Reverse-engineered aus der DJI Fly APK (v1.21.18.1003) und gegen das
bekannte Community-Protokoll (fpv-wtf/voc-poc) verifiziert.

Die Goggles (DJI FPV Goggles V1/V2, VID 0x2CA3) melden sich als
Composite-Device mit 6 Interfaces:

    Interface 0/1  RNDIS (Netzwerk, 192.168.42.x)
    Interface 2    Mass Storage
    Interface 3    Vendor Specific (class 255 / subclass 67 / protocol 1)
                   -> "BULK Interface", das ist unser Video-Pfad
    Interface 4/5  CDC ACM (seriell)

Video-Out starten:
    1. Configuration 1 setzen
    2. Interface 3 claimen
    3. 4 Bytes "RMVT" (0x52 0x4D 0x56 0x54) an Endpoint 0x03 (OUT) senden
    4. Von Endpoint 0x84 (IN) den rohen H.264-Annex-B-Stream lesen

Wenn ein Read leer zurueckkommt (z.B. nach Akkuwechsel), einfach "RMVT"
erneut senden. Es gibt keinen Laengen-Header und kein Handshake-ACK.
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    import usb.core
    import usb.util
except ImportError as exc:  # pragma: no cover - nur falls Dependencies fehlen
    raise SystemExit(
        "pyusb fehlt. Bitte installieren:\n"
        "  .venv\\Scripts\\python.exe -m pip install -r requirements.txt"
    ) from exc


# --------------------------------------------------------------------------
# Protokoll-Konstanten
# --------------------------------------------------------------------------

DJI_VID = 0x2CA3

# 0x001F ist die klassische DJI FPV Goggles (V1 + V2).
# Weitere PIDs koennen per --pid uebergeben werden.
DEFAULT_PIDS = (0x001F,)

KNOWN_PIDS = {
    0x0019: "DJI Goggles / RC",
    0x001C: "DJI Goggles 2 / Integra (?)",
    0x001F: "DJI FPV Goggles",
    0x1004: "DJI Goggles (?)",
    0x1006: "DJI Goggles (?)",
}

MAGIC = b"RMVT"          # 0x52 0x4D 0x56 0x54
CONFIGURATION = 1
VIDEO_INTERFACE = 3
EP_VIDEO_IN = 0x84       # Bulk IN
EP_CONTROL_OUT = 0x03    # Bulk OUT
READ_SIZE = 64 * 1024
USB_TIMEOUT_MS = 1000


# --------------------------------------------------------------------------
# Fehler + Hilfetexte
# --------------------------------------------------------------------------

class GogglesError(RuntimeError):
    """Basisfehler fuer alle Goggles-USB-Probleme."""


class GogglesNotFound(GogglesError):
    pass


def _backend():
    """libusb-1.0 Backend laden (unter Windows aus libusb-package)."""
    try:
        import libusb_package
        return libusb_package.get_libusb1_backend()
    except Exception:
        from usb.backend import libusb1
        be = libusb1.get_backend()
        if be is None:
            raise GogglesError(
                "libusb-1.0.dll konnte nicht geladen werden.\n"
                "Bitte 'pip install libusb-package' ausfuehren."
            )
        return be


# --------------------------------------------------------------------------
# libusb-win32 (libusb0) Fallback
# --------------------------------------------------------------------------
#
# DJI Assistant 2 installiert fuer Interface 3 den libusb-win32-Treiber
# ("Vision (Interface 3)", Dienst "libusb0"). libusb-1.0 kann solche
# Interfaces NICHT oeffnen ("Entity not found"). Der legacy libusb0-Backend
# von pyusb kann es - braucht aber eine 64-Bit libusb0.dll, weil unser
# Python 64-Bit ist. Die DLLs in System32/SysWOW64 sind 32-Bit, in den
# DJI-Treiberordnern liegen aber passende 64-Bit-Versionen.

LIBUSB0_ENV = "DJI_LIBUSB0_DLL"

_LIBUSB0_BACKEND = None
_LIBUSB0_DLL_PATH: Optional[str] = None
_LIBUSB0_SEARCHED = False


def _dll_is_64bit(path: str) -> bool:
    """Prueft anhand des PE-Headers, ob eine DLL 64-Bit (x64/ARM64) ist."""
    try:
        with open(path, "rb") as fh:
            dos = fh.read(0x40)
            if len(dos) < 0x40 or dos[:2] != b"MZ":
                return False
            pe_off = int.from_bytes(dos[0x3C:0x40], "little")
            fh.seek(pe_off)
            if fh.read(4) != b"PE\x00\x00":
                return False
            machine = int.from_bytes(fh.read(2), "little")
        return machine in (0x8664, 0xAA64)  # AMD64, ARM64
    except Exception:
        return False


def _libusb0_dll_candidates() -> list[str]:
    """Sammelt moegliche libusb0.dll-Pfade, 64-Bit-Kandidaten zuerst."""
    found: list[str] = []

    env = os.environ.get(LIBUSB0_ENV)
    if env:
        found.append(env)

    here = os.path.dirname(os.path.abspath(__file__))
    found.append(os.path.join(here, "libusb0.dll"))

    pf = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    pf64 = os.environ.get("ProgramFiles") or r"C:\Program Files"

    # Bekannte DJI-Installationen (schnell, ohne rekursives Suchen).
    known = [
        os.path.join(pf, "DJI Product", "DJI Assistant 2 (DJI FPV series)",
                     "Drivers", "Drivers_Win10", "Vision", "vision_amd64"),
        os.path.join(pf, "DJI Product", "DJI Assistant 2 (Consumer Drones Series)",
                     "Drivers", "Drivers_Win10", "DJI_BULK", "bulk_amd64"),
        os.path.join(pf, "DJI Flight Simulator", "Driver", "Drivers_Other",
                     "Vision", "amd64"),
        os.path.join(pf64, "DJI Product", "DJI Assistant 2 (DJI FPV series)",
                     "Drivers", "Drivers_Win10", "Vision", "vision_amd64"),
    ]
    for d in known:
        found.append(os.path.join(d, "libusb0.dll"))

    # Rekursiv in den DJI-Ordnern suchen (falls andere Version installiert ist).
    roots = [
        os.path.join(pf, "DJI Product"),
        os.path.join(pf64, "DJI Product"),
        os.path.join(pf, "DJI Flight Simulator"),
        os.path.join(pf64, "DJI Flight Simulator"),
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            for dirpath, _dirnames, filenames in os.walk(root):
                if "libusb0.dll" in filenames:
                    found.append(os.path.join(dirpath, "libusb0.dll"))
        except Exception:
            continue

    # Duplikate entfernen, Reihenfolge erhalten.
    seen = set()
    out = []
    for p in found:
        p = os.path.abspath(p)
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def libusb0_backend():
    """
    Liefert das pyusb-libusb0-Backend mit einer 64-Bit libusb0.dll oder None.

    Wichtig: pyusb findet die DLL unter Windows nicht selbst, deshalb wird
    der gefundene Pfad explizit per find_library uebergeben.
    """
    global _LIBUSB0_BACKEND, _LIBUSB0_DLL_PATH, _LIBUSB0_SEARCHED
    if _LIBUSB0_SEARCHED:
        return _LIBUSB0_BACKEND
    _LIBUSB0_SEARCHED = True

    if not sys.platform.startswith("win"):
        return None
    try:
        from usb.backend import libusb0
    except Exception:
        return None

    for path in _libusb0_dll_candidates():
        if not os.path.isfile(path) or not _dll_is_64bit(path):
            continue
        try:
            be = libusb0.get_backend(find_library=lambda name, p=path: p)
        except Exception:
            continue
        if be is not None:
            _LIBUSB0_BACKEND = be
            _LIBUSB0_DLL_PATH = path
            return be
    return None


def libusb0_dll_path() -> Optional[str]:
    """Pfad der benutzten 64-Bit libusb0.dll (oder None)."""
    libusb0_backend()
    return _LIBUSB0_DLL_PATH


def libusb0_devices() -> list[usb.core.Device]:
    """Geraete, die ueber libusb-win32 erreichbar sind (meist nur Interface 3)."""
    be = libusb0_backend()
    if be is None:
        return []
    try:
        return list(usb.core.find(find_all=True, backend=be))
    except Exception:
        return []


def describe_usb_error(exc: BaseException) -> str:
    """Uebersetzt libusb-Fehler in konkrete Handlungshinweise."""
    code = getattr(exc, "errno", None)
    text = str(exc)

    if isinstance(exc, NotImplementedError) or code == -12 or "not supported" in text.lower():
        return (
            "Windows hat fuer Interface 3 keinen WinUSB-Treiber.\n"
            "  -> Zadig (https://zadig.akeo.ie) starten, Goggles auswaehlen,\n"
            "     'Interface 3' (bzw. MI_03) markieren und 'WinUSB' installieren.\n"
            "  -> Nicht das ganze Composite-Device ersetzen, nur Interface 3!"
        )
    if code == 13 or "access" in text.lower():
        return (
            "Zugriff verweigert. DJI Assistant 2, DJI Fly, DJI Studio oder ein\n"
            "  anderes Programm haelt das Geraet offen. Bitte komplett schliessen\n"
            "  (auch im Task-Manager pruefen) und USB-Kabel neu stecken."
        )
    if code == 16 or "busy" in text.lower():
        return (
            "Interface 3 ist belegt. Bitte alle DJI-Programme schliessen und\n"
            "  die Goggles einmal vom USB-Port trennen und wieder anschliessen."
        )
    if code in (19, 2) or "no device" in text.lower() or "not found" in text.lower():
        return (
            "Geraet nicht (mehr) gefunden. USB-Kabel/Port pruefen, Goggles\n"
            "  einschalten und erneut versuchen. Bei USB-C auf USB-C ggf. einen\n"
            "  OTG-Adapter verwenden (die Goggles koennen USB-Host sein)."
        )
    return f"USB-Fehler: {text}"


# --------------------------------------------------------------------------
# Geraete-Erkennung
# --------------------------------------------------------------------------

@dataclass
class UsbDeviceInfo:
    vid: int
    pid: int
    manufacturer: str = ""
    product: str = ""
    serial: str = ""
    valid: bool = True


def _safe_string(dev, index: Optional[int]) -> str:
    if not index:
        return ""
    try:
        return usb.util.get_string(dev, index) or ""
    except Exception:
        return ""


def list_usb_devices() -> list[UsbDeviceInfo]:
    devs = list(usb.core.find(find_all=True, backend=_backend()))
    out: list[UsbDeviceInfo] = []
    for d in devs:
        out.append(
            UsbDeviceInfo(
                vid=d.idVendor,
                pid=d.idProduct,
                manufacturer=_safe_string(d, d.iManufacturer),
                product=_safe_string(d, d.iProduct),
                serial=_safe_string(d, d.iSerialNumber),
            )
        )
    return out


def dji_devices() -> list[usb.core.Device]:
    return list(usb.core.find(find_all=True, idVendor=DJI_VID, backend=_backend()))


def _has_video_interface(dev: usb.core.Device) -> bool:
    """Prueft (ohne zu claimen), ob Interface 3 existiert."""
    try:
        cfg = dev.get_active_configuration()
    except Exception:
        try:
            cfg = dev[0]
        except Exception:
            return False
    try:
        return any(intf.bInterfaceNumber == VIDEO_INTERFACE for intf in cfg)
    except Exception:
        return False


def find_goggles(pid: Optional[int] = None, backend=None) -> usb.core.Device:
    """
    Sucht die Goggles. Bevorzugt PID 0x001F, sonst das erste DJI-Geraet
    mit einem Interface 3.
    """
    backend = backend or _backend()

    if pid is not None:
        dev = usb.core.find(idVendor=DJI_VID, idProduct=pid, backend=backend)
        if dev is None:
            raise GogglesNotFound(
                f"Keine Goggles mit VID {DJI_VID:04X}:PID {pid:04X} gefunden.\n"
                "Bitte Goggles einschalten, USB-Kabel pruefen und "
                "diagnose_usb.bat ausfuehren."
            )
        return dev

    candidates = list(usb.core.find(find_all=True, idVendor=DJI_VID, backend=backend))
    if not candidates:
        raise GogglesNotFound(
            "Keine DJI-Goggles gefunden (VID 2CA3).\n"
            "  - Goggles eingeschaltet?\n"
            "  - USB-C Kabel verbunden? (bei USB-C/USB-C ggf. OTG-Adapter)\n"
            "  - Anderes Kabel / anderen Port probiert?\n"
            "  - diagnose_usb.bat fuer Details ausfuehren."
        )

    for dev in candidates:
        if dev.idProduct in DEFAULT_PIDS:
            return dev

    for dev in candidates:
        if _has_video_interface(dev):
            return dev

    return candidates[0]


# --------------------------------------------------------------------------
# USB-Verbindung
# --------------------------------------------------------------------------

class GogglesUSB:
    """Duenne Schicht um Interface 3 der Goggles."""

    def __init__(self, pid: Optional[int] = None, verbose: bool = False):
        self.pid = pid
        self.verbose = verbose
        self.dev: Optional[usb.core.Device] = None
        self.backend_name = ""
        self.total_bytes = 0
        self.last_data_time = 0.0
        self.last_magic_time = 0.0
        self.connected = False

    # -- Lebenszyklus ------------------------------------------------------
    def open(self) -> None:
        """
        Oeffnet Interface 3. Zuerst mit libusb-1.0 (WinUSB), dann mit dem
        legacy libusb0-Backend (libusb-win32, wie von DJI Assistant 2
        installiert). So funktioniert es mit beiden Treibern.
        """
        errors: list[tuple[str, BaseException]] = []
        dev = None
        backend_name = ""

        for name, get_backend in (("libusb-1.0", _backend),
                                  ("libusb0", libusb0_backend)):
            try:
                backend = get_backend()
            except Exception as exc:
                errors.append((name, exc))
                continue
            if backend is None:
                continue

            try:
                candidate = find_goggles(self.pid, backend=backend)
            except GogglesNotFound as exc:
                errors.append((name, exc))
                continue

            try:
                self._prepare(candidate)
            except Exception as exc:
                errors.append((name, exc))
                try:
                    usb.util.dispose_resources(candidate)
                except Exception:
                    pass
                continue

            dev = candidate
            backend_name = name
            break

        if dev is None:
            raise self._open_error(errors)

        self.dev = dev
        self.backend_name = backend_name
        self.connected = True
        if self.verbose:
            print(
                f"[usb] Geraet {dev.idVendor:04X}:{dev.idProduct:04X} "
                f"({_safe_string(dev, dev.iProduct) or 'DJI'}) "
                f"ueber {backend_name}",
                file=sys.stderr,
            )

    def _prepare(self, dev: usb.core.Device) -> None:
        """Kernel-Treiber abhaengen, Konfiguration setzen, Interface claimen."""
        # Linux: Kernel-Treiber fuer Interface 3 abhaengen.
        try:
            if dev.is_kernel_driver_active(VIDEO_INTERFACE):
                dev.detach_kernel_driver(VIDEO_INTERFACE)
        except (NotImplementedError, usb.core.USBError, AttributeError):
            pass

        # Konfiguration nur setzen, wenn sie nicht schon aktiv ist. Unter
        # libusb-win32 ist Interface 3 bereits fertig konfiguriert; ein
        # unnoetiges set_configuration kann andere Interfaces stoeren.
        need_config = True
        try:
            cfg = dev.get_active_configuration()
            need_config = (cfg is None
                           or int(cfg.bConfigurationValue) != CONFIGURATION)
        except Exception:
            need_config = True
        if need_config:
            try:
                dev.set_configuration(CONFIGURATION)
            except (NotImplementedError, usb.core.USBError) as exc:
                if self.verbose:
                    print(f"[usb] set_configuration ignoriert: {exc}",
                          file=sys.stderr)

        usb.util.claim_interface(dev, VIDEO_INTERFACE)

        # Nach einem abgebrochenen Transfer (z.B. Timeout beim Senden von
        # RMVT) bleibt ein Bulk-Endpoint bei libusb-win32 haengen. Ein
        # clear_halt setzt ihn zuverlaessig zurueck.
        self.clear_halt(dev, EP_CONTROL_OUT)
        self.clear_halt(dev, EP_VIDEO_IN)

    @staticmethod
    def clear_halt(dev: usb.core.Device, endpoint: int) -> None:
        try:
            dev.clear_halt(endpoint)
        except Exception:
            pass

    @staticmethod
    def _open_error(errors: list[tuple[str, BaseException]]) -> GogglesError:
        if not errors:
            return GogglesNotFound(
                "Keine DJI-Goggles gefunden (VID 2CA3).\n"
                "  - Goggles eingeschaltet?\n"
                "  - USB-C Kabel verbunden? (bei USB-C/USB-C ggf. OTG-Adapter)\n"
                "  - diagnose_usb.bat fuer Details ausfuehren."
            )
        if all(isinstance(exc, GogglesNotFound) for _name, exc in errors):
            return GogglesNotFound(str(errors[0][1]))

        details = "\n".join(
            f"  [{name}] {describe_usb_error(exc)}" for name, exc in errors
        )
        extra = ""
        if libusb0_backend() is None:
            extra = (
                "\n  Hinweis: Keine 64-Bit libusb0.dll gefunden. DJI Assistant 2\n"
                "  installieren oder DJI_LIBUSB0_DLL auf eine passende DLL setzen."
            )
        return GogglesError(
            "Interface 3 konnte mit keinem Backend geoeffnet werden:\n"
            f"{details}{extra}"
        )

    def close(self) -> None:
        self.connected = False
        if self.dev is None:
            return
        try:
            usb.util.release_interface(self.dev, VIDEO_INTERFACE)
        except Exception:
            pass
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:
            pass
        self.dev = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- I/O ---------------------------------------------------------------
    def send_magic(self) -> None:
        if self.dev is None:
            raise GogglesError("Nicht verbunden.")
        for attempt in (0, 1):
            try:
                self.dev.write(EP_CONTROL_OUT, MAGIC, timeout=USB_TIMEOUT_MS)
                break
            except Exception as exc:
                # Ein haengender Bulk-OUT-Endpoint (libusb-win32) laesst den
                # Write in einen Timeout laufen. Einmal zuruecksetzen und
                # erneut versuchen.
                if attempt == 0 and "timeout" in str(exc).lower():
                    self.clear_halt(self.dev, EP_CONTROL_OUT)
                    continue
                raise
        self.last_magic_time = time.monotonic()

    def drain(
        self,
        max_seconds: float = 4.0,
        window: float = 0.25,
        steady_rate: float = 10.0e6,
        timeout_ms: int = 100,
    ) -> int:
        """
        Verwirft bereits gepufferte Videodaten (Backlog).

        Die Goggles streamen weiter, waehrend der Host nicht liest. Beim
        Verbinden kommen deshalb oft mehrere Sekunden alte Bilder in einem
        Schwall, die den Player kurz vorspulen lassen. Hier wird gelesen,
        bis die Datenrate auf den Live-Wert faellt: ein Backlog wird mit
        USB-Geschwindigkeit (viele MB/s) geliefert, der Live-Stream liegt
        bei ~11 Mbit/s (~1,4 MB/s). `steady_rate` liegt bewusst dazwischen.

        Rueckgabe: Anzahl der verworfenen Bytes.
        """
        if self.dev is None:
            raise GogglesError("Nicht verbunden.")

        drained = 0
        start = time.monotonic()
        window_start = start
        window_bytes = 0

        while time.monotonic() - start < max_seconds:
            data = self.read(READ_SIZE, timeout_ms=timeout_ms)
            now = time.monotonic()
            if data:
                drained += len(data)
                window_bytes += len(data)

            span = now - window_start
            if span >= window:
                rate = window_bytes / span
                window_start = now
                window_bytes = 0
                if drained > 0 and rate <= steady_rate:
                    break

        return drained

    def read_to_keyframe(
        self,
        codec: str = "h264",
        max_seconds: float = 3.0,
        timeout_ms: int = 1000,
    ) -> tuple[int, bytes]:
        """
        Liest bis zum naechsten GOP-Start (SPS/PPS) und verwirft alles davor.

        Nach `drain()` befindet man sich mitten in einer GOP. Der Decoder
        braucht aber SPS/PPS, sonst liefert FFmpeg fuer die ersten Pakete
        einen Fehler. Rueckgabe: (verworfene Bytes, Daten ab SPS).
        """
        if self.dev is None:
            raise GogglesError("Nicht verbunden.")

        if codec == "hevc":
            # HEVC: SPS hat NAL-Typ 33 -> erster Byte 0x42.
            marker = b"\x00\x00\x01\x42"
        else:
            # H.264: SPS hat NAL-Typ 7.
            marker = b"\x00\x00\x01\x67"

        discarded = 0
        buf = bytearray()
        start = time.monotonic()

        while time.monotonic() - start < max_seconds:
            data = self.read(READ_SIZE, timeout_ms=timeout_ms)
            if data:
                discarded += len(data)
                buf += data

                idx = buf.find(marker)
                if idx >= 0:
                    begin = idx - 1 if idx > 0 and buf[idx - 1] == 0 else idx
                    head = bytes(buf[begin:])
                    # Alles vor dem SPS ist Muell (Rest der alten GOP).
                    return discarded - len(head), head

                # Ohne SPS nicht unbegrenzt puffern.
                if len(buf) > 8 * 1024 * 1024:
                    buf = buf[-1024 * 1024:]
            elif time.monotonic() - start > max_seconds:
                break

        return discarded, b""

    def read(self, size: int = READ_SIZE, timeout_ms: int = USB_TIMEOUT_MS) -> bytes:
        if self.dev is None:
            raise GogglesError("Nicht verbunden.")
        try:
            data = self.dev.read(EP_VIDEO_IN, size, timeout=timeout_ms)
        except usb.core.USBError as exc:
            timeout_exc = getattr(usb.core, "USBTimeoutError", ())
            text = str(exc).lower()
            if (
                isinstance(exc, timeout_exc)
                or getattr(exc, "errno", None) in (60, 110, 116, 10060)
                or getattr(exc, "backend_error_code", None)
                in (-60, -110, -116, -10060)
                or "timeout" in text
            ):
                return b""
            raise
        if not data:
            return b""
        self.total_bytes += len(data)
        self.last_data_time = time.monotonic()
        return bytes(data)


# --------------------------------------------------------------------------
# Datei-aehnlicher Reader fuer PyAV/FFmpeg
# --------------------------------------------------------------------------

class VideoStreamReader(io.RawIOBase):
    """
    Blockierender, dateiaehnlicher Reader. PyAV liest hier den rohen
    H.264-Stream heraus. Bei kurzen Timeouts wird automatisch das
    RMVT-Magic-Paket erneut gesendet.

    Optional wird alles zusaetzlich in eine .h264-Datei geschrieben
    (Aufnahme ohne Re-Encoding).
    """

    def __init__(
        self,
        goggles: GogglesUSB,
        stop_event: threading.Event,
        stall_timeout: float = 8.0,
        resend_interval: float = 1.0,
        verbose: bool = False,
        prefix: bytes = b"",
    ):
        super().__init__()
        self.goggles = goggles
        self.stop_event = stop_event
        self.stall_timeout = stall_timeout
        self.resend_interval = resend_interval
        self.verbose = verbose
        self._prefix = bytearray(prefix or b"")

        self.error: Optional[BaseException] = None
        self.read_chunks = 0
        self.started = time.monotonic()
        self._last_magic = 0.0
        self._record_fh = None
        self._record_lock = threading.Lock()
        self.record_bytes = 0

    # -- Aufnahme-Tee ------------------------------------------------------
    def set_record_file(self, fh) -> None:
        with self._record_lock:
            self._record_fh = fh

    def _tee(self, data: bytes) -> None:
        with self._record_lock:
            fh = self._record_fh
            if fh is None:
                return
            try:
                fh.write(data)
                fh.flush()
                self.record_bytes += len(data)
            except Exception:
                self._record_fh = None

    # -- io.RawIOBase ------------------------------------------------------
    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        view = memoryview(buffer)

        # Daten, die vor dem Oeffnen bereits gelesen wurden (GOP-Start).
        if self._prefix:
            n = min(len(view), len(self._prefix))
            chunk = bytes(self._prefix[:n])
            del self._prefix[:n]
            view[:n] = chunk
            self.read_chunks += 1
            self._tee(chunk)
            return n

        while not self.stop_event.is_set():
            try:
                data = self.goggles.read(len(view), timeout_ms=USB_TIMEOUT_MS)
            except Exception as exc:  # USB weg, etc.
                self.error = exc
                return 0

            if data:
                n = len(data)
                view[:n] = data
                self.read_chunks += 1
                self._tee(data)
                return n

            now = time.monotonic()
            # Kein Stream? Nach 1 s Magic erneut senden.
            if now - self._last_magic >= self.resend_interval:
                try:
                    self.goggles.send_magic()
                    self._last_magic = now
                    if self.verbose:
                        print("[usb] RMVT erneut gesendet", file=sys.stderr)
                except Exception as exc:
                    self.error = exc
                    return 0

            # Laengere Stille: Stream als beendet betrachten (Reconnect).
            last = self.goggles.last_data_time or self.started
            if now - last > self.stall_timeout:
                self.error = GogglesError(
                    "Kein Videostream. Ist die Drohne/Air Unit an und verbunden?\n"
                    "Die Goggles liefern nur Bild, wenn sie ein Video-Signal haben."
                )
                return 0

        return 0

    def close(self) -> None:
        super().close()


# --------------------------------------------------------------------------
# H.264 Rohaufnahme -> MP4 (ohne Re-Encoding)
# --------------------------------------------------------------------------

_ANNEXB_SC = b"\x00\x00\x00\x01"


def _find_annexb_nal(data: bytes, nal_type: int, start: int = 0) -> bytes:
    """Sucht eine NAL-Einheit (z.B. SPS=7, PPS=8) und gibt sie Annex-B zurueck."""
    i = start
    n = len(data)
    while i < n - 4:
        j = data.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= n:
            return b""
        if (data[j + 3] & 0x1F) == (nal_type & 0x1F):
            begin = j - 1 if j > 0 and data[j - 1] == 0 else j
            k = data.find(b"\x00\x00\x01", j + 3)
            end = k if k >= 0 else n
            nal = data[begin:end]
            if not nal.startswith(_ANNEXB_SC):
                nal = _ANNEXB_SC + nal.lstrip(b"\x00")
            return nal
        i = j + 3
    return b""


def _headers_from_avcc(data: bytes) -> bytes:
    """Wandelt AVCC-Extradata (avcC) in Annex-B SPS/PPS um."""
    try:
        if len(data) < 8 or data[0] != 1:
            return b""
        out = bytearray()
        num_sps = data[5] & 0x1F
        off = 6
        for _ in range(num_sps):
            ln = int.from_bytes(data[off:off + 2], "big")
            off += 2
            out += _ANNEXB_SC + data[off:off + ln]
            off += ln
        num_pps = data[off]
        off += 1
        for _ in range(num_pps):
            ln = int.from_bytes(data[off:off + 2], "big")
            off += 2
            out += _ANNEXB_SC + data[off:off + ln]
            off += ln
        return bytes(out)
    except Exception:
        return b""


def normalize_annexb_headers(extradata: bytes) -> bytes:
    """Liefert SPS+PPS als Annex-B (4-Byte-Startcodes) oder b\"\"."""
    data = bytes(extradata or b"")
    if not data:
        return b""
    sps = _find_annexb_nal(data, 7)
    pps = _find_annexb_nal(data, 8)
    if sps and pps:
        return sps + pps
    return _headers_from_avcc(data)


def extract_annexb_headers(raw_path: str, max_scan: int = 16 * 1024 * 1024) -> bytes:
    """
    Sucht SPS/PPS im Rohstream. Wichtig fuer Aufnahmen, die mitten in einer
    GOP starten: ohne diese Header kann kein MP4 erzeugt werden.
    """
    try:
        with open(raw_path, "rb") as fh:
            data = fh.read(max_scan)
    except Exception:
        return b""
    sps = _find_annexb_nal(data, 7)
    if not sps:
        return b""
    pps = _find_annexb_nal(data, 8)
    if not pps:
        return b""
    return sps + pps


def remux_h264_to_mp4(
    raw_path: str,
    mp4_path: str,
    fps_hint: float = 60.0,
    extradata: bytes = b"",
) -> bool:
    """
    Packt einen rohen Annex-B H.264-Stream in ein MP4, ohne neu zu kodieren.
    Die Goggles liefern keine Zeitstempel, daher werden sie aus der
    Bildrate erzeugt. `fps_hint` sollte die gemessene Rate sein; fehlt sie,
    wird die aus dem SPS/VUI erkannte Rate benutzt, sonst 60 fps.

    `extradata` (SPS/PPS aus dem Live-Stream) hilft, wenn die Aufnahme
    mitten in einer GOP beginnt und die Datei selbst keine Header enthaelt.
    """
    try:
        import av
        import shutil
        from fractions import Fraction
    except ImportError:
        return False

    temp_path = None
    inp = None
    out = None

    def _open_input(path: str):
        c = av.open(path, format="h264")
        s = c.streams.video[0]
        if not (s.codec_context.extradata or b"") or int(s.codec_context.width or 0) == 0:
            headers = normalize_annexb_headers(extradata) or extract_annexb_headers(path)
            if headers:
                c.close()
                tmp = path + ".with_headers.h264"
                with open(tmp, "wb") as dst, open(path, "rb") as src:
                    dst.write(headers)
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                c = av.open(tmp, format="h264")
                s = c.streams.video[0]
                return c, s, tmp
        return c, s, None

    try:
        inp, in_stream, temp_path = _open_input(raw_path)
        out = av.open(mp4_path, "w")
        out_stream = out.add_stream_from_template(in_stream)

        fps = fps_hint
        if not (1.0 <= float(fps or 0) <= 240.0):
            fps = 60.0
            try:
                rate = in_stream.guessed_rate or in_stream.average_rate
                if rate and 1.0 <= float(rate) <= 240.0:
                    fps = float(rate)
            except Exception:
                pass

        out_stream.time_base = Fraction(1, 90000)
        step = int(round(90000.0 / fps))
        ts = 0

        muxed = 0
        for packet in inp.demux():
            if packet.size <= 0:
                continue
            # Erst ab dem ersten Keyframe muxen - davor liegende P-Frames
            # sind ohne Referenzbild ohnehin nicht dekodierbar.
            if muxed == 0 and not packet.is_keyframe:
                continue
            packet.stream = out_stream
            # Wichtig: ohne explizites time_base wuerde PyAV die PTS vom
            # Input-Timebase (roher H.264: 1/1200000) umrechnen und die
            # Zeitstempel dadurch um Faktor ~13 stauchen.
            packet.time_base = out_stream.time_base
            packet.pts = ts
            packet.dts = ts
            packet.duration = step
            ts += step
            out.mux(packet)
            muxed += 1

        out.close()
        out = None
        inp.close()
        inp = None
        return muxed > 0 and os.path.getsize(mp4_path) > 0
    except Exception:
        try:
            if out is not None:
                out.close()
        except Exception:
            pass
        try:
            if inp is not None:
                inp.close()
        except Exception:
            pass
        return False
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except Exception:
                pass



def default_record_dir() -> str:
    base = os.path.join(os.path.expanduser("~"), "Videos", "DJI Goggles")
    try:
        os.makedirs(base, exist_ok=True)
        return base
    except Exception:
        base = os.path.join(os.getcwd(), "recordings")
        os.makedirs(base, exist_ok=True)
        return base
