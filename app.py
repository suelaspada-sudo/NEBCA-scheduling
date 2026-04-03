"""
NEBCA R4H Scheduling App
Flask web app: upload CSVs → auto-schedule → export to Wix
"""

import os
import json
from pathlib import Path
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, send_file, jsonify, session
)
import io
import pandas as pd

from src.parser import (
    parse_models_master, parse_contact_info, parse_services, merge_services,
)
from src.scheduler import Scheduler, schedule_to_rows
from src.exporter import schedule_to_wix_csv, schedule_to_master_csv

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "nebca-r4h-2026-dev")

UPLOAD_FOLDER = Path("data/uploads")
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

# In-memory state for current session (fine for single-user use)
STATE = {
    "models": [],
    "artists": [],
    "contacts": {},   # {lowercase_name: {email, phone}} from contact info CSV
    "schedule": [],
    "schedule_rows": [],
    "provider_schedules": [],
}


# ─── Home ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", state=STATE)


# ─── Upload ───────────────────────────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
def upload():
    file_keys = ["models_master", "contact_info", "services"]
    saved = {}
    for key in file_keys:
        f = request.files.get(key)
        if f and f.filename:
            path = UPLOAD_FOLDER / f"{key}.csv"
            f.save(path)
            saved[key] = path

    errors = []
    try:
        if "models_master" in saved:
            STATE["models"] = parse_models_master(saved["models_master"])
        if "contact_info" in saved:
            STATE["contacts"] = parse_contact_info(saved["contact_info"])
        if "services" in saved:
            svc_data = parse_services(saved["services"])
            if STATE["models"]:
                STATE["models"] = merge_services(STATE["models"], svc_data)
    except Exception as e:
        errors.append(f"Parse error: {e}")

    if errors:
        for e in errors:
            flash(e, "error")
    else:
        parts = [f"{len(STATE['models'])} models"]
        if STATE["artists"]:
            parts.append(f"{len(STATE['artists'])} glam artists")
        if STATE["contacts"]:
            parts.append(f"{len(STATE['contacts'])} contacts")
        flash(f"Loaded {', '.join(parts)}.", "success")

    return redirect(url_for("index"))


# ─── Configure ────────────────────────────────────────────────────────────────

@app.route("/configure", methods=["GET", "POST"])
def configure():
    if request.method == "POST":
        raw_names = request.form.get("massage_provider_names", "")
        massage_names = [n.strip() for n in raw_names.split(",") if n.strip()]
        config = {
            "event_date": request.form.get("event_date", "2026-01-01"),
            "num_portrait_slots": int(request.form.get("num_portrait_slots", 1)),
            "num_massage_tables": int(request.form.get("num_massage_tables", 1)),
            "massage_provider_names": massage_names,
        }
        # Save config to file so it persists
        with open("data/config.json", "w") as f:
            json.dump(config, f, indent=2)
        flash("Configuration saved.", "success")
        return redirect(url_for("index"))

    # Load existing config
    config = _load_config()
    return render_template("configure.html", config=config)


def _load_config() -> dict:
    config_path = Path("data/config.json")
    if config_path.exists():
        with open(config_path) as f:
            return json.load(f)
    return {
        "event_date": "2026-04-25",
        "num_portrait_slots": 1,
        "num_massage_tables": 1,
        "massage_provider_names": [],
    }


# ─── Schedule ─────────────────────────────────────────────────────────────────

@app.route("/schedule", methods=["POST"])
def run_schedule():
    if not STATE["models"]:
        flash("Upload and match models first.", "error")
        return redirect(url_for("index"))

    config = _load_config()

    scheduler = Scheduler(
        models=STATE["models"],
        artists=STATE["artists"],
        num_portrait_slots=config["num_portrait_slots"],
        num_massage_tables=config.get("num_massage_tables", 1),
        massage_provider_names=config.get("massage_provider_names") or None,
        event_date=config["event_date"],
    )

    STATE["schedule"] = scheduler.run()
    STATE["schedule_rows"] = schedule_to_rows(STATE["schedule"])
    STATE["provider_schedules"] = scheduler.get_provider_schedules()
    flash(f"Scheduled {len(STATE['schedule'])} models.", "success")
    return redirect(url_for("view_schedule"))


@app.route("/schedule/view")
def view_schedule():
    return render_template(
        "schedule.html",
        rows=STATE["schedule_rows"],
        schedule=STATE["schedule"],
    )


# ─── Export ───────────────────────────────────────────────────────────────────

@app.route("/export/wix")
def export_wix():
    if not STATE["schedule"]:
        flash("Generate a schedule first.", "error")
        return redirect(url_for("index"))

    models_by_name = {m["name"]: m for m in STATE["models"]}
    config = _load_config()
    csv_str = schedule_to_wix_csv(
        STATE["schedule"],
        models_by_name,
        event_date=config["event_date"],
        contacts=STATE.get("contacts", {}),
    )
    return send_file(
        io.BytesIO(csv_str.encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name="r4h_wix_schedule.csv",
    )


@app.route("/export/master")
def export_master():
    if not STATE["schedule_rows"]:
        flash("Generate a schedule first.", "error")
        return redirect(url_for("index"))

    csv_str = schedule_to_master_csv(STATE["schedule_rows"])
    return send_file(
        io.BytesIO(csv_str.encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name="r4h_master_schedule.csv",
    )


# ─── Artists view ─────────────────────────────────────────────────────────────

@app.route("/artists")
def artists():
    return render_template("artists.html", artists=STATE["artists"])


@app.route("/debug/artists")
def debug_artists():
    return jsonify([{"name": a["name"], "role": a["role"]} for a in STATE["artists"]])


@app.route("/schedule/providers")
def provider_schedules():
    if not STATE["provider_schedules"]:
        flash("Generate a schedule first.", "error")
        return redirect(url_for("index"))
    return render_template("provider_schedule.html", providers=STATE["provider_schedules"])


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5001)
