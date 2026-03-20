"""
Constraint-based scheduler for NEBCA R4H event.

Services per model (if applicable):
  - chair_massage   : 12:00–19:00, must be BEFORE hair+makeup
  - hand_massage    : 12:00–19:00, any time
  - nail_stamping   : 12:00–19:00, any time
  - hair            : 12:00–19:00 (artists set up from 11:00)
  - makeup          : 12:00–19:00, must be BEFORE portrait
  - portrait        : 13:30–18:30

Hard constraints:
  - chair_massage.end <= hair.start
  - chair_massage.end <= makeup.start
  - (hair.end AND makeup.end) <= portrait.start
  - No service during rehearsal blackout:
      Group 1:               15:00–16:00
      Group 2 + Hope Amb:    13:30–14:30
      Board Members:         no blackout
  - Portrait window: 13:30–18:30
  - One-at-a-time per provider (artist / massage table / portrait slot)
  - No two services overlap for the same model

Durations (minutes):
  - chair_massage : 10
  - hand_massage  : 10
  - nail_stamping : 10
  - hair          : 60
  - makeup        : 60
  - portrait      : 5
"""

from datetime import datetime, timedelta
import copy

# ─── Constants ────────────────────────────────────────────────────────────────

DURATIONS = {
    "chair_massage": 10,
    "hand_massage": 10,
    "nail_stamping": 10,
    "hair": 45,
    "makeup": 45,
    "portrait": 5,
}

# Time-only tuples (hour, minute) — resolved to datetimes in Scheduler.__init__
_WINDOW_TIMES = {
    "chair_massage": ((12, 0), (19, 0)),
    "hand_massage":  ((12, 0), (19, 0)),
    "nail_stamping": ((12, 0), (19, 0)),
    "hair":          ((12, 0), (19, 0)),
    "makeup":        ((12, 0), (19, 0)),
    "portrait":      ((13, 30), (19, 0)),
}

_BLACKOUT_TIMES = {
    "group1":           ((15, 0), (16, 0)),
    "group2":           ((13, 30), (14, 30)),
    "hope_ambassador":  ((13, 30), (14, 30)),
    # board_member: no blackout
}


def _make_dt(date_str: str, h: int, m: int) -> datetime:
    return datetime.strptime(f"{date_str} {h:02d}:{m:02d}", "%Y-%m-%d %H:%M")


GROUP_LABELS = {
    "group1":          ["group 1", "group1", "1"],
    "group2":          ["group 2", "group2", "2"],
    "hope_ambassador": ["hope ambassador", "hope ambassadors", "ha", "hope amb"],
    "board_member":    ["board member", "board members", "board"],
}

# These are populated by Scheduler.__init__ with the real event date
WINDOWS: dict = {}
REHEARSAL_BLACKOUTS: dict = {}


def _group_key(model: dict) -> str | None:
    raw = model.get("group", "").lower().strip()
    for key, labels in GROUP_LABELS.items():
        if any(raw == lbl or lbl in raw for lbl in labels):
            return key
    return None


def _overlaps(s1: datetime, e1: datetime, s2: datetime, e2: datetime) -> bool:
    return s1 < e2 and s2 < e1


def _in_blackout(start: datetime, end: datetime, group_key: str | None) -> bool:
    if not group_key:
        return False
    blackout = REHEARSAL_BLACKOUTS.get(group_key)
    if not blackout:
        return False
    return _overlaps(start, end, blackout[0], blackout[1])


# ─── Provider schedule tracker ───────────────────────────────────────────────

class ProviderCalendar:
    """Tracks booked slots for a single provider (artist, masseur, photographer)."""

    def __init__(self, name: str, service: str):
        self.name = name
        self.service = service
        self.slots: list[tuple[datetime, datetime, str]] = []  # (start, end, model_name)

    def is_free(self, start: datetime, end: datetime) -> bool:
        return not any(_overlaps(start, end, s, e) for s, e, _ in self.slots)

    def book(self, start: datetime, end: datetime, model_name: str):
        self.slots.append((start, end, model_name))


