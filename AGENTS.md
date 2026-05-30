# AGENTS.md

Machine-readable guide for AI coding agents working with this repository.

## What this is

`port-control-wsl` does two things from inside **WSL2 (Linux)**:
1. **USB passthrough** via `usbipd` — attach/detach a Windows USB device into WSL.
2. **Serial console control** — open `/dev/ttyUSB*` / `/dev/ttyS*`, send commands,
   stream output, log in, run shells. Plus Ingenic IP-camera shortcuts.

Aimed at embedded development (USB-serial adapters, cameras, SBCs) from WSL2.

## Entry point

```
port_control.py        # CLI
requirements.txt       # pyserial (serial/camera commands only)
```

Dependencies:
- `pyserial` — needed for `serial-*` and `camera-*`. Not needed for `usb-*`.
- `usbipd-win` (Windows) + the win-admin helper — needed for `usb-attach` /
  `usb-detach` (the elevated `usbipd bind`). `win_admin.py` from
  https://github.com/marcusice/wsl-win-admin-bridge, located via the `WIN_ADMIN`
  env var / next to this script / sibling checkout / $HOME. Loaded lazily, so the
  other commands run without it.

## How to invoke (CLI)

```bash
# USB (usbipd; attach/detach need win-admin)
python3 port_control.py usb-list [--json]
python3 port_control.py usb-attach <busid> [--force]            # e.g. busid 6-3
python3 port_control.py usb-detach <busid> [--keep-bound]

# Serial (need pyserial)
python3 port_control.py serial-list
python3 port_control.py serial-send <port> "<cmd>" [--baud N] [--wait S] [--json]
python3 port_control.py serial-read <port> [--duration S] [--baud N]
python3 port_control.py serial-login <port> [--user root] [--password ""] [--baud N]
python3 port_control.py serial-shell <port> --cmd "<c1>" ["<c2>" ...] [--user] [--password] [--wait] [--baud] [--json]

# Camera (Ingenic IPC; default --port /dev/ttyUSB0 --user root --baud 115200)
python3 port_control.py camera-stop [--port] [--user] [--password] [--baud]
python3 port_control.py camera-start [--port] [--baud]
python3 port_control.py camera-reboot [--port] [--baud]
```

Exact subcommand set: `usb-list, usb-attach, usb-detach, serial-list,
serial-send, serial-read, serial-login, serial-shell, camera-stop, camera-start,
camera-reboot`. Each `cmd_*` returns an int exit code (0 = success).

Note: for `serial-send`, the command is a positional arg; for `serial-shell`,
commands are passed via the required `--cmd` (nargs="+").

## Preconditions

- Running inside WSL2 with `/mnt/c` accessible.
- `usb-attach`/`usb-detach`: usbipd-win installed on Windows AND the win-admin
  helper present. If absent, `admin_cmd()` raises SystemExit with an install
  hint — surface it; the first bind needs Windows admin and can't be forced from
  WSL. `usb-list` only needs `usbipd.exe` reachable.
- `serial-*`/`camera-*`: `pip install pyserial`, and the target device attached
  (use `usb-attach` first, then `serial-list` to find the port).

## Configuration (environment variables)

| Variable           | Default                           | Purpose                       |
|--------------------|-----------------------------------|-------------------------------|
| `WIN_ADMIN`        | auto-located `win_admin.py`       | Path to the win-admin helper  |
| `PORT_CONTROL_LOG` | `port_control.log` next to script | Log file path                 |

## Safety notes for agents

- USB attach/detach runs elevated Windows commands via the win-admin helper.
- `serial-shell`/`camera-*` execute commands on a live device over serial —
  `camera-stop` and `camera-reboot` change device state immediately; confirm
  before running against hardware you don't want disrupted.
- `serial-login`/`serial-shell` transmit credentials over the serial line.

## Verifying a change

```bash
python3 -m py_compile port_control.py   # syntax check (no hardware needed)
python3 port_control.py usb-list        # needs usbipd on Windows
python3 port_control.py serial-list     # needs pyserial
```
