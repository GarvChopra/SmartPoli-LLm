"""SmartPoli Care Companion: medication schedule, reminders, activity log and caregiver records.

SQLite-backed (data/care.db). Everything is SYNTHETIC demo data (fictional user, fictional medicines,
demonstration phone numbers). The store never gives dosing advice: it only records what the user says
they did (taken / snoozed / missed) and when reminders are due.

Reminder policy (deliberately conservative):
- A dose slot (medication + calendar day) is DUE once its scheduled time has passed and it has not been
  marked taken or missed, and it is not snoozed.
- A due slot is surfaced at most MAX_NOTIFICATIONS times, at least RENOTIFY_MINUTES apart. After that it
  stays "pending" on the schedule. It is NEVER silently marked missed: only the user (or a request that
  came from the user) can log a missed dose.
"""
from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

MAX_NOTIFICATIONS = 3
RENOTIFY_MINUTES = 10
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

SEED_USER = {"name": "Alex Demo", "note": "Fictional demo profile. No real patient data."}
SEED_MEDS = [
    ("Examplol", "10 mg tablet", "08:00"),
    ("Demostatin", "20 mg tablet", "13:00"),
    ("Sampleamine", "5 mg capsule", "20:00"),
]
SEED_CAREGIVER = {
    "name": "Rohan (demo son)",
    "relationship": "son",
    "phone": "555-0142",  # demonstration number only
    "authorized": 1,
    "consent_note": "Alex Demo authorized Rohan to be contacted if Alex asks for help (synthetic consent record).",
    "share": "minimal",  # only "your family member asked you to check on them"; no medical details
}


class CareError(ValueError):
    """A request the store refuses (bad time, unknown medicine, ...). The message is safe to show the user."""


def parse_hhmm(value: str) -> str:
    m = _HHMM.match((value or "").strip())
    if not m:
        raise CareError("Please give the time as HH:MM, for example 20:30.")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


