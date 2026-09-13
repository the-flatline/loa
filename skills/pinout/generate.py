#!/usr/bin/env python3
"""Render a single-component pinout diagram from a YAML spec.

    python3 generate.py components/hc-sr04.yaml -o out/
    python3 generate.py components/*.yaml -o out/ --fritzing

Every field the cheap module datasheets leave out is mandatory here: pin voltage,
direction, body size and the gotchas. A 5 V output on a 3.3 V part renders as a
warning and drags its note onto the drawing whether you remember it or not.
"""
import argparse, os, sys, xml.sax.saxutils as sx
import yaml

MM = 3.7795                                  # px per mm at 96 dpi
BG, CARD, INK, MUTE, LINE = "#f4f2ec", "#ffffff", "#20242a", "#8a867c", "#c9c2b1"
BODY, BODY_TXT = "#2b3138", "#e7ebef"
TYPE = {                                     # pin class -> (colour, short label)
    "power": ("#c81e1e", "PWR"), "gnd": ("#3c3c3c", "GND"),
    "input": ("#0a7a2e", "IN"),  "output": ("#0a7a2e", "OUT"),
    "bidir": ("#7a1fc9", "I/O"), "i2c": ("#0b62c9", "I2C"),
    "spi": ("#7a1fc9", "SPI"),   "analog": ("#a3791c", "ANA"),
    "nc": ("#9aa3ad", "NC"),
}
DANGER = "#d1490b"

def esc(t): return sx.escape(str(t))

class Doc:
    def __init__(s, w, h): s.w, s.h, s.o = w, h, []
    def add(s, x): s.o.append(x)
    def txt(s, x, y, t, fill=INK, size=12, anchor="start", weight="normal", family=None):
        f = f' font-family="{family}"' if family else ""
        s.add(f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" '
              f'text-anchor="{anchor}" font-weight="{weight}"{f}>{esc(t)}</text>')
    def save(s, path):
        with open(path, "w") as fh:
            fh.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{s.w}" height="{s.h}" '
                     f'viewBox="0 0 {s.w} {s.h}" font-family="ui-monospace, DejaVu Sans Mono, monospace">'
                     f'<rect width="{s.w}" height="{s.h}" fill="{BG}"/>'
                     + "\n".join(s.o) + "</svg>")

def validate(c, path):
    errs = []
    for k in ("name", "body_mm", "pins"):
        if k not in c: errs.append(f"missing required key: {k}")
    for p in c.get("pins", []):
        if "n" not in p or "name" not in p: errs.append(f"pin missing n/name: {p}")
        t = p.get("type")
        if t not in TYPE: errs.append(f"pin {p.get('n')}: unknown type {t!r} (one of {sorted(TYPE)})")
        if t in ("power", "input", "output", "bidir", "analog") and "voltage" not in p:
            errs.append(f"pin {p.get('n')} ({p.get('name')}): voltage is required for {t} pins")
    for g in c.get("gotchas", []):
        if not isinstance(g, str):
            errs.append(f"gotcha is not a string (an unquoted colon makes YAML a dict): {g!r}")
    if errs:
        print(f"[{path}] SPEC ERRORS:", file=sys.stderr)
        for e in errs: print("  -", e, file=sys.stderr)
        return False
    return True