# ─── Scheduler ───────────────────────────────────────────────────────────────

class Scheduler:
    def __init__(
        self,
        models: list[dict],
        artists: list[dict],
        num_massage_tables: int = 2,
        num_hand_massage_tables: int = 2,
        num_nail_stations: int = 2,
        num_portrait_slots: int = 1,
        event_date: str = "2026-01-01",
        hair_duration_min: int = 60,
        makeup_duration_min: int = 60,
    ):
        global WINDOWS, REHEARSAL_BLACKOUTS
        WINDOWS = {
            svc: (_make_dt(event_date, s[0], s[1]), _make_dt(event_date, e[0], e[1]))
            for svc, (s, e) in _WINDOW_TIMES.items()
        }
        REHEARSAL_BLACKOUTS = {
            grp: (_make_dt(event_date, s[0], s[1]), _make_dt(event_date, e[0], e[1]))
            for grp, (s, e) in _BLACKOUT_TIMES.items()
        }
        self._event_date = event_date

        self.models = copy.deepcopy(models)
        self.artists = {a["name"]: a for a in artists}
        self.hair_duration = hair_duration_min
        self.makeup_duration = makeup_duration_min

        DURATIONS["hair"] = hair_duration_min
        DURATIONS["makeup"] = makeup_duration_min

        # Build provider calendars
        self.calendars: dict[str, ProviderCalendar] = {}

        for a in artists:
            self.calendars[f"hair::{a['name']}"] = ProviderCalendar(a["name"], "hair")
            self.calendars[f"makeup::{a['name']}"] = ProviderCalendar(a["name"], "makeup")

        for i in range(num_massage_tables):
            self.calendars[f"chair_massage::table{i+1}"] = ProviderCalendar(f"Massage Table {i+1}", "chair_massage")

        for i in range(num_hand_massage_tables):
            self.calendars[f"hand_massage::table{i+1}"] = ProviderCalendar(f"Hand Massage {i+1}", "hand_massage")

        for i in range(num_nail_stations):
            self.calendars[f"nail_stamping::station{i+1}"] = ProviderCalendar(f"Nail Station {i+1}", "nail_stamping")

        for i in range(num_portrait_slots):
            self.calendars[f"portrait::slot{i+1}"] = ProviderCalendar(f"Portrait Slot {i+1}", "portrait")

        self.schedule: list[dict] = []

    def _find_slot(
        self,
        service: str,
        provider_key: str,
        earliest: datetime,
        group_key: str | None,
        model_busy: list[tuple[datetime, datetime]],
    ) -> tuple[datetime, datetime] | None:
        """
        Find the earliest slot >= earliest that is free for both the provider
        and the model, outside any blackout window, within the service window.
        """
        win_start, win_end = WINDOWS[service]
        dur = timedelta(minutes=DURATIONS[service])
        cal = self.calendars.get(provider_key)
        if not cal:
            return None

        start = max(earliest, win_start)

        while start + dur <= win_end:
            end = start + dur

            # Jump past blackout if needed
            if _in_blackout(start, end, group_key):
                start = REHEARSAL_BLACKOUTS[group_key][1]
                continue

            # Check provider availability
            provider_conflict = None
            for s, e, _ in sorted(cal.slots, key=lambda x: x[0]):
                if _overlaps(start, end, s, e):
                    provider_conflict = e
                    break
            if provider_conflict is not None:
                start = provider_conflict
                continue

            # Check model's own schedule (no personal overlaps)
            model_conflict = None
            for ms, me in sorted(model_busy):
                if _overlaps(start, end, ms, me):
                    model_conflict = me
                    break
            if model_conflict is not None:
                start = model_conflict
                continue

            return start, end

        return None

    def _book(self, service: str, provider_key: str, start: datetime, end: datetime, model_name: str):
        self.calendars[provider_key].book(start, end, model_name)

    def _schedule_model(self, model: dict) -> dict:
        name = model["name"]
        group_key = _group_key(model)
        appointments = {}
        model_busy: list[tuple[datetime, datetime]] = []

        day_start = WINDOWS["chair_massage"][0]  # 12:00 PM

        # ── 1. Chair massage (before hair & makeup) ────────────────────────────
        massage_end = day_start
        for mk in sorted(k for k in self.calendars if k.startswith("chair_massage::")):
            slot = self._find_slot("chair_massage", mk, day_start, group_key, model_busy)
            if slot:
                start, end = slot
                self._book("chair_massage", mk, start, end, name)
                model_busy.append((start, end))
                appointments["chair_massage"] = {"provider": self.calendars[mk].name, "start": start, "end": end}
                massage_end = end
                break

        # ── 2. Hair ────────────────────────────────────────────────────────────
        hair_end = massage_end
        if model.get("wants_hair", True):
            assigned_hair = model.get("assigned_hair_stylist", "")
            all_hair = [
                a_name for key in self.calendars
                if key.startswith("hair::")
                for a_name in [key[len("hair::"):]]
                if self.artists.get(a_name, {}).get("role") in ("hair", "both")
            ]
            # Find earliest available slot across all artists; prefer assigned if within 30 min of best
            best_slot, best_candidate = None, None
            for candidate in all_hair:
                slot = self._find_slot("hair", f"hair::{candidate}", massage_end, group_key, model_busy)
                if slot and (best_slot is None or slot[0] < best_slot[0]):
                    best_slot, best_candidate = slot, candidate
            # If assigned artist can serve within 30 min of the best slot, prefer them
            if assigned_hair and f"hair::{assigned_hair}" in self.calendars and best_slot:
                assigned_slot = self._find_slot("hair", f"hair::{assigned_hair}", massage_end, group_key, model_busy)
                if assigned_slot and (assigned_slot[0] - best_slot[0]).total_seconds() <= 1800:
                    best_slot, best_candidate = assigned_slot, assigned_hair
            if best_slot:
                start, end = best_slot
                self._book("hair", f"hair::{best_candidate}", start, end, name)
                model_busy.append((start, end))
                appointments["hair"] = {"provider": best_candidate, "start": start, "end": end}
                hair_end = end

        # ── 3. Makeup (after hair) ─────────────────────────────────────────────
        makeup_end = hair_end
        if model.get("wants_makeup", True):
            assigned_mu = model.get("assigned_makeup_artist", "")
            booked_hair = appointments.get("hair", {}).get("provider", "")
            all_makeup = [
                a_name for key in self.calendars
                if key.startswith("makeup::")
                for a_name in [key[len("makeup::"):]]
                if a_name != booked_hair
                and self.artists.get(a_name, {}).get("role") in ("makeup", "both")
            ]
            # Find earliest available slot across all artists; prefer assigned if within 30 min of best
            best_slot, best_candidate = None, None
            for candidate in all_makeup:
                slot = self._find_slot("makeup", f"makeup::{candidate}", hair_end, group_key, model_busy)
                if slot and (best_slot is None or slot[0] < best_slot[0]):
                    best_slot, best_candidate = slot, candidate
            if assigned_mu and f"makeup::{assigned_mu}" in self.calendars and best_slot:
                assigned_slot = self._find_slot("makeup", f"makeup::{assigned_mu}", hair_end, group_key, model_busy)
                if assigned_slot and (assigned_slot[0] - best_slot[0]).total_seconds() <= 1800:
                    best_slot, best_candidate = assigned_slot, assigned_mu
            if best_slot:
                start, end = best_slot
                self._book("makeup", f"makeup::{best_candidate}", start, end, name)
                model_busy.append((start, end))
                appointments["makeup"] = {"provider": best_candidate, "start": start, "end": end}
                makeup_end = end

        # ── 4. Portrait (after hair + makeup, within portrait window) ──────────
        glam_done = max(hair_end, makeup_end)
        portrait_earliest = max(glam_done, WINDOWS["portrait"][0])
        for pk in sorted(k for k in self.calendars if k.startswith("portrait::")):
            slot = self._find_slot("portrait", pk, portrait_earliest, group_key, model_busy)
            if slot:
                start, end = slot
                self._book("portrait", pk, start, end, name)
                model_busy.append((start, end))
                appointments["portrait"] = {"provider": self.calendars[pk].name, "start": start, "end": end}
                break

        # ── 5. Hand massage (any time, non-overlapping with model's schedule) ──
        for hk in sorted(k for k in self.calendars if k.startswith("hand_massage::")):
            slot = self._find_slot("hand_massage", hk, day_start, group_key, model_busy)
            if slot:
                start, end = slot
                self._book("hand_massage", hk, start, end, name)
                model_busy.append((start, end))
                appointments["hand_massage"] = {"provider": self.calendars[hk].name, "start": start, "end": end}
                break

        # ── 6. Nail stamping (only if requested) ──────────────────────────────
        if model.get("wants_nails", False):
            for nk in sorted(k for k in self.calendars if k.startswith("nail_stamping::")):
                slot = self._find_slot("nail_stamping", nk, day_start, group_key, model_busy)
                if slot:
                    start, end = slot
                    self._book("nail_stamping", nk, start, end, name)
                    model_busy.append((start, end))
                    appointments["nail_stamping"] = {"provider": self.calendars[nk].name, "start": start, "end": end}
                    break

        return {
            "model": name,
            "group": model.get("group", ""),
            "rehearsal_time": model.get("rehearsal_time", ""),
            "hair_stylist": model.get("assigned_hair_stylist", ""),
            "makeup_artist": model.get("assigned_makeup_artist", ""),
            "appointments": appointments,
        }

    def run(self) -> list[dict]:
        """Schedule all models. Group 2 / Hope Ambassadors first (tighter window)."""
        def priority(model):
            gk = _group_key(model)
            return 0 if gk in ("group2", "hope_ambassador") else 1

        for model in sorted(self.models, key=priority):
            self.schedule.append(self._schedule_model(model))

        return self.schedule


