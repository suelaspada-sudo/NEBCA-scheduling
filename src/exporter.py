"""
Export the generated schedule to formats usable by Wix Bookings.

Wix Bookings CSV import columns (standard):
  Service Name, Staff Member, Start Date, Start Time, End Date, End Time,
  Client Name, Client Email, Client Phone, Notes

One row per appointment (chair massage, hair, makeup, portrait, etc.).
"""

import csv
import io
from datetime import datetime


WIX_FIELDNAMES = [
    "Service Name",
    "Staff Member",
    "Start Date",
    "Start Time",
    "End Date",
    "End Time",
    "Client Name",
    "Client Email",
    "Client Phone",
    "Notes",
]

SERVICE_DISPLAY = {
    "massage": "Massage",
    "hair": "Hair Styling",
    "makeup": "Makeup",
    "portrait": "Portrait Session",
}


def _fmt_date(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def _fmt_time_wix(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def schedule_to_wix_csv(
    schedule: list[dict],
    models_by_name: dict,
    event_date: str = "2026-01-01",
    contacts: dict | None = None,
    output_path: str | None = None,
) -> str:
    """
    Convert schedule to Wix Bookings CSV string.
    contacts: optional {lowercase_name: {email, phone}} from contact info CSV.
    If output_path provided, also writes to file.
    Returns CSV string.
    """
    contacts = contacts or {}
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=WIX_FIELDNAMES)
    writer.writeheader()

    for entry in schedule:
        model_name = entry["model"]
        model_info = models_by_name.get(model_name, {})
        # Contact info CSV takes priority over models master sheet columns
        contact = contacts.get(model_name.lower(), {})
        client_email = contact.get("email") or model_info.get("email", "")
        client_phone = contact.get("phone") or model_info.get("phone", "")

        for service_key, display_name in SERVICE_DISPLAY.items():
            appt = entry.get("appointments", {}).get(service_key)
            if not appt:
                continue

            start: datetime = appt["start"]
            end: datetime = appt["end"]
            provider: str = appt.get("provider", "")

            notes_parts = []
            if service_key == "hair":
                notes = model_info.get("hair_ideas", "") or model_info.get("hair_notes", "")
                if notes:
                    notes_parts.append(f"Hair ideas: {notes}")
                if model_info.get("hair_wig"):
                    notes_parts.append(f"Wig/extensions: {model_info['hair_wig']}")
            elif service_key == "makeup":
                notes = model_info.get("makeup_ideas", "") or model_info.get("makeup_notes", "")
                if notes:
                    notes_parts.append(f"Makeup ideas: {notes}")
                if model_info.get("skin_sensitivities"):
                    notes_parts.append(f"Skin sensitivities: {model_info['skin_sensitivities']}")
                if model_info.get("fake_lashes"):
                    notes_parts.append(f"Lashes: {model_info['fake_lashes']}")

            writer.writerow({
                "Service Name": display_name,
                "Staff Member": provider,
                "Start Date": _fmt_date(start),
                "Start Time": _fmt_time_wix(start),
                "End Date": _fmt_date(end),
                "End Time": _fmt_time_wix(end),
                "Client Name": model_name,
                "Client Email": client_email,
                "Client Phone": client_phone,
                "Notes": " | ".join(notes_parts),
            })

    csv_str = output.getvalue()

    if output_path:
        with open(output_path, "w", newline="") as f:
            f.write(csv_str)

    return csv_str


def schedule_to_artist_csv(provider_schedules: list[dict], output_path: str | None = None) -> str:
    """
    Export per-artist schedule as CSV.
    One row per appointment, grouped by artist, sorted by start time.
    Columns: Artist, Service, Start Time, End Time, Model
    """
    if not provider_schedules:
        return ""

    output = io.StringIO()
    fieldnames = ["Artist", "Service", "Start Time", "End Time", "Model"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    for provider in sorted(provider_schedules, key=lambda p: p["name"]):
        for slot in provider["slots"]:
            writer.writerow({
                "Artist":     provider["name"],
                "Service":    slot["service"].title(),
                "Start Time": _fmt_time_wix(slot["start"]),
                "End Time":   _fmt_time_wix(slot["end"]),
                "Model":      "— BREAK —" if slot["is_break"] else slot["model"],
            })

    csv_str = output.getvalue()
    if output_path:
        with open(output_path, "w", newline="") as f:
            f.write(csv_str)
    return csv_str

    """
    Export the full master schedule (one row per model) as CSV.
    Useful for printing / sharing with the team.
    """
    if not schedule_rows:
        return ""

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(schedule_rows[0].keys()))
    writer.writeheader()
    writer.writerows(schedule_rows)
    csv_str = output.getvalue()

    if output_path:
        with open(output_path, "w", newline="") as f:
            f.write(csv_str)

    return csv_str
