#!/usr/bin/env python3
"""Regenerate docs/demo.gif and docs/demo.mp4 — the README hero demo.

Everything is reproducible from public-domain footage (U.S. National Park Service
time-lapses on Wikimedia Commons, see docs/DEMO-CREDITS.md). The script:

  1. downloads the five source clips by URL (never touches local media),
  2. pre-processes them with jy's local engine (trim, reverse, speed) and then
     edits the film with `jy` itself:
       --engine jianying (default, what the README shows): `jy draft build` + `jy render`
         — 剪映's own native engine renders genuine 剪映 materials (叠化 transitions,
         暗灰电影感 filter, 暗角 video effect, 渐显入场 text animation). Needs 剪映专业版
         desktop running and those materials in its local cache (open each once in 剪映).
         The draft lives in this script's temp dir; nothing is added to your 剪映 projects.
       --engine local: the same film with ffmpeg approximations only (montage + 叠化,
         filter 青橙, ASS titles, export) — no 剪映 needed.
  3. renders the terminal + player stage with headless Chrome,
  4. composites the finished film into the stage and encodes GIF + MP4.

Needs macOS Google Chrome, ffmpeg (ffmpeg-full recommended), jq, gifsicle, and network
for the first run. Usage:
    python3 docs/render_demo.py [--engine jianying|local] [--footage-dir DIR] [--keep]
--footage-dir caches the downloads between runs (default: a temp dir).
"""
import argparse, concurrent.futures, html, json, os, re, shutil, subprocess, sys, tempfile
import time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FF = shutil.which("ffmpeg", path="/opt/homebrew/opt/ffmpeg-full/bin") or shutil.which("ffmpeg")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
UA = "jy-demo/1.0 (https://github.com/risingjerry/jianying-agent-cli)"

# name -> (public-domain source URL, trim start s, trim duration s). Credits: docs/DEMO-CREDITS.md
COMMONS = "https://upload.wikimedia.org/wikipedia/commons/"
FOOTAGE = {
    "sunrise": (COMMONS + "f/f1/Sunrise_Timelapse_%2830174294051%29.webm", 8, 9.5),
    "valley": (COMMONS + "e/e9/Sunset_Near_Hidden_Valley_timelapse_%2840734114682%29.webm", 0, 10.8),
    "dusk": (COMMONS + "3/32/Dusk_to_night_at_Joshua_Tree_%2830497809811%29.webm", 0, 12),
    "rock": (COMMONS + "1/12/Stars_and_Clouds_Over_Headstone_Rock_%2830230087163%29.webm", 0, 14),
    "stars": (COMMONS + "e/e6/Night_Sky_Timelapse_%2830549248476%29.webm", 0, 9.6),
}

# The agent session shown in the terminal: real commands, real (jq-trimmed) output.
PREP = [
    "jy probe clips/*.mp4 | jq -c '{ok, seconds: [.data.files[].duration_s]}'",
    "jy reverse clips/sunrise.mp4 -o clips/sunset.mp4 | jq -c '{ok, outcome}'",
    "for c in sunset valley dusk rock stars; do jy speed clips/$c.mp4 --factor 3.6 --no-audio -o fast/$c.mp4; done"
    " | jq -sc '{ok: (map(.ok) | all), clips: length}'",
]
JIANYING_CMDS = PREP + [
    "jq -c '{transitions: [.clips[].transition.name // empty], filter: .filters[0].name, effect: .effects[0].name,"
    " title: .texts[0].content}' nightfall.json",
    "jy draft build --spec nightfall.json -o jy-demo-nightfall"
    " | jq -c '{ok, engine: .data.engine, tracks: [.data.tracks[].type]}'",
    "jy render jy-demo-nightfall -o nightfall.mp4"
    " | jq -c '{ok, engine: .data.engine, output: .data.output | {codec, width, height, duration_s}}'",
]
LOCAL_CMDS = [
    "jy probe clips/*.mp4 | jq -c '{ok, seconds: [.data.files[].duration_s]}'",
    "jy reverse clips/sunrise.mp4 -o clips/sunset.mp4 | jq -c '{ok, outcome}'",
    "for c in sunset valley dusk rock stars; do jy speed clips/$c.mp4 --factor 3.6 --no-audio -o fast/$c.mp4; done"
    " | jq -sc '{ok: (map(.ok) | all), clips: length}'",
    "jy montage fast/{sunset,valley,dusk,rock,stars}.mp4 --per-clip 2.6 --transition 叠化 --transition-duration 0.6"
    " -o cut.mp4 | jq -c '.data.resolution | {requested, resolved, fidelity}'",
    "jy filter cut.mp4 --preset 青橙 --intensity 60 -o graded.mp4 | jq -c '.data.resolution | {requested, resolved, fidelity}'",
    "jy subtitle graded.mp4 --srt titles.ass -o titled.mp4 | jq -c '{ok, outcome}'",
    "jy export titled.mp4 -o nightfall.mp4 --resolution 1080p --faststart | jq -c '.data.output | {codec, width, height, duration_s}'",
]

