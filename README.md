# port-control-wsl

> **TL;DR** — from inside **WSL2 (Linux)**: pass USB devices through to WSL
> (`usbipd`), and drive **serial consoles** on embedded devices (cameras, SBCs)
> — list ports, send commands, log in, run shells, all over `/dev/ttyUSB*`.

> 🤖 **AI agents:** see [AGENTS.md](AGENTS.md) for a machine-readable invocation
> guide and [llms.txt](llms.txt) for a quick index.

Two jobs in one tool, aimed at embedded development from WSL2:

1. **USB passthrough** — attach a USB-serial adapter from Windows into WSL with
   `usbipd`, and detach it back, with state-aware checks (won't double-bind, can
   keep-bound for fast re-attach).
2. **Serial console** — open the resulting `/dev/ttyUSB*` (or native `/dev/ttyS*`),
   send one-off commands, stream output, log into an embedded Linux console, or
   run a sequence of commands non-interactively. Includes convenience shortcuts
   for Ingenic-based IP cameras (stop/start the app, reboot).

```bash
python3 port_control.py usb-attach 6-3
python3 port_control.py serial-shell /dev/ttyUSB0 --user root --cmd "uname -a" "df -h"
python3 port_control.py usb-detach 6-3
```

## Requirements

- Windows 10/11 with WSL2
- Python 3.7+ inside WSL
- **For serial/camera commands:** `pip install -r requirements.txt` (pyserial)
- **For USB attach/detach:** [usbipd-win](https://github.com/dorssel/usbipd-win)
  on Windows, plus the **[wsl-win-admin-bridge](https://github.com/marcusice/wsl-win-admin-bridge)**
  helper (the first `usbipd bind` needs admin rights, obtained through it).
  `usb-list` and all serial/camera commands work **without** win-admin.

Point at the win-admin helper via the `WIN_ADMIN` env var, or place its
`win_admin.py` next to this script / in a sibling `wsl-win-admin-bridge/` checkout.

## Usage

### USB passthrough (usbipd)

```bash
# List all USB devices and their state (--json for structured output)
python3 port_control.py usb-list

# Attach a device to WSL (binds first if needed; --force to re-attach)
python3 port_control.py usb-attach 6-3
python3 port_control.py usb-attach 6-3 --force

# Detach back to Windows (--keep-bound leaves it bound for fast re-attach)
python3 port_control.py usb-detach 6-3
python3 port_control.py usb-detach 6-3 --keep-bound
```

`usb-attach` checks state first — skips bind if already bound, skips everything if
already attached. The bus ID (e.g. `6-3`) comes from `usb-list`.

### Serial console

```bash
# List serial ports (WSL /dev/ttyUSB*, /dev/ttyS*, and Windows COM ports)
python3 port_control.py serial-list

# Send one command and read the reply
python3 port_control.py serial-send /dev/ttyUSB0 "ls -la" --wait 3

# Stream raw output for N seconds (live debug log)
python3 port_control.py serial-read /dev/ttyUSB0 --duration 5

# Log into a serial console
python3 port_control.py serial-login /dev/ttyUSB0 --user root

# Log in and run one or more commands non-interactively
python3 port_control.py serial-shell /dev/ttyUSB0 --user root --cmd "uname -a" "df -h" "ps"
```

Serial options: `--baud` (default 115200), `--user`/`--password` (login),
`--wait` (per-command read window), `--json`.

### Camera shortcuts (Ingenic IPC)

Convenience wrappers for Ingenic-based IP cameras. Default to
`--port /dev/ttyUSB0 --user root --baud 115200`.

```bash
python3 port_control.py camera-stop     # kill ipc_app + grab watchdog fd (device stays up, shell ready)
python3 port_control.py camera-start    # relaunch /mnt/mtd/ipc_app
python3 port_control.py camera-reboot   # reboot the device
```

`camera-stop` kills `ipc_app` and immediately busy-loops to claim the watchdog
file descriptor the instant it's released — on these cameras, killing the app
without holding the watchdog causes an immediate hardware reboot.

## Configuration

| Variable           | Default                           | Purpose                              |
|--------------------|-----------------------------------|--------------------------------------|
| `WIN_ADMIN`        | auto-located `win_admin.py`       | Path to the win-admin helper (USB)   |
| `PORT_CONTROL_LOG` | `port_control.log` next to script | Log file path                        |

## Notes

- After attach, the device typically appears as `/dev/ttyUSB0` (e.g. an FTDI
  FT232R USB-serial adapter).
- **DTR/RTS held low:** `_get_serial()` explicitly clears DTR and RTS after
  opening the port. Some FTDI dev cables wire those lines to board signals that
  corrupt the device's UART input when asserted — without this, a bare `\r` can
  reach U-Boot as several bytes of framing-error garbage. Clearing them is
  harmless for cables that don't wire those lines.
- The serial helpers are tuned for BusyBox-style embedded Linux consoles.

## Security notes

- USB attach/detach runs an **elevated** Windows command (via the win-admin
  helper). Only use this on a machine you control.
- `serial-shell` / `serial-login` send credentials over the serial line; the
  defaults target a passwordless `root` console typical of dev boards — set
  `--user`/`--password` for anything else.

## License

MIT — see [LICENSE](LICENSE).
