import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import battery_check as bc  # noqa: E402

SAMPLES = ROOT / "samples"


def sample(name):
    return (SAMPLES / name).read_text(encoding="utf-8")


# --- reading the report -------------------------------------------------------

def test_a_charging_phone_is_read_in_human_units():
    b = bc.parse_dumpsys(sample("charging.txt"))
    assert b.percent == 78
    assert b.status == "charging"
    assert b.health == "good"
    assert b.temperature_c == 31.2
    assert b.voltage_v == 4.213
    assert b.technology == "Li-ion"
    assert b.power_source == "USB"
    assert b.present is True
    assert b.readings_frozen is False


def test_level_is_scaled_when_the_scale_is_not_100():
    b = bc.parse_dumpsys("  level: 30\n  scale: 60\n")
    assert b.percent == 50


@pytest.mark.parametrize("raw, volts", [("4213", 4.213), ("4213000", 4.213), ("4", 4.0)])
def test_voltage_in_millivolts_microvolts_or_volts(raw, volts):
    assert bc.parse_dumpsys(f"  level: 50\n  voltage: {raw}\n").voltage_v == volts


def test_unknown_codes_read_as_unknown():
    b = bc.parse_dumpsys("  level: 50\n  status: 9\n  health: x\n")
    assert (b.status, b.health) == ("unknown", "unknown")


def test_text_that_is_not_a_battery_report_is_refused():
    with pytest.raises(bc.CheckError, match="not a battery report"):
        bc.parse_dumpsys("Hello\nthis is something else\n")


def test_values_set_by_hand_are_flagged():
    b = bc.parse_dumpsys(sample("frozen.txt"))
    bc.add_notes(b)
    assert b.readings_frozen
    assert any("dumpsys battery reset" in n for n in b.notes)


# --- notes --------------------------------------------------------------------

def test_a_healthy_phone_gets_no_warnings():
    b = bc.parse_dumpsys(sample("charging.txt"))
    bc.add_notes(b)
    assert b.notes == []
    assert "Nothing to worry about." in bc.render(b)


def test_overheat_is_reported_once_not_twice():
    b = bc.parse_dumpsys(sample("hot_and_low.txt"))
    bc.add_notes(b)
    assert b.health == "overheat"
    assert sum("overheat" in n or "is hot" in n for n in b.notes) == 1
    assert any("Charge is low (15%)" in n for n in b.notes)


@pytest.mark.parametrize("tenths, words",
                         [(410, "is warm"), (455, "is hot"), (-20, "below freezing")])
def test_temperature_bands(tenths, words):
    b = bc.parse_dumpsys(f"  level: 60\n  health: 2\n  temperature: {tenths}\n")
    bc.add_notes(b)
    assert any(words in n for n in b.notes)


def test_plugged_in_but_not_charging_is_explained():
    b = bc.parse_dumpsys("  level: 80\n  status: 4\n  AC powered: true\n")
    bc.add_notes(b)
    assert any("not charging" in n and "AC charger" in n for n in b.notes)


# --- cycle count and worn capacity -------------------------------------------

def test_capacity_and_cycles_from_the_kernel_files():
    b = bc.parse_dumpsys(sample("charging.txt"))
    files = {"cycle_count": "412", "charge_full": "3900000", "charge_full_design": "5000000"}
    bc.add_sysfs(b, files.get)
    bc.add_notes(b)
    assert (b.cycle_count, b.full_mah, b.design_mah, b.capacity_percent) == (412, 3900, 5000, 78)
    assert any("78% of the original" in n for n in b.notes)
    assert "Charge cycles  412" in bc.render(b)


def test_a_phone_that_hides_the_kernel_files_says_so():
    b = bc.parse_dumpsys(sample("charging.txt"))
    bc.add_sysfs(b, lambda name: None)
    assert b.cycle_count is None and b.capacity_percent is None
    assert "does not share its cycle count" in bc.render(b)


# --- choosing the phone -------------------------------------------------------

HEADER = "List of devices attached\n"


def test_the_one_ready_phone_is_picked():
    assert bc.pick_device(HEADER + "R58N123\tdevice\n\n") == "R58N123"


@pytest.mark.parametrize("listing, words", [
    ("", "No phone found"),
    ("R58N123\tunauthorized\n", "Allow USB debugging"),
    ("R58N123\toffline\n", "offline"),
    ("A1\tdevice\nB2\tdevice\n", "Several phones"),
])
def test_device_problems_say_what_to_do(listing, words):
    with pytest.raises(bc.CheckError, match=words):
        bc.pick_device(HEADER + listing)


