#!/usr/bin/env python3
"""
Entertainment Hub server (Windows / macOS / Linux)

Run it on the computer that holds your movies. Open the printed address on any
device on the same Wi-Fi (phone, tablet, TV browser) to browse and stream.

    pip install flask            (optional: pip install qrcode  -> QR code in the page)
    python hub_server.py                       # uses folders saved in hub_config.json
    python hub_server.py "D:\\Movies" ~/Films  # add folders from the command line

Optional: install ffmpeg (on PATH) for automatic thumbnails and a
"compatible mode" that converts MKV/AVI/HEVC on the fly for phones.
"""
import argparse
import hashlib
import io
import json
import os
import re
import shutil
import socket
import string
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file

FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
CONFIG_FILE = APP_DIR / "hub_config.json"
THUMB_DIR = APP_DIR / "hub_thumbs"
HTML_FILE = "entertainment_hub.html"

VIDEO_EXT = {".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm", ".wmv", ".mpg", ".mpeg", ".ts", ".flv"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}
MIME = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mkv": "video/x-matroska",
    ".mov": "video/quicktime", ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv",
    ".mpg": "video/mpeg", ".mpeg": "video/mpeg", ".ts": "video/mp2t", ".flv": "video/x-flv",
}
IMG_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
EXTRA_RE = re.compile(
    r"(^|[\s._\-\[(])(sample|trailers?|featurettes?|extras|bonus|behind the scenes|deleted scenes?|"
    r"interviews|teasers?|bloopers?|gag reel|making of)([\s._\-\])]|$)", re.I)
BAD_ART_RE = re.compile(r"fanart|backdrop|banner|screenshot|screen|sample|logo|clearart|disc|landscape", re.I)
ART_NAMES = ["poster", "folder", "cover", "front", "thumb", "thumbnail", "movie", "artwork", "art", "default", "box"]

FFMPEG = shutil.which("ffmpeg")
thumb_sem = threading.Semaphore(2)
lock = threading.Lock()

CFG = {"folders": [], "min_mb": 250}
LIB = {}  # id -> entry

app = Flask(__name__)


# ---------------------------------------------------------------- config
def load_config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text("utf-8"))
    except Exception:
        cfg = {}
    cfg.setdefault("folders", [])
    cfg.setdefault("min_mb", 250)  # ignore videos smaller than this (samples/extras)
    return cfg


def save_config():
    CONFIG_FILE.write_text(json.dumps(CFG, indent=2), "utf-8")


# ---------------------------------------------------------------- scanning
def base_key(name):
    return re.sub(r"[^a-z0-9]+", "", os.path.splitext(name)[0].lower())


def pick_art(images):
    """images: list of (name, size, full_path). Mirrors the old in-browser poster logic."""
    ok = [i for i in images if not BAD_ART_RE.search(i[0])]
    for k in ART_NAMES:
        for i in ok:
            if base_key(i[0]) == k:
                return i[2]
    if len(ok) == 1:
        return ok[0][2]
    return max(ok, key=lambda i: i[1])[2] if ok else None


def scan_library():
    items = {}
    min_bytes = int(CFG.get("min_mb", 250)) * 1024 * 1024
    for folder in CFG["folders"]:
        root = Path(folder)
        if not root.is_dir():
            continue
        root_name = root.name or str(root).rstrip("\\/")
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            vids, imgs = [], []
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                full = Path(dirpath) / fn
                if ext in VIDEO_EXT:
                    vids.append(full)
                elif ext in IMAGE_EXT:
                    try:
                        imgs.append((fn, full.stat().st_size, full))
                    except OSError:
                        pass
            kept = []
            for full in vids:
                try:
                    size = full.stat().st_size
                except OSError:
                    continue
                rel = full.relative_to(root).parts
                if size < min_bytes or any(EXTRA_RE.search(p) for p in rel):
                    continue
                kept.append((full, size, rel))
            # a folder poster only makes sense when the folder holds one movie
            art = pick_art(imgs) if len(kept) == 1 else None
            for full, size, rel in kept:
                vid = hashlib.sha1(str(full).encode("utf-8", "surrogateescape")).hexdigest()[:12]
                items[vid] = {
                    "id": vid,
                    "path": "/".join((root_name,) + tuple(rel)),
                    "name": full.name,
                    "size": size,
                    "file": str(full),
                    "art": str(art) if art else None,
                }
    return items


def rescan():
    global LIB
    new = scan_library()
    with lock:
        LIB = new


# ---------------------------------------------------------------- helpers
def is_local():
    return request.remote_addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def require_local():
    if not is_local():
        abort(403)


