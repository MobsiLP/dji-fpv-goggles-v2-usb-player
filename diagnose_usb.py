#!/usr/bin/env python3
"""
DJI FPV Goggles V2 - USB-Diagnose
=================================

Prueft in dieser Reihenfolge:

  1. libusb-Geraeteliste (VID:PID)
  2. DJI-Geraete + USB-Deskriptoren (Interface 3, Endpoints 0x03/0x84)
  3. Windows PnP-Status + Treiber (WinUSB? RNDIS? Mass Storage?)
  4. RNDIS-Netzwerk (192.168.42.x)
  5. Optional: RMVT-Magic senden und 3 s Videodaten lesen (--test)

Aufruf:
    diagnose_usb.bat
    .venv\\Scripts\\python.exe diagnose_usb.py --test
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import usb.core
import usb.util

from dji_usb import (
    DJI_VID,
    EP_CONTROL_OUT,
    EP_VIDEO_IN,
    KNOWN_PIDS,
    VIDEO_INTERFACE,
    GogglesError,
    GogglesNotFound,
    GogglesUSB,
    describe_usb_error,
    dji_devices,
    libusb0_devices,
    libusb0_dll_path,
    list_usb_devices,
    remux_h264_to_mp4,
)


IS_WINDOWS = sys.platform.startswith("win")


# --------------------------------------------------------------------------
# Ausgabe-Helfer
# --------------------------------------------------------------------------

def section(title: str) -> None:
    print()
    print("=" * 74)
    print(title)
    print("=" * 74)


def ok(text: str) -> None:
    print(f"  [OK]    {text}")


def warn(text: str) -> None:
    print(f"  [HINWEIS] {text}")


def fail(text: str) -> None:
    print(f"  [FEHLER]  {text}")


def info(text: str) -> None:
    print(f"          {text}")


# --------------------------------------------------------------------------
# 1) libusb
# --------------------------------------------------------------------------

def check_libusb() -> None:
    section("1) libusb / pyusb")
    try:
        devs = list_usb_devices()
    except Exception as exc:
        fail(f"libusb-1.0 konnte nicht initialisiert werden: {exc}")
        info("Bitte 'pip install pyusb libusb-package' ausfuehren.")
    else:
        ok(f"libusb-1.0 geladen, {len(devs)} USB-Geraet(e) sichtbar")
        for d in devs:
            if d.vid == DJI_VID:
                continue
            name = " ".join(x for x in (d.manufacturer, d.product) if x).strip()
            print(f"          {d.vid:04X}:{d.pid:04X}  {name}")

    # libusb-win32 (libusb0) - wird von DJI Assistant 2 fuer Interface 3
    # installiert und ist fuer den Video-Pfad oft der einzige Weg.
    dll = libusb0_dll_path()
    if dll:
        ok(f"libusb-win32-Backend verfuegbar (64-Bit DLL)")
        info(dll)
        try:
            devs0 = libusb0_devices()
            info(f"{len(devs0)} Geraet(e) ueber libusb0 erreichbar")
            for d in devs0:
                print(f"          {d.idVendor:04X}:{d.idProduct:04X}")
        except Exception as exc:
            warn(f"libusb0-Enumeration fehlgeschlagen: {exc}")
    else:
        warn("Keine 64-Bit libusb0.dll gefunden (nur noetig fuer den")
        info("libusb-win32-Treiber von DJI Assistant 2).")


# --------------------------------------------------------------------------
# 2) DJI-Geraete + Deskriptoren
# --------------------------------------------------------------------------

def _dump_config(dev: usb.core.Device) -> None:
    print(f"          Device: {dev.idVendor:04X}:{dev.idProduct:04X}")
    for attr in ("bcdUSB", "bDeviceClass", "bDeviceSubClass", "bDeviceProtocol",
                 "bMaxPacketSize0", "bcdDevice"):
        try:
            print(f"          {attr}: {getattr(dev, attr)}")
        except Exception:
            pass

    for idx, cfg in enumerate(dev.configurations()):
        print(f"          Config {idx}: value={cfg.bConfigurationValue} "
              f"interfaces={cfg.bNumInterfaces} attrs=0x{cfg.bmAttributes:02X}")
        for intf in cfg:
            cls, sub, proto = intf.bInterfaceClass, intf.bInterfaceSubClass, intf.bInterfaceProtocol
            tag = ""
            if intf.bInterfaceNumber == VIDEO_INTERFACE:
                tag = "  <== VIDEO-INTERFACE"
            print(
                f"            Interface {intf.bInterfaceNumber}.{intf.bAlternateSetting}"
                f"  class={cls} subclass={sub} protocol={proto}{tag}"
            )
            for ep in intf:
                direction = "IN " if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
                print(
                    f"              EP 0x{ep.bEndpointAddress:02X} {direction} "
                    f"{usb.util.endpoint_type(ep.bmAttributes)} "
                    f"maxPacket={ep.wMaxPacketSize}"
                )


def check_dji_devices(pid: int | None) -> bool:
    section("2) DJI-Geraete (VID 2CA3)")
    try:
        devs = dji_devices()
    except Exception:
        devs = []

    if not devs:
        # libusb-1.0 sieht das Geraet evtl. nicht, libusb0 schon.
        devs = [d for d in libusb0_devices() if d.idVendor == DJI_VID]
        if devs:
            ok("Geraet nur ueber libusb0 (libusb-win32) sichtbar.")

    if not devs:
        fail("Keine Geraete mit VID 2CA3 gefunden.")
        info("Goggles einschalten, USB-C Kabel pruefen (ggf. OTG-Adapter).")
        info("USB-C auf USB-C kann dazu fuehren, dass die Goggles laden statt")
        info("Daten liefern - dann einen USB-A-auf-USB-C Port/Adapter nutzen.")
        return False

    found_video = False
    for dev in devs:
        name = KNOWN_PIDS.get(dev.idProduct, "unbekanntes DJI-Geraet")
        print()
        ok(f"Gefunden: {dev.idVendor:04X}:{dev.idProduct:04X}  ({name})")
        try:
            _dump_config(dev)
        except Exception as exc:
            warn(f"Deskriptoren nicht lesbar: {describe_usb_error(exc)}")
            info("Das ist normal, solange noch kein WinUSB-Treiber fuer")
            info("Interface 3 installiert ist.")
            continue

        try:
            cfg = dev.get_active_configuration()
            interfaces = {i.bInterfaceNumber for i in cfg}
        except Exception:
            interfaces = set()
            try:
                for cfg in dev.configurations():
                    interfaces |= {i.bInterfaceNumber for i in cfg}
                    break
            except Exception:
                pass

        if VIDEO_INTERFACE in interfaces:
            found_video = True
            ok(f"Interface {VIDEO_INTERFACE} vorhanden (Video-Pfad)")

        try:
            if dev.is_kernel_driver_active(VIDEO_INTERFACE):
                warn("Kernel-Treiber ist auf Interface 3 aktiv (Linux).")
        except Exception:
            pass

    if pid is not None:
        match = [d for d in devs if d.idProduct == pid]
        if not match:
            warn(f"Gewuenschte PID 0x{pid:04X} nicht gefunden.")
    return found_video


# --------------------------------------------------------------------------
# 3) Windows PnP / Treiber
# --------------------------------------------------------------------------

def _run_ps(script: str, timeout: int = 30) -> str:
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        return (p.stdout or "") + (p.stderr or "")
    except Exception as exc:
        return f"(PowerShell-Fehler: {exc})"


def check_windows_pnp() -> None:
    section("3) Windows PnP / Treiber")
    if not IS_WINDOWS:
        warn("Kein Windows - Abschnitt uebersprungen.")
        return

    script = r"""
