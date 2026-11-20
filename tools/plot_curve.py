"""Draw docs/threshold_curve.svg from docs/threshold_curve.csv (written by threshold_curve.py).

    python tools/plot_curve.py

Plain SVG, no plotting library: x = false alerts (log scale), y = detection, one line
per method, every 5th threshold marked. Light and dark mode follow the viewer's setting.
"""
from __future__ import annotations

import csv
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC, OUT = ROOT / "docs" / "threshold_curve.csv", ROOT / "docs" / "threshold_curve.svg"

# Fixed categorical order (validated for colour-blind separation, light and dark).
SERIES = [("plain fuzzy, token_set_ratio", "token_set", "s1"),
          ("plain fuzzy, token_sort_ratio", "token_sort (best simple)", "s2"),
          ("ours + birth year", "ours + birth year", "s3"),
          ("ours + birth year + nationality", "ours + birth year + nationality", "s4")]
W, H = 720, 460
L, R, T, B = 64, 200, 48, 56          # plot margins; the right one holds the direct labels
X_MIN, X_MAX = 0.1, 100.0             # false alerts, %, log scale (0 is drawn at 0.1)
Y_MIN, Y_MAX = 40.0, 100.0            # detection, %


def x(fa: float) -> float:
    v = min(max(fa, X_MIN), X_MAX)
    return L + (math.log10(v) - math.log10(X_MIN)) / (math.log10(X_MAX) - math.log10(X_MIN)) * (W - L - R)


def y(det: float) -> float:
    return T + (Y_MAX - max(det, Y_MIN)) / (Y_MAX - Y_MIN) * (H - T - B)


def main() -> None:
    rows = list(csv.DictReader(open(SRC, encoding="utf-8")))
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
           'font-family="system-ui, -apple-system, Segoe UI, sans-serif" font-size="12">',
           "<style>",
           ":root{--bg:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--grid:#e1e0d9;"
           "--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100}",
           "@media (prefers-color-scheme: dark){:root{--bg:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--grid:#2c2c2a;"
           "--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500}}",
           ".bg{fill:var(--bg)}.t{fill:var(--ink)}.m{fill:var(--ink2)}.g{stroke:var(--grid);stroke-width:1}",
           "".join(f".{c}{{stroke:var(--{c})}}.{c}f{{fill:var(--{c})}}" for *_, c in SERIES),
           "</style>",
           f'<rect class="bg" width="{W}" height="{H}" rx="8"/>',
           f'<text class="t" x="{L}" y="24" font-size="14" font-weight="600">'
           "Detection vs false alerts, thresholds 60-95 (seed 7, held-out spellings)</text>"]
    for v in (0.1, 0.3, 1, 3, 10, 30, 100):
        out.append(f'<line class="g" x1="{x(v):.1f}" x2="{x(v):.1f}" y1="{T}" y2="{H - B}"/>')
        out.append(f'<text class="m" x="{x(v):.1f}" y="{H - B + 18}" text-anchor="middle">{v:g}%</text>')
    for v in range(40, 101, 10):
        out.append(f'<line class="g" x1="{L}" x2="{W - R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>')
        out.append(f'<text class="m" x="{L - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{v}%</text>')
    out.append(f'<text class="m" x="{(L + W - R) / 2}" y="{H - 14}" text-anchor="middle">'
               "false alerts on 5,000 fictional customers (log scale, 0 drawn at 0.1%)</text>")
    out.append(f'<text class="m" transform="translate(18 {(T + H - B) / 2}) rotate(-90)" text-anchor="middle">'
               "detection of 1,200 altered listed names</text>")

    label_y: list[float] = []
    for method, label, c in SERIES:
        pts = sorted(((int(r["threshold"]), 100 * float(r["false_alerts"]), 100 * float(r["detection"]))
                      for r in rows if r["method"] == method))
        path = " ".join(f"{x(fa):.1f},{y(d):.1f}" for _, fa, d in pts)
        out.append(f'<polyline class="{c}" fill="none" stroke-width="2" stroke-linejoin="round" points="{path}"/>')
        for t, fa, d in pts:
            if t % 5 == 0:
                out.append(f'<circle class="{c}f" cx="{x(fa):.1f}" cy="{y(d):.1f}" r="4" stroke="var(--bg)" stroke-width="2">'
                           f"<title>{label}, threshold {t}: detection {d:.1f}%, false alerts {fa:.1f}%</title></circle>")
        # threshold-80 point gets its number; the line end gets the series name
        t80 = next(p for p in pts if p[0] == 80)
        out.append(f'<text class="m" x="{x(t80[1]) + 7:.1f}" y="{y(t80[2]) - 7:.1f}" font-size="11">80</text>')
        ly = y(pts[0][2])
        while any(abs(ly - o) < 15 for o in label_y):
            ly += 15
        label_y.append(ly)
        out.append(f'<circle class="{c}f" cx="{W - R + 14}" cy="{ly:.1f}" r="4"/>')
        out.append(f'<text class="t" x="{W - R + 24}" y="{ly + 4:.1f}">{label}</text>')
    out.append(f'<text class="m" x="{W - R + 14}" y="{max(label_y) + 28:.1f}" font-size="11">'
               "dots: thresholds 60, 65 ... 95</text>")
    out.append("</svg>")
    OUT.write_text("\n".join(out), encoding="utf-8")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