class CareStore:
    def __init__(self, path: Path | str = ":memory:", clock: Callable[[], float] = time.time, seed: bool = True):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.clock = clock
        self._create()
        if seed:
            self._seed()

    # ---------------------------------------------------------------- setup
    def _create(self) -> None:
        with self._lock:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS medications(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, dose_label TEXT NOT NULL,
                    time_hhmm TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS dose_slots(
                    med_id INTEGER NOT NULL, day TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    snooze_until REAL, notified INTEGER NOT NULL DEFAULT 0, last_notified REAL, updated REAL,
                    PRIMARY KEY(med_id, day));
                CREATE TABLE IF NOT EXISTS activity(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS caregiver(
                    id INTEGER PRIMARY KEY CHECK (id = 1), name TEXT, relationship TEXT, phone TEXT,
                    authorized INTEGER NOT NULL DEFAULT 0, consent_note TEXT, share TEXT);
                CREATE TABLE IF NOT EXISTS contact_requests(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, reason TEXT NOT NULL,
                    status TEXT NOT NULL, provider TEXT, provider_ref TEXT, result TEXT, updated REAL);
                CREATE TABLE IF NOT EXISTS reminder_proposals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, name TEXT NOT NULL,
                    dose_label TEXT NOT NULL, time_hhmm TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    med_id INTEGER, updated REAL);
                """
            )
            self._db.commit()

    def _seed(self) -> None:
        with self._lock:
            if self._db.execute("SELECT COUNT(*) FROM medications").fetchone()[0] == 0:
                self._db.executemany(
                    "INSERT INTO medications(name, dose_label, time_hhmm) VALUES (?,?,?)", SEED_MEDS)
            if self._db.execute("SELECT COUNT(*) FROM caregiver").fetchone()[0] == 0:
                c = SEED_CAREGIVER
                self._db.execute(
                    "INSERT INTO caregiver(id,name,relationship,phone,authorized,consent_note,share) VALUES (1,?,?,?,?,?,?)",
                    (c["name"], c["relationship"], c["phone"], c["authorized"], c["consent_note"], c["share"]))
            self._db.commit()

    # --------------------------------------------------------------- helpers
    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock())

    def _today(self) -> str:
        return self._now().strftime("%Y-%m-%d")

    def _log(self, kind: str, detail: str) -> None:
        self._db.execute("INSERT INTO activity(ts, kind, detail) VALUES (?,?,?)", (self.clock(), kind, detail))

    def log_smart_action(self, kind: str, detail: str) -> None:
        with self._lock:
            self._log(kind, detail)
            self._db.commit()

    def find_medication(self, name: str | int | None) -> sqlite3.Row:
        """Resolve a medicine by id or (partial, case-insensitive) name; refuse ambiguity."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM medications WHERE active = 1 ORDER BY time_hhmm").fetchall()
            if name in (None, ""):
                raise CareError("Which medicine do you mean?")
            if isinstance(name, int) or str(name).isdigit():
                for r in rows:
                    if r["id"] == int(name):
                        return r
            needle = str(name).strip().lower()
            exact = [r for r in rows if r["name"].lower() == needle]
            hits = exact or [r for r in rows if needle in r["name"].lower() or r["name"].lower() in needle]
            if not hits:
                raise CareError(f"I couldn't find a medicine called '{name}' on your schedule.")
            if len(hits) > 1:
                raise CareError("That matches more than one medicine: " + ", ".join(r["name"] for r in hits) + ".")
            return hits[0]

    def _slot(self, med_id: int, day: str | None = None) -> sqlite3.Row:
        day = day or self._today()
        self._db.execute("INSERT OR IGNORE INTO dose_slots(med_id, day) VALUES (?,?)", (med_id, day))
        return self._db.execute("SELECT * FROM dose_slots WHERE med_id=? AND day=?", (med_id, day)).fetchone()

    def _scheduled_at(self, time_hhmm: str, day: str | None = None) -> float:
        d = datetime.strptime(day or self._today(), "%Y-%m-%d")
        h, m = (int(x) for x in time_hhmm.split(":"))
        return d.replace(hour=h, minute=m).timestamp()

    # --------------------------------------------------------------- queries
    def profile(self) -> dict:
        return dict(SEED_USER)

    def schedule(self) -> list[dict]:
        """Today's schedule with live status: upcoming | due | snoozed | taken | missed."""
        now = self.clock()
        out = []
        with self._lock:
            for med in self._db.execute("SELECT * FROM medications WHERE active = 1 ORDER BY time_hhmm"):
                slot = self._slot(med["id"])
                at = self._scheduled_at(med["time_hhmm"])
                status = slot["status"]
                if status == "pending":
                    if slot["snooze_until"] and slot["snooze_until"] > now:
                        status = "snoozed"
                    elif at <= now:
                        status = "due"
                    else:
                        status = "upcoming"
                out.append({
                    "id": med["id"], "name": med["name"], "dose_label": med["dose_label"],
                    "time": med["time_hhmm"], "status": status,
                    "snoozed_until": datetime.fromtimestamp(slot["snooze_until"]).strftime("%H:%M")
                    if status == "snoozed" else None,
                })
            self._db.commit()
        return out

    def next_dose(self) -> dict | None:
        """The earliest dose still ahead (or currently due) today, else tomorrow's first."""
        sched = [s for s in self.schedule() if s["status"] in ("due", "snoozed", "upcoming")]
        if sched:
            return sched[0]
        with self._lock:
            first = self._db.execute(
                "SELECT * FROM medications WHERE active=1 ORDER BY time_hhmm LIMIT 1").fetchone()
        if first is None:
            return None
        return {"id": first["id"], "name": first["name"], "dose_label": first["dose_label"],
                "time": first["time_hhmm"], "status": "tomorrow"}

    def due_reminders(self) -> list[dict]:
        """Slots whose reminder should be shown now. Calling this counts as showing them."""
        now = self.clock()
        out = []
        with self._lock:
            for med in self._db.execute("SELECT * FROM medications WHERE active = 1 ORDER BY time_hhmm"):
                slot = self._slot(med["id"])
                if slot["status"] != "pending":
                    continue
                if self._scheduled_at(med["time_hhmm"]) > now:
                    continue
                if slot["snooze_until"] and slot["snooze_until"] > now:
                    continue
                if slot["notified"] >= MAX_NOTIFICATIONS:
                    continue
                last = slot["last_notified"]
                if last and now - last < RENOTIFY_MINUTES * 60:
                    continue
                self._db.execute(
                    "UPDATE dose_slots SET notified = notified + 1, last_notified = ?, snooze_until = NULL "
                    "WHERE med_id=? AND day=?", (now, med["id"], slot["day"]))
                self._log("reminder", f"Reminder shown for {med['name']} ({med['time_hhmm']})")
                out.append({"id": med["id"], "name": med["name"], "dose_label": med["dose_label"],
                            "time": med["time_hhmm"], "attempt": slot["notified"] + 1})
            self._db.commit()
        return out

    def activity(self, limit: int = 15) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM activity ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"time": datetime.fromtimestamp(r["ts"]).strftime("%a %H:%M"), "kind": r["kind"],
                 "detail": r["detail"]} for r in rows]

    # --------------------------------------------------------------- actions
    def mark_taken(self, medication) -> dict:
        with self._lock:
            med = self.find_medication(medication)
            slot = self._slot(med["id"])
            if slot["status"] == "taken":
                return {"ok": True, "already": True, "medication": med["name"],
                        "message": f"{med['name']} was already marked as taken today."}
            self._db.execute("UPDATE dose_slots SET status='taken', snooze_until=NULL, updated=? WHERE med_id=? AND day=?",
                             (self.clock(), med["id"], slot["day"]))
            self._log("taken", f"{med['name']} marked as taken (scheduled {med['time_hhmm']})")
            self._db.commit()
        return {"ok": True, "already": False, "medication": med["name"],
                "message": f"Recorded: {med['name']} taken."}

    def snooze(self, medication, minutes: int) -> dict:
        if not isinstance(minutes, int) or not 1 <= minutes <= 180:
            raise CareError("I can snooze a reminder for 1 to 180 minutes.")
        with self._lock:
            med = self.find_medication(medication)
            slot = self._slot(med["id"])
            if slot["status"] != "pending":
                raise CareError(f"{med['name']} is already marked as {slot['status']} today, so there is nothing to snooze.")
            until = self.clock() + minutes * 60
            self._db.execute("UPDATE dose_slots SET snooze_until=?, updated=? WHERE med_id=? AND day=?",
                             (until, self.clock(), med["id"], slot["day"]))
            self._log("snoozed", f"{med['name']} reminder snoozed {minutes} min")
            self._db.commit()
        return {"ok": True, "medication": med["name"], "minutes": minutes,
                "until": datetime.fromtimestamp(until).strftime("%H:%M"),
                "message": f"Okay, I'll remind you about {med['name']} again at {datetime.fromtimestamp(until).strftime('%H:%M')}."}

    def log_missed(self, medication) -> dict:
        """Records that the user says they missed a dose. Gives NO advice about what to do next."""
        with self._lock:
            med = self.find_medication(medication)
            slot = self._slot(med["id"])
            if slot["status"] == "taken":
                raise CareError(f"{med['name']} is already marked as taken today.")
            self._db.execute("UPDATE dose_slots SET status='missed', snooze_until=NULL, updated=? WHERE med_id=? AND day=?",
                             (self.clock(), med["id"], slot["day"]))
            self._log("missed", f"{med['name']} dose logged as missed (scheduled {med['time_hhmm']})")
            self._db.commit()
        return {"ok": True, "medication": med["name"], "message": f"Logged: missed dose of {med['name']}."}

    def add_medication(self, name: str, time_hhmm: str, dose_label: str = "") -> dict:
        name = (name or "").strip()
        if not 2 <= len(name) <= 40 or not re.fullmatch(r"[A-Za-z0-9 .\-']+", name):
            raise CareError("Please give a short medicine name (letters and numbers).")
        t = parse_hhmm(time_hhmm)
        with self._lock:
            cur = self._db.execute("INSERT INTO medications(name, dose_label, time_hhmm) VALUES (?,?,?)",
                                   (name, (dose_label or "as prescribed")[:40], t))
            self._log("added", f"Added reminder for {name} at {t}")
            self._db.commit()
        return {"ok": True, "id": cur.lastrowid, "name": name, "time": t,
                "dose_label": (dose_label or "as prescribed")[:40],
                "message": f"Added a reminder for {name} at {t}."}

    def propose_medication(self, name: str, time_hhmm: str, dose_label: str = "") -> dict:
        """Propose a new medication reminder that awaits explicit user confirmation before being added."""
        name = (name or "").strip()
        if not 2 <= len(name) <= 40 or not re.fullmatch(r"[A-Za-z0-9 .\-']+", name):
            raise CareError("Please give a short medicine name (letters and numbers).")
        t = parse_hhmm(time_hhmm)
        dl = (dose_label or "as prescribed").strip()[:40]
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO reminder_proposals(created, name, dose_label, time_hhmm, status, updated) "
                "VALUES (?,?,?,?, 'pending', ?)",
                (self.clock(), name, dl, t, self.clock()),
            )
            self._log("reminder_proposed", f"Proposed reminder for {name} at {t} (awaiting confirmation)")
            self._db.commit()
        return {"ok": True, "id": cur.lastrowid, "name": name, "time": t, "dose_label": dl, "status": "pending"}

    def get_medication_proposal(self, proposal_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM reminder_proposals WHERE id=?", (proposal_id,)).fetchone()
        return dict(r) if r else None

    def confirm_medication_proposal(self, proposal_id: int) -> dict:
        with self._lock:
            prop = self.get_medication_proposal(proposal_id)
            if not prop:
                raise CareError("Unknown reminder proposal.")
            if prop["status"] != "pending":
                raise CareError(f"This reminder proposal is already {prop['status']}.")
            added = self.add_medication(prop["name"], prop["time_hhmm"], prop["dose_label"])
            self._db.execute(
                "UPDATE reminder_proposals SET status='confirmed', med_id=?, updated=? WHERE id=?",
                (added["id"], self.clock(), proposal_id),
            )
            self._db.commit()
        return {
            "ok": True,
            "proposal_id": proposal_id,
            "medication_id": added["id"],
            "name": prop["name"],
            "time": prop["time_hhmm"],
            "dose_label": prop["dose_label"],
            "status": "confirmed",
            "message": added["message"],
        }

    def cancel_medication_proposal(self, proposal_id: int) -> dict:
        with self._lock:
            prop = self.get_medication_proposal(proposal_id)
            if not prop:
                raise CareError("Unknown reminder proposal.")
            if prop["status"] != "pending":
                raise CareError(f"This reminder proposal is already {prop['status']}.")
            self._db.execute(
                "UPDATE reminder_proposals SET status='cancelled', updated=? WHERE id=?",
                (self.clock(), proposal_id),
            )
            self._log("reminder_cancelled", f"Cancelled proposed reminder for {prop['name']} at {prop['time_hhmm']}")
            self._db.commit()
        return {"ok": True, "proposal_id": proposal_id, "status": "cancelled"}

    def medication_proposals(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM reminder_proposals ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def set_reminder_time(self, medication, time_hhmm: str) -> dict:
        """Changes WHEN the reminder fires. It does not change the medicine, the dose or how often it is taken."""
        t = parse_hhmm(time_hhmm)
        with self._lock:
            med = self.find_medication(medication)
            self._db.execute("UPDATE medications SET time_hhmm=? WHERE id=?", (t, med["id"]))
            self._db.execute("DELETE FROM dose_slots WHERE med_id=? AND day=? AND status='pending'",
                             (med["id"], self._today()))
            self._log("reminder_time", f"{med['name']} reminder moved {med['time_hhmm']} -> {t}")
            self._db.commit()
        return {"ok": True, "medication": med["name"], "time": t,
                "message": f"The {med['name']} reminder is now at {t}. (Only the reminder time changed.)"}

    # ------------------------------------------------------------- caregiver
    def caregiver(self) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM caregiver WHERE id = 1").fetchone()
        return dict(r) if r else None

    def set_caregiver_authorization(self, authorized: bool) -> None:
        with self._lock:
            self._db.execute("UPDATE caregiver SET authorized=? WHERE id=1", (1 if authorized else 0,))
            self._log("consent", f"Caregiver authorization set to {bool(authorized)}")
            self._db.commit()

    def create_contact_request(self, reason: str) -> dict:
        """A PROPOSAL only. Nothing is contacted until confirm_contact_request() is called by the user."""
        cg = self.caregiver()
        if not cg or not cg["authorized"]:
            raise CareError("There is no authorized caregiver on this profile, so I can't contact anyone for you.")
        reason = re.sub(r"\s+", " ", (reason or "").strip())[:200] or "asked for help"
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO contact_requests(created, reason, status, updated) VALUES (?,?, 'pending', ?)",
                (self.clock(), reason, self.clock()))
            self._log("caregiver_proposed", f"Proposed contacting {cg['name']} (awaiting user confirmation)")
            self._db.commit()
        return {"id": cur.lastrowid, "status": "pending", "caregiver": cg["name"], "relationship": cg["relationship"],
                "reason": reason}

    def get_contact_request(self, request_id: int) -> dict | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM contact_requests WHERE id=?", (request_id,)).fetchone()
        return dict(r) if r else None

    def update_contact_request(self, request_id: int, status: str, provider: str | None = None,
                               provider_ref: str | None = None, result: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE contact_requests SET status=?, provider=COALESCE(?,provider), provider_ref=COALESCE(?,provider_ref), "
                "result=COALESCE(?,result), updated=? WHERE id=?",
                (status, provider, provider_ref, result, self.clock(), request_id))
            self._log("caregiver_" + status, f"Caregiver contact request #{request_id}: {status}"
                      + (f" via {provider}" if provider else ""))
            self._db.commit()

    def contact_requests(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM contact_requests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
