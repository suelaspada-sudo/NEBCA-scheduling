"""
Parsers for the 3 input CSVs:
  1. models_master  - Glams Master / Models Master Schedule
  2. glam_info      - 2026 Glam Info (hair + makeup artists)
  3. questionnaire  - R4H 2026 Model & HA Questionnaire responses
"""

import pandas as pd
import re
from pathlib import Path


# ─── Column aliases ───────────────────────────────────────────────────────────

MODELS_MASTER_COLS = {
    "name": "Name",
    "email": "E-mail",
    "phone": "Phone #",
    "hair": "Hair",
    "hair_stylist_name": "Hair Stylist Name",
    "hair_stylist_email": "Email",        # first Email after Hair block
    "hair_stylist_phone": "Phone",        # first Phone after Hair block
    "hair_time_slot": "Time Slot",
    "hair_notes": "Notes",
    "makeup": "Makeup",
    "makeup_artist_name": "Makeup Artist Name",
    "makeup_artist_email": "Email.1",
    "makeup_artist_phone": "Phone.1",
    "makeup_time_slot": "Time Slot.1",
    "makeup_notes": "Notes.1",
}

GLAM_INFO_COLS = {
    "first_name": "First Name",
    "last_name": "Last Name",
    "business": "Business Name",
    "email": "Email",
    "cell": "Cell",
    "veteran_new": "Veteran/New",
    "street": "Street",
    "city": "City/Town",
    "state": "State",
    "zip": "Zip",
    "referral": "Referral",
    "styling_pref": "Styling preference",
    "model_count_pref": "# models preference",
    "hair_uncomfortable": "Looks uncomfortable with? [HAIR]",
    "hair_own_supplies": "Bring own supplies? [HAIR]",
    "makeup_uncomfortable": "Looks uncomfortable with? [MAKEUP]",
    "makeup_own_supplies": "Bring own supplies? [MAKEUP]",
    "info_from_models": "Info you want to know from models?",
    "links": "Links",
}

# Questionnaire columns (abbreviated keys → partial column name match)
Q_COL_MAP = {
    "timestamp": "Timestamp",
    "first_name": "First name:",
    "last_name": "Last name:",
    "email": "Email:",
    "phone": "Phone number:",
    "street": "Street address:",
    "city": "City/town:",
    "state": "State:",
    "zip": "Zip code:",
    "costume_designer": "costume designer",
    "costume_desc": "describe the costume",
    "hair_service": "hair__ to be styled",
    "hair_own_stylist_info": "bringing someone to do your hair",
    "hair_ideas": "ideas for your hair",
    "hair_wig": "wear a wig",
    "hair_description": "describe your hair",
    "makeup_service": "makeup__ to be done",
    "makeup_own_artist_info": "bringing someone to do your makeup",
    "makeup_ideas": "ideas for your makeup",
    "skin_sensitivities": "skin sensitiv",
    "medications": "medications",
    "own_makeup": "own makeup",
    "fake_lashes": "fake eye lashes",
    "nails_stamping": "nails stamped",
    "questions": "initial questions",
}


def _find_col(df: pd.DataFrame, partial: str) -> str | None:
    """Return the first column whose name contains `partial` (case-insensitive)."""
    partial_lower = partial.lower()
    for col in df.columns:
        if partial_lower in col.lower():
            return col
    return None


def _safe_str(val) -> str:
    if pd.isna(val):
        return ""
    return str(val).strip()


# ─── Models Master ────────────────────────────────────────────────────────────

GROUP_ROW_PATTERNS = [
    ("group1",           ["group 1", "group1", "runway part 1", "runway part1"]),
    ("group2",           ["group 2", "group2", "runway part 2", "runway part2"]),
    ("hope_ambassador",  ["hope ambassador", "hope ambassadors", "hope amb"]),
    ("board_member",     ["board member", "board members"]),
]

# Keywords that indicate a row is a section/group header rather than a person
_HEADER_KEYWORDS = ["rehearsal", "runway", "group", "ambassador", "board member"]

# Suffixes that may be appended to a real person's name indicating their group
_NAME_SUFFIXES = {
    "board member": "board_member",
    "board members": "board_member",
    "hope ambassador": "hope_ambassador",
    "hope ambassadors": "hope_ambassador",
}

