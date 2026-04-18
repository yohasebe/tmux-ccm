#!/usr/bin/env python3
"""Generate SVG screenshots for ccm README.

Produces terminal-style SVG images closely matching actual ccm output.

Usage: python3 scripts/generate-svg.py
"""

import os, html

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
os.makedirs(OUT_DIR, exist_ok=True)

# ── Tokyo Night palette ──
BG      = "#1a1b26"
FG      = "#c0caf5"
DIM     = "#565f89"
WHITE   = "#e0e0e0"
GREEN   = "#9ece6a"
YELLOW  = "#e0af68"
RED     = "#f7768e"
CYAN    = "#7dcfff"
ORANGE  = "#ff9e64"
BLUE    = "#7aa2f7"
MAGENTA = "#bb9af7"

# tmux status bar
BAR_GREEN   = "#5faf5f"   # standard tmux green
BAR_TEXT    = "#000000"
BAR_DIM     = "#2a4a2a"   # dim text on green
LINE_BG     = "#1f2335"   # mode 2 dedicated line
SEL_BG      = "#283457"   # dashboard selection
ACTIVE_BG   = "#3b4261"   # active window highlight

# ── Monospace cell ──
CW = 8.2          # character width
CH = 19            # line height
FONT = "'JetBrains Mono','Fira Code','SF Mono','Menlo',monospace"
FS = 13.5          # font size
PAD = 14           # horizontal padding
TOP = 36           # top offset (below window controls)

def _e(s): return html.escape(s)

class SVG:
    def __init__(self, w_chars, h_lines, title="", chrome=True):
        self.w = int(PAD*2 + w_chars * CW)
        top = TOP if chrome else 8
        self.h = int(top + h_lines * CH + 8)
        self.top = top
        self.chrome = chrome
        self.title = title
        self.els = []

    def x(self, c): return PAD + c * CW
    def y(self, r): return self.top + r * CH + CH * 0.76

    def t(self, c, r, s, color=FG, bold=False, sz=FS):
        """Add text at character position (c, r)."""
        self.els.append(
            f'<text x="{self.x(c):.1f}" y="{self.y(r):.1f}" fill="{color}" '
            f'font-weight="{"bold" if bold else "normal"}" font-size="{sz}">'
            f'{_e(s)}</text>')

    def bar(self, r, h, color):
        """Full-width rect."""
        self.els.append(
            f'<rect x="0" y="{self.top + r*CH:.1f}" width="{self.w}" '
            f'height="{h*CH:.1f}" fill="{color}"/>')

    def box(self, c, r, wc, hc, color, rx=0):
        """Rect at char position."""
        self.els.append(
            f'<rect x="{self.x(c):.1f}" y="{self.top + r*CH:.1f}" '
            f'width="{wc*CW:.1f}" height="{hc*CH:.1f}" fill="{color}" rx="{rx}"/>')

    def save(self, name):
        path = os.path.join(OUT_DIR, name)
        chrome = ""
        if self.chrome:
            chrome = ('  <circle cx="18" cy="14" r="5.5" fill="#ff5f57"/>\n'
                      '  <circle cx="36" cy="14" r="5.5" fill="#febc2e"/>\n'
                      '  <circle cx="54" cy="14" r="5.5" fill="#28c840"/>')
        body = "\n".join(f"  {e}" for e in self.els)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" '
               f'viewBox="0 0 {self.w} {self.h}" width="{self.w}" height="{self.h}">\n'
               f'  <title>{_e(self.title)}</title>\n'
               f'  <style>text{{font-family:{FONT};font-size:{FS}px}}</style>\n'
               f'  <rect width="{self.w}" height="{self.h}" fill="{BG}" rx="8"/>\n'
               f'{chrome}\n{body}\n</svg>')
        with open(path, "w") as f: f.write(svg)
        print(f"  {path}")

