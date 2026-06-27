#!/usr/bin/env python3
"""
Cable Jig Tester — PC Software
Communicates with the MCU over USB-COM (FT232), shows results,
and generates an Excel report from the template.

Requirements: pip install pyserial openpyxl
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import serial
import serial.tools.list_ports
import threading
import queue
import time
import os
import sys
import json
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional
import shutil

# ── Try import openpyxl ───────────────────────────────────────────────────
try:
    import openpyxl
    from openpyxl.styles import (Font, PatternFill, Alignment, Border, Side,
                                  GradientFill)
    from openpyxl.utils import get_column_letter
    EXCEL_OK = True
except ImportError:
    EXCEL_OK = False

# ── Constants ─────────────────────────────────────────────────────────────
APP_TITLE   = "Cable Jig Tester v4.6"
BAUD_RATE   = 115200
TIMEOUT_S   = 0.1
TEMPLATE_FILE = "cable_test_report_template.xlsx"
HISTORY_FILE  = "test_history.json"
PROFILE_DIR   = "cable_profiles"

# Colors
CLR_PASS    = "#2ECC71"
CLR_FAIL    = "#E74C3C"
CLR_WARN    = "#F39C12"
CLR_IDLE    = "#95A5A6"
CLR_BG      = "#1C2833"
CLR_PANEL   = "#2C3E50"
CLR_CARD    = "#34495E"
CLR_TEXT    = "#ECF0F1"
CLR_MUTED   = "#95A5A6"
CLR_ACCENT  = "#3498DB"
CLR_OPEN    = "#F39C12"
CLR_SHORT   = "#E74C3C"
CLR_MISWIRE = "#9B59B6"

TOTAL_PINS = 116

# (Cable-specific data lives in the built-in DEFAULT_PROFILE below — defined
# after CableProfile. The app is fully profile-driven; no hard-coded
# GND/EXPECTED constants are used by the logic.)



# ── Data classes ──────────────────────────────────────────────────────────
@dataclass
class ConnectionRecord:
    j1_pin: str
    j2_pin: str

@dataclass
class TestResult:
    timestamp: datetime               = field(default_factory=datetime.now)
    serial_number: str                = ""
    operator: str                     = ""
    fw_version: str                   = ""
    com_port: str                     = ""
    total_pins: int                   = 116
    connections: List[ConnectionRecord] = field(default_factory=list)
    duration_s: float                 = 0.0
    completed: bool                   = False
    raw_log: List[str]                = field(default_factory=list)


@dataclass
class JigPinResult:
    """Hardware diagnostic result for one J1 pin (Jig Self-Diagnostic)."""
    pin_name:       str
    status:         str        # "OK" or "FAIL"
    conn_count:     int = 0    # connection count (only when status="OK")
    mi:             int = -1   # MUX group index
    ch:             int = -1   # channel trong group
    lat:            int = -1   # ZA LAT readback (0=LOW/OK, 1=HIGH/FAIL)
    out0:           int = -1   # U19 OUT0 readback (-1 = none / I2C error)
    out1:           int = -1   # U19 OUT1 readback (-1 = none / I2C error)
    i2c_err:        bool = False  # firmware reports I2C error when reading the register
    root_cause:     str = ""   # analyzed root cause
    recommendation: str = ""   # corrective action

# ── Excel Report Generator ────────────────────────────────────────────────
# -- Cable profile (netlist) model -----------------------------------------
@dataclass
class CableProfile:
    """Expected connection map (J1<->J2 edge set) for one cable type.

    Learned from a known-good 'golden' cable or loaded from JSON. Handles any
    cable: pure point-to-point, with a GND bus, or different GND pins -- it
    only compares the measured edge set against the expected edge set, with no
    special-casing of GND.
    """
    name:       str
    total_pins: int = TOTAL_PINS
    edges:      set = field(default_factory=set)   # {(j1_pin, j2_pin)}
    notes:      str = ""
    created:    str = ""

    def to_dict(self):
        return {"name": self.name, "total_pins": self.total_pins,
                "notes": self.notes, "created": self.created,
                "edges": sorted([list(e) for e in self.edges])}

    @classmethod
    def from_dict(cls, d):
        return cls(name=d.get("name", "?"),
                   total_pins=int(d.get("total_pins", TOTAL_PINS)),
                   notes=d.get("notes", ""), created=d.get("created", ""),
                   edges={(e[0], e[1]) for e in d.get("edges", [])})

    @classmethod
    def from_connections(cls, name, connections, notes=""):
        return cls(name=name,
                   edges={(c.j1_pin, c.j2_pin) for c in connections},
                   notes=notes,
                   created=datetime.now().isoformat(timespec="seconds"))

    def _cache(self):
        c = getattr(self, "_dcache", None)
        if c is None:
            d1 = {}; d2 = {}
            for a, b in self.edges:
                d1[a] = d1.get(a, 0) + 1
                d2[b] = d2.get(b, 0) + 1
            sig = {a: b for a, b in self.edges if d1[a] == 1 and d2[b] == 1}
            g1 = {a for a in d1 if d1[a] > 1}
            g2 = {b for b in d2 if d2[b] > 1}
            c = (sig, g1, g2)
            self._dcache = c
        return c

    def signal_map(self):
        """J1->J2 for 1:1 edges (both endpoints degree 1) = signal wires."""
        return self._cache()[0]

    def gnd_j1(self):
        """J1 pins on a shared bus (degree > 1)."""
        return self._cache()[1]

    def gnd_j2(self):
        return self._cache()[2]

    def compare(self, measured_edges):
        exp = set(self.edges); meas = set(measured_edges)
        opens  = sorted(exp - meas)
        extras = sorted(meas - exp)
        return {"verdict": "PASS" if not opens and not extras else "FAIL",
                "opens": opens, "extras": extras,
                "n_expected": len(exp), "n_measured": len(meas),
                "n_ok": len(exp & meas)}


def _profile_path(name):
    safe = "".join(ch if ch.isalnum() or ch in "-_ ." else "_" for ch in name).strip()
    return Path(PROFILE_DIR) / f"{safe or 'cable'}.json"

def save_profile(profile):
    Path(PROFILE_DIR).mkdir(exist_ok=True)
    p = _profile_path(profile.name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(profile.to_dict(), f, indent=2, ensure_ascii=False)
    return p

def list_profiles():
    d = Path(PROFILE_DIR); out = {}
    if d.exists():
        for fp in sorted(d.glob("*.json")):
            try:
                with open(fp, encoding="utf-8") as f:
                    out[json.load(f).get("name", fp.stem)] = fp
            except Exception:
                pass
    return out

def load_profile(path):
    with open(path, encoding="utf-8") as f:
        return CableProfile.from_dict(json.load(f))


DEFAULT_PROFILE_NAME = "Default cable (66 signal + GND bus)"

def _build_default_profile():
    """The original 116-pin cable, defined once as the seed of a profile."""
    signal = {
        "A1":"D1",
        "A3":"D3",
        "A5":"D5",
        "A7":"D7",
        "A9":"D9",
        "A11":"D11",
        "A13":"D13",
        "A15":"D15",
        "A17":"D17",
        "B2":"C2",
        "B4":"C4",
        "B6":"C6",
        "B8":"C8",
        "B10":"C10",
        "B12":"C12",
        "B14":"C14",
        "B16":"C16",
        "C1":"B1",
        "C3":"B3",
        "C5":"B5",
        "C7":"B7",
        "C9":"B9",
        "C11":"B11",
        "C13":"B13",
        "C15":"B15",
        "C17":"B17",
        "D2":"A2",
        "D4":"A4",
        "D6":"A6",
        "D8":"A8",
        "D10":"A10",
        "D12":"A12",
        "D14":"A14",
        "D16":"A16",
        "E2":"G2",
        "E4":"G4",
        "E6":"G6",
        "E8":"G8",
        "E10":"G10",
        "E12":"G12",
        "E14":"G14",
        "E16":"G16",
        "F1":"F1",
        "F2":"F2",
        "F3":"F3",
        "F4":"F4",
        "F5":"F5",
        "F6":"F6",
        "F7":"F7",
        "F8":"F8",
        "F9":"F9",
        "F10":"F10",
        "F11":"F11",
        "F12":"F12",
        "F13":"F13",
        "F14":"F14",
        "F15":"F15",
        "F16":"F16",
        "G1":"E1",
        "G3":"E3",
        "G5":"E5",
        "G7":"E7",
        "G9":"E9",
        "G11":"E11",
        "G13":"E13",
        "G15":"E15"
    }
    gnd_j1 = ["A10", "A12", "A14", "A16", "A2", "A4", "A6", "A8", "B1", "B11", "B13", "B15", "B17", "B3", "B5", "B7", "B9", "C10", "C12", "C14", "C16", "C2", "C4", "C6", "C8", "D1", "D11", "D13", "D15", "D17", "D3", "D5", "D7", "D9", "E1", "E11", "E13", "E15", "E3", "E5", "E7", "E9", "G10", "G12", "G14", "G16", "G2", "G4", "G6", "G8"]
    gnd_j2 = ["A1", "A11", "A13", "A15", "A17", "A3", "A5", "A7", "A9", "B10", "B12", "B14", "B16", "B2", "B4", "B6", "B8", "C1", "C11", "C13", "C15", "C17", "C3", "C5", "C7", "C9", "D10", "D12", "D14", "D16", "D2", "D4", "D6", "D8", "E10", "E12", "E14", "E16", "E2", "E4", "E6", "E8", "G1", "G11", "G13", "G15", "G3", "G5", "G7", "G9"]
    edges = set(signal.items()) | {(a, b) for a in gnd_j1 for b in gnd_j2}
    return CableProfile(name=DEFAULT_PROFILE_NAME, edges=edges,
                        notes="Built-in original cable")

DEFAULT_PROFILE = _build_default_profile()



class SerialReader(threading.Thread):
    def __init__(self, port, baud, rx_queue, stop_event):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.q    = rx_queue
        self.stop = stop_event
        self.ser  = None

    def run(self):
        try:
            self.ser = serial.Serial(self.port, self.baud,
                                     timeout=TIMEOUT_S,
                                     write_timeout=1.0)
            self.q.put(("CONNECTED", self.port))
            buf = ""
            while not self.stop.is_set():
                try:
                    chunk = self.ser.read(64).decode("ascii", errors="replace")
                    if chunk:
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            line = line.strip()
                            if line:
                                self.q.put(("LINE", line))
                except serial.SerialTimeoutException:
                    pass
                except Exception as e:
                    self.q.put(("ERROR", str(e)))
                    break
        except Exception as e:
            self.q.put(("ERROR", str(e)))
        finally:
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.q.put(("DISCONNECTED", self.port))

    def send(self, cmd: str):
        try:
            if self.ser and self.ser.is_open:
                self.ser.write((cmd + "\r\n").encode())
        except (serial.SerialException, OSError) as e:
            self.q.put(("ERROR", f"Write failed: {e}"))


# ── Main Application ──────────────────────────────────────────────────────
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1100x780")
        self.minsize(900, 640)
        self.configure(bg=CLR_BG)
        self.resizable(True, True)

        # State
        self.serial_reader: Optional[SerialReader] = None
        self.serial_stop   = threading.Event()
        self.rx_queue      = queue.Queue()
        self.current_result: Optional[TestResult] = None
        self.test_start_time: Optional[float] = None
        self.in_test       = False
        self.test_stopped  = False          # user pressed STOP
        self.test_timeout_s = 120           # auto-stop after 120s no response
        self.history: List[TestResult] = []
        self.operator_name = ""

        # Jig self-diagnostic state — PASS 1 (J1 side)
        self._jig_fail_pins:     List[str]           = []
        self._jig_results_all:   List[JigPinResult]  = []
        self._sweep_data:        dict                 = {}   # {mi_str: set(ch_int)}
        self._sweep_hit_count:   int                 = 0
        self._sweep_mi:          Optional[str]       = None
        self._jig_ok_count:      int                 = 0
        self._jig_diag_ts:       Optional[datetime]  = None
        self._jig_diag_com:      str                 = ""
        self._jig_diag_fw:       str                 = ""
        # Jig self-diagnostic state — PASS 2 (J2 side)
        self._jig_j2_fail_pins:     List[str]           = []
        self._jig_j2_results_all:   List[JigPinResult]  = []
        self._sweep_j2_data:        dict                 = {}   # {mi_str: set(ch_int)}
        self._sweep_j2_hit_count:   int                 = 0
        self._sweep_j2_mi:          Optional[str]       = None
        self._jig_j2_ok_count:      int                 = 0
        # F-row even-pin debug
        self._fdiag_rows:           list                = []
        # J2 MUX-B group-0 sweep
        self._j2g0_rows:            list                = []
        # GND grouping state
        self._gnd_tree_id:          Optional[str]       = None
        self._gnd_count:            int                 = 0
        # Cable profile (multi-cable support)
        self.active_profile             = DEFAULT_PROFILE
        self._profile_paths:    dict    = {}      # name -> path
        self._last_profile_result       = None    # last netlist compare result

        self._build_ui()
        self._load_history()
        self._refresh_ports()
        self._refresh_profiles()
        self._poll_queue()

    # ── UI Build ──────────────────────────────────────────────────────────
    def _build_ui(self):
        self._build_topbar()
        self._build_main()
        self._build_statusbar()

    def _build_topbar(self):
        top = tk.Frame(self, bg=CLR_PANEL, height=60)
        top.pack(fill="x", side="top")
        top.pack_propagate(False)

        tk.Label(top, text="⚡ " + APP_TITLE,
                 font=("Arial",16,"bold"), fg=CLR_TEXT, bg=CLR_PANEL
                 ).pack(side="left", padx=20, pady=10)

        # About button
        tk.Button(top, text="ℹ  About",
                  font=("Arial", 9), fg=CLR_TEXT, bg=CLR_CARD,
                  activebackground=CLR_ACCENT, activeforeground=CLR_TEXT,
                  relief="flat", bd=0, padx=10, pady=4,
                  cursor="hand2",
                  command=self._show_about
                  ).pack(side="left", padx=(0, 16), pady=14)

        # Operator input
        tk.Label(top, text="Operator:", fg=CLR_MUTED, bg=CLR_PANEL,
                 font=("Arial",10)).pack(side="right", padx=(0,5))
        self.var_operator = tk.StringVar(value="Operator")
        tk.Entry(top, textvariable=self.var_operator,
                 font=("Arial",10), width=14,
                 bg=CLR_CARD, fg=CLR_TEXT, insertbackground=CLR_TEXT,
                 relief="flat", bd=4
                 ).pack(side="right", padx=(0,10), pady=12)

        # Serial number input
        tk.Label(top, text="S/N:", fg=CLR_MUTED, bg=CLR_PANEL,
                 font=("Arial",10)).pack(side="right", padx=(0,5))
        self.var_sn = tk.StringVar(value="SN-000001")
        tk.Entry(top, textvariable=self.var_sn,
                 font=("Arial",10,"bold"), width=14,
                 bg=CLR_CARD, fg=CLR_ACCENT, insertbackground=CLR_ACCENT,
                 relief="flat", bd=4
                 ).pack(side="right", padx=(0,10), pady=12)

    def _show_about(self):
        win = tk.Toplevel(self)
        win.title("About — Cable Jig Tester")
        win.resizable(False, False)
        win.configure(bg=CLR_BG)
        win.grab_set()

        # ── Header ───────────────────────────────────────────────────────
        hdr = tk.Frame(win, bg="#1A3A5C", pady=24)
        hdr.pack(fill="x")
        tk.Label(hdr, text="⚡ Cable Jig Tester",
                 font=("Arial", 20, "bold"), fg="#FFFFFF", bg="#1A3A5C"
                 ).pack()
        tk.Label(hdr, text="Cable Continuity Test System",
                 font=("Arial", 11), fg="#90B8D8", bg="#1A3A5C"
                 ).pack()

        # ── Info block ───────────────────────────────────────────────────
        info = tk.Frame(win, bg=CLR_BG, padx=32, pady=20)
        info.pack(fill="x")

        rows = [
            ("Version",      APP_TITLE),
            ("Firmware",     "dsPIC30F4011 — v4.10"),
            ("Developed by", "NEU Corporation"),
            ("Contact",      "Jasonquay@corporation.com"),
            ("Support",      "Jasonquay@corporation.com"),
            ("License",      "Proprietary — NEU Corporation"),
        ]
        for label, value in rows:
            row = tk.Frame(info, bg=CLR_BG)
            row.pack(fill="x", pady=3)
            tk.Label(row, text=f"{label}:", width=14, anchor="e",
                     font=("Arial", 10, "bold"), fg="#90B8D8", bg=CLR_BG
                     ).pack(side="left")
            tk.Label(row, text=value, anchor="w",
                     font=("Arial", 10), fg=CLR_TEXT, bg=CLR_BG
                     ).pack(side="left", padx=(8, 0))

        # ── Divider ──────────────────────────────────────────────────────
        tk.Frame(win, bg="#2E4F6F", height=1).pack(fill="x", padx=32)

        # ── Footer ───────────────────────────────────────────────────────
        ftr = tk.Frame(win, bg=CLR_BG, pady=16)
        ftr.pack()
        import datetime
        tk.Label(ftr,
                 text=f"© {datetime.datetime.now().year} NEU Corporation. All rights reserved.",
                 font=("Arial", 9), fg=CLR_MUTED, bg=CLR_BG
                 ).pack()
        tk.Button(ftr, text="Close",
                  font=("Arial", 10), fg=CLR_TEXT, bg="#2E4F6F",
                  activebackground=CLR_ACCENT, activeforeground=CLR_TEXT,
                  relief="flat", padx=24, pady=6, bd=0, cursor="hand2",
                  command=win.destroy
                  ).pack(pady=(10, 0))

        # Centre on parent
        win.update_idletasks()
        x = self.winfo_x() + (self.winfo_width()  - win.winfo_width())  // 2
        y = self.winfo_y() + (self.winfo_height() - win.winfo_height()) // 2
        win.geometry(f"+{x}+{y}")

    def _build_main(self):
        paned = tk.PanedWindow(self, orient="horizontal",
                               bg=CLR_BG, sashwidth=6,
                               sashrelief="flat", sashpad=2)
        paned.pack(fill="both", expand=True, padx=6, pady=4)

        left_frame = tk.Frame(paned, bg=CLR_BG)
        right_frame = tk.Frame(paned, bg=CLR_BG)
        paned.add(left_frame, minsize=300)
        paned.add(right_frame, minsize=500)

        self._build_control_panel(left_frame)
        self._build_result_panel(right_frame)

    def _card(self, parent, title):
        f = tk.LabelFrame(parent, text=f"  {title}  ",
                          bg=CLR_CARD, fg=CLR_MUTED,
                          font=("Arial",9,"bold"),
                          relief="flat", bd=2,
                          labelanchor="nw",
                          padx=8, pady=6)
        return f

    def _build_control_panel(self, parent):
        # ── Connection card ──
        conn = self._card(parent, "CONNECTION")
        conn.pack(fill="x", padx=6, pady=(6,4))

        row1 = tk.Frame(conn, bg=CLR_CARD)
        row1.pack(fill="x", pady=3)
        tk.Label(row1, text="Port:", fg=CLR_MUTED, bg=CLR_CARD,
                 font=("Arial",10), width=7, anchor="w").pack(side="left")
        self.var_port = tk.StringVar()
        self.cb_port = ttk.Combobox(row1, textvariable=self.var_port,
                                     width=16, font=("Arial",10))
        self.cb_port.pack(side="left", padx=4)

        btn_refresh = tk.Button(row1, text="⟳", command=self._refresh_ports,
                                bg=CLR_PANEL, fg=CLR_TEXT, font=("Arial",11),
                                relief="flat", cursor="hand2", padx=6)
        btn_refresh.pack(side="left")

        row2 = tk.Frame(conn, bg=CLR_CARD)
        row2.pack(fill="x", pady=3)
        self.btn_connect = tk.Button(row2, text="🔌  Connect",
                                      command=self._toggle_connect,
                                      bg=CLR_ACCENT, fg="white",
                                      font=("Arial",10,"bold"),
                                      relief="flat", cursor="hand2",
                                      padx=12, pady=6, width=16)
        self.btn_connect.pack(side="left", padx=(0,6))

        self.lbl_conn_status = tk.Label(row2, text="● Disconnected",
                                         fg=CLR_IDLE, bg=CLR_CARD,
                                         font=("Arial",10,"bold"))
        self.lbl_conn_status.pack(side="left")

        # -- Cable type card --
        cab = self._card(parent, "CABLE TYPE")
        cab.pack(fill="x", padx=6, pady=4)
        rowp = tk.Frame(cab, bg=CLR_CARD); rowp.pack(fill="x", pady=3)
        tk.Label(rowp, text="Profile:", fg=CLR_MUTED, bg=CLR_CARD,
                 font=("Arial",10), width=7, anchor="w").pack(side="left")
        self.var_profile = tk.StringVar(value=DEFAULT_PROFILE_NAME)
        self.cb_profile = ttk.Combobox(rowp, textvariable=self.var_profile,
                                       width=22, font=("Arial",9), state="readonly")
        self.cb_profile.pack(side="left", padx=4)
        self.cb_profile.bind("<<ComboboxSelected>>", self._on_profile_select)
        tk.Button(rowp, text="⟳", command=self._refresh_profiles,
                  bg=CLR_PANEL, fg=CLR_TEXT, font=("Arial",11),
                  relief="flat", cursor="hand2", padx=6).pack(side="left")
        self.btn_learn = tk.Button(cab, text="📚  Learn current cable as profile",
                                   command=self._learn_profile,
                                   bg="#117A65", fg="white",
                                   disabledforeground="#ABEBC6",
                                   font=("Arial",9,"bold"),
                                   relief="flat", cursor="hand2", padx=10, pady=5,
                                   state="disabled")
        self.btn_learn.pack(fill="x", pady=(2,0))
        self.lbl_profile_info = tk.Label(cab,
                 text="Default: built-in map (66 signal pairs + GND bus)",
                 fg="#BDC3C7", bg=CLR_CARD, font=("Arial",8),
                 wraplength=240, justify="left")
        self.lbl_profile_info.pack(fill="x", pady=(2,0))

        # ── Test control card ──
        ctl = self._card(parent, "TEST CONTROL")
        ctl.pack(fill="x", padx=6, pady=4)

        self.btn_test = tk.Button(ctl, text="▶  START TEST",
                                   command=self._start_test,
                                   bg="#27AE60", fg="white",
                                   font=("Arial",13,"bold"),
                                   relief="flat", cursor="hand2",
                                   padx=16, pady=12)
        self.btn_test.pack(fill="x", pady=(0,4))

        self.btn_stop = tk.Button(ctl, text="■  STOP",
                                   command=self._stop_test,
                                   bg="#7F8C8D", fg="white",
                                   font=("Arial",11,"bold"),
                                   relief="flat", cursor="hand2",
                                   padx=16, pady=8,
                                   state="disabled")
        self.btn_stop.pack(fill="x", pady=(0,6))

        self.btn_diag = tk.Button(ctl, text="🔬  Run Diagnostic (D)",
                                   command=self._run_diag,
                                   bg="#1A5276", fg="white",
                                   font=("Arial",9),
                                   relief="flat", cursor="hand2",
                                   padx=10, pady=6)
        self.btn_diag.pack(fill="x", pady=(0,2))

        self.btn_i2c_scan = tk.Button(ctl, text="🔍  I2C Bus Scan (I)",
                                       command=self._run_i2c_scan,
                                       bg="#1A5276", fg="white",
                                       font=("Arial",9),
                                       relief="flat", cursor="hand2",
                                       padx=10, pady=6)
        self.btn_i2c_scan.pack(fill="x", pady=(0,2))

        self.btn_jig_diag = tk.Button(ctl, text="🔧  Jig Self-Diag (J)",
                                       command=self._run_jig_diag,
                                       bg="#CA6F1E", fg="white",
                                       font=("Arial",9,"bold"),
                                       relief="flat", cursor="hand2",
                                       padx=10, pady=6)
        self.btn_jig_diag.pack(fill="x", pady=(0,2))

        self.btn_fdiag = tk.Button(ctl, text="🔍  F-Row Debug (F)",
                                    command=self._run_fdiag,
                                    bg="#1E8449", fg="white",
                                    font=("Arial",9),
                                    relief="flat", cursor="hand2",
                                    padx=10, pady=5)
        self.btn_fdiag.pack(fill="x", pady=(0,2))

        self.btn_j2g0 = tk.Button(ctl, text="🔎  J2 Group-0 Scan (G)",
                                   command=self._run_j2g0,
                                   bg="#1A5276", fg="white",
                                   font=("Arial",9),
                                   relief="flat", cursor="hand2",
                                   padx=10, pady=5)
        self.btn_j2g0.pack(fill="x", pady=(0,2))

        self.btn_jig_export = tk.Button(ctl, text="📋  Export Jig Report",
                                         command=self._export_jig_report,
                                         bg="#1A5276", fg="#95A5A6",
                                         font=("Arial",9),
                                         relief="flat", cursor="hand2",
                                         padx=10, pady=5,
                                         state="disabled")
        self.btn_jig_export.pack(fill="x", pady=(0,6))

        self.progress_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(ctl, variable=self.progress_var,
                                         maximum=100, mode="indeterminate",
                                         length=200)
        self.progress.pack(fill="x", pady=(0,4))

        self.lbl_progress = tk.Label(ctl, text="Ready — press START",
                                      fg=CLR_MUTED, bg=CLR_CARD,
                                      font=("Arial",9), wraplength=240)
        self.lbl_progress.pack(fill="x")

        # ── Report card ──
        rpt = self._card(parent, "REPORT")
        rpt.pack(fill="x", padx=6, pady=4)

        self.btn_export = tk.Button(rpt, text="📊  Export Excel Report",
                                     command=self._export_report,
                                     bg="#8E44AD", fg="white",
                                     font=("Arial",10,"bold"),
                                     relief="flat", cursor="hand2",
                                     padx=10, pady=8,
                                     state="disabled")
        self.btn_export.pack(fill="x", pady=(0,4))

        tk.Button(rpt, text="📁  Open Reports Folder",
                  command=self._open_reports_folder,
                  bg=CLR_PANEL, fg=CLR_TEXT,
                  font=("Arial",9),
                  relief="flat", cursor="hand2",
                  padx=10, pady=6
                  ).pack(fill="x")

        # ── Stats card ──
        stat = self._card(parent, "SESSION STATS")
        stat.pack(fill="x", padx=6, pady=4)

        self.lbl_stats = tk.Label(stat,
            text="Tests: 0  |  Pass: 0  |  Fail: 0",
            fg=CLR_TEXT, bg=CLR_CARD, font=("Arial",10))
        self.lbl_stats.pack()

    def _build_result_panel(self, parent):
        # ── Big result indicator ──
        self.result_frame = tk.Frame(parent, bg=CLR_IDLE, height=80)
        self.result_frame.pack(fill="x", padx=6, pady=(6,4))
        self.result_frame.pack_propagate(False)

        self.lbl_result = tk.Label(self.result_frame,
                                    text="—  WAITING FOR TEST  —",
                                    font=("Arial",18,"bold"),
                                    fg="white", bg=CLR_IDLE)
        self.lbl_result.pack(expand=True)

        # ── Connection count badge ──
        badge_row = tk.Frame(parent, bg=CLR_BG)
        badge_row.pack(fill="x", padx=6, pady=2)

        self.badge_conn = self._badge(badge_row, "Connections Found", "0", CLR_ACCENT)
        self.badge_conn.pack(fill="x", padx=3)

        # ── Connection table ──
        tbl_frame = self._card(parent, "CONNECTIONS FOUND")
        tbl_frame.pack(fill="both", expand=True, padx=6, pady=4)

        cols = ("num", "j1", "j2")
        self.tree = ttk.Treeview(tbl_frame, columns=cols,
                                  show="headings", height=14,
                                  selectmode="browse")
        for cid, heading, w in [("num","#",50), ("j1","J1 Pin",200), ("j2","J2 Pin",200)]:
            self.tree.heading(cid, text=heading)
            self.tree.column(cid, width=w, anchor="center", minwidth=40)

        style = ttk.Style()
        style.configure("Treeview",
                         background=CLR_CARD, foreground=CLR_TEXT,
                         rowheight=24, font=("Arial",10),
                         fieldbackground=CLR_CARD)
        style.configure("Treeview.Heading",
                         background=CLR_PANEL, foreground=CLR_TEXT,
                         font=("Arial",10,"bold"))
        style.map("Treeview", background=[("selected","#2980B9")])

        sb = ttk.Scrollbar(tbl_frame, orient="vertical",
                            command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        self.tree.tag_configure("even",       background=CLR_CARD)
        self.tree.tag_configure("odd",        background="#3D5166")
        self.tree.tag_configure("gnd",        background="#7D6608", foreground="#F9E79F")
        self.tree.tag_configure("miswire",    background="#6C3483", foreground="#F8C471")
        self.tree.tag_configure("unexpected", background="#922B21", foreground="#FADBD8")

        # ── Raw log ──
        log_frame = self._card(parent, "RAW LOG")
        log_frame.pack(fill="x", padx=6, pady=(0,4))

        self.txt_log = tk.Text(log_frame, height=6, bg="#0D1117",
                                fg="#58D68D", font=("Courier",9),
                                relief="flat", wrap="word",
                                insertbackground="#58D68D")
        self.txt_log.pack(fill="both", expand=True)
        sb2 = ttk.Scrollbar(log_frame, command=self.txt_log.yview)
        self.txt_log.configure(yscrollcommand=sb2.set)

    def _badge(self, parent, label, value, color):
        f = tk.Frame(parent, bg=color, padx=8, pady=4)
        tk.Label(f, text=value, font=("Arial",18,"bold"),
                 fg="white", bg=color).pack()
        tk.Label(f, text=label, font=("Arial",8),
                 fg="white", bg=color).pack()
        f._value_label = f.winfo_children()[0]
        return f

    def _build_statusbar(self):
        bar = tk.Frame(self, bg=CLR_PANEL, height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self.lbl_status = tk.Label(bar, text="Ready",
                                    fg=CLR_MUTED, bg=CLR_PANEL,
                                    font=("Arial",9))
        self.lbl_status.pack(side="left", padx=10, pady=4)

        self.lbl_time = tk.Label(bar, text="",
                                  fg=CLR_MUTED, bg=CLR_PANEL,
                                  font=("Arial",9))
        self.lbl_time.pack(side="right", padx=10)
        self._tick()

    def _tick(self):
        self.lbl_time.config(text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        self.after(1000, self._tick)

    # ── Actions ───────────────────────────────────────────────────────────
    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.cb_port["values"] = ports
        if ports and not self.var_port.get():
            self.var_port.set(ports[0])

    def _toggle_connect(self):
        if self.serial_reader and not self.serial_stop.is_set():
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        port = self.var_port.get()
        if not port:
            messagebox.showwarning("No Port", "Please select a COM port.")
            return
        self.serial_stop.clear()
        self.serial_reader = SerialReader(port, BAUD_RATE,
                                           self.rx_queue, self.serial_stop)
        self.serial_reader.start()
        self.btn_connect.config(text="⏏  Disconnect", bg="#E74C3C")

    def _disconnect(self):
        if self.serial_reader:
            self.serial_stop.set()
            self.serial_reader = None
        self.btn_connect.config(text="🔌  Connect", bg=CLR_ACCENT)
        self.lbl_conn_status.config(text="● Disconnected", fg=CLR_IDLE)

    def _start_test(self):
        if not self.serial_reader:
            messagebox.showwarning("Not Connected",
                                   "Connect to tester first.")
            return
        if self.in_test:
            return

        sn = self.var_sn.get().strip() or "SN-000000"
        self.serial_reader.send(f"S,{sn}")
        time.sleep(0.1)
        self.serial_reader.send("T")

        self.in_test      = True
        self.test_stopped = False
        self.test_start_time = time.time()
        self.current_result = TestResult(
            serial_number = sn,
            operator      = self.var_operator.get(),
            com_port      = self.var_port.get(),
        )

        # Reset UI
        for item in self.tree.get_children():
            self.tree.delete(item)
        self._gnd_tree_id = None
        self._gnd_count   = 0
        self._set_conn_badge(0)
        self.result_frame.config(bg="#2980B9")
        self.lbl_result.config(text="⏳  TESTING…", bg="#2980B9")
        self.btn_test.config(state="disabled")
        self.btn_stop.config(state="normal", bg="#E74C3C")
        self.btn_export.config(state="disabled")
        self.progress.config(mode="indeterminate")
        self.progress.start(15)
        self._log("=== TEST STARTED ===")

        # Start timeout watcher
        self._check_test_timeout()

    def _run_diag(self):
        """Send 'D' diagnostic — drives J1.A1 LOW, reads U19/U20 via I2C, scans all 116 J2 pins."""
        if not self.serial_reader:
            messagebox.showwarning("Not Connected", "Connect to tester first.")
            return
        if self.in_test:
            messagebox.showwarning("Test Running", "Wait for current test to finish.")
            return
        self._log("=== DIAGNOSTIC v2 SENT (D) ===")
        self._log("Checking: J1 LAT drive, U19 I2C readback, U20 I2C readback, all 116 J2 pins")
        self._log("Key results:")
        self._log("  ZA_LAT=0       → MCU driving J1 Z-line LOW correctly")
        self._log("  U19_OUT0=112   → I2C write to U19 (J1 mux) worked")
        self._log("  U20_OUT0=112   → I2C write to U20 (J2 mux) worked")
        self._log("  U19_OUT0=255   → *** U19 not responding (I2C address wrong?) ***")
        self._log("  DIAG_CONN,...  → connection detected")
        self.serial_reader.send("D")

    def _run_i2c_scan(self):
        """Send 'I' — scans I2C addresses 0x20-0x27 and reports which PCA9555 chips respond."""
        if not self.serial_reader:
            messagebox.showwarning("Not Connected", "Connect to tester first.")
            return
        if self.in_test:
            messagebox.showwarning("Test Running", "Wait for current test to finish.")
            return
        self._log("=== I2C BUS SCAN SENT (I) ===")
        self._log("Probing 0x20-0x27 — ACK = chip found, NACK = no chip")
        self._log("Expected: U19=0x20 ACK, U20=0x21 ACK (if wired correctly)")
        self.serial_reader.send("I")

    def _run_jig_diag(self):
        """Send 'J' — scan all 116 J1 pins, report pins with no connection."""
        if not self.serial_reader:
            messagebox.showwarning("Not Connected", "Connect to tester first.")
            return
        if self.in_test:
            messagebox.showwarning("Test Running", "Wait for current test to finish.")
            return
        # Reset state — Pass 1 (J1)
        self._jig_fail_pins      = []
        self._jig_results_all    = []
        self._sweep_data         = {}
        self._sweep_hit_count    = 0
        self._sweep_mi           = None
        self._jig_ok_count       = 0
        self._jig_diag_ts        = datetime.now()
        self._jig_diag_com       = self.var_port.get()
        self._jig_diag_fw        = (self.current_result.fw_version
                                    if self.current_result else "")
        # Reset state — Pass 2 (J2)
        self._jig_j2_fail_pins   = []
        self._jig_j2_results_all = []
        self._sweep_j2_data      = {}
        self._sweep_j2_hit_count = 0
        self._sweep_j2_mi        = None
        self._jig_j2_ok_count    = 0
        self.btn_jig_diag.config(state="disabled")
        if hasattr(self, "btn_jig_export"):
            self.btn_jig_export.config(state="disabled", fg="#95A5A6")
        self._log("=== JIG SELF-DIAGNOSTIC SENT (J) ===")
        self._log("Pass 1: Scan 116 J1 pins (drive J1 → detect J2)")
        self._log("Pass 2: Scan 116 J2 pins (drive J2 → detect J1)")
        self._status("Jig self-diagnostic running…")
        self.serial_reader.send("J")

    def _run_j2g0(self):
        """Send 'G' — sweep J2 MUX-B group 0 (rp 0..15) from J2 side, scan J1."""
        if not self.serial_reader:
            messagebox.showwarning("Not Connected", "Connect to tester first.")
            return
        if self.in_test:
            messagebox.showwarning("Test Running", "Wait for current test to finish.")
            return
        self._j2g0_rows = []
        if hasattr(self, "btn_j2g0"):
            self.btn_j2g0.config(state="disabled")
        self._log("=== J2 GROUP-0 SWEEP SENT (G) ===")
        self._status("J2 group-0 sweep running…")
        self.serial_reader.send("G")

    def _run_fdiag(self):
        """Send 'F' — debug J1.F even pins (F2,F4,...,F16) → show actual rp and J2 label."""
        if not self.serial_reader:
            messagebox.showwarning("Not Connected", "Connect to tester first.")
            return
        if self.in_test:
            messagebox.showwarning("Test Running", "Wait for current test to finish.")
            return
        self._fdiag_rows = []
        if hasattr(self, "btn_fdiag"):
            self.btn_fdiag.config(state="disabled")
        self._log("=== F-ROW EVEN DEBUG SENT (F) ===")
        self._status("F-row even debug running…")
        self.serial_reader.send("F")

    def _stop_test(self):
        """User pressed STOP — cancel waiting for MCU response."""
        if not self.in_test:
            return
        self.test_stopped = True
        self._log("=== TEST STOPPED BY USER ===")
        self._status("Test stopped by user")

        # Reset UI immediately
        self.in_test = False
        self.progress.stop()
        self.progress.config(mode="determinate")
        self.progress_var.set(0)
        self.result_frame.config(bg="#7F8C8D")
        self.lbl_result.config(text="⏹  STOPPED", bg="#7F8C8D")
        self.btn_test.config(state="normal")
        self.btn_stop.config(state="disabled", bg="#7F8C8D")
        self.btn_export.config(state="disabled")

    def _check_test_timeout(self):
        """Called periodically while test is running to detect timeout."""
        if not self.in_test:
            return  # test already finished or stopped

        elapsed = time.time() - (self.test_start_time or time.time())

        # Update elapsed time in status
        self._status(f"Testing… {int(elapsed)}s elapsed  (STOP to cancel)")

        if elapsed >= self.test_timeout_s:
            # Auto-stop after timeout
            self._log(f"=== TEST TIMEOUT ({self.test_timeout_s}s) — No response from MCU ===")
            self._status(f"Timeout! No response after {self.test_timeout_s}s")
            self.test_stopped = True
            self.in_test = False
            self.progress.stop()
            self.progress.config(mode="determinate")
            self.progress_var.set(0)
            self.result_frame.config(bg=CLR_WARN)
            self.lbl_result.config(
                text=f"⚠  TIMEOUT — No response after {self.test_timeout_s}s",
                bg=CLR_WARN)
            self.btn_test.config(state="normal")
            self.btn_stop.config(state="disabled", bg="#7F8C8D")
            messagebox.showwarning("Timeout",
                f"No response from tester after {self.test_timeout_s} seconds.\n\n"
                "Possible causes:\n"
                "• I2C bus locked — power-cycle the tester\n"
                "• USB cable disconnected\n"
                "• MCU firmware crashed\n\n"
                "Check the log for the last MCU message.")
            return

        # Schedule next check in 1 second
        self.after(1000, self._check_test_timeout)

    def _set_conn_badge(self, n: int):
        self.badge_conn._value_label.config(text=str(n))

    def _log(self, line):
        self.txt_log.insert("end", line + "\n")
        self.txt_log.see("end")
        if self.current_result:
            self.current_result.raw_log.append(line)

    def _status(self, msg):
        self.lbl_status.config(text=msg)
        self.lbl_progress.config(text=msg)

    # ── Queue polling ──────────────────────────────────────────────────────
    def _poll_queue(self):
        try:
            while True:
                msg_type, data = self.rx_queue.get_nowait()
                self._handle_msg(msg_type, data)
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)

    def _handle_msg(self, msg_type, data):
        if msg_type == "CONNECTED":
            self.lbl_conn_status.config(text=f"● {data}", fg=CLR_PASS)
            self._status(f"Connected: {data}")
            # Query version and ping
            if self.serial_reader:
                self.serial_reader.send("V")
                self.serial_reader.send("P")

        elif msg_type == "DISCONNECTED":
            self.lbl_conn_status.config(text="● Disconnected", fg=CLR_IDLE)
            self._status("Disconnected")

        elif msg_type == "ERROR":
            self.lbl_conn_status.config(text=f"● Error: {data}", fg=CLR_FAIL)
            self._status(f"Error: {data}")
            messagebox.showerror("Connection Error", data)

        elif msg_type == "LINE":
            self._log(data)
            self._parse_line(data)

    def _parse_line(self, line: str):
        parts = [p.strip() for p in line.split(",")]
        tag   = parts[0] if parts else ""

        if tag == "TESTER_READY":
            self._status("Tester ready")
            if self.serial_reader:
                self.serial_reader.send("V")

        elif tag == "PONG":
            self._status("Tester responded (PONG)")

        elif tag == "VERSION" and len(parts) >= 2:
            if self.current_result:
                self.current_result.fw_version = parts[1]
            self._status(f"Firmware: v{parts[1]}")

        elif tag == "PINS" and len(parts) >= 2 and parts[1].isdigit():
            if self.current_result:
                self.current_result.total_pins = int(parts[1])

        elif tag == "TEST_START" and len(parts) >= 2:
            if self.current_result:
                self.current_result.serial_number = parts[1]
            self._status(f"Scanning… S/N: {parts[1]}")

        elif tag == "CONN" and len(parts) >= 3:
            if not self.in_test:
                return
            j1 = parts[1][3:] if parts[1].startswith("J1.") else parts[1]
            j2 = parts[2][3:] if parts[2].startswith("J2.") else parts[2]
            conn = ConnectionRecord(j1, j2)
            if self.current_result:
                self.current_result.connections.append(conn)
            self._add_conn_row(conn)
            self._set_conn_badge(len(self.tree.get_children()))

        elif tag == "TEST_DONE" and len(parts) >= 2:
            n = int(parts[1]) if parts[1].isdigit() else 0
            self._status(f"Scan complete — {n} connection(s) found")
            self._on_test_done()

        elif tag == "DEBUG_START":
            self._log("[DIAG] ─── Diagnostic started ───")

        elif tag == "DEBUG_DONE":
            self._log("[DIAG] ─── Diagnostic done ───")

        elif tag == "DEBUG" and len(parts) >= 2:
            if parts[1] == "CONN_FOUND":
                n = parts[2] if len(parts) >= 3 else "?"
                self._log(f"[DIAG] Connections found during diag: {n}")
                self._status(f"Diagnostic complete — {n} connection(s) found on J1 pin")
            else:
                self._log(f"[DIAG] {','.join(parts[1:])}")

        elif tag == "ZA_LAT" and len(parts) >= 2:
            if parts[1] == "0":
                self._log("[DIAG] ZA_LAT=0 → J1 Z-line LAT is LOW (za_drive_low OK)")
            else:
                self._log("[DIAG] ZA_LAT=1 → J1 Z-line LAT is HIGH (za_drive_low FAILED — MCU pin stuck?)")

        elif tag == "U19_OUT0" and len(parts) >= 2:
            v = parts[1]
            if v == "112":   # 0x70 = MUX1 enabled, channel 0
                self._log(f"[DIAG] U19_OUT0={v} (0x70) → I2C write to U19 OK, MUX1-A enabled ch=0")
            elif v == "255":  # 0xFF = unchanged = write failed
                self._log(f"[DIAG] U19_OUT0={v} (0xFF) → *** I2C WRITE TO U19 FAILED — chip not responding! ***")
            else:
                self._log(f"[DIAG] U19_OUT0={v} (unexpected value)")

        elif tag == "U20_OUT0" and len(parts) >= 2:
            v = parts[1]
            if v == "112":
                self._log(f"[DIAG] U20_OUT0={v} (0x70) → I2C write to U20 OK, MUX1-B enabled ch=0")
            elif v == "255":
                self._log(f"[DIAG] U20_OUT0={v} (0xFF) → *** I2C WRITE TO U20 FAILED — chip not responding! ***")
            else:
                self._log(f"[DIAG] U20_OUT0={v} (unexpected value)")

        elif tag == "DIAG_CONN" and len(parts) >= 3:
            self._log(f"[DIAG] CONNECTION FOUND: J1.{parts[1]} ↔ J2.{parts[2]}")

        elif tag == "I2C_SCAN_START":
            self._log("[I2C] ─── I2C bus scan started ───")

        elif tag == "I2C_SCAN_DONE":
            self._log("[I2C] ─── I2C bus scan done ───")
            self._status("I2C scan complete — check log for results")

        elif tag == "BUS1":
            self._log("[I2C] ── Bus 1 (I2C1 hardware — U19, SCL1=RF3, SDA1=RF2) ──")

        elif tag == "BUS2":
            self._log("[I2C] ── Bus 2 (I2C2 software — U20, SCL2=RD0, SDA2=RF6) ──")

        elif tag == "I2C1_PROBE" and len(parts) >= 3:
            addr_str = parts[1]
            result   = parts[2]
            try:
                addr_int = int(addr_str, 16)
            except ValueError:
                addr_int = 0
            if result == "ACK":
                note = " ← U19 (J1 mux) — OK" if addr_int == 0x20 else f" ← unexpected chip at {addr_str}"
                self._log(f"[I2C1] {addr_str} : ACK  ✓{note}")
            else:
                note = " ← U19 MISSING!" if addr_int == 0x20 else ""
                self._log(f"[I2C1] {addr_str} : NACK  —{note}")

        elif tag == "I2C2_PROBE" and len(parts) >= 3:
            addr_str = parts[1]
            result   = parts[2]
            try:
                addr_int = int(addr_str, 16)
            except ValueError:
                addr_int = 0
            if result == "ACK":
                note = " ← U20 (J2 mux) — OK" if addr_int == 0x24 else f" ← unexpected chip at {addr_str}"
                self._log(f"[I2C2] {addr_str} : ACK  ✓{note}")
            else:
                note = " ← U20 MISSING!" if addr_int == 0x24 else ""
                self._log(f"[I2C2] {addr_str} : NACK  —{note}")

        elif tag == "I2C_PROBE" and len(parts) >= 3:
            # backward compat with old firmware (before I2C2 was split out)
            addr_str = parts[1]
            result   = parts[2]
            try:
                addr_int = int(addr_str, 16)
            except ValueError:
                addr_int = 0
            if result == "ACK":
                if addr_int == 0x20:   note = " ← U19 (J1 mux) — OK"
                elif addr_int == 0x24: note = " ← U20 (J2 mux) — FOUND at 0x24 (A2=1)"
                else:                  note = f" ← chip at {addr_str}"
                self._log(f"[I2C] {addr_str} : ACK  ✓{note}")
            else:
                if addr_int == 0x20: note = " ← U19 MISSING!"
                else:                note = ""
                self._log(f"[I2C] {addr_str} : NACK  —{note}")

        # ── Jig self-diagnostic (command J) ──────────────────────────────────
        elif tag == "JIG_DIAG_START":
            self._jig_fail_pins   = []
            self._jig_ok_count    = 0
            self._sweep_hit_count = 0
            self._sweep_mi        = None
            self._log("[JIG] ══════════════════════════════════════")
            self._log("[JIG] ▶  ALL-PIN SCAN  (116 J1 pins)")
            self._log("[JIG] ══════════════════════════════════════")

        elif tag == "DIAG_OK" and len(parts) >= 2:
            # DIAG_OK,<pin>,conn=<n>  — collect data, update counter silently
            pin   = parts[1]
            n_str = parts[2].replace("conn=", "") if len(parts) >= 3 else "0"
            n_val = int(n_str) if n_str.isdigit() else 0
            self._jig_results_all.append(
                JigPinResult(pin_name=pin, status="OK", conn_count=n_val))
            self._jig_ok_count += 1
            scanned = self._jig_ok_count + len(self._jig_fail_pins)
            self._status(f"Jig diagnostic: scanned {scanned}/{TOTAL_PINS}…")

        elif tag == "DIAG_FAIL" and len(parts) >= 2:
            # DIAG_FAIL,<pin>,mi=<m>,ch=<c>,LAT=<l>,OUT0=<x>,OUT1=<y>
            pin  = parts[1]
            self._jig_fail_pins.append(pin)
            info  = {k.split("=")[0]: k.split("=")[1]
                     for k in parts[2:] if "=" in k}
            mi_s   = info.get("mi",  "-1")
            ch_s   = info.get("ch",  "-1")
            lat_s  = info.get("LAT", "-1")
            out0_s = info.get("OUT0","-1")
            out1_s = info.get("OUT1","-1")
            def _toint(s):
                return int(s) if s.lstrip("-").isdigit() else -1
            mi_i, ch_i, lat_i = _toint(mi_s), _toint(ch_s), _toint(lat_s)
            out0_i, out1_i    = _toint(out0_s), _toint(out1_s)
            # Check I2C_ERR token (firmware outputs ",I2C_ERR" instead of OUT0/OUT1)
            has_i2c_err = any("I2C_ERR" in p for p in parts[2:])
            # Store structured result for analysis
            self._jig_results_all.append(JigPinResult(
                pin_name=pin, status="FAIL",
                mi=mi_i, ch=ch_i, lat=lat_i,
                out0=out0_i, out1=out1_i,
                i2c_err=has_i2c_err
            ))
            lat_str  = "LOW ✓" if lat_s == "0" else "HIGH ✗"
            out0_hex = f"(0x{out0_i:02X})" if out0_i >= 0 else ""
            out1_hex = f"(0x{out1_i:02X})" if out1_i >= 0 else ""
            self._log(f"[JIG] ✗  {pin:<4}  mi={mi_s} ch={ch_s}"
                      f"  LAT={lat_s}={lat_str}"
                      f"  OUT0={out0_s}{out0_hex}"
                      f"  OUT1={out1_s}{out1_hex}")
            if lat_s == "1":
                self._log(f"[JIG]     ⚠ Z-line drive FAIL (mi={mi_s}) — MCU port stuck?")
            if out0_s == "255" or out1_s == "255":
                self._log(f"[JIG]     ⚠ OUT=255 → I2C write FAILED (U19 not responding)")

        elif tag == "SWEEP_START" and len(parts) >= 2:
            mi_val = parts[1].replace("mi=", "")
            self._sweep_mi        = mi_val
            self._sweep_hit_count = 0
            # Initialize entry so empty sweep ≠ "never swept"
            self._sweep_data.setdefault(mi_val, set())
            self._log(f"[JIG]     ─── Sweep MUX group {mi_val} (16 ch) ───────────────")

        elif tag == "SWEEP_HIT" and len(parts) >= 3:
            self._sweep_hit_count += 1
            ch_val = parts[1].replace("ch=", "")
            j2_val = parts[2].replace("J2=", "")
            # Track which channels produced a hit in this group
            if self._sweep_mi is not None and ch_val.isdigit():
                self._sweep_data.setdefault(self._sweep_mi, set()).add(int(ch_val))
            self._log(f"[JIG]         ch={ch_val:>2} → J2.{j2_val}")

        elif tag == "SWEEP_END" and len(parts) >= 2:
            mi_val = parts[1].replace("mi=", "")
            n_hit  = self._sweep_hit_count
            if n_hit == 0:
                self._log(f"[JIG]     ─── group {mi_val}: 0 hits → IC dead or Z-line broken")
            else:
                self._log(f"[JIG]     ─── group {mi_val}: {n_hit} ch OK, rest dead → trace broken")
            self._sweep_mi        = None
            self._sweep_hit_count = 0

        # ── J2 pass tags ─────────────────────────────────────────────────────
        elif tag == "JIG_DIAG_J2_START":
            self._jig_j2_fail_pins   = []
            self._jig_j2_ok_count    = 0
            self._sweep_j2_hit_count = 0
            self._sweep_j2_mi        = None
            self._log("[JIG] ══════════════════════════════════════")
            self._log("[JIG] ▶  PASS 2 — J2 SIDE SCAN  (116 J2 pins)")
            self._log("[JIG] ══════════════════════════════════════")

        elif tag == "DIAG_J2_OK" and len(parts) >= 2:
            pin   = parts[1]
            n_str = parts[2].replace("conn=", "") if len(parts) >= 3 else "0"
            n_val = int(n_str) if n_str.isdigit() else 0
            self._jig_j2_results_all.append(
                JigPinResult(pin_name=pin, status="OK", conn_count=n_val))
            self._jig_j2_ok_count += 1
            scanned = self._jig_j2_ok_count + len(self._jig_j2_fail_pins)
            self._status(f"Jig diagnostic P2: scanned {scanned}/{TOTAL_PINS}…")

        elif tag == "DIAG_J2_FAIL" and len(parts) >= 2:
            # DIAG_J2_FAIL,<pin>,mi=<m>,ch=<c>,LAT=<l>,OUT0=<x>,OUT1=<y>
            pin  = parts[1]
            self._jig_j2_fail_pins.append(pin)
            info  = {k.split("=")[0]: k.split("=")[1]
                     for k in parts[2:] if "=" in k}
            mi_s   = info.get("mi",  "-1")
            ch_s   = info.get("ch",  "-1")
            lat_s  = info.get("LAT", "-1")
            out0_s = info.get("OUT0","-1")
            out1_s = info.get("OUT1","-1")
            def _toint2(s):
                return int(s) if s.lstrip("-").isdigit() else -1
            mi_i, ch_i, lat_i = _toint2(mi_s), _toint2(ch_s), _toint2(lat_s)
            out0_i, out1_i    = _toint2(out0_s), _toint2(out1_s)
            has_i2c_err = any("I2C_ERR" in p for p in parts[2:])
            self._jig_j2_results_all.append(JigPinResult(
                pin_name=pin, status="FAIL",
                mi=mi_i, ch=ch_i, lat=lat_i,
                out0=out0_i, out1=out1_i,
                i2c_err=has_i2c_err
            ))
            lat_str  = "LOW ✓" if lat_s == "0" else "HIGH ✗"
            out0_hex = f"(0x{out0_i:02X})" if out0_i >= 0 else ""
            out1_hex = f"(0x{out1_i:02X})" if out1_i >= 0 else ""
            self._log(f"[J2]  ✗  {pin:<4}  mi={mi_s} ch={ch_s}"
                      f"  LAT={lat_s}={lat_str}"
                      f"  OUT0={out0_s}{out0_hex}"
                      f"  OUT1={out1_s}{out1_hex}")

        elif tag == "SWEEP_J2_START" and len(parts) >= 2:
            mi_val = parts[1].replace("mi=", "")
            self._sweep_j2_mi        = mi_val
            self._sweep_j2_hit_count = 0
            self._sweep_j2_data.setdefault(mi_val, set())
            self._log(f"[J2]      ─── Sweep J2 MUX group {mi_val} (16 ch) ───────────")

        elif tag == "SWEEP_J2_HIT" and len(parts) >= 3:
            self._sweep_j2_hit_count += 1
            ch_val = parts[1].replace("ch=", "")
            j1_val = parts[2].replace("J1=", "")
            if self._sweep_j2_mi is not None and ch_val.isdigit():
                self._sweep_j2_data.setdefault(self._sweep_j2_mi, set()).add(int(ch_val))
            self._log(f"[J2]          ch={ch_val:>2} → J1.{j1_val}")

        elif tag == "SWEEP_J2_END" and len(parts) >= 2:
            mi_val = parts[1].replace("mi=", "")
            n_hit  = self._sweep_j2_hit_count
            if n_hit == 0:
                self._log(f"[J2]      ─── group {mi_val}: 0 hits → IC dead or Z-line broken")
            else:
                self._log(f"[J2]      ─── group {mi_val}: {n_hit} ch OK, rest dead → trace broken")
            self._sweep_j2_mi        = None
            self._sweep_j2_hit_count = 0

        elif tag == "JIG_DIAG_DONE":
            # Parse j1=N,j2=M  (or legacy fail=N for backward compat)
            j1_n = 0;  j2_n = 0
            for p in parts[1:]:
                if p.startswith("j1="):
                    try: j1_n = int(p[3:])
                    except ValueError: pass
                elif p.startswith("j2="):
                    try: j2_n = int(p[3:])
                    except ValueError: pass
                elif p.startswith("fail="):       # legacy firmware
                    try: j1_n = int(p[5:])
                    except ValueError: pass
            fail_n = j1_n + j2_n

            # ── Root cause analysis ────────────────────────────────────────
            self._analyze_jig_results()
            self._analyze_jig_j2_results()

            # ── Log summary ────────────────────────────────────────────────
            self._log("[JIG] ══════════════════════════════════════")
            if j1_n == 0:
                self._log("[JIG] ✓  J1 ALL CLEAR — 116/116 pins OK")
            else:
                self._log(f"[JIG] ✗  J1: {j1_n} pin(s) FAILED — ROOT CAUSE:")
                for r in self._jig_results_all:
                    if r.status == "FAIL":
                        self._log(f"[JIG]   • J1.{r.pin_name:<4}: {r.root_cause}")
                        self._log(f"[JIG]          → {r.recommendation}")
            if j2_n == 0:
                self._log("[JIG] ✓  J2 ALL CLEAR — 116/116 pins OK")
            else:
                self._log(f"[JIG] ✗  J2: {j2_n} pin(s) FAILED — ROOT CAUSE:")
                for r in self._jig_j2_results_all:
                    if r.status == "FAIL":
                        self._log(f"[JIG]   • J2.{r.pin_name:<4}: {r.root_cause}")
                        self._log(f"[JIG]          → {r.recommendation}")
            self._log("[JIG] ══════════════════════════════════════")
            if fail_n == 0:
                self._status("Jig diagnostic: J1+J2 all OK ✓")
            else:
                self._status(f"Jig diagnostic: J1={j1_n} fail, J2={j2_n} fail")

            # ── Enable buttons ─────────────────────────────────────────────
            if hasattr(self, "btn_jig_diag"):
                self.btn_jig_diag.config(state="normal")
            if hasattr(self, "btn_jig_export"):
                self.btn_jig_export.config(state="normal", fg="white")

            # ── Popup ──────────────────────────────────────────────────────
            if fail_n == 0:
                messagebox.showinfo("Jig Self-Diagnostic",
                                    "✓  All 116 J1 pins + 116 J2 pins OK!\n"
                                    "Jig hardware is completely normal.")
            else:
                rows_j1 = [r for r in self._jig_results_all       if r.status == "FAIL"]
                rows_j2 = [r for r in self._jig_j2_results_all    if r.status == "FAIL"]
                detail_j1 = "\n".join(
                    f"  ✗ J1.{r.pin_name}: {r.root_cause}" for r in rows_j1)
                detail_j2 = "\n".join(
                    f"  ✗ J2.{r.pin_name}: {r.root_cause}" for r in rows_j2)
                body = ""
                if rows_j1:
                    body += f"J1 ({j1_n} pin):\n{detail_j1}\n\n"
                if rows_j2:
                    body += f"J2 ({j2_n} pin):\n{detail_j2}\n\n"
                messagebox.showwarning(
                    "Jig Self-Diagnostic — Root Cause Analysis",
                    f"Total {fail_n} pin(s) failed:\n\n{body}"
                    "Click 'Export Jig Report' to save the full Excel report."
                )

        elif tag == "J2G0_START":
            self._j2g0_rows = []
            self._log("[J2G0] ══ J2 MUX-B group-0 sweep (rp 0..15) ══")

        elif tag == "J2G0":
            # J2G0,rp=<n>,J1.<pin>  or  J2G0,rp=<n>,NO_CONN
            rp_str = parts[1].replace("rp=", "") if len(parts) >= 2 else "?"
            j1_pin = parts[2].replace("J1.", "") if len(parts) >= 3 else "NO_CONN"
            if j1_pin == "NO_CONN":
                self._log(f"[J2G0] rp={rp_str:>2} → NO_CONN")
            else:
                self._log(f"[J2G0] rp={rp_str:>2} → J1.{j1_pin}")
            self._j2g0_rows.append((rp_str, j1_pin))

        elif tag == "J2G0_END":
            connected = [(rp, j1) for rp, j1 in self._j2g0_rows if j1 != "NO_CONN"]
            self._log(f"[J2G0] {len(connected)}/16 channels connected to J1")
            self._log("[J2G0] ══════════════════════════════")
            if hasattr(self, "btn_j2g0"):
                self.btn_j2g0.config(state="normal")

        elif tag == "FDIAG_START":
            self._fdiag_rows = []
            self._log("[FDIAG] ══ F-row even-pin debug ══")

        elif tag == "FDIAG":
            # FDIAG,J1.F2,rp=33,J2.F2   or   FDIAG,J1.F2,NO_CONN
            if len(parts) >= 3:
                j1_pin = parts[1].replace("J1.", "")
                if parts[2] == "NO_CONN":
                    self._log(f"[FDIAG] J1.{j1_pin:>3} → NO_CONN  ✗")
                    self._fdiag_rows.append((j1_pin, None, None, "FAIL"))
                else:
                    rp_str  = parts[2].replace("rp=", "")
                    j2_pin  = parts[3].replace("J2.", "") if len(parts) >= 4 else "?"
                    self._log(f"[FDIAG] J1.{j1_pin:>3} → J2.{j2_pin:<4}  (MUX-B rp={rp_str})")
                    self._fdiag_rows.append((j1_pin, j2_pin, rp_str, "OK"))

        elif tag == "FDIAG_END":
            ok  = sum(1 for r in self._fdiag_rows if r[3] == "OK")
            nok = sum(1 for r in self._fdiag_rows if r[3] == "FAIL")
            self._log(f"[FDIAG] Result: {ok} OK, {nok} NO_CONN")
            self._log("[FDIAG] ══════════════════════════════")
            if nok:
                fails = ", ".join(f"J1.F{r[0]}" for r in self._fdiag_rows if r[3]=="FAIL")
                messagebox.showwarning("F-Row Debug",
                    f"{nok} pin(s) with no J2 connection:\n{fails}\n\n"
                    "Check PCB J2 MUX-B group 0 (rp=0..15).")
            if hasattr(self, "btn_fdiag"):
                self.btn_fdiag.config(state="normal")

        elif tag == "JIG_DIAG_I2C_ERR":
            drv = parts[1].replace("drv=", "") if len(parts) >= 2 else "?"
            self._log(f"[JIG] ⚠  I2C error J1 (drv={drv}) — pin skipped")

        elif tag == "JIG_DIAG_J2_I2C_ERR":
            drv = parts[1].replace("drv=", "") if len(parts) >= 2 else "?"
            self._log(f"[JIG] ⚠  I2C error J2 (drv={drv}) — pin skipped")

        elif tag == "SWEEP_I2C_ERR":
            ch = parts[1].replace("ch=", "") if len(parts) >= 2 else "?"
            self._log(f"[JIG] ⚠  I2C error in sweep J1 (ch={ch}) — sweep stopped")
            if hasattr(self, "btn_jig_diag"):
                self.btn_jig_diag.config(state="normal")

        elif tag == "SWEEP_J2_I2C_ERR":
            ch = parts[1].replace("ch=", "") if len(parts) >= 2 else "?"
            self._log(f"[JIG] ⚠  I2C error in sweep J2 (ch={ch}) — sweep stopped")
            if hasattr(self, "btn_jig_diag"):
                self.btn_jig_diag.config(state="normal")

        elif tag == "ERROR":
            err_code = parts[1] if len(parts) >= 2 else "UNKNOWN"
            self._log(f"=== MCU ERROR: {err_code} ===")
            if self.in_test:
                self.test_stopped = True
                self.in_test = False
                self.progress.stop()
                self.progress.config(mode="determinate")
                self.progress_var.set(0)
                self.result_frame.config(bg=CLR_FAIL)
                self.lbl_result.config(text=f"⚠  ERROR: {err_code}", bg=CLR_FAIL)
                self.btn_test.config(state="normal")
                self.btn_stop.config(state="disabled", bg="#7F8C8D")
                msg = {
                    "I2C_TIMEOUT":   "I2C bus is locked (SDA stuck low).\n"
                                     "Power-cycle the tester and check the\n"
                                     "PCA9555 expanders (U19 on I2C1, U20 on I2C2).",
                    "I2C_INIT_FAIL": "I2C bus failed during startup.\n"
                                     "Check PCA9555 power and wiring:\n"
                                     "U19: I2C1 (RF3=SCL, RF2=SDA)\n"
                                     "U20: I2C2 soft (RD0=SCL2, RF6=SDA2).",
                }.get(err_code, f"MCU reported error: {err_code}")
                messagebox.showerror("MCU Hardware Error", msg)
            else:
                self._status(f"MCU error: {err_code}")

    def _add_conn_row(self, conn: ConnectionRecord):
        j1, j2 = conn.j1_pin, conn.j2_pin
        prof = self.active_profile or DEFAULT_PROFILE
        _g1, _g2, _exp = prof.gnd_j1(), prof.gnd_j2(), prof.signal_map()

        # GND/bus net — collapse to one summary row
        if j1 in _g1 or j2 in _g2:
            self._gnd_count += 1
            if self._gnd_tree_id is None:
                idx = len(self.tree.get_children()) + 1
                self._gnd_tree_id = self.tree.insert(
                    "", "end",
                    values=(idx, "GND bus", f"GND — {self._gnd_count} connection(s)"),
                    tags=("gnd",))
            else:
                vals = self.tree.item(self._gnd_tree_id, "values")
                self.tree.item(self._gnd_tree_id,
                               values=(vals[0], "GND bus",
                                       f"GND — {self._gnd_count} connection(s)"))
            return

        # Signal connection — validate
        idx = len(self.tree.get_children()) + 1
        expected_j2 = _exp.get(j1)
        if expected_j2 is None:
            tag_name = "unexpected"
            j2_disp  = f"J2.{j2}  ⚠ unexpected"
        elif j2 == expected_j2:
            tag_name = "odd" if idx % 2 else "even"
            j2_disp  = f"J2.{j2}  ✓"
        else:
            tag_name = "miswire"
            j2_disp  = f"J2.{j2}  ✗ expected J2.{expected_j2}"
        self.tree.insert("", "end",
                          values=(idx, f"J1.{j1}", j2_disp),
                          tags=(tag_name,))

    def _on_test_done(self):
        if self.test_stopped:
            return

        self.in_test = False
        self.progress.stop()
        self.progress.config(mode="determinate")
        self.progress_var.set(100)

        if self.current_result:
            self.current_result.completed = True
            if self.test_start_time:
                self.current_result.duration_s = time.time() - self.test_start_time
            n = len(self.current_result.connections)
            self.history.append(self.current_result)
            self._save_history()
            self._update_stats()
            self._finish_with_profile(n)
        else:
            self.btn_test.config(state="normal")
            self.btn_stop.config(state="disabled", bg="#7F8C8D")

    def _increment_sn(self):
        sn = self.var_sn.get()
        try:
            prefix = sn.rstrip("0123456789")
            num    = int(sn[len(prefix):]) + 1
            self.var_sn.set(f"{prefix}{num:06d}")
        except Exception:
            pass

    # -- Cable profile management ------------------------------------------
    def _refresh_profiles(self):
        self._profile_paths = list_profiles()
        names = [DEFAULT_PROFILE_NAME] + list(self._profile_paths.keys())
        self.cb_profile["values"] = names
        if self.var_profile.get() not in names:
            self.var_profile.set(DEFAULT_PROFILE_NAME)
            self.active_profile = DEFAULT_PROFILE

    def _on_profile_select(self, event=None):
        name = self.var_profile.get()
        if name == DEFAULT_PROFILE_NAME or name not in self._profile_paths:
            self.active_profile = DEFAULT_PROFILE
            self.lbl_profile_info.config(
                text=f"{DEFAULT_PROFILE_NAME}: {len(DEFAULT_PROFILE.signal_map())} signal + "
                     f"GND bus ({len(DEFAULT_PROFILE.gnd_j1())}/{len(DEFAULT_PROFILE.gnd_j2())})")
            return
        try:
            prof = load_profile(self._profile_paths[name])
            self.active_profile = prof
            self.lbl_profile_info.config(
                text=f"{prof.name}: {len(prof.edges)} expected connections, "
                     f"{prof.total_pins} pins")
            self._status(f"Cable profile: {prof.name}")
        except Exception as e:
            messagebox.showerror("Profile", f"Could not load profile: {e}")
            self.active_profile = DEFAULT_PROFILE

    def _learn_profile(self):
        from tkinter import simpledialog
        if not self.current_result or not self.current_result.connections:
            messagebox.showinfo("Learn cable",
                                "Run a test with a known-good cable first.")
            return
        name = simpledialog.askstring(
            "Learn cable", "Cable type name (profile):",
            initialvalue=f"Cable_{datetime.now():%Y%m%d_%H%M}")
        if not name:
            return
        prof = CableProfile.from_connections(
            name, self.current_result.connections,
            notes=f"Learned from S/N {self.current_result.serial_number}")
        prof.total_pins = self.current_result.total_pins or TOTAL_PINS
        try:
            p = save_profile(prof)
        except Exception as e:
            messagebox.showerror("Learn cable", f"Save failed: {e}")
            return
        self._refresh_profiles()
        self.var_profile.set(prof.name)
        self._on_profile_select()
        self._log(f"[PROFILE] Saved '{prof.name}' ({len(prof.edges)} connections) -> {p}")
        messagebox.showinfo(
            "Learn cable",
            f"Profile '{prof.name}' saved\n{len(prof.edges)} expected connections.\n\n"
            "Select this profile next time to test this cable type.")

    def _finish_with_profile(self, n):
        prof = self.active_profile
        measured = {(c.j1_pin, c.j2_pin) for c in self.current_result.connections}
        res = prof.compare(measured)
        self._last_profile_result = res
        opens, extras = res["opens"], res["extras"]
        if res["verdict"] == "PASS":
            self.result_frame.config(bg=CLR_PASS)
            self.lbl_result.config(
                text=f"PASS - {prof.name} - {res['n_ok']}/{res['n_expected']} connections",
                bg=CLR_PASS)
            self._status(f"PASS [{prof.name}] - {res['n_ok']}/{res['n_expected']} OK")
        else:
            issues = len(opens) + len(extras)
            self.result_frame.config(bg=CLR_FAIL)
            self.lbl_result.config(
                text=f"FAIL - {issues} fault(s) ({len(opens)} OPEN, {len(extras)} SHORT/wrong)",
                bg=CLR_FAIL)
            self._status(f"FAIL [{prof.name}] - {len(opens)} OPEN, {len(extras)} SHORT/wrong")
            if opens:
                self._log("[RESULT] OPEN (" + str(len(opens)) + "): "
                          + ", ".join(f"J1.{a}<->J2.{b}" for a, b in opens[:12])
                          + (" ..." if len(opens) > 12 else ""))
            if extras:
                self._log("[RESULT] SHORT/WRONG (" + str(len(extras)) + "): "
                          + ", ".join(f"J1.{a}<->J2.{b}" for a, b in extras[:12])
                          + (" ..." if len(extras) > 12 else ""))
        self.btn_test.config(state="normal")
        self.btn_stop.config(state="disabled", bg="#7F8C8D")
        self.btn_export.config(state="normal" if EXCEL_OK else "disabled")
        if hasattr(self, "btn_learn"):
            self.btn_learn.config(state="normal")
        self._increment_sn()

    def _export_profile_report(self):
        if not EXCEL_OK:
            messagebox.showerror("Missing Library", "openpyxl is not installed.")
            return
        if not self.current_result or self._last_profile_result is None:
            messagebox.showinfo("No Data", "No profile test result yet.")
            return
        from openpyxl import Workbook
        res = self._last_profile_result
        prof = self.active_profile
        thinb = Side(style="thin", color="BFBFBF")
        bd = Border(left=thinb, right=thinb, top=thinb, bottom=thinb)
        def FNT(sz=10, b=False, c="000000"):
            return Font(name="Arial", size=sz, bold=b, color=c)
        def FILL(c):
            return PatternFill("solid", fgColor=c)
        AL = Alignment(horizontal="center", vertical="center")
        wb = Workbook()
        ws = wb.active; ws.title = "Summary"; ws.sheet_view.showGridLines = False
        verdict = res["verdict"]; vclr = "1E8449" if verdict == "PASS" else "922B21"
        ws.merge_cells("A1:D1")
        ws["A1"] = f"CABLE TEST - {prof.name}"
        ws["A1"].font = FNT(15, True, "FFFFFF"); ws["A1"].fill = FILL("1F3864")
        ws["A1"].alignment = AL; ws.row_dimensions[1].height = 30
        info = [("Serial Number", self.current_result.serial_number),
                ("Profile", prof.name),
                ("Date", self.current_result.timestamp.strftime("%Y-%m-%d %H:%M:%S")),
                ("COM Port", self.current_result.com_port),
                ("Expected connections", res["n_expected"]),
                ("Measured connections", res["n_measured"]),
                ("Matched", res["n_ok"]),
                ("OPEN (broken)", len(res["opens"])),
                ("SHORT/wrong", len(res["extras"])),
                ("VERDICT", verdict)]
        r = 3
        for k, v in info:
            ws[f"A{r}"] = k; ws[f"A{r}"].font = FNT(10, True); ws[f"A{r}"].border = bd
            ws.merge_cells(f"B{r}:D{r}")
            cb = ws[f"B{r}"]; cb.value = v; cb.border = bd
            if k == "VERDICT":
                cb.fill = FILL(vclr); cb.font = FNT(11, True, "FFFFFF")
            else:
                cb.font = FNT(10)
            r += 1
        ws.column_dimensions["A"].width = 20
        for col in "BCD":
            ws.column_dimensions[col].width = 16
        wf = wb.create_sheet("Faults"); wf.sheet_view.showGridLines = False
        for ci, h in enumerate(["#", "Fault Type", "J1 Pin", "J2 Pin"], 1):
            c = wf.cell(1, ci, h); c.font = FNT(10, True, "FFFFFF")
            c.fill = FILL("2E75B6"); c.alignment = AL; c.border = bd
        rr = 2
        for a, b in res["opens"]:
            for ci, v in enumerate([rr - 1, "OPEN (broken)", f"J1.{a}", f"J2.{b}"], 1):
                c = wf.cell(rr, ci, v); c.font = FNT(10); c.fill = FILL("FDEDEC")
                c.alignment = AL; c.border = bd
            rr += 1
        for a, b in res["extras"]:
            for ci, v in enumerate([rr - 1, "SHORT/WRONG", f"J1.{a}", f"J2.{b}"], 1):
                c = wf.cell(rr, ci, v); c.font = FNT(10); c.fill = FILL("F3E5F5")
                c.alignment = AL; c.border = bd
            rr += 1
        if rr == 2:
            wf.merge_cells("A2:D2"); wf["A2"] = "No faults - PASS"
            wf["A2"].font = FNT(11, True, "1E8449"); wf["A2"].alignment = AL
        for col, w in zip("ABCD", [6, 16, 12, 12]):
            wf.column_dimensions[col].width = w
        wn = wb.create_sheet("Measured Netlist"); wn.sheet_view.showGridLines = False
        for ci, h in enumerate(["#", "J1 Pin", "J2 Pin", "Status"], 1):
            c = wn.cell(1, ci, h); c.font = FNT(10, True, "FFFFFF")
            c.fill = FILL("2E75B6"); c.alignment = AL; c.border = bd
        measured = sorted({(c.j1_pin, c.j2_pin) for c in self.current_result.connections})
        exp = set(prof.edges)
        for i, (a, b) in enumerate(measured, 1):
            st = "OK" if (a, b) in exp else "EXTRA/WRONG"
            for ci, v in enumerate([i, f"J1.{a}", f"J2.{b}", st], 1):
                c = wn.cell(i + 1, ci, v); c.font = FNT(9); c.alignment = AL; c.border = bd
                c.fill = FILL("EAF7EE" if st == "OK" else "F3E5F5")
        for col, w in zip("ABCD", [6, 12, 12, 12]):
            wn.column_dimensions[col].width = w
        reports_dir = Path("reports"); reports_dir.mkdir(exist_ok=True)
        ts = self.current_result.timestamp.strftime("%Y%m%d_%H%M%S")
        snm = self.current_result.serial_number.replace("/", "_")
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in prof.name)
        out = reports_dir / f"cable_{safe}_{snm}_{ts}.xlsx"
        try:
            wb.save(str(out))
        except Exception as e:
            messagebox.showerror("Export Error", str(e)); return
        self._log(f"[PROFILE] Report saved: {out}")
        if messagebox.askyesno("Saved", f"Saved: {out.name}\n\nOpen now?"):
            if sys.platform == "win32":
                os.startfile(str(out))
            else:
                os.system(f"xdg-open '{out}'")

    def _update_stats(self):
        total = len(self.history)
        done  = sum(1 for r in self.history if r.completed)
        self.lbl_stats.config(
            text=f"Scans: {total}  |  Completed: {done}")

    # ── Jig Diagnostic Analysis & Export ─────────────────────────────────────
    def _analyze_jig_results(self):
        """Analyze the root cause for each FAILED pin from sweep data.

        Priority order:
          1. i2c_err  → firmware reports I2C error reading the register
          2. lat==1   → MCU cannot drive the Z-line LOW
          3. OUT0/OUT1 != expected → I2C write not registered
          4. sweep data → broken trace / dead IC / bad pogo

        IMPORTANT NOTE:
          OUT1=0xFF (255) is the CORRECT value for IO0-based groups (mi=0,1,4,5)
          because mux_a() always sets u19_io1=0xFF for these groups.
          Do NOT use OUT1==255 as an I2C-error indicator.
        """
        EN_IO0 = {0: 0x80, 1: 0x20, 4: 0x40, 5: 0x10}  # IO0-based groups
        EN_IO1 = {2: 0x80, 3: 0x20, 6: 0x40, 7: 0x10}  # IO1-based groups

        def expected_regs(mi, ch):
            """Expected OUT0/OUT1 values after a successful mux_a(mi, ch)."""
            ch = ch & 0x0F
            if mi in EN_IO0:
                return (0xF0 | ch) & (~EN_IO0[mi] & 0xFF), 0xFF
            if mi in EN_IO1:
                return 0xF0 | ch, (~EN_IO1[mi]) & 0xFF
            return -1, -1

        for r in self._jig_results_all:
            if r.status == "OK":
                r.root_cause     = "Normal"
                r.recommendation = "—"
                continue

            # ── 1. I2C error explicitly reported by firmware ──────────────────
            if r.i2c_err:
                r.root_cause = "I2C error reading back register U19"
                r.recommendation = ("Check PCA9555 U19: VCC power, "
                                    "I2C address (0x20), SDA/SCL wiring (RF2/RF3)")

            # ── 2. MCU cannot drive the Z-line ─────────────────────────────
            elif r.lat == 1:
                r.root_cause = "MCU Z-line drive failed (LAT=HIGH)"
                r.recommendation = (f"Check MCU port pin (group mi={r.mi}): "
                                    "dsPIC RE/RF shorted or firmware error")

            # ── 3. Not enough data to analyze ───────────────────────
            elif r.mi < 0 or r.out0 < 0:
                r.root_cause     = "Not enough data to analyze"
                r.recommendation = "Re-run Jig Diagnostic"

            else:
                # ── 4. Compare actual OUT0/OUT1 vs expected ──────
                exp0, exp1 = expected_regs(r.mi, r.ch)
                regs_match = (exp0 < 0) or (r.out0 == exp0 and r.out1 == exp1)

                if not regs_match:
                    r.root_cause = (
                        f"Wrong MUX register after mux_a({r.mi},{r.ch}): "
                        f"OUT0={r.out0} (need {exp0}), "
                        f"OUT1={r.out1} (need {exp1})"
                    )
                    r.recommendation = ("Check U19 I2C: VCC power, I2C address; "
                                        "try increasing I2C clock delay in firmware")

                else:
                    # ── 5. Drive + MUX config OK → jig hardware ──────────
                    mi_key = str(r.mi)
                    sweep  = self._sweep_data.get(mi_key, None)

                    if sweep is None:
                        r.root_cause     = ("No continuity "
                                            "— no sweep data yet")
                        r.recommendation = "Re-run Jig Diagnostic"

                    elif len(sweep) == 0:
                        r.root_cause = (
                            f"Entire MUX group {r.mi} not working "
                            f"(0/16 channels have continuity)"
                        )
                        r.recommendation = (
                            f"Check MUX IC group {r.mi}: "
                            f"(1) IC VCC power, "
                            f"(2) EN pin (active LOW from U19), "
                            f"(3) Z-line on PCB"
                        )

                    elif r.ch not in sweep:
                        working = sorted(sweep)
                        r.root_cause = (
                            f"Broken PCB trace: MUX group {r.mi} "
                            f"output Y{r.ch} → pogo pin J1.{r.pin_name}"
                        )
                        r.recommendation = (
                            f"Repair/check PCB trace from MUX group {r.mi} "
                            f"output Y{r.ch} to pogo J1.{r.pin_name}  "
                            f"(ch {working} in the same group still OK)"
                        )

                    else:
                        r.root_cause = (
                            f"Pogo pin J1.{r.pin_name} poor contact "
                            f"(ch={r.ch} had signal in sweep)"
                        )
                        r.recommendation = (
                            f"Check/replace pogo pin J1.{r.pin_name}; "
                            f"or increase settle delay "
                            f"(delay_us 200 → 500)"
                        )

    def _analyze_jig_j2_results(self):
        """Analyze the root cause for each FAILED J2 pin (PASS 2).

        Same logic as _analyze_jig_results() but:
          • Data: _jig_j2_results_all + _sweep_j2_data
          • Registers: U20 (address 0x24, same structure as U19)
          • Z-line: PORTB (thay PORTE/PORTF)
          • OUT1=0xFF is correct for IO0-based groups (mi=0,1,4,5) — same rule as U19

        NOTE: expected_regs() uses the same formula since U20 has an identical PCA9555 to U19.
        """
        EN_IO0 = {0: 0x80, 1: 0x20, 4: 0x40, 5: 0x10}
        EN_IO1 = {2: 0x80, 3: 0x20, 6: 0x40, 7: 0x10}

        def expected_regs(mi, ch):
            ch = ch & 0x0F
            if mi in EN_IO0:
                return (0xF0 | ch) & (~EN_IO0[mi] & 0xFF), 0xFF
            if mi in EN_IO1:
                return 0xF0 | ch, (~EN_IO1[mi]) & 0xFF
            return -1, -1

        for r in self._jig_j2_results_all:
            if r.status == "OK":
                r.root_cause     = "Normal"
                r.recommendation = "—"
                continue

            # ── 1. I2C error ────────────────────────────────────────────────
            if r.i2c_err:
                r.root_cause = "I2C2 error reading back register U20"
                r.recommendation = ("Check PCA9555 U20: VCC power, "
                                    "I2C address (0x24), SDA2/SCL2 wiring (RF6/RD0)")

            # ── 2. MCU cannot drive the Z-line ─────────────────────────────
            elif r.lat == 1:
                r.root_cause = "MCU Z-line drive failed (LAT=HIGH)"
                r.recommendation = (f"Check MCU PORTB pin (group mi={r.mi}): "
                                    "dsPIC RB shorted or firmware error")

            # ── 3. Not enough data ─────────────────────────────────────────
            elif r.mi < 0 or r.out0 < 0:
                r.root_cause     = "Not enough data to analyze"
                r.recommendation = "Re-run Jig Diagnostic"

            else:
                # ── 4. Compare actual OUT0/OUT1 vs expected ──────
                exp0, exp1 = expected_regs(r.mi, r.ch)
                regs_match = (exp0 < 0) or (r.out0 == exp0 and r.out1 == exp1)

                if not regs_match:
                    r.root_cause = (
                        f"Wrong MUX register after mux_b({r.mi},{r.ch}): "
                        f"OUT0={r.out0} (need {exp0}), "
                        f"OUT1={r.out1} (need {exp1})"
                    )
                    r.recommendation = ("Check U20 I2C2: VCC power, I2C address (0x24); "
                                        "try increasing delay in software I2C2 (RD0/RF6)")

                else:
                    # ── 5. Drive + MUX config OK → analyze sweep ───────
                    mi_key = str(r.mi)
                    sweep  = self._sweep_j2_data.get(mi_key, None)

                    if sweep is None:
                        r.root_cause     = ("No continuity "
                                            "— no J2 sweep data yet")
                        r.recommendation = "Re-run Jig Diagnostic"

                    elif len(sweep) == 0:
                        r.root_cause = (
                            f"Entire J2 MUX group {r.mi} not working "
                            f"(0/16 channels have continuity)"
                        )
                        r.recommendation = (
                            f"Check B-side MUX IC group {r.mi}: "
                            f"(1) VCC power, "
                            f"(2) EN pin (active LOW from U20), "
                            f"(3) PORTB Z-line on PCB"
                        )

                    elif r.ch not in sweep:
                        working = sorted(sweep)
                        r.root_cause = (
                            f"Broken PCB trace: J2 MUX group {r.mi} "
                            f"output Y{r.ch} → pogo pin J2.{r.pin_name}"
                        )
                        r.recommendation = (
                            f"Repair/check PCB trace from J2 MUX group {r.mi} "
                            f"output Y{r.ch} to pogo J2.{r.pin_name}  "
                            f"(ch {working} in the same group still OK)"
                        )

                    else:
                        r.root_cause = (
                            f"Pogo pin J2.{r.pin_name} poor contact "
                            f"(ch={r.ch} had signal in sweep)"
                        )
                        r.recommendation = (
                            f"Check/replace pogo pin J2.{r.pin_name}; "
                            f"or increase settle delay "
                            f"(delay_us 200 → 500)"
                        )

    def _export_jig_report(self):
        """Export the Jig Self-Diagnostic result to an Excel file (J1 + J2)."""
        if not EXCEL_OK:
            messagebox.showerror("Missing Library",
                                 "openpyxl is not installed.\npip install openpyxl")
            return
        if not self._jig_results_all and not self._jig_j2_results_all:
            messagebox.showinfo("No Data", "Run Jig Self-Diagnostic first.")
            return

        wb = openpyxl.Workbook()

        # Sheet 1 — J1: all 116 pins
        ws1 = wb.active
        ws1.title = "J1 All Pins"
        if self._jig_results_all:
            self._build_jig_sheet_all(ws1, self._jig_results_all, side_label="J1")
        else:
            ws1["A1"] = "No J1 data"

        # Sheet 2 — J1: failed pins only (if any)
        j1_failed = [r for r in self._jig_results_all if r.status == "FAIL"]
        if j1_failed:
            ws2 = wb.create_sheet("J1 Root Cause")
            self._build_jig_sheet_fail(ws2, j1_failed, side_label="J1")

        # Sheet 3 — J2: all 116 pins
        ws3 = wb.create_sheet("J2 All Pins")
        if self._jig_j2_results_all:
            self._build_jig_sheet_all(ws3, self._jig_j2_results_all, side_label="J2")
        else:
            ws3["A1"] = "No J2 data"

        # Sheet 4 — J2: failed pins only (if any)
        j2_failed = [r for r in self._jig_j2_results_all if r.status == "FAIL"]
        if j2_failed:
            ws4 = wb.create_sheet("J2 Root Cause")
            self._build_jig_sheet_fail(ws4, j2_failed, side_label="J2")

        reports_dir = Path("reports")
        reports_dir.mkdir(exist_ok=True)
        ts    = (self._jig_diag_ts or datetime.now()).strftime("%Y%m%d_%H%M%S")
        fname = f"jig_diag_{ts}.xlsx"
        out   = reports_dir / fname

        try:
            wb.save(str(out))
        except Exception as e:
            messagebox.showerror("Export Error", str(e))
            return

        self._log(f"[JIG] 📋  Report saved: reports/{fname}")
        if messagebox.askyesno("Saved", f"Saved:\n{fname}\n\nOpen now?"):
            if sys.platform == "win32":
                os.startfile(str(out))
            else:
                os.system(f"xdg-open '{out}'")

    # ── Excel sheet builders ──────────────────────────────────────────────
    def _jig_thin(self):
        s = Side(style='thin', color="AAAAAA")
        return Border(left=s, right=s, top=s, bottom=s)

    def _jig_fill(self, c):
        return PatternFill("solid", fgColor=c)

    def _build_jig_sheet_all(self, ws, results, side_label="J1"):
        """Sheet 'All Pins' — all 116 pins with status + drive state + analysis."""
        thin = self._jig_thin
        fill = self._jig_fill
        ts   = (self._jig_diag_ts or datetime.now()).strftime("%Y-%m-%d  %H:%M:%S")
        ok_n  = sum(1 for r in results if r.status == "OK")
        fail_n = len(results) - ok_n

        ws.sheet_view.showGridLines = False

        # ── Title ──────────────────────────────────────────────────────────
        ws.merge_cells("A1:K1")
        c = ws["A1"]
        c.value     = f"JIG SELF-DIAGNOSTIC REPORT — {side_label} SIDE"
        c.font      = Font(name="Arial", size=16, bold=True, color="FFFFFF")
        c.fill      = fill("1F3864")
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 34

        # ── Info ────────────────────────────────────────────────────────────
        for lbl_cell, lbl_val, val_cell, val_val in [
            ("A2", "Date / Time:", "B2", ts),
            ("D2", "COM Port:",    "E2", self._jig_diag_com or "—"),
            ("G2", "Firmware:",    "H2", self._jig_diag_fw  or "—"),
        ]:
            ws[lbl_cell].value = lbl_val
            ws[lbl_cell].font  = Font(name="Arial", size=10, bold=True)
            ws[lbl_cell].alignment = Alignment(horizontal="right")
            ws[val_cell].value = val_val
            ws[val_cell].font  = Font(name="Arial", size=10)
        ws.row_dimensions[2].height = 18

        # ── Summary badges ──────────────────────────────────────────────────
        for ref, txt, clr in [("A3", f"Total: {len(results)}", "1A5276"),
                               ("C3", f"OK: {ok_n}",           "1E8449"),
                               ("E3", f"FAIL: {fail_n}",       "922B21")]:
            ws.merge_cells(f"{ref}:{chr(ord(ref[0])+1)}3")
            c = ws[ref]
            c.value     = txt
            c.font      = Font(name="Arial", size=12, bold=True, color="FFFFFF")
            c.fill      = fill(clr)
            c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[3].height = 22

        # ── Column headers ──────────────────────────────────────────────────
        HDR    = ["#","Pin","Status","MI","CH","Conn","LAT","OUT0","OUT1",
                  "Root Cause","Recommendation"]
        WIDTHS = [ 5,   8,     8,     6,   6,   6,    6,    8,    8,     38,   46]
        for col, (h, w) in enumerate(zip(HDR, WIDTHS), 1):
            c = ws.cell(5, col, h)
            c.font      = Font(name="Arial", size=10, bold=True, color="FFFFFF")
            c.fill      = fill("2E75B6")
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border    = thin()
            ws.column_dimensions[get_column_letter(col)].width = w
        ws.row_dimensions[5].height = 22

        # ── Data rows ───────────────────────────────────────────────────────
        for idx, r in enumerate(results, 1):
            row    = 5 + idx
            is_fail = r.status == "FAIL"
            bg = "FDEDEC" if is_fail else ("EAF7EE" if idx % 2 else "FFFFFF")
            ws.row_dimensions[row].height = 18

            def put(col, val, align="center", bold=False, color="000000",
                    wrap=False):
                c = ws.cell(row, col, val)
                c.font      = Font(name="Arial", size=10, bold=bold, color=color)
                c.fill      = fill(bg)
                c.alignment = Alignment(horizontal=align, vertical="center",
                                        wrap_text=wrap)
                c.border    = thin()

            put(1,  idx)
            put(2,  r.pin_name)
            put(3,  r.status,
                bold=is_fail, color="C0392B" if is_fail else "1E8449")
            put(4,  r.mi   if r.mi  >= 0 else "—")
            put(5,  r.ch   if r.ch  >= 0 else "—")
            put(6,  r.conn_count if r.status == "OK" else 0)
            put(7,  r.lat  if r.lat >= 0 else "—")
            put(8,  f"0x{r.out0:02X}" if r.out0 >= 0 else "—")
            put(9,  f"0x{r.out1:02X}" if r.out1 >= 0 else "—")
            put(10, r.root_cause,     "left", wrap=True)
            put(11, r.recommendation, "left", wrap=True)

        ws.freeze_panes = "A6"

    def _build_jig_sheet_fail(self, ws, failed, side_label="J1"):
        """Sheet 'Root Cause Analysis' — failed pins only, card layout."""
        thin = self._jig_thin
        fill = self._jig_fill

        ws.sheet_view.showGridLines = False
        for col, w in zip("ABC", [6, 22, 58]):
            ws.column_dimensions[col].width = w

        # Title
        ws.merge_cells("A1:C1")
        c = ws["A1"]
        c.value     = f"ROOT CAUSE ANALYSIS — {side_label} — {len(failed)} FAILED PIN(S)"
        c.font      = Font(name="Arial", size=14, bold=True, color="FFFFFF")
        c.fill      = fill("922B21")
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 30

        row = 3
        for idx, r in enumerate(failed, 1):
            # Pin header bar
            ws.merge_cells(f"A{row}:C{row}")
            c = ws[f"A{row}"]
            c.value     = (f"#{idx}  {side_label}.{r.pin_name}"
                           f"   MUX group {r.mi},  channel {r.ch}")
            c.font      = Font(name="Arial", size=12, bold=True, color="FFFFFF")
            c.fill      = fill("C0392B")
            c.alignment = Alignment(horizontal="left", vertical="center", indent=1)
            ws.row_dimensions[row].height = 24
            row += 1

            drive_str = (f"LAT={r.lat}  "
                         f"OUT0=0x{r.out0:02X}  "
                         f"OUT1=0x{r.out1:02X}"
                         if r.lat >= 0 else "—")

            for label, value, lbl_fill, val_fill, h in [
                ("Drive State",  drive_str,         "FAD7A0", "FEF9E7", 18),
                ("Root Cause",  r.root_cause,       "F5CBA7", "FEF9E7", 36),
                ("Fix",    r.recommendation,   "A9DFBF", "EAFAF1", 36),
            ]:
                ws[f"A{row}"].border = thin()
                ws[f"B{row}"].value = label
                ws[f"B{row}"].font  = Font(name="Arial", size=10, bold=True)
                ws[f"B{row}"].fill  = fill(lbl_fill)
                ws[f"B{row}"].alignment = Alignment(
                    horizontal="left", vertical="top", indent=1)
                ws[f"B{row}"].border = thin()
                ws[f"C{row}"].value = value
                ws[f"C{row}"].font  = Font(name="Arial", size=10)
                ws[f"C{row}"].fill  = fill(val_fill)
                ws[f"C{row}"].alignment = Alignment(
                    horizontal="left", vertical="top",
                    wrap_text=True, indent=1)
                ws[f"C{row}"].border = thin()
                ws.row_dimensions[row].height = h
                row += 1

            row += 1   # blank separator between pins

    # ── Export ─────────────────────────────────────────────────────────────
    def _export_report(self):
        if not self.current_result:
            messagebox.showinfo("No Data", "No test result to export.")
            return
        self._export_profile_report()

    def _open_reports_folder(self):
        d = Path("reports")
        d.mkdir(exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(d))
        else:
            os.system(f"xdg-open '{d}'")

    # ── History ────────────────────────────────────────────────────────────
    def _save_history(self):
        try:
            data = []
            for r in self.history[-200:]:
                data.append({
                    "ts":          r.timestamp.isoformat(),
                    "sn":          r.serial_number,
                    "connections": len(r.connections),
                    "completed":   r.completed,
                    "duration":    r.duration_s,
                })
            with open(HISTORY_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def _load_history(self):
        try:
            if os.path.exists(HISTORY_FILE):
                with open(HISTORY_FILE) as f:
                    data = json.load(f)
                for d in data:
                    r = TestResult()
                    r.timestamp     = datetime.fromisoformat(d["ts"])
                    r.serial_number = d.get("sn", "")
                    r.completed     = d.get("completed", False)
                    r.duration_s    = d.get("duration", 0)
                    self.history.append(r)
                self._update_stats()
        except Exception:
            pass


# ── Entry point ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    app = App()
    app.mainloop()