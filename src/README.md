# Cable Jig Tester — Full System
## A1253-B21 Series | SAT Co., Ltd.

---

## System Architecture

```
┌─────────────────────┐          USB-COM (FT232)         ┌────────────────────┐
│   Test Jig (MCU)    │ ──────────────────────────────── │   PC Software      │
│  dsPIC30F4011       │  115200 8N1                       │  cable_tester_app  │
│                     │                                   │                    │
│  • 74HC4067 ×16    │  ← T (start test)                │  • Live fault view │
│  • PCA9555 ×2      │  ← M (mode toggle)               │  • Excel report    │
│  • 119 pins/side   │  ← S,SN-001 (set serial)         │  • Test history    │
│  • Bidi test       │  → FAULT,TYPE,J1,J2              │  • Auto S/N incr.  │
└─────────────────────┘  → RESULT,PASS/FAIL              └────────────────────┘
```

---

## Files

```
cable_tester_system/
├── firmware/
│   └── main.c                      # dsPIC30F4011 firmware (XC16)
├── pc_software/
│   ├── cable_tester_app.py         # Main PC GUI application
│   ├── create_template.py          # Excel template generator
│   ├── cable_test_report_template.xlsx  # Report template
│   └── requirements.txt
└── README.md
```

---

## Firmware (MCU)

### Build
Requires **Microchip MPLAB X IDE** + **XC16 compiler**:
1. Create new project → Standalone → dsPIC30F4011
2. Add `firmware/main.c`
3. Build → Flash via PICkit 3/4

### UART Protocol (115200 8N1)

**MCU → PC:**
```
TESTER_READY
VERSION,3.0
MODE,SIGNAL
TEST_START,SIGNAL,SN-000123
TEST_PASS1
TEST_PASS2
FAULT,OPEN_J1J2,A3,A3
FAULT,SHORT,C1,C2
TEST_DONE,2
RESULT,FAIL
```

**PC → MCU:**
```
T          Start test
M          Toggle cable mode (SIGNAL/POWER)
S,SN-001   Set serial number
V          Query version
P          Ping
```

---

## PC Software

### Install
```bash
pip install -r requirements.txt
```

### Run
```bash
python cable_tester_app.py
```

### Features
- **Auto port detection** — scans all COM ports, shows FT232 devices
- **Live fault display** — faults appear in table as MCU sends them
- **Color coded** by fault type: 🟡 Open / 🔴 Short / 🟣 Mis-wire
- **PASS/FAIL banner** — large green/red indicator
- **Excel report** — auto-fills template, saves to `reports/` folder
- **Test history** — persists across sessions (`test_history.json`)
- **Auto S/N increment** — serial number auto-advances after each test

---

## Test Method (Bidirectional)

```
Pass 1: J1(drive) → J2(read)
  - Detects opens, shorts, mis-wires from J1 side

Pass 2: J2(drive) → J1(read)  ← KEY for shared-net opens
  - Detects broken wire in shared-net group:

  J1.A1 ─────┬───── J2.A1
  J1.A2 ─────┤───── J2.A2
  J1.A3 ──╳──┘───── J2.A3  <- broken

  Pass1 MISSES this (J2 net pulls J2.A3 LOW anyway)
  Pass2 CATCHES this (J1.A3 floats HIGH when J2.A3 is driven)
```

---

## Excel Report

Report auto-filled from template with:
- Cable info (part number, type, serial, length)
- Test info (date, time, operator, firmware version, duration)
- Summary counts (opens, shorts, mis-wires, total)
- Full fault table with pin names and descriptions
- Test History sheet (cumulative log across all tests)
- Pin Map reference sheet

Reports saved as: `reports/cable_test_SN-000123_20250425_103000.xlsx`

---

## Cable Types

| Part Number     | Type   | Connector  | Cable              |
|-----------------|--------|------------|--------------------|
| A1253-B21-001   | Signal | FS1600HD   | Coaxial #32-TGFSF, 2m |
| A1253-B21-004   | Power  | PS1600HD   | UL1061 AWG26/28, 2m   |
