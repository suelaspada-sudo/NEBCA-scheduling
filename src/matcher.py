"""
Style-based matching: pair each model with a compatible hair stylist and makeup artist.

Approach:
  1. Extract style keywords from model's hair/makeup ideas + hair description
  2. Extract style keywords from artist's styling_pref + uncomfortable list
  3. Score each (model, artist) pair by keyword overlap
  4. Use a greedy best-match assignment respecting artist capacity
"""

import re
from collections import defaultdict


# Canonical style categories with keyword variations
STYLE_KEYWORDS = {
    "natural": ["natural", "simple", "minimal", "clean", "soft", "subtle", "everyday", "low key"],
    "glam": ["glam", "glamour", "glamorous", "dramatic", "bold", "full glam", "va voom", "va-voom"],
    "editorial": ["editorial", "artistic", "avant garde", "avant-garde", "creative", "unique", "edgy", "fashion"],
    "classic": ["classic", "timeless", "traditional", "elegant", "polished", "refined"],
    "bohemian": ["boho", "bohemian", "ethereal", "romantic", "flowy", "loose", "beachy", "festival"],
    "vintage": ["vintage", "retro", "old hollywood", "pin up", "pin-up", "50s", "60s", "70s"],
    "smokey": ["smokey", "smoky", "sultry", "dark", "moody"],
    "colorful": ["colorful", "color", "vibrant", "bright", "pop of color", "fun"],
    "curly": ["curly", "curl", "waves", "wavy", "textured"],
    "straight": ["straight", "sleek", "smooth", "flat iron"],
    "updo": ["updo", "up do", "up", "bun", "chignon", "twist", "pinned"],
    "extensions": ["extension", "extensions", "wig", "wigs", "weave"],
    "skin_glow": ["glow", "dewy", "luminous", "radiant", "glowing"],
    "matte": ["matte", "no shine", "flat"],
    "lashes": ["lash", "lashes", "dramatic lashes", "full lashes"],
}


def _extract_keywords(text: str) -> set[str]:
    """Return set of canonical style categories found in text."""
    if not text:
        return set()
    text_lower = text.lower()
    found = set()
    for category, variants in STYLE_KEYWORDS.items():
        for kw in variants:
            if re.search(r"\b" + re.escape(kw) + r"\b", text_lower):
                found.add(category)
                break
    return found


def _model_style_keywords(model: dict) -> set[str]:
    fields = [
        model.get("hair_ideas", ""),
        model.get("makeup_ideas", ""),
        model.get("hair_description", ""),
        model.get("hair_notes", ""),
        model.get("makeup_notes", ""),
    ]
    return _extract_keywords(" ".join(fields))


def _artist_style_keywords(artist: dict) -> tuple[set[str], set[str]]:
    """Returns (preferred_styles, uncomfortable_styles)."""
    preferred = _extract_keywords(artist.get("styling_pref", ""))
    role = artist.get("role", "both")
    uncomfortable_text = ""
    if role in ("hair", "both"):
        uncomfortable_text += " " + artist.get("hair_uncomfortable", "")
    if role in ("makeup", "both"):
        uncomfortable_text += " " + artist.get("makeup_uncomfortable", "")
    uncomfortable = _extract_keywords(uncomfortable_text)
    return preferred, uncomfortable


def score_match(model: dict, artist: dict) -> float:
    """
    Score how well a model's style desires match an artist's preferences.
    Higher = better match. Returns 0.0 – 1.0 (+ bonuses).
    """
    model_kws = _model_style_keywords(model)
    preferred, uncomfortable = _artist_style_keywords(artist)

    if not model_kws and not preferred:
        return 0.5  # neutral — no data either way

    # Penalty: artist uncomfortable with something model wants
    conflicts = model_kws & uncomfortable
    if conflicts:
        return 0.0  # hard block

    if not model_kws or not preferred:
        return 0.3  # one side has no data — possible but not ideal

    overlap = model_kws & preferred
    score = len(overlap) / max(len(model_kws), len(preferred))
    return score