def render(c, out_dir):
    pins = c["pins"]
    n = len(pins)
    # Pin spacing is set for legibility, not to scale — real pitch is printed in the header.
    # The body keeps its true aspect ratio and carries mm labels on both edges.
    pitch = 78
    span = pitch * (n - 1)
    bw = max(span + 90, 280)
    aspect = c["body_mm"][1] / c["body_mm"][0]
    bh = max(90, min(bw * aspect, 300))
    W = max(820, bw + 300)
    lead, label_h = 46, 128
    top = 168
    H = int(top + bh + lead + label_h + 190)
    d = Doc(int(W), H)

    d.txt(40, 52, c["name"], INK, 27, weight="bold")
    d.txt(40, 76, c.get("full_name", ""), MUTE, 12.5)
    d.txt(40, 96, f'body {c["body_mm"][0]} x {c["body_mm"][1]} mm  ·  '
                  f'{n} pin  ·  {c.get("pitch_mm",2.54)} mm pitch'
                  + (f'  ·  {c["current_ma"]} mA' if c.get("current_ma") else "")
                  + (f'  ·  {c["bus"]}' if c.get("bus") else ""), MUTE, 11.5)
    d.txt(40, 114, 'body to scale (aspect + mm) · pin spacing enlarged for legibility', MUTE, 10)

    bx = 40
    d.add(f'<rect x="{bx}" y="{top}" width="{bw:.1f}" height="{bh:.1f}" rx="9" '
          f'fill="{BODY}" stroke="#1a1e24"/>')
    d.txt(bx + bw / 2, top + bh / 2 + 5, c["name"], BODY_TXT, 15, anchor="middle", weight="bold")
    # body is drawn to scale; say so
    d.add(f'<line x1="{bx}" y1="{top-14}" x2="{bx+bw:.1f}" y2="{top-14}" stroke="{MUTE}" stroke-width="1"/>')
    d.txt(bx + bw / 2, top - 20, f'{c["body_mm"][0]} mm', MUTE, 10, anchor="middle")
    d.add(f'<line x1="{bx+bw+12:.1f}" y1="{top}" x2="{bx+bw+12:.1f}" y2="{top+bh:.1f}" stroke="{MUTE}"/>')
    d.txt(bx + bw + 18, top + bh / 2, f'{c["body_mm"][1]} mm', MUTE, 10)

    x0 = bx + (bw - pitch * (n - 1)) / 2
    ytop, ybot = top + bh, top + bh + lead
    for i, p in enumerate(pins):
        x = x0 + i * pitch
        col, short = TYPE[p["type"]]
        danger = bool(p.get("danger"))
        if danger: col = DANGER
        d.add(f'<rect x="{x-7:.1f}" y="{ytop-3:.1f}" width="14" height="12" fill="{col}"/>')
        d.add(f'<line x1="{x:.1f}" y1="{ytop:.1f}" x2="{x:.1f}" y2="{ybot:.1f}" '
              f'stroke="{col}" stroke-width="3.4" stroke-linecap="round"/>')
        d.add(f'<circle cx="{x:.1f}" cy="{ybot:.1f}" r="4" fill="{col}"/>')
        ty = ybot + 16
        d.txt(x, ty, str(p["n"]), MUTE, 10, anchor="middle")
        d.txt(x, ty + 15, p["name"], INK, 12, anchor="middle", weight="bold")
        d.txt(x, ty + 30, short, col, 9.5, anchor="middle")
        if "voltage" in p:
            v = f'{p["voltage"]}V'
            d.txt(x, ty + 45, v, DANGER if danger else MUTE, 10.5, anchor="middle",
                  weight="bold" if danger else "normal")
        if danger:
            d.add(f'<rect x="{x-20:.1f}" y="{ty+34}" width="40" height="16" rx="3" fill="none" '
                  f'stroke="{DANGER}" stroke-width="1.2"/>')

    py = top + bh + lead + label_h + 20
    px, pw = bx, W - 80
    notes = list(c.get("gotchas", []))
    dngr = [f'pin {p["n"]} {p["name"]}: {p.get("danger_note", "level mismatch — check before wiring")}'
            for p in pins if p.get("danger")]
    rows = [(DANGER, t) for t in dngr] + [(INK, t) for t in notes]
    if rows:
        d.add(f'<rect x="{px}" y="{py}" width="{pw:.1f}" height="{28+len(rows)*17}" rx="7" '
              f'fill="{CARD}" stroke="{LINE}"/>')
        d.txt(px + 16, py + 20, "before you wire it", INK, 12, weight="bold")
        for i, (colr, t) in enumerate(rows):
            d.txt(px + 16, py + 40 + i * 17, ("!  " if colr == DANGER else "·  ") + t, colr, 10.5)
    d.txt(40, H - 22, f'source: {c.get("source", "unverified — no datasheet cited")}', MUTE, 10)

    p = os.path.join(out_dir, c["name"].lower().replace(" ", "-").replace("/", "-") + ".svg")
    d.save(p)
    return p

def fritzing(c, out_dir):
    """Emit a Fritzing custom part: breadboard SVG with connectorNpin ids + the .fzp.
    Untested against Fritzing itself — open it and check the connectors before trusting it."""
    pins, name = c["pins"], c["name"]
    slug = name.lower().replace(" ", "-").replace("/", "-")
    fdir = os.path.join(out_dir, "fritzing"); os.makedirs(fdir, exist_ok=True)
    n = len(pins)
    pitch_in, w_in, h_in = 0.1, max(0.1 * n, 0.4), 0.3
    U = 1000                                    # 1 unit = 1/1000 in
    sw, sh = int(w_in * U), int(h_in * U)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w_in}in" height="{h_in}in" '
             f'viewBox="0 0 {sw} {sh}">',
             f'<rect x="0" y="0" width="{sw}" height="{int(sh*0.62)}" fill="#2b3138"/>']
    for i, p in enumerate(pins):
        cx = int((i + 0.5) * pitch_in * U)
        parts.append(f'<rect id="connector{i}pin" x="{cx-15}" y="{int(sh*0.62)}" width="30" '
                     f'height="{int(sh*0.38)}" fill="#8c8942"/>')
        parts.append(f'<text x="{cx}" y="{int(sh*0.45)}" font-size="40" fill="#e7ebef" '
                     f'text-anchor="middle">{esc(p["name"])}</text>')
    parts.append("</svg>")
    open(os.path.join(fdir, f"{slug}_breadboard.svg"), "w").write("\n".join(parts))

    conns = []
    for i, p in enumerate(pins):
        desc = f'{p["name"]} ({p["type"]}' + (f', {p["voltage"]}V' if "voltage" in p else "") + ")"
        conns.append(
            f'    <connector type="male" id="connector{i}" name="{esc(p["name"])}">\n'
            f'      <description>{esc(desc)}</description>\n'
            f'      <views><breadboardView><p svgId="connector{i}pin" layer="breadboard"/>'
            f'</breadboardView></views>\n    </connector>')
    fzp = (f'<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<module fritzingVersion="0.9.6" moduleId="{slug}ModuleID">\n'
           f'  <title>{esc(name)}</title>\n'
           f'  <label>{esc(name)}</label>\n'
           f'  <description>{esc(c.get("full_name", name))}</description>\n'
           f'  <views>\n    <breadboardView><layers image="breadboard/{slug}_breadboard.svg">'
           f'<layer layerId="breadboard"/></layers></breadboardView>\n  </views>\n'
           f'  <connectors>\n' + "\n".join(conns) + '\n  </connectors>\n</module>\n')
    open(os.path.join(fdir, f"{slug}.fzp"), "w").write(fzp)
    return fdir

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("specs", nargs="+")
    ap.add_argument("-o", "--out", default="out")
    ap.add_argument("--fritzing", action="store_true", help="also emit a Fritzing part (untested)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    bad = 0
    for s in a.specs:
        c = yaml.safe_load(open(s))
        if not validate(c, s): bad += 1; continue
        print("wrote", render(c, a.out))
        if a.fritzing: print("  fritzing ->", fritzing(c, a.out))
    sys.exit(1 if bad else 0)