# ─── Helpers ─────────────────────────────────────────────────────────────────

def fmt_time(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.strftime("%-I:%M %p")


def schedule_to_rows(schedule: list[dict]) -> list[dict]:
    """Flatten schedule to simple rows for display / export."""
    rows = []
    for entry in schedule:
        appts = entry.get("appointments", {})

        def appt(key):
            a = appts.get(key, {})
            return fmt_time(a.get("start")), fmt_time(a.get("end")), a.get("provider", "")

        cm_s, cm_e, _ = appt("chair_massage")
        h_s,  h_e,  h_p  = appt("hair")
        m_s,  m_e,  m_p  = appt("makeup")
        p_s,  p_e,  _    = appt("portrait")
        hm_s, hm_e, _    = appt("hand_massage")
        n_s,  n_e,  _    = appt("nail_stamping")

        rows.append({
            "Model":         entry["model"],
            "Group":         entry["group"],
            "Rehearsal":     entry.get("rehearsal_time", ""),
            "Hair Stylist":  h_p or entry.get("hair_stylist", ""),
            "Hair Time":     f"{h_s}–{h_e}" if h_s else "",
            "Makeup Artist": m_p or entry.get("makeup_artist", ""),
            "Makeup Time":   f"{m_s}–{m_e}" if m_s else "",
            "Chair Massage": f"{cm_s}–{cm_e}" if cm_s else "",
            "Portrait":      f"{p_s}–{p_e}" if p_s else "",
            "Hand Massage":  f"{hm_s}–{hm_e}" if hm_s else "",
            "Nails":         f"{n_s}–{n_e}" if n_s else "",
        })
    return rows
