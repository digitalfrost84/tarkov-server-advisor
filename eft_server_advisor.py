"""EFT Server Advisor

Reads Escape from Tarkov log folders, finds every raid server you connected to,
collects the game's own network statistics (RTT, packet loss, timeouts), live-pings
those servers, and maps each datacenter to the in-game
"Configure game servers list for matching" regions so you know what to untick.

Pure standard library (tkinter). Run:  python eft_server_advisor.py
"""

import json
import os
import queue
import re
import statistics
import subprocess
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_DIR = Path(sys.argv[0]).resolve().parent
# User-editable mapping lives in %APPDATA% so the .exe can sit anywhere (even read-only folders).
DATA_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "EFT Server Advisor"
MAP_FILE = DATA_DIR / "region_map.json"


def _registry_installs():
    import winreg
    uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    for hive, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                       (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                       (winreg.HKEY_CURRENT_USER, 0)):
        try:
            root = winreg.OpenKey(hive, uninstall, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with root:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                vals = {}
                try:
                    with winreg.OpenKey(root, name) as k:
                        for v in ("DisplayName", "InstallLocation", "DisplayIcon"):
                            try:
                                vals[v] = str(winreg.QueryValueEx(k, v)[0])
                            except OSError:
                                pass
                except OSError:
                    continue
                # BSG launcher uses key "EscapeFromTarkov", Steam uses "Steam App <id>".
                # Exact name match keeps "Escape from Tarkov: Arena" out.
                if name == "EscapeFromTarkov" or vals.get("DisplayName", "").strip() == "Escape from Tarkov":
                    if vals.get("InstallLocation"):
                        yield Path(vals["InstallLocation"].strip('" '))
                    if vals.get("DisplayIcon"):
                        yield Path(vals["DisplayIcon"].strip('" ').split(",")[0]).parent
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            steam = Path(winreg.QueryValueEx(k, "SteamPath")[0])
        vdf = (steam / "steamapps" / "libraryfolders.vdf").read_text("utf-8", errors="replace")
        libs = [steam] + [Path(m.replace("\\\\", "\\")) for m in re.findall(r'"path"\s+"([^"]+)"', vdf)]
        for lib in libs:
            yield from (lib / "steamapps" / "common").glob("Escape from Tarkov")
    except OSError:
        pass


def find_logs_dirs():
    """Candidate EFT Logs folders, best first: registry (BSG launcher / Steam), Steam libraries,
    then a Logs folder next to or one level above this program."""
    installs = list(_registry_installs()) if os.name == "nt" else []
    installs += [APP_DIR, APP_DIR.parent]
    found = []
    for d in installs:
        logs = d / "Logs"
        if logs.is_dir() and logs.resolve() not in [f.resolve() for f in found]:
            found.append(logs)
    return found

# In-game regions, in the order the game lists them.
INGAME_REGIONS = [
    ("AF", "Africa"), ("AF", "Af Nigeria"),
    ("ASI", "Asia Central"), ("ASI", "Japan"), ("ASI", "Singapore"), ("ASI", "China"),
    ("ASI", "Korea"), ("ASI", "Malaysia"),
    ("EUR", "Europe Central"), ("EUR", "Europe North"), ("EUR", "Europe West"),
    ("EUR", "Europe East"), ("EUR", "Europe South"),
    ("ME", "Middle East"), ("ME", "Me Dubai"),
    ("OCE", "Australia"), ("OCE", "New Zealand"),
    ("RUS", "Russia Central"), ("RUS", "Russia East"), ("RUS", "Russia West"),
    ("SAM", "South America"), ("SAM", "Mexico"), ("SAM", "Sa Colombia"),
    ("USA", "North America Central"), ("USA", "North America South East"),
    ("USA", "North America West"), ("USA", "North America North East"),
    ("USA", "North America South"),
]

# Best-guess datacenter -> in-game region mapping. The server id ("Sid") in the logs
# starts with COUNTRY-CITY, e.g. "FR-SBG01G148_..." = Strasbourg, France.
# Keys are "CC-CIT" (exact city) or "CC" (whole-country fallback).
# Override or extend any of this in region_map.json next to this script.
DEFAULT_MAP = {
    # Africa
    "ZA": "Africa", "EG": "Africa", "KE": "Africa", "MA": "Africa",
    "NG": "Af Nigeria",
    # Asia
    "KZ": "Asia Central", "UZ": "Asia Central", "KG": "Asia Central",
    "JP": "Japan", "SG": "Singapore", "CN": "China", "HK": "China",
    "KR": "Korea", "MY": "Malaysia",
    # Europe
    "DE": "Europe Central", "AT": "Europe Central", "CH": "Europe Central",
    "CZ": "Europe Central", "SK": "Europe Central", "HU": "Europe Central",
    "PL": "Europe Central",
    "SE": "Europe North", "FI": "Europe North", "NO": "Europe North",
    "DK": "Europe North", "EE": "Europe North", "LV": "Europe North", "LT": "Europe North",
    "FR": "Europe West", "NL": "Europe West", "BE": "Europe West", "UK": "Europe West",
    "GB": "Europe West", "IE": "Europe West", "LU": "Europe West",
    "UA": "Europe East", "RO": "Europe East", "BG": "Europe East", "MD": "Europe East",
    "RS": "Europe East", "BY": "Europe East",
    "ES": "Europe South", "PT": "Europe South", "IT": "Europe South", "GR": "Europe South",
    "HR": "Europe South", "CY": "Europe South", "MT": "Europe South", "SI": "Europe South",
    "FR-MRS": "Europe South",
    # Middle East
    "TR": "Middle East", "IL": "Middle East", "GE": "Middle East", "AM": "Middle East",
    "AE": "Me Dubai", "UAE": "Me Dubai", "SA": "Me Dubai", "BH": "Me Dubai", "QA": "Me Dubai",
    # Oceania
    "AU": "Australia", "NZ": "New Zealand",
    # Russia
    "RU": "Russia Central", "RU-MSK": "Russia Central",
    "RU-SPB": "Russia West", "RU-LED": "Russia West",
    "RU-NSK": "Russia East", "RU-OVB": "Russia East", "RU-KHV": "Russia East",
    "RU-VVO": "Russia East", "RU-KJA": "Russia East", "RU-IKT": "Russia East",
    # South America
    "BR": "South America", "AR": "South America", "CL": "South America",
    "PE": "South America", "MX": "Mexico", "CO": "Sa Colombia",
    # North America
    "US": "North America Central", "CA": "North America North East",
    "US-CHI": "North America Central", "US-KCY": "North America Central",
    "US-DEN": "North America Central",
    "US-MIA": "North America South East", "US-ATL": "North America South East",
    "US-LAX": "North America West", "US-SJC": "North America West",
    "US-SEA": "North America West", "US-PHX": "North America West",
    "US-SLC": "North America West", "CA-VAN": "North America West",
    "US-NYC": "North America North East", "US-ASH": "North America North East",
    "US-BOS": "North America North East", "US-PHL": "North America North East",
    "CA-TOR": "North America North East", "CA-YUL": "North America North East",
    "US-DAL": "North America South", "US-HOU": "North America South",
}

CITY_NAMES = {
    "AT-VIE": "Vienna", "BE-BRU": "Brussels", "BG-SOF": "Sofia", "CA-TOR": "Toronto",
    "CA-YUL": "Montreal", "CH-ZRH": "Zurich", "CZ-PR": "Prague", "CZ-PRG": "Prague",
    "DE-FRM": "Frankfurt", "DE-FRA": "Frankfurt", "DE-BER": "Berlin", "ES-MDR": "Madrid",
    "FI-HEL": "Helsinki", "FR-BOD": "Bordeaux", "FR-MRS": "Marseille", "FR-PAR": "Paris",
    "FR-SBG": "Strasbourg", "GR-ATH": "Athens", "HR-ZAG": "Zagreb", "IE-DUB": "Dublin",
    "IL-TLV": "Tel Aviv", "IT-MIL": "Milan", "IT-PMO": "Palermo", "NG-LOS": "Lagos",
    "NL-AMS": "Amsterdam", "PL-WAW": "Warsaw", "PT-LIS": "Lisbon", "RO-BHR": "Bucharest",
    "RU-MSK": "Moscow", "RU-SPB": "St Petersburg", "SE-STH": "Stockholm", "SK-BRS": "Bratislava",
    "TR-IST": "Istanbul", "UA-IEV": "Kyiv", "UAE-DXB": "Dubai", "UK-LON": "London",
    "US-ASH": "Ashburn", "US-BOS": "Boston", "US-CHI": "Chicago", "US-NYC": "New York",
    "US-DAL": "Dallas", "US-LAX": "Los Angeles", "US-MIA": "Miami", "US-SEA": "Seattle",
}

TS_FMT = "%Y-%m-%d %H:%M:%S.%f"
RE_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\|")
RE_RAID = re.compile(r"Ip: ([\d.]+), Port: (\d+), Location: ([^,]+), Sid: ([^,]+),"
                     r"(?: GameMode: ([^,]+),)?")
RE_SID = re.compile(r"^([A-Za-z]{2,3})-([A-Za-z]{2,3}?)(\d+)")
RE_STATS = re.compile(r"Statistics \(address: ([\d.]+):(\d+), rtt: ([\d.]+), lose: ([\d.eE+-]+),"
                      r" sent: (\d+), received: (\d+)\)")
RE_TIMEOUT = re.compile(r"Timeout: Connection timed out.*\(address: ([\d.]+):(\d+)\)")
# Connection handshake: time from our first "Send connect" to the server's reply. Logged as soon
# as the raid connects, so it exists even when the raid never reaches the end-of-raid Statistics
# line (game closed / crashed / alt-F4 mid-raid). Close to the game RTT on a healthy server.
RE_HS_START = re.compile(r"Send connect \(address: ([\d.]+):(\d+), syn: True")
RE_HS_DONE = re.compile(r"Enter to the 'Connected' state \(address: ([\d.]+):(\d+)")
RE_MATCH = re.compile(r"MatchingCompleted:([\d.]+) real:([\d.]+)")
SCAN_KINDS = ("application", "network-connection", "errors")

MAP_NAMES = {
    "bigmap": "Customs", "factory4_day": "Factory (day)", "factory4_night": "Factory (night)",
    "Sandbox": "Ground Zero", "Sandbox_high": "Ground Zero 21+", "RezervBase": "Reserve",
    "TarkovStreets": "Streets", "laboratory": "Labs", "Labyrinth": "Labyrinth",
    "Shoreline": "Shoreline", "Woods": "Woods", "Interchange": "Interchange",
    "Lighthouse": "Lighthouse",
}


@dataclass
class Raid:
    start: datetime
    ip: str
    port: int
    location: str
    sid: str
    mode: str
    dc: str
    region: str
    rtts: list = field(default_factory=list)
    losses: list = field(default_factory=list)
    sent: list = field(default_factory=list)  # packets per stats sample, weights the loss
    timeouts: int = 0
    failed_connects: int = 0
    handshakes: list = field(default_factory=list)  # ms
    matching_s: float | None = None


def load_map():
    m = dict(DEFAULT_MAP)
    if MAP_FILE.exists():
        try:
            m.update({k.upper(): v for k, v in json.loads(MAP_FILE.read_text("utf-8")).items()
                      if not k.startswith("_")})
        except Exception as e:  # keep running on a broken user file
            messagebox.showwarning("region_map.json", f"Could not read mapping file:\n{e}")
    return m


def dc_of(sid):
    m = RE_SID.match(sid)
    if not m:
        return sid.split("_")[0].upper()
    return f"{m.group(1)}-{m.group(2)}{m.group(3)}".upper()


def region_of(dc, mapping):
    cc_city = dc.rstrip("0123456789")
    return mapping.get(cc_city) or mapping.get(cc_city.split("-")[0]) or "? (unmapped)"


def parse_ts(s):
    try:
        return datetime.strptime(s, TS_FMT)
    except ValueError:
        return None


# Markers of lines worth parsing, per log kind. bytes.find() per marker is far faster than
# one regex with alternatives, and only a few thousand lines match in ~600 MB of logs.
INTEREST = {
    "application": (b"Ip: ", b"MatchingCompleted"),
    "network-connection": (b"Statistics (", b"connect (", b"Connected' state", b"Timeout:"),
    # errors logs are huge (mostly unrelated); only read for timeouts when the folder has
    # no network-connection log, which records the same timeouts
    "errors": (b"Timeout:",),
}


def read_folder(folder):
    """Worker: return (folder, bytes read, [interesting lines per log file]).

    Runs in a thread pool; only I/O and line extraction, no shared state."""
    files, nbytes = [], 0
    logs = {f: f.stem.rsplit(" ", 1)[-1].rsplit("_", 1)[0] for f in sorted(folder.glob("*.log"))}
    has_netconn = "network-connection" in logs.values()
    for f, kind in logs.items():
        if kind not in INTEREST or (kind == "errors" and has_netconn):
            continue
        try:
            data = f.read_bytes()
        except OSError:
            continue
        nbytes += len(data)
        starts = set()
        for marker in INTEREST[kind]:
            pos = data.find(marker)
            while pos != -1:
                start = data.rfind(b"\n", 0, pos) + 1
                starts.add(start)
                nl = data.find(b"\n", pos)
                if nl == -1:
                    break
                pos = data.find(marker, nl)
        lines = []
        for start in sorted(starts):
            end = data.find(b"\n", start)
            lines.append(data[start:end if end != -1 else len(data)]
                         .decode("utf-8", errors="replace").rstrip("\r"))
        files.append(lines)
    return folder, nbytes, files


def scan_logs(roots, mapping, since=None, progress=None, workers=8):
    """Return list[Raid] with network stats attached.

    progress(done, total, folder_name, mb_read, raids_so_far) is called on the calling thread."""
    folders = []
    for root in roots:
        if root.is_dir():
            folders += [p for p in root.iterdir() if p.is_dir()]
    folders.sort(key=lambda p: p.name)

    raids, seen_raid = [], set()
    stats, timeouts, matches, handshakes = [], [], [], []
    seen_ev = set()
    total = 0

    def file_lines():
        nonlocal total
        with ThreadPoolExecutor(workers) as ex:
            # map() yields in folder order, so dedupe matches a sequential scan
            for i, (folder, nchars, files) in enumerate(ex.map(read_folder, folders)):
                total += nchars
                if progress:
                    progress(i + 1, len(folders), folder.name, total / 1e6, len(raids))
                yield from files

    for lines in file_lines():
        pending = {}  # (ip, port) -> handshake start, paired within one file
        for line in lines:
            mt = RE_TS.match(line)
            if not mt:
                continue
            ts = parse_ts(mt.group(1))
            if ts is None or (since and ts < since):
                continue
            if (m := RE_RAID.search(line)) and "profileStatus" in line:
                ip, port, loc, sid, mode = m.groups()
                key = (ip, port, sid)
                if key in seen_raid:
                    continue
                seen_raid.add(key)
                dc = dc_of(sid)
                raids.append(Raid(ts, ip, int(port), loc, sid, mode or "", dc,
                                  region_of(dc, mapping)))
            elif m := RE_STATS.search(line):
                key = ("s", mt.group(1), m.group(1), m.group(2))
                if key not in seen_ev:
                    seen_ev.add(key)
                    stats.append((ts, m.group(1), int(m.group(2)), float(m.group(3)),
                                  float(m.group(4)), int(m.group(5)), int(m.group(6))))
            elif m := RE_HS_START.search(line):
                pending[(m.group(1), int(m.group(2)))] = ts
            elif m := RE_HS_DONE.search(line):
                addr = (m.group(1), int(m.group(2)))
                if addr in pending:
                    t0 = pending.pop(addr)
                    key = ("h", str(t0), *addr)
                    if key not in seen_ev:
                        seen_ev.add(key)
                        handshakes.append((ts, *addr, (ts - t0).total_seconds() * 1000))
            elif m := RE_TIMEOUT.search(line):
                key = ("t", mt.group(1), m.group(1), m.group(2))
                if key not in seen_ev:
                    seen_ev.add(key)
                    timeouts.append((ts, m.group(1), int(m.group(2))))
            elif m := RE_MATCH.search(line):
                key = ("m", mt.group(1))
                if key not in seen_ev:
                    seen_ev.add(key)
                    matches.append((ts, float(m.group(2))))

    raids.sort(key=lambda r: r.start)
    by_addr = defaultdict(list)
    for r in raids:
        by_addr[(r.ip, r.port)].append(r)

    def owner(ts, ip, port):
        cands = [r for r in by_addr.get((ip, port), []) if r.start - timedelta(minutes=5) <= ts]
        cands = [r for r in cands if ts - r.start <= timedelta(hours=3)]
        return cands[-1] if cands else None

    for ts, ip, port, rtt, lose, sent, recv in stats:
        r = owner(ts, ip, port)
        if not r:
            continue
        if recv == 0 or rtt == 0:
            r.failed_connects += 1
        else:
            r.rtts.append(rtt)
            r.losses.append(max(lose, 0.0) * 100.0)
            r.sent.append(sent)
    for ts, ip, port, ms in handshakes:
        if r := owner(ts, ip, port):
            r.handshakes.append(ms)
    for ts, ip, port in timeouts:
        if r := owner(ts, ip, port):
            r.timeouts += 1
    for r in raids:  # nearest matching-completed event within 10 min
        best = min(matches, key=lambda m: abs((m[0] - r.start).total_seconds()), default=None)
        if best and abs((best[0] - r.start).total_seconds()) <= 600:
            r.matching_s = best[1]
    return raids


RE_PING_REPLY = re.compile(r"[=<](\d+)\s*ms", re.I)


def icmp_ping(ip, count=4):
    """Return list of RTTs in ms (empty = no reply). Locale independent: reads TTL lines."""
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    args = ["ping", "-n", str(count), "-w", "1000", ip] if os.name == "nt" else \
        ["ping", "-c", str(count), "-W", "1", ip]
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=count * 2 + 5,
                             creationflags=flags, errors="replace").stdout
    except Exception:
        return []
    res = []
    for line in out.splitlines():
        if "TTL" in line.upper():
            if m := RE_PING_REPLY.search(line):
                res.append(int(m.group(1)))
    return res


