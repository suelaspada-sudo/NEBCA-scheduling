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

from src.parser import parse_models_master, parse_glam_info, parse_questionnaire, merge_model_data
from src.matcher import run_matching, get_match_quality_report
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
    "schedule": [],
    "schedule_rows": [],
    "match_report": [],
}


# ─── Home ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", state=STATE)


# ─── Upload ───────────────────────────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
def upload():
    files = {
        "models_master": request.files.get("models_master"),
        "glam_info": request.files.get("glam_info"),
        "questionnaire": request.files.get("questionnaire"),
    }

    saved = {}
    for key, f in files.items():
        if f and f.filename:
            path = UPLOAD_FOLDER / f"{key}.csv"
            f.save(path)
            saved[key] = path

    errors = []

    try:
        if "models_master" in saved:
            STATE["models"] = parse_models_master(saved["models_master"])
        if "glam_info" in saved:
            STATE["artists"] = parse_glam_info(saved["glam_info"])
        if "questionnaire" in saved:
            responses = parse_questionnaire(saved["questionnaire"])
            if STATE["models"]:
                STATE["models"] = merge_model_data(STATE["models"], responses)
    except Exception as e:
        errors.append(f"Parse error: {e}")

    if errors:
        for e in errors:
            flash(e, "error")
    else:
        flash(f"Loaded {len(STATE['models'])} models and {len(STATE['artists'])} glam artists.", "success")

    return redirect(url_for("index"))


# ─── Configure ────────────────────────────────────────────────────────────────

@app.route("/configure", methods=["GET", "POST"])
def configure():
    if request.method == "POST":
        config = {
            "event_date": request.form.get("event_date", "2026-01-01"),
            "num_massage_tables": int(request.form.get("num_massage_tables", 2)),
            "num_hand_massage_tables": int(request.form.get("num_hand_massage_tables", 2)),
            "num_nail_stations": int(request.form.get("num_nail_stations", 2)),
            "num_portrait_slots": int(request.form.get("num_portrait_slots", 1)),
            "hair_duration": int(request.form.get("hair_duration", 60)),
            "makeup_duration": int(request.form.get("makeup_duration", 60)),
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
        "num_massage_tables": 2,
        "num_hand_massage_tables": 2,
        "num_nail_stations": 2,
        "num_portrait_slots": 1,
        "hair_duration": 45,
        "makeup_duration": 45,
    }


# ─── Match ────────────────────────────────────────────────────────────────────

@app.route("/match", methods=["POST"])
def match():
    if not STATE["models"] or not STATE["artists"]:
        flash("Upload all three CSV files first.", "error")
        return redirect(url_for("index"))

    STATE["models"] = run_matching(STATE["models"], STATE["artists"])
    STATE["match_report"] = get_match_quality_report(STATE["models"], STATE["artists"])
    flash(f"Matched {len(STATE['models'])} models to glam artists.", "success")
    return redirect(url_for("matches"))


@app.route("/matches")
def matches():
    return render_template("matches.html", report=STATE["match_report"], models=STATE["models"])


@app.route("/matches/override", methods=["POST"])
def override_match():
    """Allow manual override of a single model's assignments."""
    model_name = request.form.get("model_name")
    hair = request.form.get("hair_stylist")
    makeup = request.form.get("makeup_artist")

    for model in STATE["models"]:
        if model["name"] == model_name:
            if hair:
                model["assigned_hair_stylist"] = hair
            if makeup:
                model["assigned_makeup_artist"] = makeup
            break

    STATE["match_report"] = get_match_quality_report(STATE["models"], STATE["artists"])
    flash(f"Updated assignments for {model_name}.", "success")
    return redirect(url_for("matches"))


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
        num_massage_tables=config["num_massage_tables"],
        num_hand_massage_tables=config["num_hand_massage_tables"],
        num_nail_stations=config["num_nail_stations"],
        num_portrait_slots=config["num_portrait_slots"],
        event_date=config["event_date"],
        hair_duration_min=config["hair_duration"],
        makeup_duration_min=config["makeup_duration"],
    )

    STATE["schedule"] = scheduler.run()
    STATE["schedule_rows"] = schedule_to_rows(STATE["schedule"])
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


# ─── Run ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True, port=5001)