def lan_ips():
    ips = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))  # no packet is actually sent
        ips.append(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def hub_urls():
    port = request.host.rsplit(":", 1)[-1] if ":" in request.host else "80"
    return [f"http://{ip}:{port}" for ip in lan_ips()]


def qr_svg(text):
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
        buf = io.BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode("utf-8")
        return re.sub(r"^<\?xml[^>]*\?>\s*", "", svg)
    except Exception:
        return None


def make_thumb(item):
    out = THUMB_DIR / f"{item['id']}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return out
    if not FFMPEG:
        return None
    THUMB_DIR.mkdir(exist_ok=True)
    with thumb_sem:
        for ss in ("120", "20", "0"):
            try:
                subprocess.run(
                    [FFMPEG, "-v", "error", "-ss", ss, "-i", item["file"], "-frames:v", "1",
                     "-vf", "scale=480:-2", "-q:v", "4", "-y", str(out)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
            except Exception:
                pass
            if out.exists() and out.stat().st_size > 0:
                return out
            try:
                out.unlink()
            except OSError:
                pass
    return None


# ---------------------------------------------------------------- routes
@app.get("/")
def index():
    return send_file(RES_DIR / HTML_FILE, mimetype="text/html", max_age=0)


@app.get("/api/library")
def api_library():
    if request.args.get("rescan"):
        rescan()
    with lock:
        items = [{k: v[k] for k in ("id", "path", "name", "size")} for v in LIB.values()]
    return jsonify(items=items, folders=CFG["folders"], admin=is_local(), ffmpeg=bool(FFMPEG))


@app.get("/api/info")
def api_info():
    urls = hub_urls()
    return jsonify(urls=urls, qr=qr_svg(urls[0]) if urls else None, ffmpeg=bool(FFMPEG), admin=is_local())


@app.get("/api/folders")
def api_folders():
    require_local()
    return jsonify(folders=CFG["folders"])


@app.post("/api/folders")
def api_add_folder():
    require_local()
    path = (request.get_json(silent=True) or {}).get("path", "")
    p = Path(path)
    if not path or not p.is_dir():
        return jsonify(error="That folder doesn't exist."), 400
    path = str(p.resolve())
    if path not in CFG["folders"]:
        CFG["folders"].append(path)
        save_config()
    rescan()
    return jsonify(folders=CFG["folders"])


@app.delete("/api/folders")
def api_remove_folder():
    require_local()
    path = (request.get_json(silent=True) or {}).get("path", "")
    if path in CFG["folders"]:
        CFG["folders"].remove(path)
        save_config()
    rescan()
    return jsonify(folders=CFG["folders"])


@app.get("/api/browse")
def api_browse():
    """Folder picker for the laptop's own browser (lists directories only)."""
    require_local()
    p = request.args.get("path", "")
    if not p:
        roots = [{"name": "Home", "path": str(Path.home())}]
        if os.name == "nt":
            roots += [{"name": f"{l}:\\", "path": f"{l}:\\"} for l in string.ascii_uppercase
                      if os.path.exists(f"{l}:\\")]
        else:
            roots.append({"name": "/", "path": "/"})
            for extra in ("/Volumes", "/media", "/mnt"):
                if os.path.isdir(extra):
                    roots.append({"name": extra, "path": extra})
        return jsonify(path="", parent=None, dirs=roots)
    d = Path(p)
    if not d.is_dir():
        return jsonify(error="Not a folder."), 404
    dirs = []
    try:
        for c in sorted(d.iterdir(), key=lambda x: x.name.lower()):
            try:
                if c.is_dir() and not c.name.startswith("."):
                    dirs.append({"name": c.name, "path": str(c)})
            except OSError:
                pass
    except (PermissionError, OSError):
        pass
    parent = str(d.parent) if d.parent != d else ""
    return jsonify(path=str(d), parent=parent, dirs=dirs)


@app.get("/stream/<vid>")
def stream(vid):
    it = LIB.get(vid)
    if not it:
        abort(404)
    ext = os.path.splitext(it["file"])[1].lower()
    # conditional=True gives HTTP Range support, so seeking works on phones too
    return send_file(it["file"], mimetype=MIME.get(ext, "application/octet-stream"),
                     conditional=True, max_age=3600)


@app.get("/art/<vid>")
def art(vid):
    it = LIB.get(vid)
    if not it:
        abort(404)
    if it["art"] and os.path.isfile(it["art"]):
        ext = os.path.splitext(it["art"])[1].lower()
        return send_file(it["art"], mimetype=IMG_MIME.get(ext, "image/jpeg"), max_age=86400)
    t = make_thumb(it)
    if t:
        return send_file(t, mimetype="image/jpeg", max_age=86400)
    abort(404)


@app.get("/transcode/<vid>")
def transcode(vid):
    """Compatible mode: convert to H.264/AAC fragmented MP4 on the fly (needs ffmpeg)."""
    it = LIB.get(vid)
    if not it:
        abort(404)
    if not FFMPEG:
        abort(503)
    cmd = [FFMPEG, "-v", "error", "-i", it["file"], "-map", "0:v:0", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-ac", "2", "-b:a", "160k",
           "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def gen():
        try:
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:  # runs when the viewer closes the video, so ffmpeg never lingers
            proc.kill()
            proc.wait()

    return Response(gen(), mimetype="video/mp4", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- main
def main():
    global CFG
    ap = argparse.ArgumentParser(description="Entertainment Hub server")
    ap.add_argument("folders", nargs="*", help="movie folders to add")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true", help="don't open the hub on this computer")
    args = ap.parse_args()

    CFG = load_config()
    changed = False
    for f in args.folders:
        p = str(Path(f).expanduser().resolve())
        if Path(p).is_dir() and p not in CFG["folders"]:
            CFG["folders"].append(p)
            changed = True
        elif not Path(p).is_dir():
            print(f"  ! Not a folder, skipped: {f}")
    if changed or not CONFIG_FILE.exists():
        save_config()

    print("Scanning library...")
    rescan()
    print(f"  {len(LIB)} movie(s) in {len(CFG['folders'])} folder(s)")
    print("  ffmpeg:", FFMPEG or "not found (no auto thumbnails / compatible mode)")
    print("\nOn this computer:   http://localhost:%d" % args.port)
    for ip in lan_ips():
        print("On your phone:      http://%s:%d   (same Wi-Fi)" % (ip, args.port))
    print("\nPress Ctrl+C to stop.\n")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{args.port}")).start()
    app.run(host="0.0.0.0", port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
