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
import re

# ─── Constants ────────────────────────────────────────────────────────────────

DURATIONS = {
    "hair": 45,
    "makeup": 45,
    "portrait": 5,
    "massage": 15,   # 10-min session + 5-min buffer in calendar
}

MASSAGE_SESSION_MIN = 10  # actual displayed appointment length (excludes buffer)

# Time-only tuples (hour, minute) — resolved to datetimes in Scheduler.__init__
_WINDOW_TIMES = {
    "hair":     ((10, 0), (16, 0)),    # 10 AM – 4 PM
    "makeup":   ((10, 0), (16, 0)),    # 10 AM – 4 PM
    "portrait": ((13, 30), (18, 0)),
    "massage":  ((9, 0),   (17, 0)),   # 9 AM – 5 PM
}

_BLACKOUT_TIMES = {
    "group1":           ((13, 0), (14, 0)),   # Rehearsal: 1 PM – 2 PM
    "group2":           ((15, 0), (16, 0)),   # Rehearsal: 3 PM – 4 PM
    "group3":           ((14, 0), (14, 20)),  # Rehearsal: 2 PM – 2:20 PM
    "hope_ambassador":  ((13, 0), (14, 0)),   # Rehearsal: 1 PM – 2 PM (own entry, not group2)
    "all_rehearsals":   ((13, 0), (16, 0)),   # All 3 rehearsals combined: 1 PM – 4 PM
    # board_member: no blackout
}

# Models who wear wigs — schedule their hair appointment later in the day.
_LATER_HAIR_MODELS = {
    "brooke myshrall",
    "alison willette",
    "jodi peterson",
    "michelle neas",
    "kristen hall",
    "cassia leach",
}

# Models attending multiple rehearsals — use a custom combined blackout key.
_MODEL_BLACKOUT_OVERRIDE: dict[str, str] = {
    "cassia leach": "all_rehearsals",   # blocked 1 PM – 4 PM (all 3 rehearsals)
}

# Models whose makeup should be placed at the LAST possible slot in the day.
_LAST_SLOT_MAKEUP_MODELS: set[str] = {
    "cassia leach",
}

# Per-model availability windows: (earliest_hour, earliest_min), (latest_hour, latest_min)
# All appointments for these models must fall within this window.
_MODEL_AVAILABILITY_TIMES: dict[str, tuple[tuple[int, int], tuple[int, int]]] = {
    "jaime gagnon":   ((11, 0), (14, 30)),
    "micayla lawson": ((13, 0), (16, 0)),
}


def _make_dt(date_str: str, h: int, m: int) -> datetime:
    return datetime.strptime(f"{date_str} {h:02d}:{m:02d}", "%Y-%m-%d %H:%M")


def _parse_time_slot(ts: str, event_date: str) -> datetime | None:
    """Parse a start-only time string like '2:00 PM' → datetime, or None."""
    if not ts:
        return None
    ts = ts.strip()
    for fmt in ("%I:%M %p", "%I:%M%p", "%H:%M"):
        try:
            t = datetime.strptime(ts, fmt)
            return _make_dt(event_date, t.hour, t.minute)
        except ValueError:
            continue
    return None


def _parse_duration_min(ts: str) -> int | None:
    """
    Extract exact minutes from the Time Slot column value.
    Hours (e.g. '1 hr', '1.5 hours') are converted to minutes.
    Any other value: grab the first integer and use it as minutes exactly.
    Returns None if no number found.
    """
    if not ts:
        return None
    ts = ts.strip().lower()

    # Hours: "1 hr", "1.5 hrs", "2 hours", "1 hour"
    m = re.search(r'(\d+(?:\.\d+)?)\s*h(?:ou?r?)?s?', ts)
    if m:
        return int(float(m.group(1)) * 60)

    # Any integer → exact minutes
    m = re.search(r'(\d+)', ts)
    if m:
        return int(m.group(1))

    return None