def mean(xs):
    return sum(xs) / len(xs) if xs else None


def fmt(v, nd=0, suffix=""):
    if v is None:
        return "–"
    return f"{v:.{nd}f}{suffix}"


def enable_dpi_awareness():
    """Without this Windows bitmap-stretches the window on scaled displays (blurry text)."""
    if os.name != "nt":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    # Own taskbar identity, otherwise pythonw groups the window under Python's icon.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("EFT.ServerAdvisor")
    except Exception:
        pass


def icon_path():
    """Same .ico the exe is built with: bundled next to the code when frozen, build_assets/ otherwise."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent / "build_assets"))
    p = base / "icon.ico"
    return p if p.exists() else None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("EFT Server Advisor")
        ico = icon_path()
        if ico:
            try:
                self.iconbitmap(default=str(ico))  # title bar + taskbar, also for child dialogs
            except tk.TclError:
                pass
        # Tk scales fonts by itself once DPI-aware; pixel sizes we set must be scaled by hand.
        self.dpi = self.winfo_fpixels("1i") / 96.0
        self.geometry(f"{self.px(1180)}x{self.px(680)}")
        self.minsize(self.px(900), self.px(500))
        from tkinter import font as tkfont
        line = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        ttk.Style(self).configure("Treeview", rowheight=line + self.px(6))
        self.small_font = tkfont.nametofont("TkDefaultFont").copy()
        self.small_font.configure(size=max(8, self.small_font.cget("size") - 1))
        self.all_raids: list[Raid] = []  # everything in the logs
        self.raids: list[Raid] = []  # after the "Last N days" filter
        self.live = {}  # ip -> list[ms]
        self.mapping = load_map()

        found = find_logs_dirs()
        self.logs_var = tk.StringVar(value=str(found[0]) if found else "")
        self.days_var = tk.IntVar(value=0)
        self.good_var = tk.IntVar(value=60)
        self.bad_var = tk.IntVar(value=100)
        self.loss_var = tk.DoubleVar(value=1.0)
        self.status_var = tk.StringVar(value="Pick your EFT Logs folder and press Analyze.")
        self.thresholds = (60, 100, 1.0)  # last valid (good RTT, bad RTT, bad loss %)
        self._refresh_job = None

        self._ui_q = queue.Queue()
        self._poll_ui()
        self._build()
        if self.logs_var.get():
            self.after(100, self.analyze)

    def px(self, n):
        return int(round(n * self.dpi))

    # ---------- UI ----------
    def _build(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Logs folder:").pack(side="left")
        ttk.Entry(top, textvariable=self.logs_var, width=55).pack(side="left", padx=4)
        ttk.Button(top, text="Browse…", command=self.browse).pack(side="left")
        ttk.Label(top, text="Last").pack(side="left")
        days_sb = ttk.Spinbox(top, from_=0, to=3650, width=5, textvariable=self.days_var,
                              command=self._apply_days)
        days_sb.pack(side="left", padx=2)
        days_sb.bind("<Return>", lambda e: self._apply_days())
        self.days_var.trace_add("write", self._days_changed)
        ttk.Label(top, text="days (0 = all)").pack(side="left")
        self.analyze_btn = ttk.Button(top, text="Analyze logs", command=self.analyze)
        self.analyze_btn.pack(side="right")
        self.ping_btn = ttk.Button(top, text="Ping again", command=self.live_ping)
        self.ping_btn.pack(side="right", padx=6)

        thr = ttk.Frame(self, padding=(8, 0, 8, 6))
        thr.pack(fill="x")
        ttk.Label(thr, text="Thresholds —  good RTT ≤").pack(side="left")
        for var, lbl, lo, hi, inc in ((self.good_var, "ms   bad RTT ≥", 5, 500, 5),
                                      (self.bad_var, "ms   bad loss ≥", 5, 1000, 5),
                                      (self.loss_var, "%", 0.1, 20, 0.1)):
            sb = ttk.Spinbox(thr, from_=lo, to=hi, increment=inc, width=6, textvariable=var,
                             command=self.refresh)
            sb.pack(side="left", padx=2)
            sb.bind("<Return>", lambda e: self.refresh())
            var.trace_add("write", self._thresholds_changed)
            ttk.Label(thr, text=lbl).pack(side="left")
        ttk.Button(thr, text="Edit region mapping…", command=self.edit_mapping).pack(side="right")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8)

        reg_cols = [("verdict", "Verdict", 80), ("region", "Region", 55), ("name", "In-game name", 190),
                    ("raids", "Raids", 55), ("rtt", "Game RTT avg", 95), ("rttmax", "RTT worst", 80),
                    ("hs", "Handshake", 80), ("loss", "Loss %", 65), ("to", "Timeouts", 70), ("fail", "Failed conn.", 80),
                    ("ping", "Live ping", 75), ("match", "Queue avg s", 85), ("dcs", "Datacenters", 260)]
        self.reg_tree = self._tree(self.nb, reg_cols, expandable=True)
        self.nb.add(self.reg_tree.master, text="In-game regions (what to tick)")

        dc_cols = [("verdict", "Verdict", 80), ("dc", "Datacenter", 100), ("name", "In-game region", 180),
                   ("raids", "Raids", 55), ("rtt", "Game RTT avg", 95), ("rttmax", "RTT worst", 80),
                   ("hs", "Handshake", 80), ("loss", "Loss %", 65), ("to", "Timeouts", 70), ("fail", "Failed conn.", 80),
                   ("ping", "Live ping", 75), ("last", "Last seen", 130), ("ips", "IPs", 230)]
        self.dc_tree = self._tree(self.nb, dc_cols)
        self.nb.add(self.dc_tree.master, text="Datacenters")

        raid_cols = [("time", "Start", 135), ("dc", "Datacenter", 100), ("name", "In-game region", 170),
                     ("map", "Map", 110), ("mode", "Mode", 90), ("rtt", "Game RTT", 75),
                     ("hs", "Handshake", 80), ("loss", "Loss %", 65), ("to", "Timeouts", 70), ("fail", "Failed conn.", 80),
                     ("match", "Queue s", 65), ("addr", "Address", 150)]
        self.raid_tree = self._tree(self.nb, raid_cols)
        self.nb.add(self.raid_tree.master, text="Raids")

        legend = ttk.Frame(self, padding=(8, 4))
        legend.pack(fill="x")
        ttk.Label(legend, text="Click ▸ to see each region's datacenters   •   UNTICK = bad latency / loss / timeouts   •   "
                  "OK = acceptable   •   KEEP = good   •   no data = never played there "
                  "(live ping only)   •   ≈ = no end-of-raid stats (game closed mid-raid), judged by "
                  "connect handshake   •   Game RTT is the game's own measurement, ICMP live ping "
                  "is usually lower.", foreground="#555").pack(side="left")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w",
                  padding=(6, 2)).pack(fill="x", side="bottom")

    def _tree(self, parent, cols, expandable=False):
        frame = ttk.Frame(parent)
        tree = ttk.Treeview(frame, columns=[c[0] for c in cols],
                            show="tree headings" if expandable else "headings")
        tree.column("#0", width=self.px(28) if expandable else 0, stretch=False)
        for cid, label, w in cols:
            tree.heading(cid, text=label, command=lambda c=cid, t=tree: self.sort(t, c))
            tree.column(cid, width=self.px(w), anchor="w" if cid in ("name", "dcs", "ips", "map", "addr") else "center")
        vs = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        tree.tag_configure("bad", background="#f6c6c6")
        tree.tag_configure("warn", background="#fbe8b5")
        tree.tag_configure("good", background="#cdeccd")
        tree.tag_configure("none", foreground="#888")
        tree.tag_configure("child", font=self.small_font)
        tree._sort_desc = {}
        return tree

    def sort(self, tree, col):
        desc = tree._sort_desc.get(col, False)
        rows = [(tree.set(k, col), k) for k in tree.get_children("")]

        def key(v):
            s = v[0].rstrip("%ms ").replace("–", "")
            try:
                return (0, float(s))
            except ValueError:
                return (1 if s else 2, s.lower())
        rows.sort(key=key, reverse=desc)
        for i, (_, k) in enumerate(rows):
            tree.move(k, "", i)
        tree._sort_desc[col] = not desc

    # ---------- actions ----------
    def browse(self):
        d = filedialog.askdirectory(title="Select EFT Logs folder", initialdir=self.logs_var.get() or None)
        if d:
            self.logs_var.set(d)
            self.analyze()

    def edit_mapping(self):
        if not MAP_FILE.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            seen = sorted({r.dc.rstrip("0123456789") for r in self.all_raids})
            MAP_FILE.write_text(json.dumps({
                "_help": "Keys: 'CC-CIT' (country-city from server id) or 'CC' (whole country). "
                         "Values must match an in-game region name exactly. Entries here override "
                         "the built-in defaults. Save, then press Analyze logs.",
                "_seen_in_your_logs": {k: region_of(k, self.mapping) for k in seen},
                "_valid_regions": [n for _, n in INGAME_REGIONS],
            }, indent=2), "utf-8")
        if os.name == "nt":
            os.startfile(MAP_FILE)
        else:
            subprocess.Popen(["xdg-open", str(MAP_FILE)])

    # ---------- thread -> UI ----------
    def _ui(self, fn, *args):
        """Schedule fn(*args) on the Tk thread. Safe to call from any thread."""
        self._ui_q.put((fn, args))

    def _poll_ui(self):
        try:
            while True:
                fn, args = self._ui_q.get_nowait()
                fn(*args)
        except queue.Empty:
            pass
        self.after(40, self._poll_ui)

    # ---------- busy overlay ----------
    def _busy_show(self, title, detail="", corner=False):
        """Card centred over the tables so long scans are obvious, not just a status-bar line."""
        if not hasattr(self, "_busy"):
            self._busy = tk.Frame(self, bg="#ffffff", highlightthickness=1,
                                  highlightbackground="#9aa7b0", padx=self.px(28), pady=self.px(20))
            from tkinter import font as tkfont
            big = tkfont.nametofont("TkDefaultFont").copy()
            big.configure(size=big.cget("size") + 4, weight="bold")
            self._busy_title = tk.Label(self._busy, bg="#ffffff", font=big)
            self._busy_title.pack(anchor="w")
            self._busy_detail = tk.Label(self._busy, bg="#ffffff", fg="#555", justify="left",
                                         anchor="w", width=62)
            self._busy_detail.pack(anchor="w", pady=(self.px(4), self.px(10)))
            self._busy_bar = ttk.Progressbar(self._busy, length=self.px(380), mode="indeterminate")
            self._busy_bar.pack(fill="x")
        self._busy_title.config(text=title)
        self._busy_detail.config(text=detail)
        self._busy_bar.config(mode="indeterminate", value=0)
        self._busy_bar.start(15)
        if corner:  # bottom-right, so the table can be watched filling in
            self._busy.place(in_=self.nb, relx=1.0, rely=1.0, x=-self.px(24), y=-self.px(16), anchor="se")
        else:
            self._busy.place(in_=self.nb, relx=0.5, rely=0.4, anchor="center")
        self._busy.lift()
        self._busy_on = True

    def _busy_update(self, i, n, detail):
        if not getattr(self, "_busy_on", False):
            return
        if str(self._busy_bar.cget("mode")) != "determinate":
            self._busy_bar.stop()
            self._busy_bar.config(mode="determinate", maximum=n)
        self._busy_bar.config(value=i)
        self._busy_detail.config(text=detail)

    def _busy_hide(self):
        if hasattr(self, "_busy"):
            self._busy_bar.stop()
            self._busy.place_forget()
            self._busy_on = False

    def analyze(self):
        root = Path(self.logs_var.get())
        if not root.is_dir():
            messagebox.showerror("Logs folder", f"Folder not found:\n{root}")
            return
        roots = [root]
        self.mapping = load_map()
        self.analyze_btn.state(["disabled"])
        self._busy_show("Scanning EFT logs…", f"Reading {', '.join(r.name for r in roots)}")

        def prog(done, total, folder, mb, raids_so_far):
            self._ui(self._busy_update, done, total,
                     f"{folder}\nFolder {done} of {total}  ·  {mb:,.0f} MB read  ·  "
                     f"{raids_so_far} raids found")

        def work():
            try:
                raids = scan_logs(roots, self.mapping, None, prog)
                err = None
            except Exception as e:
                raids, err = [], e
            self._ui(self._analyzed, raids, err)
        threading.Thread(target=work, daemon=True).start()

    def _analyzed(self, raids, err):
        self.analyze_btn.state(["!disabled"])
        self._busy_hide()
        if err:
            messagebox.showerror("Analyze failed", str(err))
            return
        self.all_raids = raids
        self._apply_days()
        # Ping right away so log history and current latency can be compared side by side.
        if raids:
            self.live_ping()

    def _days_changed(self, *_):
        # typing in the box: re-filter shortly after the last keystroke
        if getattr(self, "_days_job", None):
            self.after_cancel(self._days_job)
        self._days_job = self.after(300, self._apply_days)

    def _apply_days(self):
        self._days_job = None
        try:
            days = max(0, int(self.days_var.get()))
        except (tk.TclError, ValueError):
            return  # half-typed value; wait for a valid one
        if days:
            since = datetime.now() - timedelta(days=days)
            self.raids = [r for r in self.all_raids if r.start >= since]
        else:
            self.raids = list(self.all_raids)
        raids = self.raids
        with_stats = sum(1 for r in raids if r.rtts)
        if raids or not self.all_raids:
            self._scan_summary = (f"{len(raids)} raids found, {with_stats} with in-game network stats, "
                                  f"{len({r.dc for r in raids})} datacenters.")
            if days and len(raids) < len(self.all_raids):
                self._scan_summary += f" (last {days} days; {len(self.all_raids)} in total)"
        else:
            newest = max(r.start for r in self.all_raids)
            self._scan_summary = (f"No raids in the last {days} days. Your newest raid is from "
                                  f"{newest:%Y-%m-%d}; {len(self.all_raids)} raids in total. "
                                  "Set days to 0 to see all.")
        self.status_var.set(self._scan_summary + getattr(self, "_ping_summary", ""))
        self.refresh()

    def live_ping(self):
        if not self.all_raids:
            messagebox.showinfo("Live ping", "Analyze logs first.")
            return
        if getattr(self, "_pinging", False):
            return
        self._pinging = True
        # ping up to the 3 most recently used IPs per datacenter
        per_dc = defaultdict(list)
        for r in reversed(self.all_raids):
            if r.ip not in per_dc[r.dc] and len(per_dc[r.dc]) < 3:
                per_dc[r.dc].append(r.ip)
        ips = sorted({ip for v in per_dc.values() for ip in v})
        self.ping_btn.state(["disabled"])
        workers = min(32, len(ips))
        self._busy_show("Pinging servers…", f"{len(ips)} server IPs, {workers} at a time, 4 pings each",
                        corner=True)
        self._ping_done = 0
        dc_of_ip = {r.ip: r.dc for r in self.all_raids}

        def one(ip):
            res = icmp_ping(ip)
            self._ui(self._ping_result, ip, dc_of_ip.get(ip, ""), res, len(ips))
            return ip, res

        def work():
            with ThreadPoolExecutor(workers) as ex:
                results = dict(ex.map(one, ips))
            self._ui(self._pinged, results)
        threading.Thread(target=work, daemon=True).start()

    def _ping_result(self, ip, dc, res, total):
        self._ping_done += 1
        self.live[ip] = res
        city = CITY_NAMES.get(dc.rstrip("0123456789"), "")
        got = f"{statistics.median(res):.0f} ms" if res else "no reply"
        self._busy_update(self._ping_done, total,
                          f"{dc}{' · ' + city if city else ''}  {ip}  →  {got}\n"
                          f"{self._ping_done} of {total} servers done")
        # refresh the table every few results so the Live ping column fills in visibly
        if self._ping_done % 4 == 0:
            self.refresh()

    def _pinged(self, results):
        self._pinging = False
        self.ping_btn.state(["!disabled"])
        self._busy_hide()
        self.live.update(results)
        ok = sum(1 for v in results.values() if v)
        self._ping_summary = (f"  Live ping: {ok}/{len(results)} servers replied "
                              "(no reply = server blocks ping, not necessarily bad).")
        self.status_var.set(getattr(self, "_scan_summary", "") + self._ping_summary)
        self.refresh()

    # ---------- aggregation ----------
    def _agg(self, raids):
        rtts = [x for r in raids for x in r.rtts]
        hs = [x for r in raids for x in r.handshakes]
        # weight loss by packets sent so a short raid with a blip doesn't dominate the average
        lw = [(l, n) for r in raids for l, n in zip(r.losses, r.sent)]
        total = sum(n for _, n in lw)
        pings = [x for ip in {r.ip for r in raids} for x in self.live.get(ip, [])]
        q = [r.matching_s for r in raids if r.matching_s is not None]
        return dict(
            raids=len(raids),
            rtt=mean(rtts), rttmax=max(rtts) if rtts else None, hs=mean(hs),
            loss=sum(l * n for l, n in lw) / total if total else None,
            to=sum(r.timeouts for r in raids),
            fail=sum(r.failed_connects for r in raids),
            ping=statistics.median(pings) if pings else None,
            match=mean(q),
        )

    def _verdict(self, a):
        # Without end-of-raid stats fall back to the connect handshake, then live ping (marked ≈).
        lat = next((x for x in (a["rtt"], a["hs"], a["ping"]) if x is not None), None)
        if lat is None and not a["raids"]:
            return "no data", "none"
        v, tag = self._judge(a, lat)
        return (v + " ≈", tag) if a["rtt"] is None and tag != "none" else (v, tag)

    def _judge(self, a, lat):
        good, bad, lossbad = self.thresholds
        problems = a["raids"] and (a["to"] + a["fail"]) / a["raids"] >= 1
        if (lat is not None and lat >= bad) or (a["loss"] or 0) >= lossbad or problems:
            return "UNTICK", "bad"
        if lat is None:
            return "?", "none"
        if lat > good or (a["to"] + a["fail"]) > 0:
            return "OK", "warn"
        return "KEEP", "good"

    def _thresholds_changed(self, *_):
        # Refresh while typing, debounced; half-typed values ("", "1.") are ignored.
        try:
            self.thresholds = (self.good_var.get(), self.bad_var.get(), self.loss_var.get())
        except (tk.TclError, ValueError):
            return
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
        self._refresh_job = self.after(300, self.refresh)

    def refresh(self):
        self._refresh_job = None
        opened = {k for k in self.reg_tree.get_children("") if self.reg_tree.item(k, "open")}
        for t in (self.reg_tree, self.dc_tree, self.raid_tree):
            t.delete(*t.get_children())

        by_dc = defaultdict(list)
        for r in self.raids:
            by_dc[r.dc].append(r)
        by_region = defaultdict(list)
        for r in self.raids:
            by_region[r.region].append(r)

        known = {n for _, n in INGAME_REGIONS}
        extra = [(None, n) for n in sorted(by_region) if n not in known]
        for code, name in INGAME_REGIONS + extra:
            rs = by_region.get(name, [])
            a = self._agg(rs)
            v, tag = self._verdict(a)
            dcs = sorted({r.dc for r in rs})
            # Only whole regions can be unticked in game, so one bad datacenter
            # makes the whole region as bad as its worst member.
            dc_verdicts = {dc: self._verdict(self._agg(by_dc[dc])) for dc in dcs}
            severity = {"none": 0, "good": 1, "warn": 2, "bad": 3}
            worst = max(dc_verdicts.values(), key=lambda vt: severity[vt[1]], default=None)
            if worst and severity[worst[1]] > severity[tag]:
                v, tag = worst
            dc_labels = [("✗ " if dc_verdicts[dc][1] == "bad" else "") + dc for dc in dcs]
            iid = "reg:" + name
            self.reg_tree.insert("", "end", iid=iid, open=iid in opened, tags=(tag,), values=(
                v, code or "?", name, a["raids"] or "", fmt(a["rtt"], 0, " ms"), fmt(a["rttmax"], 0, " ms"),
                fmt(a["hs"], 0, " ms"), fmt(a["loss"], 2), a["to"] or "", a["fail"] or "", fmt(a["ping"], 0, " ms"),
                fmt(a["match"], 0), ", ".join(dc_labels)))
            for dc in dcs:
                drs = by_dc[dc]
                da = self._agg(drs)
                dv, dtag = dc_verdicts[dc]
                city = CITY_NAMES.get(dc.rstrip("0123456789"), "")
                self.reg_tree.insert(iid, "end", tags=(dtag, "child"), values=(
                    dv, "", f"    {dc}" + (f"  ·  {city}" if city else ""), da["raids"],
                    fmt(da["rtt"], 0, " ms"), fmt(da["rttmax"], 0, " ms"),
                    fmt(da["hs"], 0, " ms"), fmt(da["loss"], 2),
                    da["to"] or "", da["fail"] or "", fmt(da["ping"], 0, " ms"), fmt(da["match"], 0),
                    ", ".join(sorted({r.ip for r in drs}))))

        for dc, rs in sorted(by_dc.items(), key=lambda kv: kv[1][0].region):
            a = self._agg(rs)
            v, tag = self._verdict(a)
            ips = sorted({f"{r.ip}" for r in rs})
            self.dc_tree.insert("", "end", tags=(tag,), values=(
                v, dc, rs[0].region, a["raids"], fmt(a["rtt"], 0, " ms"), fmt(a["rttmax"], 0, " ms"),
                fmt(a["hs"], 0, " ms"), fmt(a["loss"], 2), a["to"] or "", a["fail"] or "", fmt(a["ping"], 0, " ms"),
                max(r.start for r in rs).strftime("%Y-%m-%d %H:%M"), ", ".join(ips)))

        for r in reversed(self.raids):
            a = self._agg([r])
            _, tag = self._verdict(a)
            self.raid_tree.insert("", "end", tags=(tag,), values=(
                r.start.strftime("%Y-%m-%d %H:%M"), r.dc, r.region, MAP_NAMES.get(r.location, r.location),
                r.mode, fmt(a["rtt"], 0, " ms"), fmt(a["hs"], 0, " ms"), fmt(a["loss"], 2), r.timeouts or "",
                r.failed_connects or "", fmt(r.matching_s, 0), f"{r.ip}:{r.port}"))


if __name__ == "__main__":
    enable_dpi_awareness()
    App().mainloop()
