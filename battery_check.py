"""Check an Android phone's battery over USB, using adb.

    python battery_check.py                  # the one connected phone
    python battery_check.py -s SERIAL        # pick a phone when several are connected
    python battery_check.py --json           # machine-readable output
    python battery_check.py --file dump.txt  # read a saved `adb shell dumpsys battery`

Everything is read-only: the script asks Android for its battery report and
prints it. Nothing is changed on the phone and nothing leaves this computer.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

# android.os.BatteryManager constants, as `dumpsys battery` prints them.
STATUS = {1: "unknown", 2: "charging", 3: "discharging", 4: "not charging", 5: "full"}
HEALTH = {1: "unknown", 2: "good", 3: "overheat", 4: "dead", 5: "over voltage",
          6: "unspecified failure", 7: "cold"}

# Kernel files some phones expose without root. Many do not; each is optional.
SYSFS = "/sys/class/power_supply/battery/"

Runner = Callable[[list], "subprocess.CompletedProcess[str]"]


class CheckError(Exception):
    """A problem the user can fix; the message says how."""


@dataclass
class Battery:
    percent: int | None = None
    status: str = "unknown"
    health: str = "unknown"
    temperature_c: float | None = None
    voltage_v: float | None = None
    technology: str | None = None
    power_source: str | None = None
    present: bool | None = None
    cycle_count: int | None = None
    full_mah: int | None = None
    design_mah: int | None = None
    readings_frozen: bool = False
    kernel_files_checked: bool = False
    notes: list = field(default_factory=list)

    @property
    def capacity_percent(self) -> int | None:
        if self.full_mah and self.design_mah:
            return round(self.full_mah * 100 / self.design_mah)
        return None


def _int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _volts(raw: int) -> float:
    """Android prints millivolts; a few vendors print microvolts or plain volts."""
    if raw > 100_000:
        return round(raw / 1_000_000, 3)
    if raw > 100:
        return round(raw / 1000, 3)
    return float(raw)


def parse_dumpsys(text: str) -> Battery:
    """Read the output of `adb shell dumpsys battery`."""
    fields = {}
    frozen = False
    for line in text.splitlines():
        if "UPDATES STOPPED" in line:
            # `dumpsys battery set ...` was used; the numbers are whatever was typed.
            frozen = True
        key, sep, value = line.strip().partition(":")
        if sep:
            fields[key.strip().lower()] = value.strip()
    if "level" not in fields:
        raise CheckError("That is not a battery report: no 'level' line in it. "
                         "It should be the output of `adb shell dumpsys battery`.")

    b = Battery(readings_frozen=frozen)
    level, scale = _int(fields.get("level")), _int(fields.get("scale")) or 100
    if level is not None:
        b.percent = round(level * 100 / scale)
    b.status = STATUS.get(_int(fields.get("status")), "unknown")
    b.health = HEALTH.get(_int(fields.get("health")), "unknown")
    tenths = _int(fields.get("temperature"))
    if tenths is not None:
        b.temperature_c = tenths / 10
    raw_voltage = _int(fields.get("voltage"))
    if raw_voltage:
        b.voltage_v = _volts(raw_voltage)
    b.technology = fields.get("technology") or None
    if "present" in fields:
        b.present = fields["present"] == "true"
    sources = [name for name, key in (("AC charger", "ac powered"), ("USB", "usb powered"),
                                      ("wireless", "wireless powered"), ("dock", "dock powered"))
               if fields.get(key) == "true"]
    b.power_source = ", ".join(sources) or None
    return b


def add_sysfs(b: Battery, read: Callable[[str], str | None]) -> None:
    """Fill cycle count and capacity from the kernel files, where the phone allows it."""
    b.kernel_files_checked = True
    b.cycle_count = _int(read("cycle_count"))
    full, design = _int(read("charge_full")), _int(read("charge_full_design"))
    # The kernel reports microamp-hours.
    if full and design and full > 0 and design > 0:
        b.full_mah, b.design_mah = round(full / 1000), round(design / 1000)


def add_notes(b: Battery) -> None:
    notes = b.notes
    if b.readings_frozen:
        notes.append("These readings were set by hand with `dumpsys battery set`, so they are "
                     "not real. Run `adb shell dumpsys battery reset` and check again.")
    if b.present is False:
        notes.append("Android reports no battery present.")
    if b.health not in ("good", "unknown"):
        notes.append({
            "overheat": "Android marks the battery as overheating. Unplug it, close heavy apps "
                        "and let it cool before charging again.",
            "cold": "Android marks the battery as too cold. Warm the phone to room temperature "
                    "before charging.",
            "dead": "Android marks the battery as dead. It needs replacing.",
            "over voltage": "Android reports over-voltage. Try another charger and cable; if it "
                            "persists, have the phone checked.",
        }.get(b.health, f"Android reports a battery failure ({b.health}). Have it checked."))
    t = b.temperature_c
    if t is not None and b.health != "overheat":
        if t >= 45:
            notes.append(f"{t:.1f} °C is hot. Heat is what ages a lithium cell fastest: "
                         "unplug, take it out of the case and let it cool.")
        elif t >= 40:
            notes.append(f"{t:.1f} °C is warm. Fine for a short while; avoid charging and "
                         "gaming at the same time.")
        elif t <= 0:
            notes.append(f"{t:.1f} °C is below freezing. Do not charge until it warms up.")
    cap = b.capacity_percent
    if cap is not None and cap < 80:
        notes.append(f"Capacity is {cap}% of the original. Most makers treat under 80% as worn; "
                     "a new battery would bring the runtime back.")
    if b.percent is not None and b.percent <= 20 and b.status in ("discharging", "not charging"):
        notes.append(f"Charge is low ({b.percent}%).")
    if b.status == "not charging" and b.power_source:
        notes.append(f"Plugged in ({b.power_source}) but not charging. Many phones pause on "
                     "purpose at a set limit or when warm; if not, try another cable and charger.")


def find_adb(explicit: str | None = None) -> str:
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise CheckError(f"No adb at {explicit}.")
    found = shutil.which("adb")
    if found:
        return found
    exe = "adb.exe" if os.name == "nt" else "adb"
    homes = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk"),
             os.path.expanduser("~/Library/Android/sdk"), os.path.expanduser("~/Android/Sdk")]
    for home in filter(None, homes):
        candidate = Path(home) / "platform-tools" / exe
        if candidate.is_file():
            return str(candidate)
    raise CheckError("adb was not found. Install Android SDK Platform-Tools "
                     "(https://developer.android.com/tools/releases/platform-tools) and put "
                     "its folder on PATH, or pass --adb PATH.")


def _not_ready(state: str) -> CheckError:
    """Advice for a phone that `adb devices` lists but cannot use yet."""
    if state == "unauthorized":
        return CheckError("The phone has not allowed this computer yet. Unlock it and accept "
                          "the 'Allow USB debugging?' prompt, then run this again.")
    if state.startswith("no permissions"):
        return CheckError("adb has no permission to open the phone (" + state + "). On Linux, "
                          "add your user to the plugdev group and install udev rules for "
                          "Android phones, then log in again and reconnect the phone.")
    return CheckError(f"The phone is connected but {state}. Reconnect the cable and try again.")


def pick_device(devices_output: str, wanted: str | None = None) -> str:
    """Choose the phone from `adb devices` output."""
    ready, waiting = [], []
    for line in devices_output.splitlines():
        line = line.strip()
        if not line or line.startswith(("*", "List of devices")):
            continue  # adb's own messages, e.g. "* daemon started successfully"
        parts = line.split(None, 1)
        if len(parts) == 2:
            serial, state = parts[0], parts[1].strip()
            (ready if state == "device" else waiting).append((serial, state))
    if wanted:
        if any(serial == wanted for serial, _ in ready):
            return wanted
        for serial, state in waiting:
            if serial == wanted:
                raise _not_ready(state)
        listed = ", ".join(s for s, _ in ready + waiting) or "nothing"
        raise CheckError(f"No ready phone with serial {wanted}. `adb devices` lists: {listed}")
    if len(ready) == 1:
        return ready[0][0]
    if len(ready) > 1:
        listed = ", ".join(s for s, _ in ready)
        raise CheckError(f"Several phones are connected ({listed}). Choose one with -s SERIAL.")
    for _, state in waiting:
        if state == "unauthorized":
            raise _not_ready(state)
    if waiting:
        raise _not_ready(waiting[0][1])
    raise CheckError("No phone found. On the phone: Settings > About phone > tap 'Build "
                     "number' seven times, then Developer options > USB debugging on. Connect "
                     "it with a data cable (some cables only charge).")


def _default_run(cmd: list) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20)


def read_phone(adb: str, serial: str | None, run: Runner = _default_run) -> Battery:
    devices = run([adb, "devices"])
    if devices.returncode != 0:
        reason = devices.stderr.strip() or "no reason given"
        raise CheckError(f"adb could not list devices: {reason}")
    serial = pick_device(devices.stdout, serial)
    report = run([adb, "-s", serial, "shell", "dumpsys", "battery"])
    if report.returncode != 0:
        reason = report.stderr.strip() or "no reason given"
        raise CheckError(f"The phone did not return a battery report: {reason}")
    b = parse_dumpsys(report.stdout)

    def read(name: str) -> str | None:
        out = run([adb, "-s", serial, "shell", "cat", SYSFS + name])
        return out.stdout.strip() if out.returncode == 0 else None

    add_sysfs(b, read)
    return b


def render(b: Battery) -> str:
    rows = []
    charge = f"{b.percent}%" if b.percent is not None else "?"
    status = b.status
    if b.power_source and b.status != "discharging":
        status += f" ({b.power_source})"
    rows.append(("Charge", f"{charge}  {status}"))
    rows.append(("Health", b.health))
    if b.temperature_c is not None:
        rows.append(("Temperature", f"{b.temperature_c:.1f} °C"))
    if b.voltage_v is not None:
        rows.append(("Voltage", f"{b.voltage_v:.3f} V"))
    if b.technology:
        rows.append(("Type", b.technology))
    if b.cycle_count is not None:
        rows.append(("Charge cycles", str(b.cycle_count)))
    if b.capacity_percent is not None:
        rows.append(("Capacity", f"{b.capacity_percent}% of original "
                                 f"({b.full_mah:,} of {b.design_mah:,} mAh)"))
    width = max(len(k) for k, _ in rows)
    lines = [f"{k.ljust(width)}  {v}" for k, v in rows]
    if b.kernel_files_checked and b.cycle_count is None and b.capacity_percent is None:
        lines.append("")
        lines.append("This phone does not share its cycle count or worn capacity without root.")
    lines.append("")
    if b.notes:
        lines.extend("- " + n for n in b.notes)
    else:
        lines.append("Nothing to worry about.")
    return "\n".join(lines)


def read_saved_report(path: Path) -> str:
    """Read a saved report. Windows PowerShell 5.1 `>` writes UTF-16 with a BOM."""
    raw = path.read_bytes()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def main(argv: list | None = None, run: Runner = _default_run) -> int:
    ap = argparse.ArgumentParser(description="Check an Android phone's battery over USB (adb).")
    ap.add_argument("-s", "--serial", help="which phone, when several are connected")
    ap.add_argument("--adb", help="path to adb, when it is not on PATH")
    ap.add_argument("--file", type=Path, help="read a saved `adb shell dumpsys battery` instead")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a report")
    args = ap.parse_args(argv)
    try:
        if args.file:
            b = parse_dumpsys(read_saved_report(args.file))
        else:
            b = read_phone(find_adb(args.adb), args.serial, run)
        add_notes(b)
    except CheckError as e:
        print(e, file=sys.stderr)
        return 1
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"Could not read the battery: {e}", file=sys.stderr)
        return 1
    if args.json:
        data = asdict(b)
        data["capacity_percent"] = b.capacity_percent
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(render(b))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
