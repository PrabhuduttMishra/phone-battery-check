# Phone battery check

Plug an Android phone into your computer and see how its battery is really doing:
charge, health, temperature, voltage and — where the phone allows it — charge cycles
and how much capacity the battery has lost.

```
$ python battery_check.py
Charge         78%  charging (USB)
Health         good
Temperature    31.2 °C
Voltage        4.213 V
Type           Li-ion
Charge cycles  412
Capacity       78% of original (3,900 of 5,000 mAh)

- Capacity is 78% of the original. Most makers treat under 80% as worn; a new battery would bring the runtime back.
```

It only reads. Nothing on the phone is changed, nothing is installed on it, and nothing
leaves your computer.

## What you need

- **Python 3.10 or newer.** No packages — the script uses the standard library only.
- **adb**, from Google's [SDK Platform-Tools](https://developer.android.com/tools/releases/platform-tools).
  Unzip it and put the folder on your `PATH`, or point the script at it with `--adb`.
  If Android Studio is installed, its copy is found automatically.
- **USB debugging on the phone:** *Settings → About phone →* tap *Build number* seven
  times, then *Settings → Developer options → USB debugging*. Connect with a data cable
  (some cables only charge) and accept the *Allow USB debugging?* prompt on the phone.

## Use

```
python battery_check.py                   # the connected phone
python battery_check.py -s R58N1234567    # choose one when several are connected
python battery_check.py --json            # for scripts and spreadsheets
python battery_check.py --adb C:\tools\platform-tools\adb.exe
```

No phone at hand? Save a report once and read it later, or read one someone sent you:

```
adb shell dumpsys battery > myphone.dump.txt
python battery_check.py --file myphone.dump.txt
```

The exit code is `0` when a report was printed and `1` when it could not be (no adb, no
phone, phone not authorised); the message on screen says what to do.

## What the numbers mean

| Field | Where it comes from | Worth knowing |
| --- | --- | --- |
| Charge | `level` / `scale` | |
| Health | Android's own verdict: good, overheat, cold, dead, over voltage | Anything but *good* is called out below the report. |
| Temperature | `temperature`, in tenths of a degree | Above 40 °C is warm, above 45 °C is hot. Heat ages a battery faster than anything else. |
| Voltage | `voltage` | A full lithium cell sits around 4.2–4.4 V, an empty one around 3.4–3.6 V. |
| Charge cycles | `/sys/class/power_supply/battery/cycle_count` | Not every phone shares this without root. |
| Capacity | `charge_full` ÷ `charge_full_design` | Under 80% is the usual line for "worn". Same caveat as cycles. |

When a phone keeps its cycle count and capacity to itself, the report says so instead of
guessing.

The tool also notices when the readings were faked with `dumpsys battery set` (a
developer setting that freezes the numbers) and tells you how to undo it.

## Tests

```
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m pytest
```

The tests run without a phone: they use the sample reports in `samples/`, written in the
format `adb shell dumpsys battery` prints, and stand in for adb itself.