def match_models_to_artists(
    models: list[dict],
    artists: list[dict],
    role: str,  # "hair" or "makeup"
    other_assignments: dict[str, str] | None = None,  # already-assigned other role, to avoid same artist for both
) -> dict[str, str]:
    """
    Greedy best-match assignment.
    Returns {model_name: artist_name}.
    Respects:
      - artist.role (must be role or "both")
      - artist.max_models capacity
      - pre-assigned artists (already in model dict)
      - other_assignments: won't assign the same artist to both hair AND makeup for the same model
    """
    eligible = [a for a in artists if a["role"] in (role, "both")]

    # Track remaining capacity
    capacity = {a["name"]: a["max_models"] for a in eligible}

    # Pre-assigned: respect existing assignments
    assignment = {}
    pre_assigned_key = f"assigned_{role}_stylist" if role == "hair" else "assigned_makeup_artist"

    for model in models:
        pre = model.get(pre_assigned_key, "").strip()
        if pre:
            assignment[model["name"]] = pre
            # Deduct from capacity if we know who it is
            for a in eligible:
                if a["name"].lower() in pre.lower() or pre.lower() in a["name"].lower():
                    capacity[a["name"]] = max(0, capacity[a["name"]] - 1)
                    break

    # Skip models who don't want this service — mark them as no-service
    wants_key = "wants_hair" if role == "hair" else "wants_makeup"
    for model in models:
        if not model.get(wants_key, True) and model["name"] not in assignment:
            assignment[model["name"]] = ""  # explicitly no service

    # Score all unassigned models against eligible artists
    unassigned = [m for m in models if m["name"] not in assignment]

    # Sort models by how constrained they are (fewest compatible artists first)
    def constraint_score(model):
        already_assigned = (other_assignments or {}).get(model["name"], "")
        compat = sum(
            1 for a in eligible
            if capacity.get(a["name"], 0) > 0
            and score_match(model, a) > 0
            and a["name"] != already_assigned
        )
        return compat

    unassigned.sort(key=constraint_score)

    for model in unassigned:
        # Artist already assigned to this model for the other role — don't reuse them
        already_assigned_other = (other_assignments or {}).get(model["name"], "")

        best_artist = None
        best_score = -1.0

        for artist in eligible:
            if capacity.get(artist["name"], 0) <= 0:
                continue
            # Skip if this artist is already doing the other service for this model
            if artist["name"] == already_assigned_other:
                continue
            s = score_match(model, artist)
            if s > best_score:
                best_score = s
                best_artist = artist

        if not best_artist:
            # Fallback: allow any available artist (including other-role artist if truly no choice)
            candidates = [a for a in eligible if capacity.get(a["name"], 0) > 0]
            if candidates:
                best_artist = max(candidates, key=lambda a: capacity.get(a["name"], 0))

        if best_artist:
            assignment[model["name"]] = best_artist["name"]
            capacity[best_artist["name"]] -= 1
        else:
            assignment[model["name"]] = "UNASSIGNED"

    return assignment


def run_matching(models: list[dict], artists: list[dict]) -> list[dict]:
    """
    Run hair + makeup matching and attach results to models.
    Returns updated models list.
    """
    # Note: if a model has the same artist pre-assigned for both hair and makeup
    # (e.g. the model explicitly requested Carol for both), we honour that request.
    # The scheduler's _find_slot already handles "both" artists correctly so
    # consecutive appointments with the same provider will not double-book.

    hair_assignments = match_models_to_artists(models, artists, "hair")
    makeup_assignments = match_models_to_artists(models, artists, "makeup", other_assignments=hair_assignments)

    for model in models:
        name = model["name"]
        if not model.get("assigned_hair_stylist"):
            model["assigned_hair_stylist"] = hair_assignments.get(name, "UNASSIGNED")
        if not model.get("assigned_makeup_artist"):
            model["assigned_makeup_artist"] = makeup_assignments.get(name, "UNASSIGNED")

    return models


def get_match_quality_report(models: list[dict], artists: list[dict]) -> list[dict]:
    """Return a per-model match quality summary for display."""
    artist_map = {a["name"]: a for a in artists}
    report = []
    for model in models:
        hair_artist = artist_map.get(model.get("assigned_hair_stylist", ""))
        makeup_artist = artist_map.get(model.get("assigned_makeup_artist", ""))

        hair_score = score_match(model, hair_artist) if hair_artist else None
        makeup_score = score_match(model, makeup_artist) if makeup_artist else None

        model_kws = _model_style_keywords(model)

        report.append({
            "model": model["name"],
            "hair_stylist": model.get("assigned_hair_stylist", ""),
            "hair_match_score": round(hair_score, 2) if hair_score is not None else "n/a",
            "makeup_artist": model.get("assigned_makeup_artist", ""),
            "makeup_match_score": round(makeup_score, 2) if makeup_score is not None else "n/a",
            "model_styles": sorted(model_kws),
        })
    return report