# 剪映 materials for the native render: (cache subdir, resource_id, name). They must already be
# in 剪映's local cache — jy never invents ids, and an uncached package renders as a no-op.
JY_CACHE = os.path.expanduser("~/Movies/JianyingPro/User Data/Cache")
JY_MATERIALS = {
    "transition": ("effect", "6724845717472416269", "叠化"),
    "filter": ("artistEffect", "7682752012542332169", "暗灰电影感"),
    "effect": ("effect", "7399495012515433728", "暗角"),
    "text_in": ("effect", "7660452282344983851", "渐显入场"),
}
SHOT = 2.4  # seconds per shot in the 剪映 timeline; transitions overlap the junctions
CAPTIONS = [("blue hour", 2.6, 1.6), ("starlight", 5.0, 1.6), ("moonlit clouds", 7.4, 1.6),
            ("made with jy", 9.8, 2.0)]
FONT = "/System/Library/Fonts/Avenir Next.ttc"


def jy_package(kind):
    sub, rid, name = JY_MATERIALS[kind]
    base = os.path.join(JY_CACHE, sub, rid)
    pkgs = [d for d in os.listdir(base) if os.path.isdir(os.path.join(base, d))] if os.path.isdir(base) else []
    if not pkgs:
        raise SystemExit(f"剪映 material {name} ({rid}) is not cached; open it once in 剪映, then retry")
    return {"resource_id": rid, "name": name, "path": os.path.join(base, pkgs[0])}


def jianying_spec():
    names = ["sunset", "valley", "dusk", "rock", "stars"]
    clips = []
    for i, n in enumerate(names):
        clip = {"path": f"fast/{n}.mp4", "start": round(i * SHOT, 3), "duration": SHOT}
        if i < len(names) - 1:
            clip["transition"] = dict(jy_package("transition"), duration=0.6)
        clips.append(clip)

    def text(content, start, dur, y, size, tracking, anim):
        return {"content": content, "start": start, "duration": dur, "position": {"x": 0, "y": y},
                "style": {"color": "#FFFFFF", "font_size": size, "stroke_width": 0.0, "bold": False,
                          "letter_spacing": tracking, "font_path": FONT},
                "animations": [dict(jy_package("text_in"), type="in", duration=anim)]}
    texts = [text("NIGHTFALL", 0.3, 1.9, -0.12, 16, 0.35, 0.8)]
    texts += [text(c, s, d, -0.8, 5.6, 0.12, 0.4) for c, s, d in CAPTIONS]
    return {"canvas": {"width": 1920, "height": 1080, "fps": 30}, "clips": clips, "texts": texts,
            "filters": [dict(jy_package("filter"), value=0.6)], "effects": [jy_package("effect")]}

