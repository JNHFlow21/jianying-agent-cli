#!/usr/bin/env python3
"""Regenerate docs/demo.gif from synthetic media only (no user footage, no 剪映).

Needs: ffmpeg (ffmpeg-full recommended), jq, Google Chrome. Usage:
    python3 docs/render_demo.py
"""
import html, json, os, re, shutil, subprocess, sys, tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FF = shutil.which("ffmpeg", path="/opt/homebrew/opt/ffmpeg-full/bin") or shutil.which("ffmpeg")
S = tempfile.mkdtemp(prefix="jy-demo-")
RUN = os.path.join(S, "run")
os.makedirs(RUN)
CMDS = [
    "jy probe a.mp4 | jq -c '.data.files[0] | {codec, width, height, duration_s}'",
    "jy transition a.mp4 b.mp4 --type 叠化 --duration 0.8 -o joined.mp4 | jq -c '{ok, resolution: .data.resolution | {requested, resolved, fidelity}}'",
    "jy text joined.mp4 --content \"Hello from jy\" --position top --font-size 64 --animation slideup -o titled.mp4 | jq -c '{ok, outcome}'",
    "jy export titled.mp4 -o final.mp4 --resolution 720p --faststart | jq -c '.data.output | {codec, width, height, duration_s}'",
    "jy frame final.mp4 --at 40% -o shot.jpg | jq -c '{ok, at_s: .data.at_s}'",
    "jy export missing.mp4 -o x.mp4 | jq -c '{ok, error: .error | {code, retriable}}'",
]
for name, src_filter, hz in (("a.mp4", "testsrc2", 440), ("b.mp4", "smptebars", 660)):
    subprocess.run([FF, "-v", "error", "-f", "lavfi", "-i", f"{src_filter}=size=1280x720:rate=30:duration=3",
                    "-f", "lavfi", "-i", f"sine=frequency={hz}:duration=3", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", os.path.join(RUN, name)], check=True)
env = dict(os.environ, PATH=os.path.join(ROOT, "bin") + os.pathsep + os.environ["PATH"])
pairs = []
for cmd in CMDS:
    out = subprocess.run(cmd, shell=True, cwd=RUN, env=env, capture_output=True, text=True).stdout.strip()
    pairs.append((cmd, out))
os.makedirs(os.path.join(S, "vf"))
for i, t in enumerate((0.3, 0.8, 1.4, 2.0, 2.3, 2.5, 2.7, 2.9, 3.3, 4.0, 4.8)):
    subprocess.run([FF, "-v", "error", "-ss", str(t), "-i", os.path.join(RUN, "final.mp4"), "-frames:v", "1",
                    "-vf", "scale=1760:-2", "-y", os.path.join(S, "vf", f"v{i:02d}.jpg")], check=True)
W, H = 960, 600
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CSS = """
body{margin:0;background:#0d1117;font-family:'SF Mono',Menlo,monospace;}
.win{width:%dpx;height:%dpx;box-sizing:border-box;background:#0d1117;color:#c9d1d9;overflow:hidden}
.bar{height:34px;background:#161b22;display:flex;align-items:center;padding:0 14px;gap:8px;border-bottom:1px solid #30363d}
.dot{width:12px;height:12px;border-radius:50%%}
.title{color:#8b949e;font-size:13px;margin-left:12px;font-family:-apple-system,Helvetica}
.term{padding:14px 18px;font-size:14.5px;line-height:1.5;white-space:pre-wrap;word-break:break-all}
.p{color:#3fb950}.c{color:#e6edf3}.k{color:#79c0ff}.s{color:#a5d6ff}.t{color:#56d364}.f{color:#ff7b72}.n{color:#d2a8ff}
.cur{display:inline-block;width:8px;height:17px;background:#c9d1d9;vertical-align:-3px}
.vid{display:flex;flex-direction:column;align-items:center;justify-content:center;height:%dpx;gap:12px}
.vid img{width:880px;border-radius:6px;border:1px solid #30363d}
.cap{color:#8b949e;font-size:14px}
""" % (W, H, H - 34)

def color_json(s):
    out = []
    tok = re.compile(r'("(?:[^"\\]|\\.)*")(\s*:)?|\b(true)\b|\b(false)\b|(-?\d+(?:\.\d+)?)|([{}\[\],:])')
    pos = 0
    for m in tok.finditer(s):
        out.append(html.escape(s[pos:m.start()]))
        if m.group(1):
            cls = "k" if m.group(2) else "s"
            out.append(f'<span class="{cls}">{html.escape(m.group(1))}</span>{m.group(2) or ""}')
        elif m.group(3): out.append('<span class="t">true</span>')
        elif m.group(4): out.append('<span class="f">false</span>')
        elif m.group(5): out.append(f'<span class="n">{m.group(5)}</span>')
        else: out.append(html.escape(m.group(6)))
        pos = m.end()
    out.append(html.escape(s[pos:]))
    return "".join(out)

def page(body, title="jy — headless video editing"):
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body><div class="win">
<div class="bar"><span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span><span class="title">{title}</span></div>{body}</div></body></html>"""

frames = []  # (html, duration)
done = []
def term(extra, cursor=True):
    body = "".join(done) + extra + ('<span class="cur"></span>' if cursor else "")
    return f'<div class="term">{body}</div>'

intro = '<span class="c"># Edit a video with plain CLI calls. Every call prints one jy.v1 JSON object.</span>\n'
done.append(intro)
frames.append((page(term("")), 1.2))
for cmd, out in pairs:
    prompt = '<span class="p">$ </span>'
    for frac in (0.35, 0.7, 1.0):
        n = int(len(cmd) * frac)
        frames.append((page(term(prompt + f'<span class="c">{html.escape(cmd[:n])}</span>')), 0.18 if frac < 1 else 0.5))
    done.append(prompt + f'<span class="c">{html.escape(cmd)}</span>\n' + color_json(out) + "\n")
    frames.append((page(term("")), 1.6))
frames[-1] = (frames[-1][0], 2.6)
vids = sorted(os.listdir(os.path.join(S, "vf")))
for v in vids:
    img = "file://" + os.path.join(S, "vf", v)
    frames.append((page(f'<div class="vid"><img src="{img}"><div class="cap">final.mp4 — synthetic test clips · 叠化 transition · animated title · 720p</div></div>', "final.mp4"), 0.35))
frames[-1] = (frames[-1][0], 1.5)

os.makedirs(os.path.join(S, "frames"), exist_ok=True)
concat = []
for i, (h, d) in enumerate(frames):
    hp = os.path.join(S, "frames", f"f{i:03d}.html"); pp = hp[:-5] + ".png"
    open(hp, "w", encoding="utf-8").write(h)
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files",
                    f"--window-size={W},{H}", "--force-device-scale-factor=2", f"--screenshot={pp}", "file://" + hp],
                   check=True, capture_output=True, timeout=60)
    concat.append(f"file '{pp}'\nduration {d}")
concat.append(f"file '{pp}'")
open(os.path.join(S, "frames", "list.txt"), "w").write("\n".join(concat) + "\n")
out_gif = os.path.join(ROOT, "docs", "demo.gif")
subprocess.run([FF, "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", os.path.join(S, "frames", "list.txt"),
                "-vf", "fps=10,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];"
                       "[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
                "-loop", "0", out_gif], check=True)
shutil.rmtree(S, ignore_errors=True)
print(json.dumps({"ok": True, "gif": out_gif, "frames": len(frames), "bytes": os.path.getsize(out_gif)}))
