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
  - Portrait window: 13:30–18:30
  - One-at-a-time per provider (artist / massage table / portrait slot)

Durations (minutes):
  - chair_massage : 20
  - hand_massage  : 15
  - nail_stamping : 15
  - hair          : 60
  - makeup        : 60
  - portrait      : 10  (includes transition)
"""

from datetime import datetime, timedelta
from collections import defaultdict
import copy

# ─── Constants ────────────────────────────────────────────────────────────────

DURATIONS = {
    "chair_massage": 20,
    "hand_massage": 15,
    "nail_stamping": 15,
    "hair": 60,
    "makeup": 60,
    "portrait": 10,
}

# Time-only tuples (hour, minute) — resolved to datetimes in Scheduler.__init__
_WINDOW_TIMES = {
    "chair_massage": ((12, 0), (19, 0)),
    "hand_massage":  ((12, 0), (19, 0)),
    "nail_stamping": ((12, 0), (19, 0)),
    "hair":          ((12, 0), (19, 0)),
    "makeup":        ((12, 0), (19, 0)),
    "portrait":      ((13, 30), (18, 30)),
}

_BLACKOUT_TIMES = {
    "group1":           ((15, 0), (16, 0)),
    "group2":           ((13, 30), (14, 30)),
    "hope_ambassador":  ((13, 30), (14, 30)),
}


def _make_dt(date_str: str, h: int, m: int) -> datetime:
    return datetime.strptime(f"{date_str} {h:02d}:{m:02d}", "%Y-%m-%d %H:%M")

GROUP_LABELS = {
    "group1": ["group 1", "group1", "1"],
    "group2": ["group 2", "group2", "2"],
    "hope_ambassador": ["hope ambassador", "hope ambassadors", "ha", "hope amb"],
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

    def next_free_after(self, earliest: datetime, duration_min: int) -> datetime | None:
        """Find the earliest slot >= earliest that fits duration_min minutes."""
        candidate = earliest
        duration = timedelta(minutes=duration_min)
        # Try every minute up to end of day
        deadline = earliest.replace(hour=19, minute=0, second=0, microsecond=0)
        while candidate + duration <= deadline:
            end = candidate + duration
            if self.is_free(candidate, end):
                return candidate
            # Jump to end of next conflicting slot
            jumped = False
            for s, e, _ in sorted(self.slots, key=lambda x: x[0]):
                if _overlaps(candidate, end, s, e):
                    candidate = e
                    jumped = True
                    break
            if not jumped:
                candidate += timedelta(minutes=1)
        return None


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
        # Build date-aware windows and blackouts
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

        # Update durations from config
        DURATIONS["hair"] = hair_duration_min
        DURATIONS["makeup"] = makeup_duration_min

        # Build provider calendars
        self.calendars: dict[str, ProviderCalendar] = {}

        for a in artists:
            self.calendars[f"hair::{a['name']}"] = ProviderCalendar(a["name"], "hair")
            self.calendars[f"makeup::{a['name']}"] = ProviderCalendar(a["name"], "makeup")

        for i in range(num_massage_tables):
            key = f"chair_massage::table{i+1}"
            self.calendars[key] = ProviderCalendar(f"Massage Table {i+1}", "chair_massage")

        for i in range(num_hand_massage_tables):
            key = f"hand_massage::table{i+1}"
            self.calendars[key] = ProviderCalendar(f"Hand Massage {i+1}", "hand_massage")

        for i in range(num_nail_stations):
            key = f"nail_stamping::station{i+1}"
            self.calendars[key] = ProviderCalendar(f"Nail Station {i+1}", "nail_stamping")

        for i in range(num_portrait_slots):
            key = f"portrait::slot{i+1}"
            self.calendars[key] = ProviderCalendar(f"Portrait Slot {i+1}", "portrait")

        self.schedule: list[dict] = []

    def _find_slot(
        self,
        service: str,
        provider_key: str,
        earliest: datetime,
        group_key: str | None,
    ) -> tuple[datetime, datetime] | None:
        """Find the next available slot for a provider, respecting blackouts and windows."""
        win_start, win_end = WINDOWS[service]
        duration = DURATIONS[service]
        cal = self.calendars.get(provider_key)
        if not cal:
            return None

        start = max(earliest, win_start)
        deadline = win_end

        while True:
            end = start + timedelta(minutes=duration)
            if end > deadline:
                return None
            if _in_blackout(start, end, group_key):
                # Jump past blackout
                blackout_end = REHEARSAL_BLACKOUTS[group_key][1]
                start = blackout_end
                continue
            if cal.is_free(start, end):
                return start, end
            # Advance past conflict
            next_start = cal.next_free_after(start, duration)
            if next_start is None:
                return None
            start = next_start

    def _book(self, service: str, provider_key: str, start: datetime, end: datetime, model_name: str):
        cal = self.calendars[provider_key]
        cal.book(start, end, model_name)

    def _schedule_model(self, model: dict) -> dict:
        name = model["name"]
        group_key = _group_key(model)

        appointments = {}

        # ── 1. Chair massage (must happen before hair AND makeup) ──────────────
        day_start = WINDOWS["chair_massage"][0]  # 12:00 PM on event day
        massage_end = day_start
        if True:  # all models offered massage
            massage_keys = [k for k in self.calendars if k.startswith("chair_massage::")]
            for mk in massage_keys:
                slot = self._find_slot("chair_massage", mk, day_start, group_key)
                if slot:
                    start, end = slot
                    self._book("chair_massage", mk, start, end, name)
                    appointments["chair_massage"] = {
                        "provider": self.calendars[mk].name,
                        "start": start,
                        "end": end,
                    }
                    massage_end = end
                    break

        # ── 2. Hair ────────────────────────────────────────────────────────────
        hair_end = massage_end
        if model.get("wants_hair", True):
            hair_artist = model.get("assigned_hair_stylist", "")
            hair_key = f"hair::{hair_artist}"
            if hair_key in self.calendars:
                slot = self._find_slot("hair", hair_key, massage_end, group_key)
                if slot:
                    start, end = slot
                    self._book("hair", hair_key, start, end, name)
                    appointments["hair"] = {
                        "provider": hair_artist,
                        "start": start,
                        "end": end,
                    }
                    hair_end = end

        # ── 3. Makeup ──────────────────────────────────────────────────────────
        makeup_end = massage_end
        if model.get("wants_makeup", True):
            makeup_artist = model.get("assigned_makeup_artist", "")
            makeup_key = f"makeup::{makeup_artist}"
            if makeup_key in self.calendars:
                slot = self._find_slot("makeup", makeup_key, massage_end, group_key)
                if slot:
                    start, end = slot
                    self._book("makeup", makeup_key, start, end, name)
                    appointments["makeup"] = {
                        "provider": makeup_artist,
                        "start": start,
                        "end": end,
                    }
                    makeup_end = end

        # ── 4. Portrait (after both hair AND makeup) ──────────────────────────
        glam_done = max(hair_end, makeup_end)
        portrait_earliest = max(glam_done, WINDOWS["portrait"][0])
        portrait_keys = [k for k in self.calendars if k.startswith("portrait::")]
        for pk in portrait_keys:
            slot = self._find_slot("portrait", pk, portrait_earliest, group_key)
            if slot:
                start, end = slot
                self._book("portrait", pk, start, end, name)
                appointments["portrait"] = {
                    "provider": self.calendars[pk].name,
                    "start": start,
                    "end": end,
                }
                break

        # ── 5. Hand massage (any time) ─────────────────────────────────────────
        hand_keys = [k for k in self.calendars if k.startswith("hand_massage::")]
        for hk in hand_keys:
            slot = self._find_slot("hand_massage", hk, day_start, group_key)
            if slot:
                start, end = slot
                self._book("hand_massage", hk, start, end, name)
                appointments["hand_massage"] = {
                    "provider": self.calendars[hk].name,
                    "start": start,
                    "end": end,
                }
                break

        # ── 6. Nail stamping (any time, only if requested) ─────────────────────
        if model.get("wants_nails", False):
            nail_keys = [k for k in self.calendars if k.startswith("nail_stamping::")]
            for nk in nail_keys:
                slot = self._find_slot("nail_stamping", nk, day_start, group_key)
                if slot:
                    start, end = slot
                    self._book("nail_stamping", nk, start, end, name)
                    appointments["nail_stamping"] = {
                        "provider": self.calendars[nk].name,
                        "start": start,
                        "end": end,
                    }
                    break

        return {
            "model": name,
            "group": model.get("group", ""),
            "hair_stylist": model.get("assigned_hair_stylist", ""),
            "makeup_artist": model.get("assigned_makeup_artist", ""),
            "appointments": appointments,
        }

    def run(self) -> list[dict]:
        """Schedule all models. Returns list of schedule entries."""
        # Sort: Group 2 / Hope Ambassadors first (tighter portrait window at 1:30)
        def priority(model):
            gk = _group_key(model)
            if gk in ("group2", "hope_ambassador"):
                return 0
            return 1

        ordered = sorted(self.models, key=priority)

        for model in ordered:
            entry = self._schedule_model(model)
            self.schedule.append(entry)

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

        cm_s, cm_e, cm_p = appt("chair_massage")
        h_s, h_e, h_p = appt("hair")
        m_s, m_e, m_p = appt("makeup")
        p_s, p_e, p_p = appt("portrait")
        hm_s, hm_e, hm_p = appt("hand_massage")
        n_s, n_e, n_p = appt("nail_stamping")

        rows.append({
            "Model": entry["model"],
            "Group": entry["group"],
            "Chair Massage Start": cm_s,
            "Chair Massage End": cm_e,
            "Hair Stylist": h_p or entry.get("hair_stylist", ""),
            "Hair Start": h_s,
            "Hair End": h_e,
            "Makeup Artist": m_p or entry.get("makeup_artist", ""),
            "Makeup Start": m_s,
            "Makeup End": m_e,
            "Portrait Start": p_s,
            "Portrait End": p_e,
            "Hand Massage Start": hm_s,
            "Hand Massage End": hm_e,
            "Nails Start": n_s,
            "Nails End": n_e,
        })
    return rows