def test_a_named_serial_must_be_ready():
    assert bc.pick_device(HEADER + "A1\tdevice\nB2\tdevice\n", "B2") == "B2"
    with pytest.raises(bc.CheckError, match="No ready phone with serial C3"):
        bc.pick_device(HEADER + "A1\tdevice\n", "C3")


def test_a_linux_permission_problem_points_at_udev_not_the_cable():
    listing = ("R58N123\tno permissions (user archi is not in the plugdev group); "
               "see [http://developer.android.com/tools/device.html]\n")
    with pytest.raises(bc.CheckError) as err:
        bc.pick_device(HEADER + listing)
    msg = str(err.value)
    assert "plugdev" in msg and "udev" in msg
    assert "but no." not in msg and "cable" not in msg


def test_adb_daemon_start_lines_are_not_phones():
    out = ("* daemon not running; starting now at tcp:5037\n"
           "* daemon started successfully\n"
           + HEADER + "R58N123\tdevice\n")
    assert bc.pick_device(out) == "R58N123"
    with pytest.raises(bc.CheckError, match="No phone found"):
        bc.pick_device("* daemon started successfully\n" + HEADER)


def test_a_named_serial_that_is_unauthorized_gets_the_allow_advice():
    with pytest.raises(bc.CheckError, match="Allow USB debugging"):
        bc.pick_device(HEADER + "A1\tdevice\nB2\tunauthorized\n", "B2")


# --- end to end, with adb replaced -------------------------------------------

def fake_adb(responses):
    calls = []

    def run(cmd):
        calls.append(cmd)
        key = " ".join(cmd[1:])
        for prefix, (code, out) in responses.items():
            if key.endswith(prefix):
                return subprocess.CompletedProcess(cmd, code, out, "" if code == 0 else "denied")
        return subprocess.CompletedProcess(cmd, 1, "", "unknown command")

    run.calls = calls
    return run


def test_reading_a_phone_through_adb(capsys, monkeypatch):
    run = fake_adb({
        "devices": (0, HEADER + "R58N123\tdevice\n"),
        "dumpsys battery": (0, sample("charging.txt")),
        "cycle_count": (0, "120\n"),
        "charge_full": (1, ""),  # permission denied on this phone
        "charge_full_design": (0, "5000000\n"),
    })
    monkeypatch.setattr(bc, "find_adb", lambda explicit=None: "adb")
    assert bc.main(["--json"], run=run) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["percent"] == 78 and data["cycle_count"] == 120
    assert data["capacity_percent"] is None  # needs both capacity files
    assert ["adb", "-s", "R58N123", "shell", "dumpsys", "battery"] in run.calls


def test_a_missing_phone_exits_1_with_advice(capsys, monkeypatch):
    monkeypatch.setattr(bc, "find_adb", lambda explicit=None: "adb")
    assert bc.main([], run=fake_adb({"devices": (0, HEADER)})) == 1
    assert "USB debugging" in capsys.readouterr().err


def test_missing_adb_exits_1_with_advice(capsys, monkeypatch):
    monkeypatch.setattr(bc.shutil, "which", lambda name: None)
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(bc.os.path, "expanduser", lambda p: "/nonexistent")
    assert bc.main([]) == 1
    assert "Platform-Tools" in capsys.readouterr().err


def test_a_saved_report_can_be_read_from_a_file(capsys):
    assert bc.main(["--file", str(SAMPLES / "hot_and_low.txt")]) == 0
    out = capsys.readouterr().out
    assert "15%  discharging" in out
    assert "overheat" in out
    # a saved report never holds the kernel files, so it must not blame the phone
    assert "does not share" not in out


@pytest.mark.parametrize("encoding, bom", [
    ("utf-16-le", b"\xff\xfe"),  # what `>` writes in Windows PowerShell 5.1
    ("utf-16-be", b"\xfe\xff"),
    ("utf-8", b"\xef\xbb\xbf"),  # PowerShell 7 `Out-File -Encoding utf8BOM`
])
def test_a_saved_report_in_another_encoding_is_read(tmp_path, capsys, encoding, bom):
    saved = tmp_path / "myphone.dump.txt"
    saved.write_bytes(bom + sample("charging.txt").encode(encoding))
    assert bc.main(["--file", str(saved), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["percent"] == 78 and data["status"] == "charging"