$devs = Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue |
        Where-Object { $_.InstanceId -match 'VID_2CA3' }
if (-not $devs) {
  $devs = Get-WmiObject Win32_PnPEntity -ErrorAction SilentlyContinue |
          Where-Object { $_.DeviceID -match 'VID_2CA3' }
}
if (-not $devs) { Write-Output 'KEINE_2CA3_GERAETE'; exit }
foreach ($d in $devs) {
  Write-Output ('INSTANCE: ' + $d.InstanceId)
  Write-Output ('STATUS  : ' + $d.Status)
  Write-Output ('CLASS   : ' + $d.Class)
  Write-Output ('NAME    : ' + $d.FriendlyName)
  if ($d.PSObject.Properties.Name -contains 'Service') { Write-Output ('SERVICE : ' + $d.Service) }
  $drv = Get-PnpDeviceProperty -InstanceId $d.InstanceId -KeyName 'DEVPKEY_Device_DriverProvider','DEVPKEY_Device_DriverDesc' -ErrorAction SilentlyContinue
  foreach ($p in $drv) { Write-Output ('DRIVER  : ' + $p.KeyName + ' = ' + $p.Data) }
  Write-Output '---'
}
"""
    out = _run_ps(script)
    print(out.rstrip())

    if "KEINE_2CA3_GERAETE" in out:
        fail("Windows sieht kein Geraet mit VID 2CA3.")
        info("Kabel/Port/Goggles-Stromversorgung pruefen.")
    elif "WinUSB" in out:
        ok("WinUSB-Treiber ist bereits im Spiel.")
    elif "libusb0" in out.lower() or "libusb-win32" in out.lower():
        ok("libusb-win32 (libusb0) ist installiert - wird automatisch genutzt.")
        info("DJI Assistant 2 bringt diesen Treiber mit; kein Zadig noetig.")
    else:
        warn("Weder WinUSB noch libusb-win32 in der Treiberliste sichtbar.")
        info("Fuer den Video-Pfad muss Interface 3 auf einen USB-Treiber")
        info("umgestellt werden:")
        info("  a) DJI Assistant 2 installieren (bringt libusb-win32 mit), oder")
        info("  b) Zadig -> Options -> List All Devices -> 'DJI FPV GOGGLES'")
        info("     -> Interface 3 / MI_03 auswaehlen -> WinUSB installieren.")
        info("  WICHTIG: Nicht das komplette Composite-Device ersetzen!")


# --------------------------------------------------------------------------
# 4) RNDIS / Netzwerk
# --------------------------------------------------------------------------

def check_network() -> None:
    section("4) RNDIS-Netzwerk (optional)")
    if not IS_WINDOWS:
        warn("Kein Windows - Abschnitt uebersprungen.")
        return

    script = r"""