# Words appended to names that carry no group meaning — just strip them
_NAME_NOISE_WORDS = ["veteran", "new"]


def _detect_group_row(name: str) -> str | None:
    """
    Return group key if a row's Name cell is a group header rather than a person's name.
    Catches explicit patterns AND rows that look like headers (contain rehearsal/runway keywords).
    """
    name_lower = name.lower().strip()
    for key, patterns in GROUP_ROW_PATTERNS:
        for p in patterns:
            if name_lower == p or name_lower.startswith(p):
                return key
    # Catch-all: rows containing rehearsal time patterns are always headers
    if re.search(r'\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}', name_lower):
        # Determine group from content
        if "part 1" in name_lower or "group 1" in name_lower:
            return "group1"
        if "part 2" in name_lower or "group 2" in name_lower:
            return "group2"
        return "group1"  # default if unknown
    return None


def _strip_name_suffix(name: str) -> tuple[str, str | None]:
    """
    Strip group suffixes and noise words from a person's name.
    Returns (clean_name, group_key_or_None).
    """
    name_lower = name.lower().strip()
    # Check group-bearing suffixes first (e.g. "board member")
    for suffix, group_key in _NAME_SUFFIXES.items():
        if name_lower.endswith(suffix):
            clean = name[:-(len(suffix))].strip().rstrip(",").strip()
            return clean, group_key
    # Strip plain noise words like "veteran" or "new"
    for noise in _NAME_NOISE_WORDS:
        if name_lower.endswith(" " + noise):
            clean = name[:-(len(noise) + 1)].strip()
            return clean, None
    return name, None


def _extract_rehearsal_time(row: pd.Series) -> str:
    """
    Pull a rehearsal time string from any column in a header row.
    Looks for patterns like '3:00-4:00', '1:30-2:30 PM', etc.
    """
    time_pattern = re.compile(r'\d{1,2}:\d{2}\s*[-–]\s*\d{1,2}:\d{2}(?:\s*[APap][Mm])?')
    for val in row.values:
        text = str(val).strip()
        match = time_pattern.search(text)
        if match:
            return match.group()
    return ""


def parse_models_master(path: str | Path) -> list[dict]:
    """
    Returns list of model dicts with pre-assigned glam info (if any).

    Handles two group formats:
      - A dedicated 'Group' column per row
      - Group header rows interspersed (e.g. a row where Name = 'Group 1')
        with an optional rehearsal time in another cell on the same row.

    Duplicate column names (Email, Phone, Time Slot, Notes appear twice)
    are handled by pandas auto-renaming to .1 suffix.
    """
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    models = []
    current_group = ""
    current_rehearsal = ""

    for _, row in df.iterrows():
        name = _safe_str(row.get("Name", ""))
        if not name:
            continue

        # Check if this row is a group header rather than a model
        group_from_row = _detect_group_row(name)
        if group_from_row:
            current_group = group_from_row
            current_rehearsal = _extract_rehearsal_time(row)
            continue

        # Strip group suffixes from name (e.g. "Cassia Leach board member")
        name, suffix_group = _strip_name_suffix(name)

        # Use explicit Group column if present, then suffix, then tracked group
        group_col = _safe_str(row.get("Group", ""))
        group = group_col if group_col else (suffix_group or current_group)

        # Hair/Makeup columns in master sheet indicate if they want the service
        hair_val = _safe_str(row.get("Hair", "")).lower()
        makeup_val = _safe_str(row.get("Makeup", "")).lower()
        wants_hair = not (hair_val in ("no", "maybe") or hair_val.startswith("no") or hair_val.startswith("maybe"))
        wants_makeup = not (makeup_val in ("no", "maybe") or makeup_val.startswith("no") or makeup_val.startswith("maybe"))

        model = {
            "name": name,
            "email": _safe_str(row.get("E-mail", "")),
            "phone": _safe_str(row.get("Phone #", "")),
            "group": group,
            "rehearsal_time": current_rehearsal,
            "order": _safe_str(row.get("Order", row.get("#", ""))),
            # wants_hair/wants_makeup from master sheet (questionnaire may override later)
            "wants_hair": wants_hair,
            "wants_makeup": wants_makeup,
            # pre-assigned glam — if filled in, skip matching for this model
            "assigned_hair_stylist": _safe_str(row.get("Hair Stylist Name", "")),
            "assigned_makeup_artist": _safe_str(row.get("Makeup Artist Name", "")),
            "hair_time_slot": _safe_str(row.get("Time Slot", "")),
            "makeup_time_slot": _safe_str(row.get("Time Slot.1", "")),
            "hair_notes": _safe_str(row.get("Notes", "")),
            "makeup_notes": _safe_str(row.get("Notes.1", "")),
        }
        models.append(model)
    return models


