#!/usr/bin/env python3
"""
port_control.py — USB passthrough (usbipd) + serial port control for WSL2.

Manages USB device binding between Windows and WSL, and provides serial
console access for embedded devices (cameras, SBCs, etc.).

Usage:
    python3 port_control.py usb-list
    python3 port_control.py usb-attach 6-3
    python3 port_control.py usb-detach 6-3
    python3 port_control.py serial-list
    python3 port_control.py serial-send /dev/ttyUSB0 "ls -la"
    python3 port_control.py serial-login /dev/ttyUSB0 --user root
    python3 port_control.py serial-shell /dev/ttyUSB0 --user root --cmd "killall ipc_app"
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent

# Log next to the script by default; override with PORT_CONTROL_LOG.
LOG_FILE = Path(os.environ.get("PORT_CONTROL_LOG", SCRIPT_DIR / "port_control.log"))
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

DEFAULT_BAUD = 115200
DEFAULT_TIMEOUT = 2


def admin_cmd(commands, wait=3):
    """Run an elevated Windows command via the win-admin helper.

    Only USB bind/attach/detach needs admin rights, so the dependency is loaded
    lazily — serial and camera commands work without win-admin installed.

    Requires win_admin.py from the companion project:
        https://github.com/marcusice/wsl-win-admin-bridge
    Found via WIN_ADMIN env var, a copy next to this script, a sibling checkout,
    or one under $HOME.
    """
    win_admin = os.environ.get("WIN_ADMIN")
    candidates = [win_admin] if win_admin else []
    candidates += [
        SCRIPT_DIR / "win_admin.py",
        SCRIPT_DIR.parent / "wsl-win-admin-bridge" / "win_admin.py",
        Path.home() / "wsl-win-admin-bridge" / "win_admin.py",
    ]
    found = next((c for c in candidates if c and Path(c).exists()), None)
    if not found:
        raise SystemExit(
            "ERROR: win_admin.py helper not found (needed for USB bind/attach).\n"
            "Install wsl-win-admin-bridge "
            "(https://github.com/marcusice/wsl-win-admin-bridge) and either place\n"
            "win_admin.py next to this script or set the WIN_ADMIN env var to its path."
        )
    sys.path.insert(0, str(Path(found).resolve().parent))
    from win_admin import admin_cmd as _admin_cmd
    return _admin_cmd(commands, wait=wait)


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── USB device management (usbipd) ──────────────────────────


def _run_ps(cmd, admin=False):
    """Run a PowerShell command via WSL interop."""
    try:
        result = subprocess.run(
            [POWERSHELL, "-Command", cmd],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip(), result.stderr.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", "Timeout", 1
    except Exception as e:
        return "", str(e), 1


def cmd_usb_list(args):
    """List all USB devices visible to usbipd."""
    out, err, rc = _run_ps("usbipd list")
    if rc != 0:
        print(f"Error: {err}")
        return 1

    if args.json:
        devices = []
        for line in out.split("\n"):
            m = re.match(r"(\d+-\d+)\s+(\w{4}:\w{4})\s+(.+?)\s{2,}(\S+.*)$", line.strip())
            if m:
                devices.append({
                    "busid": m.group(1),
                    "vid_pid": m.group(2),
                    "device": m.group(3).strip(),
                    "state": m.group(4).strip(),
                })
        print(json.dumps(devices, indent=2))
    else:
        print(out)
    return 0


def _get_usb_state(busid):
    """Get current state of a USB device: 'attached', 'shared', 'not_shared', or None.

    Checks both usbipd list (Windows side) and /dev/ttyUSB* (WSL side).
    Some usbipd versions show 'Shared' even when attached — so we also
    check if the device exists in WSL.
    """
    import glob as globmod

    out, err, rc = _run_ps("usbipd list")
    if rc != 0:
        return None, None

    usbipd_state = None
    usbipd_line = None
    for line in out.split("\n"):
        if busid in line:
            usbipd_line = line.strip()
            if "Attached" in usbipd_line:
                usbipd_state = "attached"
            elif "Shared" in usbipd_line:
                usbipd_state = "shared"
            else:
                usbipd_state = "not_shared"
            break

    if usbipd_state is None:
        return None, None

    # Cross-check: if usbipd says shared but WSL has the ttyUSB device,
    # it's actually attached (some usbipd versions don't show "Attached")
    wsl_devices = globmod.glob("/dev/ttyUSB*")
    if wsl_devices and usbipd_state == "shared":
        return "attached", usbipd_line

    # If usbipd says attached but no WSL device, it's stale
    if not wsl_devices and usbipd_state == "attached":
        return "shared", usbipd_line

    return usbipd_state, usbipd_line


def _find_ttyusb():
    """Find the latest ttyUSB device from dmesg."""
    dmesg_result = subprocess.run(["dmesg"], capture_output=True, text=True)
    tty_match = re.findall(r"now attached to (ttyUSB\d+)", dmesg_result.stdout)
    return f"/dev/{tty_match[-1]}" if tty_match else None


def cmd_usb_attach(args):
    """Bind and attach a USB device to WSL (via win-admin). Checks state first."""
    busid = args.busid
    force = getattr(args, 'force', False)

    state, line = _get_usb_state(busid)

    if state is None:
        print(f"Device {busid} not found in usbipd list")
        return 1

    if state == "attached" and not force:
        dev = _find_ttyusb()
        if dev:
            print(f"Already attached to WSL as {dev}")
        else:
            print(f"Already attached to WSL (device: {line})")
        return 0

    if state == "attached" and force:
        print(f"Force: detaching first...")
        admin_cmd(f"usbipd detach --busid {busid}", wait=3)
        time.sleep(2)
        state = "shared"

    log(f"Attaching USB {busid} to WSL (state: {state})")

    if state == "not_shared":
        # Need bind + attach
        output, ok = admin_cmd([
            f"usbipd bind --busid {busid} --force",
            f"usbipd attach --wsl --busid {busid}",
        ], wait=4)
    else:
        # Already shared/bound — just attach
        output, ok = admin_cmd(
            f"usbipd attach --wsl --busid {busid}",
            wait=4,
        )

    if not ok:
        print(f"Failed: {output}")
        return 1

    # Wait for device to appear in WSL
    time.sleep(2)
    dev = _find_ttyusb()
    if dev:
        import glob as globmod
        if os.path.exists(dev):
            print(f"Attached: {dev}")
            log(f"Attached USB {busid} as {dev}")
            return 0

    # Device didn't appear — attach likely failed silently
    import glob as globmod
    wsl_devices = globmod.glob("/dev/ttyUSB*")
    if wsl_devices:
        print(f"Attached: {wsl_devices[0]}")
        log(f"Attached USB {busid} as {wsl_devices[0]}")
        return 0

    # Truly failed — likely Windows app is holding the COM port
    new_state, new_line = _get_usb_state(busid)
    if new_state == "shared":
        print(f"Failed: device bound but attach failed — a Windows app may be holding the COM port.")
        print(f"Close the app using COM3 in Windows, then retry.")
        log(f"Attach failed for USB {busid} — COM port held by Windows app")
        return 1
    elif new_state == "attached":
        print(f"Attached USB {busid} to WSL")
        log(f"Attached USB {busid}")
        return 0
    else:
        print(f"Failed: device state is {new_line}")
        return 1


def cmd_usb_detach(args):
    """Detach a USB device from WSL and unbind back to Windows (via win-admin). Checks state first."""
    busid = args.busid
    keep_bound = getattr(args, 'keep_bound', False)

    state, line = _get_usb_state(busid)

    if state is None:
        print(f"Device {busid} not found in usbipd list")
        return 1

    if state == "not_shared":
        print(f"Already released — {busid} is back on Windows")
        return 0

    log(f"Detaching USB {busid} from WSL (state: {state})")

    if keep_bound:
        if state == "attached":
            # Detach only, keep bound
            output, ok = admin_cmd(f"usbipd detach --busid {busid}", wait=3)
            if not ok:
                print(f"Failed: {output}")
                return 1
            print(f"Detached USB {busid} from WSL (still bound — use usb-attach to re-attach quickly)")
        elif state == "shared":
            print(f"Already detached — USB {busid} is bound (ready for quick re-attach)")
        log(f"Detached USB {busid} (kept bound)")
        return 0

    if state == "attached":
        # Detach + unbind
        output, ok = admin_cmd([
            f"usbipd detach --busid {busid}",
            "ping -n 3 127.0.0.1 >nul",
            f"usbipd unbind --busid {busid}",
        ], wait=8)
    elif state == "shared":
        # Already detached from WSL, just unbind
        output, ok = admin_cmd(f"usbipd unbind --busid {busid}", wait=3)
    else:
        print(f"Unexpected state: {line}")
        return 1

    if not ok:
        print(f"Failed: {output}")
        return 1

    print(f"Detached and unbound USB {busid} — COM port returned to Windows")
    log(f"Detached + unbound USB {busid}")
    return 0


# ── Serial port management ──────────────────────────────────


def _get_serial(port, baud=None):
    """Open a serial port with DTR/RTS held low.

    Important: pyserial's default open asserts DTR and RTS. On some FTDI-based
    cables (e.g. the Ingenic IPC dev cable) those lines are wired to board
    signals that, when asserted, corrupt the camera's UART input — sending `\\r`
    arrives at U-Boot as 5–10 bytes of framing-error garbage and every command
    is rejected as `Unknown command '...'`. Observed on an Ingenic IPC dev cable
    while recovering from u-boot. Explicitly dropping DTR/RTS produces a clean
    line and is harmless for cables that don't wire them.
    """
    import serial as pyserial
    s = pyserial.Serial()
    s.port      = port
    s.baudrate  = baud or DEFAULT_BAUD
    s.timeout   = DEFAULT_TIMEOUT
    s.dsrdtr    = False  # disable DTR-driven flow control
    s.rtscts    = False  # disable RTS-driven flow control
    s.open()
    s.dtr = False
    s.rts = False
    return s


def cmd_serial_list(args):
    """List available serial ports in WSL and Windows."""
    print("=== WSL serial devices ===")
    # ttyUSB (USB-serial adapters attached via usbipd)
    import glob
    usb_ports = sorted(glob.glob("/dev/ttyUSB*"))
    tty_ports = sorted(glob.glob("/dev/ttyS*"))

    if usb_ports:
        for p in usb_ports:
            print(f"  {p}  (USB-serial, attached to WSL)")
    else:
        print("  No /dev/ttyUSB* devices")

    # Check which ttyS ports actually work
    working = []
    for p in tty_ports[:16]:  # Only check first 16
        try:
            s = _get_serial(p, baud=9600)
            s.close()
            working.append(p)
        except Exception:
            pass
    if working:
        for p in working:
            print(f"  {p}  (native serial)")

    print("\n=== Windows COM ports ===")
    out, _, _ = _run_ps(
        "Get-CimInstance Win32_PnPEntity | "
        "Where-Object { $_.Name -match 'COM\\d+' } | "
        "Select-Object Name, Status | Format-Table -AutoSize"
    )
    if out:
        print(f"  {out}")
    else:
        out2, _, _ = _run_ps("mode COM1 2>$null; mode COM2 2>$null; mode COM3 2>$null; mode COM4 2>$null; mode COM5 2>$null")
        print(f"  {out2}" if out2 else "  None found via mode")

    print("\n=== usbipd devices ===")
    out, _, _ = _run_ps("usbipd list")
    if out:
        for line in out.split("\n"):
            if "Serial" in line or "UART" in line or "FTDI" in line or "CH340" in line or "CP210" in line or "Prolific" in line:
                print(f"  {line.strip()}")
    return 0


def cmd_serial_send(args):
    """Send a command to a serial device and read the response."""
    ser = _get_serial(args.port, args.baud)
    time.sleep(0.5)

    # Flush pending data
    while ser.in_waiting:
        ser.read(ser.in_waiting)
        time.sleep(0.1)

    # Send command
    cmd = args.command
    ser.write(f"{cmd}\r\n".encode())
    time.sleep(args.wait)

    # Read response
    data = b""
    while ser.in_waiting:
        data += ser.read(ser.in_waiting)
        time.sleep(0.2)

    text = data.decode("utf-8", errors="replace")
    if args.json:
        print(json.dumps({"port": args.port, "command": cmd, "response": text}))
    else:
        print(text)

    ser.close()
    return 0


def cmd_serial_read(args):
    """Read data from serial port for a given duration."""
    ser = _get_serial(args.port, args.baud)
    duration = args.duration
    end_time = time.time() + duration

    print(f"Reading from {args.port} for {duration}s (Ctrl+C to stop)...")
    try:
        while time.time() < end_time:
            if ser.in_waiting:
                data = ser.read(ser.in_waiting)
                sys.stdout.write(data.decode("utf-8", errors="replace"))
                sys.stdout.flush()
            else:
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    ser.close()
    return 0


def cmd_serial_login(args):
    """Login to a serial console (e.g., embedded Linux device)."""
    ser = _get_serial(args.port, args.baud)
    time.sleep(0.5)

    # Flush
    while ser.in_waiting:
        ser.read(ser.in_waiting)
        time.sleep(0.1)

    # Send enter to trigger login prompt
    for _ in range(3):
        ser.write(b"\r\n")
        time.sleep(0.3)

    # Send username
    ser.write(f"{args.user}\r\n".encode())
    time.sleep(1)

    # Check if password is needed
    data = ser.read(ser.in_waiting or 2000)
    text = data.decode("utf-8", errors="replace")

    if "Password:" in text or "password:" in text:
        pw = args.password or ""
        ser.write(f"{pw}\r\n".encode())
        time.sleep(1)
        data = ser.read(ser.in_waiting or 2000)
        text += data.decode("utf-8", errors="replace")

    if "#" in text or "$" in text:
        print(f"Logged in as {args.user}")
        log(f"Serial login to {args.port} as {args.user}")
    else:
        print(f"Login attempt sent. Response:")
        # Filter out noise (debug logs)
        for line in text.split("\n"):
            line = line.strip()
            if "login:" in line.lower() or "#" in line or "$" in line or "password" in line.lower():
                print(f"  {line}")

    ser.close()
    return 0


def cmd_serial_shell(args):
    """Login and execute command(s) on serial console."""
    ser = _get_serial(args.port, args.baud)
    time.sleep(0.5)

    # Flush
    while ser.in_waiting:
        ser.read(ser.in_waiting)
        time.sleep(0.1)

    # Login
    for _ in range(3):
        ser.write(b"\r\n")
        time.sleep(0.2)
    ser.write(f"{args.user}\r\n".encode())
    time.sleep(0.5)

    # Handle password if needed
    data = ser.read(ser.in_waiting or 2000)
    text = data.decode("utf-8", errors="replace")
    if "Password:" in text or "password:" in text:
        ser.write(f"{args.password or ''}\r\n".encode())
        time.sleep(0.5)

    # Flush login output
    while ser.in_waiting:
        ser.read(ser.in_waiting)
        time.sleep(0.1)

    # Execute each command
    results = []
    commands = args.cmd if isinstance(args.cmd, list) else [args.cmd]
    for cmd in commands:
        # Flush before command
        while ser.in_waiting:
            ser.read(ser.in_waiting)
            time.sleep(0.1)

        ser.write(f"{cmd}\r\n".encode())
        time.sleep(args.wait)

        data = b""
        while ser.in_waiting:
            data += ser.read(ser.in_waiting)
            time.sleep(0.2)

        output = data.decode("utf-8", errors="replace")
        results.append({"command": cmd, "output": output})

        if not args.json:
            print(f">>> {cmd}")
            print(output)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))

    ser.close()
    return 0


def cmd_camera_stop(args):
    """Stop camera app: kill ipc_app + disable watchdog (Ingenic devices)."""
    ser = _get_serial(args.port, args.baud)
    time.sleep(0.5)

    # Flush
    while ser.in_waiting:
        ser.read(ser.in_waiting)
        time.sleep(0.1)

    # Login
    for _ in range(3):
        ser.write(b"\r\n")
        time.sleep(0.2)
    ser.write(f"{args.user}\r\n".encode())
    time.sleep(0.5)

    data = ser.read(ser.in_waiting or 2000)
    text = data.decode("utf-8", errors="replace")
    if "Password:" in text or "password:" in text:
        ser.write(f"{args.password or ''}\r\n".encode())
        time.sleep(0.5)

    # Flush
    while ser.in_waiting:
        ser.read(ser.in_waiting)
        time.sleep(0.1)

    # Kill ipc_app then busy-loop to grab watchdog fd the instant it's released
    print("Killing ipc_app and stopping watchdog...")
    ser.write(b"killall -9 ipc_app; while ! echo -V > /dev/watchdog0 2>/dev/null; do true; done; echo WATCHDOG_STOPPED\r\n")
    time.sleep(10)

    data = b""
    while ser.in_waiting:
        data += ser.read(ser.in_waiting)
        time.sleep(0.3)
    text = data.decode("utf-8", errors="replace")

    if "WATCHDOG_STOPPED" in text:
        print("ipc_app killed and watchdog stopped. Device is ready.")
        log(f"Camera stopped on {args.port}")
    else:
        print("Result unclear. Output:")
        print(text[-500:])

    # Verify quiet
    time.sleep(3)
    remaining = ser.read(2000)
    if not remaining:
        print("Device is quiet.")
    else:
        rtext = remaining.decode("utf-8", errors="replace")
        if "login:" in rtext or "AICWFDBG" in rtext:
            print("WARNING: Device may have rebooted")
        elif "ivs_PD" in rtext or "ipc_app" in rtext:
            print("WARNING: ipc_app may still be running")

    ser.close()
    return 0


def cmd_camera_start(args):
    """Restart camera app (run ipc_app)."""
    ser = _get_serial(args.port, args.baud)
    time.sleep(0.5)

    while ser.in_waiting:
        ser.read(ser.in_waiting)
        time.sleep(0.1)

    # Just send the start command — assume already logged in
    ser.write(b"/mnt/mtd/ipc_app &\r\n")
    time.sleep(3)

    data = ser.read(ser.in_waiting or 2000)
    text = data.decode("utf-8", errors="replace")
    print("ipc_app started.")
    print(text[-300:])

    ser.close()
    return 0


def cmd_camera_reboot(args):
    """Reboot the camera device."""
    ser = _get_serial(args.port, args.baud)
    time.sleep(0.5)

    while ser.in_waiting:
        ser.read(ser.in_waiting)
        time.sleep(0.1)

    ser.write(b"reboot\r\n")
    time.sleep(2)
    print("Reboot command sent.")

    ser.close()
    return 0


# ── CLI ──────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="USB passthrough + serial port control")
    sub = parser.add_subparsers(dest="command")

    # USB commands
    p = sub.add_parser("usb-list", help="List USB devices (usbipd)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("usb-attach", help="Attach USB device to WSL")
    p.add_argument("busid", help="Bus ID (e.g., 6-3)")
    p.add_argument("--force", action="store_true", help="Force re-attach even if already attached")

    p = sub.add_parser("usb-detach", help="Detach USB device back to Windows")
    p.add_argument("busid", help="Bus ID (e.g., 6-3)")
    p.add_argument("--keep-bound", action="store_true", dest="keep_bound",
                   help="Only detach, keep bound for quick re-attach")

    # Serial commands
    p = sub.add_parser("serial-list", help="List serial ports (WSL + Windows)")

    p = sub.add_parser("serial-send", help="Send command to serial port")
    p.add_argument("port", help="Serial port (e.g., /dev/ttyUSB0)")
    p.add_argument("command", help="Command to send")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--wait", type=float, default=2, help="Seconds to wait for response")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("serial-read", help="Read from serial port for N seconds")
    p.add_argument("port", help="Serial port")
    p.add_argument("--duration", type=float, default=5, help="Read duration in seconds")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    p = sub.add_parser("serial-login", help="Login to serial console")
    p.add_argument("port", help="Serial port")
    p.add_argument("--user", default="root", help="Username (default: root)")
    p.add_argument("--password", default="", help="Password (default: empty)")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    p = sub.add_parser("serial-shell", help="Login and execute command(s)")
    p.add_argument("port", help="Serial port")
    p.add_argument("--user", default="root", help="Username (default: root)")
    p.add_argument("--password", default="", help="Password (default: empty)")
    p.add_argument("--cmd", nargs="+", required=True, help="Command(s) to execute")
    p.add_argument("--wait", type=float, default=3, help="Wait per command (seconds)")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    p.add_argument("--json", action="store_true")

    # Camera-specific commands
    p = sub.add_parser("camera-stop", help="Kill ipc_app + stop watchdog (Ingenic camera)")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--user", default="root")
    p.add_argument("--password", default="")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    p = sub.add_parser("camera-start", help="Start ipc_app on camera")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    p = sub.add_parser("camera-reboot", help="Reboot camera device")
    p.add_argument("--port", default="/dev/ttyUSB0")
    p.add_argument("--baud", type=int, default=DEFAULT_BAUD)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "usb-list": cmd_usb_list,
        "usb-attach": cmd_usb_attach,
        "usb-detach": cmd_usb_detach,
        "serial-list": cmd_serial_list,
        "serial-send": cmd_serial_send,
        "serial-read": cmd_serial_read,
        "serial-login": cmd_serial_login,
        "serial-shell": cmd_serial_shell,
        "camera-stop": cmd_camera_stop,
        "camera-start": cmd_camera_start,
        "camera-reboot": cmd_camera_reboot,
    }
    return commands[args.command](args) or 0


if __name__ == "__main__":
    sys.exit(main())
