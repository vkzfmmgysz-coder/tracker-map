import os
import re
import json
import hashlib
from datetime import datetime
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import pdfplumber

app = Flask(__name__)
CORS(app)

PDF_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(PDF_DIR, "uploads")
CACHE_FILE = os.path.join(PDF_DIR, "tracker_cache.json")
os.makedirs(UPLOAD_DIR, exist_ok=True)

DEVICE_COLORS = {
    "Samsung Find My Mobile": "#e74c3c",
    "Google Find My Device": "#3498db",
    "Samsung SmartTag": "#2ecc71",
    "AirTag": "#f39c12",
    "Unknown": "#9b59b6",
}


def pdf_fingerprint(filepath):
    st = os.stat(filepath)
    return f"{os.path.basename(filepath)}:{st.st_mtime}"


def parse_pdf(filepath):
    try:
        with pdfplumber.open(filepath) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    except Exception:
        return None

    device_type = "Unknown"
    for dt in DEVICE_COLORS:
        if dt in text:
            device_type = dt
            break

    followed = "This tracker followed you" in text

    first_seen = re.search(r"First seen:\s*(.+)", text)
    last_seen  = re.search(r"Last seen:\s*(.+)", text)
    first_seen = first_seen.group(1).strip() if first_seen else ""
    last_seen  = last_seen.group(1).strip()  if last_seen  else ""

    locations = []
    pattern = re.compile(
        r"(\w+ \d+, \d{4} at \d+:\d+ [AP]M)\s*\n"
        r"Location:\s*([\d.]+),\s*([\d.]+)",
        re.MULTILINE,
    )
    for m in pattern.finditer(text):
        try:
            dt = datetime.strptime(m.group(1).strip(), "%b %d, %Y at %I:%M %p")
            locations.append({
                "time": dt.isoformat(),
                "time_display": m.group(1).strip(),
                "lat": float(m.group(2)),
                "lng": float(m.group(3)),
            })
        except ValueError:
            continue

    if not locations:
        return None

    locations.sort(key=lambda x: x["time"])
    basename = os.path.basename(filepath)
    agent_id = hashlib.md5(basename.encode()).hexdigest()[:8]

    return {
        "id": agent_id,
        "filename": basename,
        "device_type": device_type,
        "followed": followed,
        "first_seen": first_seen,
        "last_seen": last_seen,
        "locations": locations,
        "color": DEVICE_COLORS.get(device_type, "#9b59b6"),
    }


def load_cache():
    if not os.path.exists(CACHE_FILE):
        return {"entries": {}, "all_trackers": []}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "entries" not in data:
            return {"entries": data, "all_trackers": []}
        return data
    except Exception:
        return {"entries": {}, "all_trackers": []}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def scan_and_update_cache():
    """掃描本機 PDF，更新快取並回傳完整清單。若無 PDF 則從快取讀取。"""
    cache = load_cache()
    entries = cache.get("entries", {})
    trackers = []
    seen = set()

    for d in [PDF_DIR, UPLOAD_DIR]:
        if not os.path.exists(d):
            continue
        for fname in os.listdir(d):
            if not fname.endswith(".pdf") or fname in seen:
                continue
            seen.add(fname)
            path = os.path.join(d, fname)
            key = pdf_fingerprint(path)
            data = entries.get(key) or parse_pdf(path)
            if data:
                entries[key] = data
                trackers.append(data)

    if trackers:
        trackers.sort(key=lambda x: x["locations"][0]["time"])
        cache["entries"] = entries
        cache["all_trackers"] = trackers
        save_cache(cache)
        return trackers

    # 雲端部署：無 PDF，直接從快取中的完整清單回傳
    return cache.get("all_trackers", [])


# ── 啟動時預載到記憶體，避免每次請求都重新掃描 ──
_trackers_memory: list = []


def _init_trackers():
    global _trackers_memory
    _trackers_memory = scan_and_update_cache()


_init_trackers()


@app.route("/api/debug")
def api_debug():
    cache = load_cache()
    return jsonify({
        "memory_count": len(_trackers_memory),
        "cache_all_trackers": len(cache.get("all_trackers", [])),
        "cache_entries": len(cache.get("entries", {})),
        "cache_file_exists": os.path.exists(CACHE_FILE),
        "pdf_files": [f for f in os.listdir(PDF_DIR) if f.endswith(".pdf")],
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/trackers")
def api_trackers():
    return jsonify(_trackers_memory)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    global _trackers_memory
    if "files" not in request.files:
        return jsonify({"error": "No files"}), 400
    files = request.files.getlist("files")
    cache = load_cache()
    entries = cache.get("entries", {})
    results = []

    for f in files:
        if not f.filename.endswith(".pdf"):
            continue
        dest = os.path.join(UPLOAD_DIR, f.filename)
        f.save(dest)
        data = parse_pdf(dest)
        if data:
            key = pdf_fingerprint(dest)
            entries[key] = data
        results.append({
            "filename": f.filename,
            "ok": data is not None,
            "locations": len(data["locations"]) if data else 0,
        })

    # 重新整理記憶體清單
    _trackers_memory = scan_and_update_cache()
    return jsonify({"uploaded": results})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, host="0.0.0.0", port=port)