# Title card + captions, burned by `jy subtitle`. Styling is inline (\fn \fs \fsp) because
# `jy subtitle` applies FontName/FontSize via force_style to every style in the file.
TITLES = [  # start, end, text, (x, y), font size, tracking, alpha, font
    (0.20, 2.15, "A FILM CUT BY AN AGENT", (960, 428), 30, 12, "10", "Avenir Next Demi Bold"),
    (0.35, 2.15, "NIGHTFALL", (960, 532), 156, 40, "00", "Avenir Next Medium"),
    (0.50, 2.15, "FIVE PUBLIC-DOMAIN TIME-LAPSES  ·  ONE CLI", (960, 640), 30, 12, "10", "Avenir Next Demi Bold"),
    (2.35, 3.75, "blue hour", (960, 975), 44, 6, "00", "Avenir Next Medium"),
    (4.35, 5.75, "the first stars", (960, 975), 44, 6, "00", "Avenir Next Medium"),
    (6.35, 7.75, "moonlit clouds", (960, 975), 44, 6, "00", "Avenir Next Medium"),
    (8.35, 10.5, "made with jy", (960, 975), 40, 2, "00", "Menlo"),
]


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def fetch(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    for attempt in range(6):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r, open(dest + ".part", "wb") as f:
                shutil.copyfileobj(r, f)
            os.replace(dest + ".part", dest)
            return
        except Exception as exc:  # Commons rate-limits bursts (HTTP 429)
            print(f"retry {os.path.basename(dest)}: {exc}", file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    raise SystemExit(f"could not download {url}")


def ass_time(t):
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def titles_ass():
    out = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080",
           "WrapStyle: 2", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
           "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
           "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
           "Style: T,Avenir Next,40,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1",
           "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for start, end, text, (x, y), fs, fsp, alpha, font in TITLES:
        fade = "\\fad(500,400)" if fs < 100 or y < 900 else "\\fad(250,250)"
        base = f"\\an5\\pos({x},{y}){fade}\\fn{font}\\fs{fs}\\fsp{fsp}\\b0\\i0\\bord0\\shad0"
        # layer 0: soft blurred shadow for legibility on bright skies; layer 1: crisp text
        out.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},T,,0,0,0,,"
                   f"{{{base}\\1c&H000000&\\1a&H60&\\bord10\\3c&H000000&\\3a&H90&\\blur22}}{text}")
        out.append(f"Dialogue: 1,{ass_time(start)},{ass_time(end)},T,,0,0,0,,{{{base}\\1a&H{alpha}&}}{text}")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------- stage (HTML)
