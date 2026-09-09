"""WCAG contrast for every text/background pair the two themes actually use."""
import io
import re
import sys


def parse(block):
    return dict(re.findall(r"(--[\w-]+)\s*:\s*(#[0-9A-Fa-f]{6}|var\(--[\w-]+\))", block))


def resolve(tokens, name, depth=0):
    v = tokens.get(name)
    if v is None or depth > 5:
        return None
    m = re.match(r"var\((--[\w-]+)\)", v)
    return resolve(tokens, m.group(1), depth + 1) if m else v


def lum(hexcol):
    r, g, b = (int(hexcol[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def ratio(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


src = io.open(sys.argv[1], encoding="utf-8").read()
style = src[src.index("<style>"):src.index("</style>")]

light = parse(style[style.index(":root{"):style.index("}", style.index(":root{"))])
dstart = style.index(':root[data-theme="dark"]{')
dark = dict(light)
dark.update(parse(style[dstart:style.index("}", dstart)]))

# (label, foreground token, background token, minimum)  4.5 body text, 3.0 UI
PAIRS = [
    ("body text on page",        "--ink",       "--bg",          4.5),
    ("body text on panel",       "--ink",       "--panel",       4.5),
    ("muted text on panel",      "--muted",     "--panel",       4.5),
    ("muted text on page",       "--muted",     "--bg",          4.5),
    ("accent text on panel",     "--accent",    "--panel",       4.5),
    ("accent on its own tint",   "--accent",    "--accent-soft", 4.5),
    ("text on accent button",    "--accent-ink", "--accent",     4.5),
    ("warn text on warn tint",   "--warn",      "--warn-soft",   4.5),
    ("bad text on bad tint",     "--bad",       "--bad-soft",    4.5),
    ("ok text on ok tint",       "--ok",        "--ok-soft",     4.5),
    ("hint text on hover row",   "--muted",     "--hover",       4.5),
    ("panel-2 body text",        "--ink",       "--panel-2",     4.5),
    ("off text on panel",        "--off",       "--panel",       3.0),
    ("border against panel",     "--line",      "--panel",       1.2),
    ("strong border on panel-2", "--line-strong", "--panel-2",   1.5),
]

worst = 0
for theme_name, tokens in (("LIGHT", light), ("DARK", dark)):
    print("\n%s" % theme_name)
    for label, fg, bg, need in PAIRS:
        f, b = resolve(tokens, fg), resolve(tokens, bg)
        if not f or not b:
            print("  %-26s MISSING %s/%s" % (label, fg, bg))
            worst += 1
            continue
        r = ratio(f, b)
        ok = r >= need
        if not ok:
            worst += 1
        print("  %-26s %-8s on %-8s %5.2f  need %.1f  %s"
              % (label, f, b, r, need, "ok" if ok else "FAIL"))

print("\n%d failing pair(s)" % worst)
sys.exit(1 if worst else 0)