# ─── Glam Info ────────────────────────────────────────────────────────────────

# Section header rows in the glam sheet (First Name cell only, Last Name empty)
# Check "both" patterns before "hair" so "Hair & Makeup" doesn't match "hair" first
GLAM_SECTION_PATTERNS = [
    ("both",   ["hair & makeup", "hair and makeup", "hair/makeup", "both"]),
    ("hair",   ["hair stylists", "hair stylist", "hair only", "hair"]),
    ("makeup", ["makeup artists", "makeup artist", "makeup only", "makeup"]),
]


def _detect_glam_section(first: str, last: str) -> str | None:
    """
    Return role if the row looks like a section header.
    Headers have text only in First Name (Last Name is blank).
    """
    if last.strip():
        return None  # real person has a last name
    first_lower = first.lower().strip()
    for role, patterns in GLAM_SECTION_PATTERNS:
        for p in patterns:
            if first_lower == p or first_lower.startswith(p):
                return role
    return None


def _determine_role(row: pd.Series) -> str:
    """Infer whether artist does Hair, Makeup, or Both from filled columns."""
    hair_uncomfortable = _safe_str(row.get("Looks uncomfortable with? [HAIR]", ""))
    makeup_uncomfortable = _safe_str(row.get("Looks uncomfortable with? [MAKEUP]", ""))
    hair_supplies = _safe_str(row.get("Bring own supplies? [HAIR]", ""))
    makeup_supplies = _safe_str(row.get("Bring own supplies? [MAKEUP]", ""))

    does_hair = bool(hair_supplies or hair_uncomfortable)
    does_makeup = bool(makeup_supplies or makeup_uncomfortable)

    if does_hair and does_makeup:
        return "both"
    if does_hair:
        return "hair"
    if does_makeup:
        return "makeup"
    return "both"  # default: assume both


def parse_glam_info(path: str | Path) -> list[dict]:
    """Returns list of glam-artist dicts."""
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    artists = []
    current_section_role: str | None = None  # set when a section header row is found

    for _, row in df.iterrows():
        first = _safe_str(row.get("First Name", ""))
        last = _safe_str(row.get("Last Name", ""))
        if not first and not last:
            continue

        # Check if this row is a section header (e.g. "Hair Stylists", "Makeup Artists")
        section = _detect_glam_section(first, last)
        if section:
            current_section_role = section
            continue

        pref_count = _safe_str(row.get("# models preference", ""))
        try:
            max_models = int(re.search(r"\d+", pref_count).group()) if pref_count else 6
        except AttributeError:
            max_models = 6

        # Role: use section header if available, otherwise infer from filled columns
        role = current_section_role if current_section_role else _determine_role(row)

        artist = {
            "name": f"{first} {last}".strip(),
            "first_name": first,
            "last_name": last,
            "business": _safe_str(row.get("Business Name", "")),
            "email": _safe_str(row.get("Email", "")),
            "cell": _safe_str(row.get("Cell", "")),
            "veteran_new": _safe_str(row.get("Veteran/New", "")),
            "styling_pref": _safe_str(row.get("Styling preference", "")),
            "max_models": max_models,
            "hair_uncomfortable": _safe_str(row.get("Looks uncomfortable with? [HAIR]", "")),
            "hair_own_supplies": _safe_str(row.get("Bring own supplies? [HAIR]", "")),
            "makeup_uncomfortable": _safe_str(row.get("Looks uncomfortable with? [MAKEUP]", "")),
            "makeup_own_supplies": _safe_str(row.get("Bring own supplies? [MAKEUP]", "")),
            "info_from_models": _safe_str(row.get("Info you want to know from models?", "")),
            "links": _safe_str(row.get("Links", "")),
            "role": role,
            # scheduling state (filled in by scheduler)
            "assigned_models": [],
        }
        artists.append(artist)
    return artists