W, H = 1200, 750                  # CSS px; rendered at 2x
TERM = (90, 52, 1020, 646)        # x, y, w, h
FILM = (72, 36, 1056, 594)        # 16:9 player
CSS = """
*{box-sizing:border-box}
html,body{margin:0;width:%(W)dpx;height:%(H)dpx;overflow:hidden;background:#07080b}
body{font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;-webkit-font-smoothing:antialiased}
.bg{position:absolute;inset:0;background:
 radial-gradient(760px 520px at 12%% -6%%,rgba(255,150,80,.20),transparent 70%%),
 radial-gradient(760px 560px at 104%% 108%%,rgba(70,130,220,.22),transparent 70%%),
 linear-gradient(180deg,#0a0b10,#06070a)}
.amb{position:absolute;inset:-80px;background-size:cover;background-position:center;filter:blur(70px) saturate(1.3);opacity:0}
.win{position:absolute;left:%(TX)dpx;top:%(TY)dpx;width:%(TW)dpx;height:%(TH)dpx;border-radius:14px;
 background:rgba(14,16,22,.94);border:1px solid rgba(255,255,255,.09);
 box-shadow:0 40px 90px rgba(0,0,0,.55),0 12px 30px rgba(0,0,0,.35);overflow:hidden;transform-origin:50%% 50%%}
.bar{height:40px;display:flex;align-items:center;padding:0 16px;gap:8px;border-bottom:1px solid rgba(255,255,255,.06);
 background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,.01))}
.dot{width:12px;height:12px;border-radius:50%%}
.ttl{position:absolute;left:0;right:0;text-align:center;color:#8a8f99;font-size:13px;font-weight:500;letter-spacing:.2px;pointer-events:none}
.term{padding:20px 26px;font:14.5px/1.62 ui-monospace,'SF Mono',Menlo,monospace;color:#d6d9e0;white-space:pre-wrap;overflow-wrap:anywhere}
.cl{display:block;padding-left:2ch;text-indent:-2ch}
.cm{color:#5f6673}.pr{color:#f5a65b}.jy{color:#fff;font-weight:600}.vb{color:#f5c27a}.fl{color:#8fb6ff}.pi{color:#6b7280}
.k{color:#8fb6ff}.s{color:#f5c27a}.n{color:#c6a8ff}.t{color:#6ee7b7}.p{color:#6b7280}
.out{display:block;margin:2px 0 12px}
.cur{display:inline-block;width:8px;height:17px;background:#f5a65b;vertical-align:-3px;border-radius:1px}
.film{position:absolute;left:%(FX)dpx;top:%(FY)dpx;width:%(FW)dpx;height:%(FH)dpx;border-radius:16px;overflow:hidden;
 background:#000;box-shadow:0 50px 110px rgba(0,0,0,.6),0 0 0 1px rgba(255,255,255,.08);opacity:0;transform-origin:50%% 50%%}
.film img{width:100%%;height:100%%;display:block}
.cap{position:absolute;left:%(FX)dpx;right:%(FX)dpx;top:%(CY)dpx;display:flex;align-items:center;gap:10px;opacity:0}
.name{color:#f3f4f6;font-size:15px;font-weight:600;letter-spacing:.1px;margin-right:6px}
.play{width:0;height:0;border-left:9px solid #f5a65b;border-top:6px solid transparent;border-bottom:6px solid transparent;margin-right:2px}
.chip{font:12px/1 ui-monospace,'SF Mono',Menlo,monospace;color:#c9ced8;padding:6px 9px;border-radius:7px;
 background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.07)}
.by{margin-left:auto;color:#7b818c;font-size:13px}
.by b{color:#d6d9e0;font-weight:600}
""" % dict(W=W, H=H, TX=TERM[0], TY=TERM[1], TW=TERM[2], TH=TERM[3], FX=FILM[0], FY=FILM[1], FW=FILM[2],
           FH=FILM[3], CY=FILM[1] + FILM[3] + 26)


def color_json(s):
    out, pos = [], 0
    tok = re.compile(r'("(?:[^"\\]|\\.)*")(\s*:)?|\b(true|false|null)\b|(-?\d+(?:\.\d+)?)|([{}\[\],:])')
    for m in tok.finditer(s):
        out.append(html.escape(s[pos:m.start()]))
        if m.group(1):
            out.append(f'<span class="{"k" if m.group(2) else "s"}">{html.escape(m.group(1))}</span>'
                       f'{"<span class=p>:</span>" if m.group(2) else ""}')
        elif m.group(3):
            out.append(f'<span class="t">{m.group(3)}</span>')
        elif m.group(4):
            out.append(f'<span class="n">{m.group(4)}</span>')
        else:
            out.append(f'<span class="p">{html.escape(m.group(5))}</span>')
        pos = m.end()
    out.append(html.escape(s[pos:]))
    return "".join(out)


def color_cmd(cmd):
    """Light shell highlighting: jy + verb, --flags, pipes."""
    parts = re.split(r"(\s+)", cmd)
    out, after_jy = [], False
    for p in parts:
        e = html.escape(p)
        if p == "jy":
            out.append(f'<span class="jy">{e}</span>'); after_jy = True; continue
        if after_jy and p.strip():
            out.append(f'<span class="vb">{e}</span>'); after_jy = False; continue
        if p.startswith("--") or (p.startswith("-") and len(p) == 2 and p[1].isalpha()):
            out.append(f'<span class="fl">{e}</span>')
        elif p in ("|", ";", "do", "done", "for", "in"):
            out.append(f'<span class="pi">{e}</span>')
        else:
            out.append(e)
    return "".join(out)