GROUP_LABELS = {
    "group1":          ["group 1", "group1", "1", "act 1", "act1"],
    "group2":          ["group 2", "group2", "2", "act 2", "act2"],
    "group3":          ["group 3", "group3", "3", "act 3", "act3"],
    "hope_ambassador": ["hope ambassador", "hope ambassadors", "ha", "hope amb"],
    "board_member":    ["board member", "board members", "board", "bad", "bad member", "bad members"],
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
        num_portrait_slots: int = 1,
        num_massage_tables: int = 1,
        massage_provider_names: list[str] | None = None,
        event_date: str = "2026-01-01",
        **kwargs,
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

        # ── Build provider calendars ──────────────────────────────────────────

        self.calendars: dict[str, ProviderCalendar] = {}

        # 1. Glam artists from glam_info CSV
        for a in artists:
            self.calendars[f"hair::{a['name']}"] = ProviderCalendar(a["name"], "hair")
            self.calendars[f"makeup::{a['name']}"] = ProviderCalendar(a["name"], "makeup")

        # 2. Auto-create calendars for any artist named in the models master sheet
        #    that doesn't already have a calendar.  This ensures the sheet is the
        #    single source of truth — if a name is written there, it is schedulable
        #    even if it's missing from the glam info CSV.
        assigned_names: set[str] = set()
        for model in self.models:
            h = model.get("assigned_hair_stylist", "").strip()
            m_art = model.get("assigned_makeup_artist", "").strip()
            if h:
                assigned_names.add(h)
            if m_art:
                assigned_names.add(m_art)

        for aname in assigned_names:
            if aname not in self.artists:
                self.artists[aname] = {"name": aname, "role": "both", "max_models": 99}
            if f"hair::{aname}" not in self.calendars:
                self.calendars[f"hair::{aname}"] = ProviderCalendar(aname, "hair")
            if f"makeup::{aname}" not in self.calendars:
                self.calendars[f"makeup::{aname}"] = ProviderCalendar(aname, "makeup")

        # 3. Portrait slots
        for i in range(num_portrait_slots):
            self.calendars[f"portrait::slot{i+1}"] = ProviderCalendar(f"Portrait Slot {i+1}", "portrait")

        # 4. Massage tables — 10-min sessions with 5-min buffer, 9 AM – 5 PM
        _massage_names = (massage_provider_names or [])[:num_massage_tables]
        while len(_massage_names) < num_massage_tables:
            _massage_names.append(f"Massage Table {len(_massage_names) + 1}")
        for mname in _massage_names:
            self.calendars[f"massage::{mname}"] = ProviderCalendar(mname, "massage")
            if mname not in self.artists:
                self.artists[mname] = {"name": mname, "role": "massage", "max_models": 999}

        # Count unique models assigned to each glam artist (hair + makeup combined)
        artist_model_count: dict[str, set] = {}
        for model in self.models:
            h = model.get("assigned_hair_stylist", "").strip()
            m = model.get("assigned_makeup_artist", "").strip()
            if h:
                artist_model_count.setdefault(h, set()).add(model["name"])
            if m:
                artist_model_count.setdefault(m, set()).add(model["name"])

        self._artist_model_count = {k: len(v) for k, v in artist_model_count.items()}

        self.schedule: list[dict] = []

    def _booking_count(self, artist_name: str) -> int:
        """
        Number of unique models served by this artist across all their calendars.

        Counting unique models (not raw appointments) is correct because a "both"
        artist who does hair AND makeup for the same model should count as 1 model
        served, not 2.  The old per-appointment count made "both" artists appear
        full at half their real capacity and knocked them out of the candidate list
        too early, leaving models like Kristin Perry without any hair appointment.
        """
        served: set[str] = set()
        for cal in self.calendars.values():
            if cal.name == artist_name:
                for _, _, m in cal.slots:
                    if m != "__break__":
                        served.add(m)
        return len(served)

    def _resolve_calendar_key(self, role: str, name: str) -> str | None:
        """
        Return the exact calendar key for *role* (hair/makeup) whose artist name
        matches *name*, using case-insensitive substring matching.

        Exact match is tried first, then prefix match (name starts with the
        candidate), then substring match.  Returns None if nothing matches.

        This tolerates small differences between the name typed in the models
        master sheet and the name on the glam info CSV (e.g. different
        capitalisation, truncated last name, extra spaces).
        """
        prefix = f"{role}::"
        needle = name.strip().lower()
        exact = f"{prefix}{name}"
        if exact in self.calendars:
            return exact

        candidates = [k for k in self.calendars if k.startswith(prefix)]
        # Prefer the one whose artist-name contains the needle as a prefix
        for key in candidates:
            artist = key[len(prefix):].lower()
            if artist.startswith(needle) or needle.startswith(artist):
                return key
        # Fall back to plain substring containment
        for key in candidates:
            artist = key[len(prefix):].lower()
            if needle in artist or artist in needle:
                return key
        return None

    def _find_slot(
        self,
        service: str,
        provider_key: str,
        earliest: datetime,
        group_key: str | None,
        model_busy: list[tuple[datetime, datetime]],
        duration_min: int | None = None,
        win_end_override: datetime | None = None,
    ) -> tuple[datetime, datetime] | None:
        """
        Find the earliest slot >= earliest that is free for both the provider
        and the model, outside any blackout window, within the service window.
        duration_min overrides the global DURATIONS default when provided.
        win_end_override allows the rescue pass to extend past the normal 5pm cap.
        """
        win_start, win_end = WINDOWS[service]
        if win_end_override is not None:
            win_end = win_end_override
        dur = timedelta(minutes=duration_min if duration_min is not None else DURATIONS[service])
        cal = self.calendars.get(provider_key)
        if not cal:
            return None

        # For "both" artists, combine hair+makeup slots to prevent double-booking
        sibling_service = "makeup" if service == "hair" else "hair"
        sibling_key = f"{sibling_service}::{cal.name}"
        sibling_cal = (
            self.calendars.get(sibling_key)
            if self.artists.get(cal.name, {}).get("role") == "both"
            else None
        )

        # Artists with ≤3 assigned models start at 11am (no need to come in at 10am).
        if service in ("hair", "makeup") and self._artist_model_count.get(cal.name, 0) <= 3:
            win_start = max(win_start, _make_dt(self._event_date, 11, 0))

        start = max(earliest, win_start)
        while start + dur <= win_end:
            end = start + dur

            # Jump past blackout if needed
            if _in_blackout(start, end, group_key):
                start = REHEARSAL_BLACKOUTS[group_key][1]
                continue

            # Check provider availability (include sibling calendar for "both" artists)
            all_provider_slots = cal.slots + (sibling_cal.slots if sibling_cal else [])
            provider_conflict = None
            for s, e, _ in sorted(all_provider_slots, key=lambda x: x[0]):
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

    def _find_last_slot(
        self,
        service: str,
        provider_key: str,
        earliest: datetime,
        group_key: str | None,
        model_busy: list[tuple[datetime, datetime]],
        duration_min: int | None = None,
        win_end_override: datetime | None = None,
    ) -> tuple[datetime, datetime] | None:
        """
        Find the LATEST available slot in the window (instead of earliest).
        Builds a list of all blocked intervals, finds free gaps, and returns
        the last gap large enough to fit the appointment.
        """
        win_start, win_end = WINDOWS[service]
        if win_end_override is not None:
            win_end = win_end_override
        effective_start = max(earliest, win_start)
        dur = timedelta(minutes=duration_min if duration_min is not None else DURATIONS[service])
        cal = self.calendars.get(provider_key)
        if not cal:
            return None

        sibling_service = "makeup" if service == "hair" else "hair"
        sibling_key = f"{sibling_service}::{cal.name}"
        sibling_cal = (
            self.calendars.get(sibling_key)
            if self.artists.get(cal.name, {}).get("role") == "both"
            else None
        )

        # Collect all blocked intervals within the effective window
        all_provider_slots = cal.slots + (sibling_cal.slots if sibling_cal else [])
        blocked: list[tuple[datetime, datetime]] = []
        for s, e, _ in all_provider_slots:
            if s < win_end and e > effective_start:
                blocked.append((max(s, effective_start), min(e, win_end)))
        for ms, me in model_busy:
            if ms < win_end and me > effective_start:
                blocked.append((max(ms, effective_start), min(me, win_end)))
        if group_key and group_key in REHEARSAL_BLACKOUTS:
            bs, be = REHEARSAL_BLACKOUTS[group_key]
            if bs < win_end and be > effective_start:
                blocked.append((max(bs, effective_start), min(be, win_end)))

        # Sort and merge overlapping blocked intervals
        blocked.sort()
        merged: list[list[datetime]] = []
        for s, e in blocked:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])

        # Find all free intervals and return the latest slot that fits dur
        last_slot: tuple[datetime, datetime] | None = None
        cursor = effective_start
        for bs, be in merged:
            if cursor + dur <= bs:
                # Free gap [cursor, bs] — latest start within this gap
                latest = bs - dur
                last_slot = (latest, latest + dur)
            cursor = max(cursor, be)
        # Remaining free window after last blocked interval
        if cursor + dur <= win_end:
            latest = win_end - dur
            latest = max(latest, cursor)
            last_slot = (latest, latest + dur)

        return last_slot

    def _book(self, service: str, provider_key: str, start: datetime, end: datetime, model_name: str):
        self.calendars[provider_key].book(start, end, model_name)

    def _schedule_model(self, model: dict, stagger_idx: int = 0) -> dict:
        name = model["name"]
        group_key = _group_key(model)
        appointments = {}
        model_busy: list[tuple[datetime, datetime]] = []

        day_start = WINDOWS["hair"][0]  # 10:00 AM

        # Per-model availability override (e.g. "only available 11am–2:30pm")
        _avail = _MODEL_AVAILABILITY_TIMES.get(name.lower().strip())
        if _avail:
            day_start = max(day_start, _make_dt(self._event_date, *_avail[0]))
            model_win_end = _make_dt(self._event_date, *_avail[1])
        else:
            model_win_end = None  # use service default

        # Per-model blackout override (e.g. models attending all rehearsals)
        _name_lower = name.lower().strip()
        blackout_key = _MODEL_BLACKOUT_OVERRIDE.get(_name_lower, group_key)

        # Scheduling hint from Notes field ("later" / "earlier")
        scheduling_hint = model.get("scheduling_hint", "")
        prefer_afternoon = (scheduling_hint == "later") or (_name_lower in _LATER_HAIR_MODELS)

        # Suppress "later" preference when the assigned artist has ≤4 models —
        # a sparse artist shouldn't sit idle for hours just because a model
        # has an afternoon hint. Compact their schedule instead.
        _COMPACT_THRESHOLD = 4
        assigned_hair = model.get("assigned_hair_stylist", "").strip()
        assigned_mu   = model.get("assigned_makeup_artist", "").strip()
        _mu_artist_light = self._artist_model_count.get(assigned_mu, 0) <= _COMPACT_THRESHOLD
        # Hair "later" preference: ONLY for explicit wig models (_LATER_HAIR_MODELS).
        # Notes-based "later" hints are intentionally excluded from hair — allowing
        # them pushed hair into the afternoon and left artists with 4-hour idle gaps.
        hair_prefer_afternoon = _name_lower in _LATER_HAIR_MODELS
        # Makeup "later" preference: from notes, but suppressed for sparse artists.
        mu_prefer_afternoon   = prefer_afternoon and not _mu_artist_light

        warnings: list[str] = []

        # ── 1. Hair ────────────────────────────────────────────────────────────
        # Only schedule if the sheet has an assigned stylist AND Hair != No.
        hair_end = day_start
        hair_cal_key = None
        if assigned_hair and model.get("wants_hair", True):
            hair_cal_key = self._resolve_calendar_key("hair", assigned_hair)
            if not hair_cal_key:
                msg = f"[WARN] {name}: hair artist '{assigned_hair}' not found."
                print(msg)
                warnings.append(msg[7:])
            else:
                hair_dur = _parse_duration_min(model.get("hair_time_slot", ""))
                if hair_dur is None:
                    print(f"[WARN] {name}: hair time slot missing/unparseable — using {DURATIONS['hair']}min default")
                # "later" note → try afternoon first, then fall back to morning
                # Everyone else → fill from 11am in order, no gaps
                # Prefer afternoon if Notes say "later", but always fall back to day_start
                hair_search_starts = [WINDOWS["hair"][1] - timedelta(hours=3), day_start] if hair_prefer_afternoon else [day_start]
                best_slot = None
                for search_start in hair_search_starts:
                    best_slot = self._find_slot("hair", hair_cal_key, search_start, blackout_key, model_busy, duration_min=hair_dur, win_end_override=model_win_end)
                    if best_slot:
                        break
                # Fallback: ignore later/earlier hint and try full window
                if not best_slot:
                    best_slot = self._find_slot("hair", hair_cal_key, day_start, blackout_key, model_busy, duration_min=hair_dur, win_end_override=model_win_end)
                if best_slot:
                    start, end = best_slot
                    self._book("hair", hair_cal_key, start, end, name)
                    model_busy.append((start, end))
                    appointments["hair"] = {"provider": hair_cal_key[len("hair::"):], "start": start, "end": end}
                    hair_end = end
                else:
                    artist_name = hair_cal_key[len("hair::"):]
                    msg = f"[WARN] {name}: no available hair slot with {artist_name} (artist fully booked 11am-5pm)"
                    print(msg)
                    warnings.append(msg[7:])

        # ── 3. Makeup ──────────────────────────────────────────────────────────
        # Only schedule if the sheet has an assigned artist AND Makeup != No.
        makeup_end = day_start
        assigned_mu = model.get("assigned_makeup_artist", "").strip()
        if assigned_mu and model.get("wants_makeup", True):
            mu_cal_key = self._resolve_calendar_key("makeup", assigned_mu)
            # Detect same artist for both hair and makeup → back-to-back
            hair_artist = hair_cal_key.split("::")[1] if hair_cal_key else ""
            mu_artist = mu_cal_key.split("::")[1] if mu_cal_key else ""
            same_artist = bool(hair_artist and mu_artist and hair_artist == mu_artist)

            if not mu_cal_key:
                msg = f"[WARN] {name}: makeup artist '{assigned_mu}' not found."
                print(msg)
                warnings.append(msg[7:])
            else:
                mu_dur = _parse_duration_min(model.get("makeup_time_slot", ""))
                if mu_dur is None:
                    print(f"[WARN] {name}: makeup time slot missing/unparseable — using {DURATIONS['makeup']}min default")
                if same_artist and hair_end > day_start:
                    # Same artist — makeup starts immediately after hair, no gap
                    start = hair_end
                    end = start + timedelta(minutes=mu_dur if mu_dur else DURATIONS["makeup"])
                    self._book("makeup", mu_cal_key, start, end, name)
                    model_busy.append((start, end))
                    appointments["makeup"] = {"provider": mu_cal_key[len("makeup::"):], "start": start, "end": end}
                    makeup_end = end
                else:
                    # For models with few appointments, pack makeup right after hair.
                    # Start search from hair_end so slots land back-to-back.
                    # Fall back to day_start only if no slot found from hair_end.
                    makeup_earliest = hair_end if hair_end > day_start else day_start

                    # Models in _LAST_SLOT_MAKEUP_MODELS get the LATEST available slot.
                    if _name_lower in _LAST_SLOT_MAKEUP_MODELS:
                        best_slot = self._find_last_slot("makeup", mu_cal_key, makeup_earliest, blackout_key, model_busy, duration_min=mu_dur, win_end_override=model_win_end)
                        if not best_slot:
                            best_slot = self._find_last_slot("makeup", mu_cal_key, day_start, blackout_key, model_busy, duration_min=mu_dur, win_end_override=model_win_end)
                    else:
                        # Respect later/earlier scheduling hint for makeup, but cap
                        # the afternoon search at 4pm so group1 models don't grab the
                        # only slot group2 models can use (post-blackout window).
                        _4pm = WINDOWS["makeup"][1] - timedelta(hours=1)  # 4:00 PM
                        if mu_prefer_afternoon:
                            # Try afternoon first (from max of hair_end and 2pm).
                            _2pm = _make_dt(self._event_date, 14, 0)
                            afternoon_slot = self._find_slot("makeup", mu_cal_key, max(makeup_earliest, _2pm), blackout_key, model_busy, duration_min=mu_dur, win_end_override=model_win_end)
                            if afternoon_slot and afternoon_slot[0] < _4pm:
                                best_slot = afternoon_slot
                            else:
                                best_slot = self._find_slot("makeup", mu_cal_key, makeup_earliest, blackout_key, model_busy, duration_min=mu_dur, win_end_override=model_win_end)
                        else:
                            best_slot = self._find_slot("makeup", mu_cal_key, makeup_earliest, blackout_key, model_busy, duration_min=mu_dur, win_end_override=model_win_end)
                        # If packing after hair didn't work, try anywhere in the day
                        if not best_slot and makeup_earliest > day_start:
                            best_slot = self._find_slot("makeup", mu_cal_key, day_start, blackout_key, model_busy, duration_min=mu_dur, win_end_override=model_win_end)
                    if best_slot:
                        start, end = best_slot
                        self._book("makeup", mu_cal_key, start, end, name)
                        model_busy.append((start, end))
                        appointments["makeup"] = {"provider": mu_cal_key[len("makeup::"):], "start": start, "end": end}
                        makeup_end = end
                    else:
                        artist_name = mu_cal_key[len("makeup::"):]
                        msg = f"[WARN] {name}: no available makeup slot with {artist_name} (artist fully booked 11am-5pm)"
                        print(msg)
                        warnings.append(msg[7:])

        # ── 3b. Massage — must finish BEFORE hair and makeup start ────────────────
        # Schedule last so we know the actual hair/makeup start times, then
        # find the latest massage slot that ends before glam begins.
        glam_start = None
        if "hair" in appointments:
            glam_start = appointments["hair"]["start"]
        if "makeup" in appointments:
            mu_s = appointments["makeup"]["start"]
            glam_start = mu_s if glam_start is None else min(glam_start, mu_s)

        massage_keys = sorted(k for k in self.calendars if k.startswith("massage::"))
        if massage_keys and model.get("wants_chair_massage", False):
            # Search from 9 AM; must end by glam_start (or window end if no glam)
            massage_deadline = glam_start if glam_start else WINDOWS["massage"][1]
            best_slot, best_key = None, None
            for mk in massage_keys:
                slot = self._find_slot(
                    "massage", mk, WINDOWS["massage"][0], blackout_key, model_busy
                )
                if slot and slot[1] <= massage_deadline:
                    cand_load = self._booking_count(self.calendars[mk].name)
                    best_load = (
                        self._booking_count(self.calendars[best_key].name)
                        if best_key else float("inf")
                    )
                    if best_slot is None or cand_load < best_load:
                        best_slot, best_key = slot, mk
            if best_slot:
                cal_start, cal_end = best_slot
                session_end = cal_start + timedelta(minutes=MASSAGE_SESSION_MIN)
                self._book("massage", best_key, cal_start, cal_end, name)
                model_busy.append((cal_start, cal_end))
                appointments["massage"] = {
                    "provider": self.calendars[best_key].name,
                    "start": cal_start,
                    "end": session_end,
                }

        # ── 4. Portrait (Models and HAs only — board members and specific names excluded) ──
        _PORTRAIT_EXCLUDE = {
            "michelle gaghan", "amber-jean nickel", "cassia leach",
            "kerry hekl", "michelle neas",
        }
        _type_lower = model.get("type", "").lower().strip()
        _is_board = (group_key == "board_member") or any(
            t in _type_lower for t in ("board", "bad")
        )
        _excluded = _is_board or name.lower().strip() in _PORTRAIT_EXCLUDE
        if not _excluded:
            glam_done = max(hair_end, makeup_end)
            portrait_earliest = max(glam_done, WINDOWS["portrait"][0])
            for pk in sorted(k for k in self.calendars if k.startswith("portrait::")):
                slot = self._find_slot("portrait", pk, portrait_earliest, blackout_key, model_busy)
                if slot:
                    start, end = slot
                    self._book("portrait", pk, start, end, name)
                    model_busy.append((start, end))
                    appointments["portrait"] = {"provider": self.calendars[pk].name, "start": start, "end": end}
                    break

        # Show rehearsal time from the sheet if present; otherwise derive from blackout
        rehearsal_display = model.get("rehearsal_time", "")
        if not rehearsal_display and group_key and group_key in REHEARSAL_BLACKOUTS:
            bs, be = REHEARSAL_BLACKOUTS[group_key]
            rehearsal_display = f"{fmt_time(bs)} – {fmt_time(be)}"

        return {
            "model": name,
            "group": model.get("group", ""),
            "rehearsal_time": rehearsal_display,
            "hair_stylist": model.get("assigned_hair_stylist", ""),
            "makeup_artist": model.get("assigned_makeup_artist", ""),
            "appointments": appointments,
            "warnings": warnings,
        }

    def _prebook_artist_breaks(self):
        """
        Pre-book 30-min breaks for glam artists with ≥5 models BEFORE model
        scheduling begins. Reserving the slot early means _find_slot naturally
        works around it — no gap-search needed post-hoc.

        Break times (case-insensitive prefix match on artist name):
          Lauren / Megan  → 1:30 PM
          Gianna          → 2:30 PM
          everyone else   → 1:00 PM
        """
        _BREAK_TIMES: dict[str, tuple[int, int]] = {
            "lauren": (13, 30),
            "megan":  (13, 30),
            "gianna": (14, 30),
        }
        _DEFAULT_BREAK = (13, 0)   # 1:00 PM
        _break_dur = timedelta(minutes=30)
        _BREAK_THRESHOLD = 5       # artists with ≥5 models get a break

        seen: set[str] = set()
        for key, cal in self.calendars.items():
            if key.startswith("portrait::") or key.startswith("massage::"):
                continue
            aname = cal.name
            if aname in seen:
                continue
            seen.add(aname)
            if self._artist_model_count.get(aname, 0) < _BREAK_THRESHOLD:
                continue

            aname_lower = aname.strip().lower()
            bh, bm = _DEFAULT_BREAK
            for prefix, (h, m) in _BREAK_TIMES.items():
                if aname_lower.startswith(prefix):
                    bh, bm = h, m
                    break

            b_start = _make_dt(self._event_date, bh, bm)
            b_end   = b_start + _break_dur
            all_keys = [k for k in self.calendars
                        if not k.startswith("portrait::") and not k.startswith("massage::")
                        and self.calendars[k].name == aname]
            for k in all_keys:
                self.calendars[k].book(b_start, b_end, "__break__")

    def _book_breaks(self):
        """
        Book portrait and massage breaks, then validate pre-booked glam artist
        breaks: remove any where no model appointment follows the break
        (end-of-day guard).
        """
        _break_dur = timedelta(minutes=30)
        _portrait_break = _make_dt(self._event_date, 14, 30)
        _massage_break  = _make_dt(self._event_date, 12, 30)

        # Portrait and massage breaks are still placed post-hoc
        for key, cal in self.calendars.items():
            if key.startswith("portrait::"):
                cal.book(_portrait_break, _portrait_break + _break_dur, "__break__")
            elif key.startswith("massage::"):
                cal.book(_massage_break,  _massage_break  + _break_dur, "__break__")

        # Validate pre-booked glam artist breaks: remove if nothing follows
        seen: set[str] = set()
        for key, cal in self.calendars.items():
            if key.startswith("portrait::") or key.startswith("massage::"):
                continue
            aname = cal.name
            if aname in seen:
                continue
            seen.add(aname)

            all_keys = [k for k in self.calendars
                        if not k.startswith("portrait::") and not k.startswith("massage::")
                        and self.calendars[k].name == aname]

            for k in all_keys:
                break_entries = [(s, e) for s, e, m in self.calendars[k].slots if m == "__break__"]
                model_entries = [(s, e) for s, e, m in self.calendars[k].slots if m != "__break__"]
                for bs, be in break_entries:
                    has_after = any(s >= be for s, e in model_entries)
                    if not has_after:
                        # End-of-day break — remove it from all artist calendars
                        for k2 in all_keys:
                            self.calendars[k2].slots = [
                                slot for slot in self.calendars[k2].slots
                                if not (slot[2] == "__break__" and slot[0] == bs)
                            ]
                break  # only need to inspect one calendar per artist

    def _rescue_unscheduled_makeup(self):
        """
        Second pass: find makeup slots for any model that missed one in the main pass.
        Ignores later/earlier scheduling hint — uses full window from day_start.
        Does NOT move any already-booked appointments.
        """
        day_start = WINDOWS["makeup"][0]
        for entry in self.schedule:
            if "makeup" in entry.get("appointments", {}):
                continue
            model = next((m for m in self.models if m["name"] == entry["model"]), None)
            if not model:
                continue
            assigned_mu = model.get("assigned_makeup_artist", "").strip()
            if not assigned_mu:
                continue
            if not model.get("wants_makeup", True):
                continue
            mu_cal_key = self._resolve_calendar_key("makeup", assigned_mu)
            if not mu_cal_key:
                continue
            mu_dur = _parse_duration_min(model.get("makeup_time_slot", "")) or DURATIONS["makeup"]
            group_key = _group_key(model)
            # Rebuild model_busy from already-scheduled appointments
            model_busy = [(a["start"], a["end"]) for a in entry["appointments"].values()]
            # Try full window from 10am, ignoring hint. Hard 5pm end.
            slot = self._find_slot("makeup", mu_cal_key, day_start, group_key, model_busy, duration_min=mu_dur)
            if slot:
                start, end = slot
                self._book("makeup", mu_cal_key, start, end, entry["model"])
                provider_name = mu_cal_key[mu_cal_key.index("::") + 2:]
                entry["appointments"]["makeup"] = {
                    "provider": provider_name,
                    "start": start,
                    "end": end,
                }
                # Remove the "no available makeup slot" warning if present
                entry["warnings"] = [
                    w for w in entry.get("warnings", [])
                    if "no available makeup slot" not in w.lower()
                ]
                print(f"[RESCUED] {entry['model']}: makeup {fmt_time(start)}–{fmt_time(end)} with {provider_name}")
                # If a portrait was already scheduled before this makeup slot, move it.
                portrait_appt = entry["appointments"].get("portrait")
                if portrait_appt and portrait_appt["start"] < end:
                    old_p_start = portrait_appt["start"]
                    old_p_end   = portrait_appt["end"]
                    p_dur = int((old_p_end - old_p_start).total_seconds() // 60)
                    # Unbook old portrait slot from the calendar
                    old_pk = None
                    for pk in (k for k in self.calendars if k.startswith("portrait::")):
                        self.calendars[pk].slots = [
                            s for s in self.calendars[pk].slots
                            if not (s[0] == old_p_start and s[2] == entry["model"])
                        ]
                        if any(s[0] == old_p_start and s[2] == entry["model"] for s in self.calendars[pk].slots):
                            old_pk = pk
                    # Rebuild model_busy including the new makeup slot
                    new_model_busy = [(a["start"], a["end"]) for a in entry["appointments"].values()]
                    portrait_earliest = max(end, WINDOWS["portrait"][0])
                    for pk in sorted(k for k in self.calendars if k.startswith("portrait::")):
                        p_slot = self._find_slot("portrait", pk, portrait_earliest, group_key, new_model_busy,
                                                 duration_min=p_dur, win_end_override=_make_dt(self._event_date, 19, 0))
                        if p_slot:
                            ps, pe = p_slot
                            self._book("portrait", pk, ps, pe, entry["model"])
                            entry["appointments"]["portrait"] = {
                                "provider": self.calendars[pk].name,
                                "start": ps,
                                "end": pe,
                            }
                            print(f"[RESCUED] {entry['model']}: portrait moved to {fmt_time(ps)}–{fmt_time(pe)}")
                            break
                    else:
                        print(f"[RESCUE WARN] {entry['model']}: could not reschedule portrait after makeup")
            else:
                print(f"[RESCUE FAILED] {entry['model']}: still no makeup slot available with {assigned_mu}")

    def run(self) -> list[dict]:
        """
        Schedule all models.
        Internally schedules most-constrained models first (those with rehearsal
        blackouts have fewer available windows and must be placed before less-
        constrained models grab their only viable slots).
        Results are then sorted back to original sheet order for display.
        """
        def scheduling_priority(indexed_model):
            idx, model = indexed_model
            gk = _group_key(model)
            # group1 and group2 have rehearsal blackouts → schedule first
            if gk in ("group1", "group2", "group3", "hope_ambassador"):
                return 0
            # board members have no blackout but still schedule before ungrouped
            if gk == "board_member":
                return 1
            return 1

        # Reserve break slots before model scheduling so _find_slot skips them
        self._prebook_artist_breaks()

        indexed = list(enumerate(self.models))
        for _, model in sorted(indexed, key=scheduling_priority):
            self.schedule.append(self._schedule_model(model))

        # Restore original sheet order for display
        order = {m["name"]: i for i, m in enumerate(self.models)}
        self.schedule.sort(key=lambda r: order.get(r["model"], 9999))

        # Second pass: rescue any models that missed makeup in the main pass
        self._rescue_unscheduled_makeup()

        self._book_breaks()
        return self.schedule

    def get_provider_schedules(self) -> list[dict]:
        """Return per-provider sorted appointment list for the artist schedule view."""
        by_name: dict[str, dict] = {}
        for key, cal in self.calendars.items():
            name = cal.name
            if name not in by_name:
                # Use the artist's actual role as the service so makeup-only artists
                # aren't mislabeled as "hair" just because hair:: keys are inserted first.
                artist_role = self.artists.get(name, {}).get("role")
                service = artist_role if artist_role else cal.service
                by_name[name] = {"name": name, "service": service, "slots": []}
            seen_break = any(s["is_break"] for s in by_name[name]["slots"])
            for start, end, model in cal.slots:
                is_break = model == "__break__"
                if is_break and seen_break:
                    continue  # deduplicate break for "both" artists
                if is_break:
                    seen_break = True
                by_name[name]["slots"].append({
                    "start": start,
                    "end": end,
                    "model": model,
                    "service": cal.service,
                    "is_break": is_break,
                })
        for data in by_name.values():
            data["slots"].sort(key=lambda x: x["start"])
        return sorted(by_name.values(), key=lambda x: x["name"])


# ─── Capacity diagnostic ─────────────────────────────────────────────────────

def artist_capacity_report(models: list[dict]) -> list[dict]:
    """
    For each glam artist, calculate total minutes needed vs 360-min window.

    IMPORTANT: Artists who do BOTH hair and makeup share the same 11am–5pm
    timeline, so their combined hair + makeup total is checked against 360 min,
    not each service independently.

    Returns a list of artist dicts sorted by overflow descending.
    """
    AVAIL_MIN = 360  # 11am–5pm
    FALLBACK_DUR = 45  # default if no time slot specified

    # artist_name → {hair: [{model, minutes, group}], makeup: [...]}
    by_artist: dict[str, dict] = {}

    for m in models:
        h_artist = m.get("assigned_hair_stylist", "").strip()
        mu_artist = m.get("assigned_makeup_artist", "").strip()

        if h_artist and m.get("wants_hair", True):
            dur = _parse_duration_min(m.get("hair_time_slot", "")) or FALLBACK_DUR
            by_artist.setdefault(h_artist, {"hair": [], "makeup": []})
            by_artist[h_artist]["hair"].append({
                "model": m["name"], "minutes": dur, "group": m.get("group", ""),
            })

        if mu_artist and m.get("wants_makeup", True):
            dur = _parse_duration_min(m.get("makeup_time_slot", "")) or FALLBACK_DUR
            by_artist.setdefault(mu_artist, {"hair": [], "makeup": []})
            by_artist[mu_artist]["makeup"].append({
                "model": m["name"], "minutes": dur, "group": m.get("group", ""),
            })

    results = []
    # Build combined totals for swap suggestions
    combined_totals = {
        a: sum(e["minutes"] for e in d["hair"]) + sum(e["minutes"] for e in d["makeup"])
        for a, d in by_artist.items()
    }

    for artist, data in by_artist.items():
        hair_entries = data["hair"]
        mu_entries   = data["makeup"]
        is_both = bool(hair_entries and mu_entries)

        hair_total = sum(e["minutes"] for e in hair_entries)
        mu_total   = sum(e["minutes"] for e in mu_entries)
        total      = hair_total + mu_total if is_both else (hair_total or mu_total)
        service    = "both" if is_both else ("hair" if hair_entries else "makeup")

        overflow = max(0, total - AVAIL_MIN)

        # Swap suggestions: find specific models to move so overflow is relieved
        suggestions = []
        if overflow > 0:
            # Try moving makeup models first (less disruption), then hair
            candidates = sorted(mu_entries + hair_entries, key=lambda x: x["minutes"])
            for cand in candidates:
                for other_artist, other_total in sorted(combined_totals.items(), key=lambda x: x[1]):
                    if other_artist == artist:
                        continue
                    if other_total + cand["minutes"] <= AVAIL_MIN:
                        suggestions.append({
                            "model": cand["model"],
                            "minutes": cand["minutes"],
                            "move_to": other_artist,
                            "other_artist_current_min": other_total,
                            "other_artist_after_min": other_total + cand["minutes"],
                        })
                        break

        all_models = sorted(
            [dict(e, service="hair") for e in hair_entries] +
            [dict(e, service="makeup") for e in mu_entries],
            key=lambda x: x["minutes"], reverse=True,
        )

        results.append({
            "artist": artist,
            "service": service,
            "hair_total_min": hair_total,
            "makeup_total_min": mu_total,
            "total_min": total,
            "available_min": AVAIL_MIN,
            "overflow_min": overflow,
            "models": all_models,
            "suggestions": suggestions,
        })

    results.sort(key=lambda x: x["overflow_min"], reverse=True)
    return results


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

        h_s,  h_e,  h_p  = appt("hair")
        m_s,  m_e,  m_p  = appt("makeup")
        p_s,  p_e,  _    = appt("portrait")
        ms_s, ms_e, ms_p = appt("massage")

        rows.append({
            "Model":            entry["model"],
            "Group":            entry["group"],
            "Rehearsal":        entry.get("rehearsal_time", ""),
            "Massage Provider": ms_p,
            "Massage Time":     f"{ms_s}–{ms_e}" if ms_s else "",
            "Hair Stylist":     h_p or entry.get("hair_stylist", ""),
            "Hair Time":        f"{h_s}–{h_e}" if h_s else "",
            "Makeup Artist":    m_p or entry.get("makeup_artist", ""),
            "Makeup Time":      f"{m_s}–{m_e}" if m_s else "",
            "Portrait":         f"{p_s}–{p_e}" if p_s else "",
            "Warnings":         entry.get("warnings", []),
        })
    return rows