# ─── Questionnaire ───────────────────────────────────────────────────────────

def parse_questionnaire(path: str | Path) -> list[dict]:
    """Returns list of participant preference dicts."""
    df = pd.read_csv(path, dtype=str)
    df.columns = [c.strip() for c in df.columns]

    def gcol(partial):
        return _find_col(df, partial)

    responses = []
    for _, row in df.iterrows():
        first_col = gcol("First name")
        last_col = gcol("Last name")
        if not first_col:
            continue

        first = _safe_str(row.get(first_col, ""))
        last = _safe_str(row.get(last_col, "")) if last_col else ""
        if not first:
            continue

        def g(partial):
            col = gcol(partial)
            return _safe_str(row[col]) if col and col in row else ""

        hair_service_val = g("hair__ to be styled")
        # "Yes" / "No, I am bringing..." / "No, I am doing it myself"
        wants_hair = hair_service_val.lower().startswith("yes") if hair_service_val else True

        makeup_service_val = g("makeup__ to be done")
        wants_makeup = makeup_service_val.lower().startswith("yes") if makeup_service_val else True

        nails_val = g("nails stamped")
        wants_nails = nails_val.lower().startswith("yes") if nails_val else False

        response = {
            "first_name": first,
            "last_name": last,
            "full_name": f"{first} {last}".strip(),
            "email": g("Email:"),
            "phone": g("Phone number:"),
            "costume_desc": g("describe the costume"),
            "wants_hair": wants_hair,
            "hair_ideas": g("ideas for your hair"),
            "hair_wig": g("wear a wig"),
            "hair_description": g("describe your hair"),
            "wants_makeup": wants_makeup,
            "makeup_ideas": g("ideas for your makeup"),
            "skin_sensitivities": g("skin sensitiv"),
            "medications": g("medications"),
            "own_makeup": g("own makeup"),
            "fake_lashes": g("fake eye lashes"),
            "wants_nails": wants_nails,
            "questions": g("initial questions"),
        }
        responses.append(response)
    return responses


# ─── Merge: attach questionnaire data to models ───────────────────────────────

def merge_model_data(models: list[dict], responses: list[dict]) -> list[dict]:
    """
    Join questionnaire responses onto the models list by name (fuzzy last-name match).
    Adds preference fields directly to each model dict.
    """
    resp_by_name = {}
    for r in responses:
        key = r["full_name"].lower().strip()
        resp_by_name[key] = r
        # also index by last name alone for fallback
        if r["last_name"]:
            resp_by_name[r["last_name"].lower().strip()] = r

    for model in models:
        name_lower = model["name"].lower().strip()
        # try full name first, then last name
        last = name_lower.split()[-1] if name_lower else ""
        resp = resp_by_name.get(name_lower) or resp_by_name.get(last)

        if resp:
            # If master sheet already says No/Maybe, questionnaire cannot override to Yes
            wants_hair = resp.get("wants_hair", True) and model.get("wants_hair", True)
            wants_makeup = resp.get("wants_makeup", True) and model.get("wants_makeup", True)
            model.update({
                "wants_hair": wants_hair,
                "hair_ideas": resp.get("hair_ideas", ""),
                "hair_wig": resp.get("hair_wig", ""),
                "hair_description": resp.get("hair_description", ""),
                "wants_makeup": wants_makeup,
                "makeup_ideas": resp.get("makeup_ideas", ""),
                "skin_sensitivities": resp.get("skin_sensitivities", ""),
                "own_makeup": resp.get("own_makeup", ""),
                "fake_lashes": resp.get("fake_lashes", ""),
                "wants_nails": resp.get("wants_nails", True),
                "questions": resp.get("questions", ""),
            })
        else:
            model.setdefault("wants_hair", True)
            model.setdefault("wants_makeup", True)
            model.setdefault("wants_nails", True)
            model.setdefault("hair_ideas", "")
            model.setdefault("makeup_ideas", "")
            model.setdefault("hair_description", "")
            model.setdefault("skin_sensitivities", "")

    return models