$adapters = Get-NetAdapter -ErrorAction SilentlyContinue |
  Where-Object { $_.InterfaceDescription -match 'RNDIS|Remote NDIS' -or $_.Name -match 'RNDIS' }
if ($adapters) {
  $adapters | ForEach-Object { Write-Output ('ADAPTER: ' + $_.Name + ' | ' + $_.Status + ' | ' + $_.InterfaceDescription) }
} else { Write-Output 'KEIN_RNDIS_ADAPTER' }
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
  Where-Object { $_.IPAddress -match '^192\.168\.(42|2|4)\.' } |
  ForEach-Object { Write-Output ('IP     : ' + $_.InterfaceAlias + ' = ' + $_.IPAddress + '/' + $_.PrefixLength) }
"""
    out = _run_ps(script)
    print(out.rstrip())
    if "192.168.42." in out:
        ok("RNDIS aktiv (192.168.42.x). Die Goggles sind im Netzwerk-Modus.")
        info("Das ist fuer den USB-Video-Out nicht noetig, zeigt aber, dass")
        info("die Goggles korrekt als Composite-Device erkannt wurden.")
    elif "KEIN_RNDIS_ADAPTER" in out:
        warn("Kein RNDIS-Adapter gefunden.")
        info("Entweder ist der RNDIS-Treiber nicht installiert, oder die")
        info("Goggles wurden noch nicht als Netzwerkgeraet erkannt.")


# --------------------------------------------------------------------------
# 5) Interface-3-Test
# --------------------------------------------------------------------------

def test_interface(pid: int | None, seconds: float, save_path: str | None) -> None:
    section("5) Interface-3-Test (RMVT + Video lesen)")
    info("Hinweis: Die Goggles wechseln dabei in den Video-Out-Modus.")
    info("Falls danach kein Bild mehr in der Brille ist: USB neu stecken.")

    g = GogglesUSB(pid, verbose=True)
    try:
        g.open()
        ok(f"Interface 3 erfolgreich geclaimt (Backend: {g.backend_name}).")
    except (GogglesNotFound, GogglesError) as exc:
        fail(str(exc))
        return

    try:
        g.send_magic()
        ok("Magic-Paket 'RMVT' (52 4D 56 54) an EP 0x03 gesendet.")
    except Exception as exc:
        fail(f"Magic senden fehlgeschlagen: {describe_usb_error(exc)}")
        g.close()
        return

    fh = None
    if save_path:
        os.makedirs(os.path.dirname(os.path.abspath(save_path)) or ".", exist_ok=True)
        fh = open(save_path, "wb")

    total = 0
    chunks = 0
    first = b""
    t0 = time.monotonic()
    print(f"          Lese {seconds:.0f} s von EP 0x{EP_VIDEO_IN:02X} ...")
    try:
        while time.monotonic() - t0 < seconds:
            data = g.read(65536, timeout_ms=500)
            if not data:
                continue
            total += len(data)
            chunks += 1
            if not first:
                first = data[:32]
            if fh is not None:
                fh.write(data)
    finally:
        if fh is not None:
            fh.close()
        g.close()

    elapsed = time.monotonic() - t0
    print()
    if total == 0:
        fail("Keine Videodaten empfangen.")
        info("Moegliche Ursachen:")
        info("  - Drohne/Air Unit ist aus oder nicht verbunden (kein Video-Signal)")
        info("  - Goggles sind nicht im richtigen USB-Modus")
        info("  - Interface 3 wurde zwar geclaimt, aber der Bulk-IN Endpoint")
        info("    liefert nichts (anderes Kabel/Port probieren)")
        return

    mbps = total * 8.0 / max(elapsed, 0.001) / 1e6
    ok(f"{total} Bytes in {elapsed:.1f} s empfangen "
       f"({chunks} Reads, {mbps:.2f} Mbit/s)")
    info(f"Erste Bytes: {first.hex(' ')}")

    # Kurzer Plausibilitaetscheck auf Annex-B Startcodes
    if b"\x00\x00\x01" in first or b"\x00\x00\x00\x01" in first:
        ok("Annex-B Startcode erkannt - das sieht nach H.264 aus.")
    else:
        warn("Kein Annex-B Startcode in den ersten Bytes (kann normal sein).")

    if save_path and os.path.exists(save_path):
        size = os.path.getsize(save_path)
        print(f"          Rohdaten gespeichert: {save_path} ({size/1e6:.2f} MB)")
        if save_path.lower().endswith(".mp4"):
            mp4 = save_path
            raw = os.path.splitext(save_path)[0] + ".h264"
            os.replace(save_path, raw)
            print("          Konvertiere nach MP4 ...")
            if remux_h264_to_mp4(raw, mp4, 60.0):
                ok(f"MP4 erstellt: {mp4}")
            else:
                warn(f"MP4-Konvertierung fehlgeschlagen, Rohstream: {raw}")


# --------------------------------------------------------------------------
# Ergebnis / naechste Schritte
# --------------------------------------------------------------------------

def print_verdict(args) -> None:
    section("Naechste Schritte")
    print("""  Wenn Abschnitt 5 Daten liefert:
      -> start_usb_player.bat   (oder: python dji_usb_player.py)

  Wenn Interface 3 nicht geclaimt werden kann:
      -> Der Player nutzt automatisch WinUSB (libusb-1.0) ODER
         libusb-win32 (libusb0, von DJI Assistant 2 installiert).
         Fehlt beides, Interface 3 auf WinUSB umstellen:
      -> Zadig (https://zadig.akeo.ie) installieren
      -> Options -> List All Devices
      -> 'DJI FPV GOGGLES' auswaehlen
      -> Interface 3 / MI_03 markieren
      -> Ziel-Treiber: WinUSB -> 'Install Driver' / 'Replace Driver'
      -> Danach diagnose_usb.bat erneut ausfuehren

  Wichtig:
      - DJI Assistant 2, DJI Fly (Desktop) und DJI Studio komplett schliessen.
      - Nur Interface 3 umstellen, nicht das ganze Composite-Device.
      - Die RNDIS-/Mass-Storage-/Seriell-Interfaces bleiben unberuehrt.
      - Goggles liefern nur Bild, wenn die Drohne/Air Unit an ist und
        ein Video-Signal anliegt.
""")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="DJI FPV Goggles USB-Diagnose")
    parser.add_argument("--pid", type=lambda x: int(x, 0), default=None,
                        help="USB Product ID (Default: auto, 0x001F)")
    parser.add_argument("--test", action="store_true",
                        help="RMVT senden und Videodaten lesen")
    parser.add_argument("--seconds", type=float, default=3.0,
                        help="Testdauer in Sekunden (Default: 3)")
    parser.add_argument("--save", default=None,
                        help="Testdaten speichern (.h264 oder .mp4)")
    args = parser.parse_args()

    print("DJI FPV Goggles V2 - USB-Diagnose")
    print(f"Python: {sys.version.split()[0]}  |  Plattform: {sys.platform}")

    check_libusb()
    has_video = check_dji_devices(args.pid)
    check_windows_pnp()
    check_network()

    if args.test:
        if not has_video:
            warn("Interface 3 wurde nicht erkannt - Test wird trotzdem versucht.")
        test_interface(args.pid, args.seconds, args.save)
    else:
        section("5) Interface-3-Test")
        info("Uebersprungen. Mit --test ausfuehren, um RMVT zu senden und")
        info("3 Sekunden Videodaten zu lesen.")

    print_verdict(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
        sys.exit(130)