# ── Mock data ──
PROJ = [
    dict(i=2, ic="◉", st="BUSY",   sc=ORANGE, n="api-gateway",   br="feat/rate-limiting", t="6s",  d="~/code/api-gateway",   pt="8080"),
    dict(i=5, ic="⚠", st="PERMIT", sc=YELLOW, n="ml-pipeline",   br="main*",              t="20s", d="~/code/ml-pipeline",   pt=""),
    dict(i=4, ic="●", st="IDLE",   sc=BLUE,   n="auth-service",  br="fix/token-refresh",  t="2s",  d="~/code/auth-service",  pt="9090", completed=True),
    dict(i=3, ic="●", st="IDLE",   sc=BLUE,   n="web-dashboard", br="main",               t="1m",  d="~/code/web-dashboard", pt="3000"),
    dict(i=6, ic="●", st="IDLE",   sc=BLUE,   n="mobile-app",    br="release/2.1",        t="5m",  d="~/code/mobile-app",    pt="8081"),
    dict(i=7, ic="■", st="SHELL",  sc=DIM,    n="docs-site",     br="main",               t="1d",  d="~/code/docs-site",     pt="4321"),
]

# ═══════════════════════════════════════
#  Dashboard
# ═══════════════════════════════════════
def gen_dashboard():
    s = SVG(90, 15, "ccm dashboard")
    # Title border:  ── ccm Dashboard ───────...
    s.t(1, 0, "── ccm Dashboard " + "─"*55, DIM)
    # Count
    s.t(2, 1, "6 project(s)", DIM)
    # Project rows
    #     ▶ #2  ◉BUSY   api-gateway      (feat/rate-limiting) ✔6s  ~/code/api-gateway
    # Cols: 1   3  6 7    14               32                  49   54
    # ✔ (col 50) is shown only on rows that just completed (within
    # COMPLETED_AT_TIMEOUT) — it is a display-layer marker, not a
    # detection state. Other rows show the elapsed time without ✔.
    for ri, p in enumerate(PROJ):
        r = ri + 3
        if ri == 0:
            s.box(-0.5, r-0.12, 91, 1, SEL_BG)
            s.t(1, r, "▶", DIM)
        s.t(3, r, f"#{p['i']}", DIM)
        s.t(6, r, p["ic"], p["sc"])
        s.t(7, r, p["st"], p["sc"])       # icon + state adjacent
        s.t(14, r, p["n"], WHITE, bold=True)
        s.t(30, r, f"({p['br']})", MAGENTA)
        if p.get("completed"):
            s.t(50, r, "✔", GREEN)
            s.t(51, r, p["t"], DIM)
        else:
            s.t(51, r, p["t"], DIM)
        s.t(55, r, p["d"], DIM)

    # Help line
    hr = 12
    parts = "[↑↓/jk] select  [Enter] attach  [p]review  [a]dd  [n]ame  [r]emove  e[x]it all  [s]ave  [t]ree  [m]enu  [q] quit"
    s.t(1, hr, parts, DIM, sz=11)
    # Hooks
    s.t(1, hr+1, "Hooks:", DIM, sz=11)
    s.t(8, hr+1, "ON", CYAN, sz=11)

    s.save("dashboard.svg")

# ═══════════════════════════════════════
#  Status mode 0 — icon in status-right
# ═══════════════════════════════════════
def gen_mode0():
    s = SVG(110, 5, "status bar — mode 0")
    # Prompt
    s.t(0, 0.5, "~/code/api-gateway", BLUE)
    s.t(20, 0.5, "feat/rate-limiting*", DIM)
    s.t(0, 1.5, "❯", CYAN, bold=True)

    # Green bar
    BR = 3.5
    s.bar(BR, 1.05, BAR_GREEN)
    s.t(0.5, BR, "[work]", BAR_TEXT, bold=True)

    # Window list (standard tmux, but window names include ccm icons)
    items = ["1:zsh", "2:◉ api-gateway*", "3:● web-dashboard",
             "4:● auth-service", "5:⚠ ml-pipeline", "6:● mobile-app"]
    c = 8
    for item in items:
        bold = "*" in item
        s.t(c, BR, item, BAR_TEXT if bold else BAR_DIM, bold=bold, sz=12)
        c += len(item) + 2

    # Right: time + BUSY badge
    s.t(92, BR, "11:30 08-Apr", BAR_DIM, sz=12)
    s.box(106, BR+0.08, 3.5, 0.85, ORANGE, rx=4)
    s.t(106.3, BR, "◉", BAR_TEXT, bold=True, sz=13)

    s.save("status-mode0.svg")