def page(term_html, tp=0.0, fp=0.0, poster="", cap_html=""):
    """tp: terminal exit progress 0..1; fp: film entrance progress 0..1 (eased by caller)."""
    t_style = (f"transform:translateY({-26 * tp:.2f}px) scale({1 - 0.08 * tp:.4f});"
               f"opacity:{1 - tp:.3f};filter:blur({8 * tp:.2f}px)")
    f_style = f"transform:translateY({40 * (1 - fp):.2f}px) scale({0.94 + 0.06 * fp:.4f});opacity:{fp:.3f}"
    amb = f'<div class="amb" style="background-image:url({poster});opacity:{0.22 * fp:.3f}"></div>' if poster else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<div class="bg"></div>{amb}
<div class="win" style="{t_style}"><div class="bar"><span class="dot" style="background:#ff5f57"></span>
<span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span>
<span class="ttl">agent — ~/films/nightfall</span></div><div class="term">{term_html}</div></div>
<div class="film" style="{f_style}">{f'<img src="{poster}">' if poster else ''}</div>
<div class="cap" style="opacity:{max(0.0, fp * 1.4 - 0.4):.3f}">{cap_html}</div>
</body></html>"""


def screenshot(job):
    hp, pp = job
    for attempt in range(3):  # headless Chrome occasionally exits early under load
        try:
            return _shot(hp, pp)
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(1)


def _shot(hp, pp):
    sh([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files",
        "--default-background-color=00000000", f"--window-size={W},{H}", "--force-device-scale-factor=2",
        "--virtual-time-budget=1500", f"--screenshot={pp}", "file://" + hp], capture_output=True, timeout=90)


def ease(x):
    return 1 - (1 - x) ** 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--footage-dir", help="cache dir for the downloaded public-domain clips")
    ap.add_argument("--keep", action="store_true", help="keep the scratch dir (prints its path)")
    ap.add_argument("--engine", choices=("jianying", "local"), default="jianying",
                    help="jianying = 剪映 native render (default, needs 剪映 running); local = ffmpeg only")
    a = ap.parse_args()
    native = a.engine == "jianying"
    cmds = JIANYING_CMDS if native else LOCAL_CMDS
    S = tempfile.mkdtemp(prefix="jy-demo-")
    raw = os.path.abspath(a.footage_dir) if a.footage_dir else os.path.join(S, "footage")
    run, fr = os.path.join(S, "run"), os.path.join(S, "frames")
    for d in (raw, os.path.join(run, "clips"), os.path.join(run, "fast"), fr):
        os.makedirs(d, exist_ok=True)
    # JY_STAGING_DIR keeps draft media in the temp dir instead of ~/Movies/jy-staging
    env = dict(os.environ, PATH=os.path.join(ROOT, "bin") + os.pathsep + os.environ["PATH"],
               JY_STAGING_DIR=os.path.join(S, "staging"))

    # 1. footage -> clips/*.mp4 (download, then `jy trim` to the usable section)
    for name, (url, start, dur) in FOOTAGE.items():
        src = os.path.join(raw, name + ".webm")
        fetch(url, src)
        sh(["jy", "trim", src, "--start", str(start), "--duration", str(dur), "-o",
            os.path.join(run, "clips", name + ".mp4"), "--force"], env=env, capture_output=True)
    if native:
        with open(os.path.join(run, "nightfall.json"), "w", encoding="utf-8") as f:
            json.dump(jianying_spec(), f, ensure_ascii=False, indent=1)
    else:
        with open(os.path.join(run, "titles.ass"), "w", encoding="utf-8") as f:
            f.write(titles_ass())

    # 2. the agent session — every command really runs; output is shown verbatim
    pairs = []
    for cmd in cmds:
        p = subprocess.run(cmd, shell=True, cwd=run, env=env, capture_output=True, text=True,
                           executable="/bin/bash")
        out = p.stdout.strip()
        if p.returncode or '"ok":false' in out or not out:
            raise SystemExit(f"demo command failed: {cmd}\n{out}\n{p.stderr[-2000:]}")
        pairs.append((cmd, out.replace(run + "/", "")))
    film = os.path.join(run, "nightfall.mp4")
    info = json.loads(pairs[-1][1])
    info = info.get("output", info)
    poster = os.path.join(S, "poster.jpg")
    sh([FF, "-v", "error", "-y", "-i", film, "-frames:v", "1", "-q:v", "2", poster])

    # 3. stage frames
    frames = []  # (html, seconds)
    goal = ("render it with 剪映's own engine" if native else "edit it with ffmpeg only")
    done = [f'<span class="cm"># agent: cut five public-domain time-lapses into a film and {goal}</span>\n']
    prompt = '<span class="pr">❯ </span>'

    def term(extra="", cursor=True):
        return "".join(done) + extra + ('<span class="cur"></span>' if cursor else "")

    frames.append((page(term(prompt)), 0.8))
    for cmd, out in pairs:
        steps = max(4, min(9, len(cmd) // 22))
        for i in range(1, steps + 1):
            n = int(len(cmd) * i / steps)
            frames.append((page(term(f'<span class="cl">{prompt}{color_cmd(cmd[:n])}')), 0.05 if i < steps else 0.3))
        done.append(f'<span class="cl">{prompt}{color_cmd(cmd)}</span><span class="out">{color_json(out)}</span>')
        frames.append((page(term(prompt)), 0.7))
    frames[-1] = (frames[-1][0], 1.5)
    o = info
    cap = (f'<span class="play"></span><span class="name">nightfall.mp4</span>'
           f'<span class="chip">{o["width"]}×{o["height"]}</span><span class="chip">{o["duration_s"]:.1f} s</span>'
           + ('<span class="chip">叠化 ×4</span><span class="chip">暗灰电影感</span><span class="chip">暗角</span>'
              '<span class="chip">渐显入场</span><span class="by">rendered by <b>剪映</b> engine · via jy</span>'
              if native else
              f'<span class="chip">叠化 ×4</span><span class="chip">青橙 60%</span><span class="chip">{o["codec"]} · faststart</span>'
              '<span class="by">edited by <b>jy</b> · local ffmpeg engine</span>'))
    poster_url = "file://" + poster
    n_tr = 14
    for i in range(1, n_tr + 1):
        x = i / n_tr
        frames.append((page(term("", cursor=False), tp=ease(min(1, x * 1.6)), fp=ease(max(0, x * 1.4 - 0.4)),
                            poster=poster_url, cap_html=cap), 1 / 15))
    stage_html = page(term("", cursor=False), tp=1, fp=1, poster=poster_url, cap_html=cap)
    mask_html = (f'<!doctype html><html><body style="margin:0;background:#000;width:{W}px;height:{H}px">'
                 f'<div style="position:absolute;left:{FILM[0]}px;top:{FILM[1]}px;width:{FILM[2]}px;'
                 f'height:{FILM[3]}px;border-radius:16px;background:#fff"></div></body></html>')

    jobs = []
    for i, (h, _) in enumerate(frames + [(stage_html, 0), (mask_html, 0)]):
        hp = os.path.join(fr, f"f{i:03d}.html")
        open(hp, "w", encoding="utf-8").write(h)
        jobs.append((hp, hp[:-5] + ".png"))
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        list(pool.map(screenshot, jobs))
    stage_png, mask_png = jobs[-2][1], jobs[-1][1]

    # 4. assemble a 2x master: terminal frames, then the film composited into the stage
    lst = os.path.join(fr, "list.txt")
    with open(lst, "w") as f:
        for (h, d), (_, pp) in zip(frames, jobs):
            f.write(f"file '{pp}'\nduration {d:.4f}\n")
        f.write(f"file '{jobs[len(frames) - 1][1]}'\n")
    seg1, seg2, master = (os.path.join(S, n) for n in ("seg1.mkv", "seg2.mkv", "master.mkv"))
    sh([FF, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-vf", "fps=30,format=yuv444p",
        "-c:v", "libx264", "-crf", "8", "-preset", "fast", seg1])
    fx, fy, fw, fh = (v * 2 for v in FILM)
    dur = float(o["duration_s"]) + 0.7
    sh([FF, "-v", "error", "-y", "-loop", "1", "-i", stage_png, "-i", film, "-loop", "1", "-i", mask_png,
        "-filter_complex",
        f"[2]crop={fw}:{fh}:{fx}:{fy},format=gray[m];"
        f"[1]fps=30,scale={fw}:{fh}:flags=lanczos,tpad=stop_mode=clone:stop_duration=0.7,format=yuva444p[v];"
        f"[v][m]alphamerge[vm];[0][vm]overlay={fx}:{fy}:shortest=1,format=yuv444p",
        "-t", f"{dur:.3f}", "-c:v", "libx264", "-crf", "8", "-preset", "fast", seg2])
    # loop seam: dissolve the last film frame back into the opening terminal frame
    sh([FF, "-v", "error", "-y", "-i", seg1, "-i", seg2, "-loop", "1", "-t", "0.6", "-i", jobs[0][1],
        "-filter_complex",
        f"[2]scale=2400:1500,format=yuv444p,fps=30,settb=1/30[o];[0][1]concat=n=2:v=1,fps=30,settb=1/30[c];"
        f"[c][o]xfade=transition=fade:duration=0.5:offset={_dur(seg1) + dur - 0.5:.3f}",
        "-c:v", "libx264", "-crf", "8", "-preset", "fast", master])

    out_mp4, out_gif = os.path.join(ROOT, "docs", "demo.mp4"), os.path.join(ROOT, "docs", "demo.gif")
    sh([FF, "-v", "error", "-y", "-i", master, "-vf", "scale=1920:1200:flags=lanczos,format=yuv420p",
        "-c:v", "libx264", "-crf", "24", "-preset", "slow", "-tune", "film", "-movflags", "+faststart", "-an", out_mp4])
    # GIF: 960 px (GitHub's README column is narrower), 9 fps, temporal denoise so night-sky
    # grain does not rewrite every frame. One 256-colour palette per shot (cut mid-dissolve)
    # avoids banding in the dusk gradients; gifsicle merges the chunks with lossy LZW.
    gifsicle = shutil.which("gifsicle")
    if not gifsicle:
        raise SystemExit("gifsicle is required for the GIF (brew install gifsicle)")
    # film start in the master; shot spacing (剪映: junctions at SHOT·k; montage: dissolve midpoints)
    f0 = _dur(seg1)
    shot_cuts = [SHOT * k for k in range(1, 5)] if native else [2.0 * k + 0.3 for k in range(1, 5)]
    cuts = [0, f0 + 0.3] + [f0 + c for c in shot_cuts] + [_dur(master)]
    chunks = []
    for i in range(len(cuts) - 1):
        ch = os.path.join(S, f"chunk{i}.gif")
        sh([FF, "-v", "error", "-y", "-i", master, "-vf",
            f"fps=9,scale=960:-2:flags=lanczos,hqdn3d=3:3:10:10,trim=start={cuts[i]:.3f}:end={cuts[i + 1]:.3f},"
            "setpts=PTS-STARTPTS,split[a][b];[a]palettegen=max_colors=256:stats_mode=diff[p];"
            "[b][p]paletteuse=dither=none:diff_mode=rectangle", "-loop", "0", ch])
        chunks.append(ch)
    # lossy LZW only on the film shots; the terminal chunk stays pixel-exact
    sh([gifsicle, "-O3", "--no-warnings", "--batch", chunks[0]])
    sh([gifsicle, "-O3", "--lossy=130", "--no-warnings", "--batch", *chunks[1:]])
    sh([gifsicle, "--no-warnings", "--merge", *chunks, "--loopcount=forever", "-o", out_gif])
    if a.keep:
        print(f"scratch: {S}", file=sys.stderr)
    else:
        shutil.rmtree(S, ignore_errors=True)
    print(json.dumps({"ok": True, "gif": out_gif, "gif_bytes": os.path.getsize(out_gif),
                      "mp4": out_mp4, "mp4_bytes": os.path.getsize(out_mp4), "frames": len(frames)}))


def _dur(path):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                 path], capture_output=True, text=True, check=True).stdout)


if __name__ == "__main__":
    main()
