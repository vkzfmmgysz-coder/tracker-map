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
    """Stable key: filename + mtime."""
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
        # 相容舊格式（只有 entries 沒有 all_trackers）
        if isinstance(data, dict) and "entries" not in data:
            return {"entries": data, "all_trackers": []}
        return data
    except Exception:
        return {"entries": {}, "all_trackers": []}


def save_cache(cache):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def load_all_trackers():
    cache = load_cache()
    entries = cache.get("entries", {})
    trackers = []
    seen = set()
    changed = False

    for d in [PDF_DIR, UPLOAD_DIR]:
        if not os.path.exists(d):
            continue
        for fname in os.listdir(d):
            if not fname.endswith(".pdf") or fname in seen:
                continue
            seen.add(fname)
            path = os.path.join(d, fname)
            key = pdf_fingerprint(path)

            if key in entries:
                data = entries[key]
            else:
                data = parse_pdf(path)
                if data:
                    entries[key] = data
                    changed = True

            if data:
                trackers.append(data)

    if trackers:
        trackers.sort(key=lambda x: x["locations"][0]["time"])
        # 有 PDF 時更新完整清單快取
        cache["entries"] = entries
        cache["all_trackers"] = trackers
        save_cache(cache)
    else:
        # 沒有 PDF（如雲端部署），直接回傳快取的完整清單
        trackers = cache.get("all_trackers", [])

    return trackers


@app.route("/api/debug")
def api_debug():
    cache = load_cache()
    return jsonify({
        "cache_file_exists": os.path.exists(CACHE_FILE),
        "cache_file_path": CACHE_FILE,
        "all_trackers_count": len(cache.get("all_trackers", [])),
        "entries_count": len(cache.get("entries", {})),
        "pdf_dir": PDF_DIR,
        "pdf_files": [f for f in os.listdir(PDF_DIR) if f.endswith(".pdf")],
        "upload_files": [f for f in os.listdir(UPLOAD_DIR) if f.endswith(".pdf")] if os.path.exists(UPLOAD_DIR) else [],
    })


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/trackers")
def api_trackers():
    return jsonify(load_all_trackers())


@app.route("/api/upload", methods=["POST"])
def api_upload():
    if "files" not in request.files:
        return jsonify({"error": "No files"}), 400
    files = request.files.getlist("files")
    results = []
    cache = load_cache()

    for f in files:
        if not f.filename.endswith(".pdf"):
            continue
        dest = os.path.join(UPLOAD_DIR, f.filename)
        f.save(dest)
        data = parse_pdf(dest)
        if data:
            key = pdf_fingerprint(dest)
            cache[key] = data
        results.append({
            "filename": f.filename,
            "ok": data is not None,
            "locations": len(data["locations"]) if data else 0,
        })

    save_cache(cache)
    return jsonify({"uploaded": results})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    app.run(debug=False, host="0.0.0.0", port=port)