# ═══════════════════════════════════════
#  Status mode 1 — window list replaced
# ═══════════════════════════════════════
def gen_mode1():
    s = SVG(110, 5, "status bar — mode 1")
    s.t(0, 0.5, "~/code/api-gateway", BLUE)
    s.t(20, 0.5, "feat/rate-limiting*", DIM)
    s.t(0, 1.5, "❯", CYAN, bold=True)

    BR = 3.5
    s.bar(BR, 1.05, BAR_GREEN)
    s.t(0.5, BR, "[work]", BAR_TEXT, bold=True)

    # Project entries: name:icon
    entries = [
        ("api-gateway:◉",   ORANGE, True),
        ("ml-pipeline:⚠",   YELLOW, False),
        ("auth-service:●",  BLUE,   False),
        ("web-dashboard:●", BLUE,   False),
        ("mobile-app:●",    BLUE,   False),
        ("docs-site:■",     DIM,    False),
    ]
    c = 9
    for label, color, active in entries:
        if active:
            s.box(c-0.3, BR+0.06, len(label)+0.6, 0.88, "#1a1b26", rx=3)
        s.t(c, BR, label, color if active else BAR_DIM, bold=active, sz=12)
        c += len(label) + 2

    s.t(97, BR, "11:30 08-Apr", BAR_DIM, sz=12)
    s.save("status-mode1.svg")

# ═══════════════════════════════════════
#  Status mode 2 — dedicated status lines
# ═══════════════════════════════════════
def gen_mode2():
    s = SVG(110, 7, "status bar — mode 2")
    s.t(0, 0.5, "~/code/api-gateway", BLUE)
    s.t(20, 0.5, "feat/rate-limiting*", DIM)
    s.t(0, 1.5, "❯", CYAN, bold=True)

    # Green status bar FIRST (top)
    BR = 3.2
    s.bar(BR, 1.05, BAR_GREEN)
    s.t(0.5, BR, "[work]", BAR_TEXT, bold=True)
    s.t(9, BR, "1:zsh*", BAR_TEXT, bold=True, sz=12)
    s.t(92, BR, "11:30 08-Apr", BAR_DIM, sz=12)

    # Dedicated ccm lines BELOW the green bar
    L1, L2 = 4.3, 5.3
    s.bar(L1, 2.05, LINE_BG)

    entries1 = [
        ("2:api-gateway:◉",  ORANGE), ("5:ml-pipeline:⚠",  YELLOW),
        ("4:auth-service:●", BLUE),   ("3:web-dashboard:●", BLUE),
    ]
    entries2 = [
        ("6:mobile-app:●",   BLUE),   ("7:docs-site:■",    DIM),
    ]

    # Line 1
    c = 1
    for label, color in entries1:
        s.t(c, L1, label, color, bold=True, sz=12)
        c += len(label) + 3

    # Line 2
    c = 1
    for label, color in entries2:
        s.t(c, L2, label, color, bold=True, sz=12)
        c += len(label) + 3

    s.save("status-mode2.svg")

# ═══════════════════════════════════════
if __name__ == "__main__":
    print("Generating SVGs...")
    gen_dashboard()
    gen_mode0()
    gen_mode1()
    gen_mode2()
    print("Done.")
