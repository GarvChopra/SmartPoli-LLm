"""SmartPoli Care Companion: Smart Actions & Proactive Care system.

Provides a strictly allowlisted, validated desktop action engine so the AI companion can:
1. Open verified, trusted healthcare & support resources (NIH/NHLBI, MedlinePlus, AHA, American Lung Association)
   in the user's browser — never unverified or arbitrary URLs.
2. Navigate directly to relevant SmartPoli Care Companion screens (schedule, reminders, troubleshooting,
   caregiver, activity, chat).
3. Open OS notification settings (`ms-settings:notifications` on Windows) with safe fallback instructions.
4. Display step-by-step instructional cards for devices (inhalers, blood pressure cuffs), sleep hygiene,
   and reminder troubleshooting.

No arbitrary shell execution, file access, or unverified URLs are ever permitted.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import webbrowser
from typing import Callable

from r2.care_meds import CareError, CareStore

VALID_SCREENS = ("chat", "schedule", "reminders", "troubleshooting", "caregiver", "activity")
_SCREEN_ALIASES = {
    "meds": "schedule",
    "medication": "schedule",
    "medications": "schedule",
    "medicine": "schedule",
    "medicines": "schedule",
    "pill": "schedule",
    "pills": "schedule",
    "alarm": "reminders",
    "alarms": "reminders",
    "reminder": "reminders",
    "notification": "troubleshooting",
    "notifications": "troubleshooting",
    "settings": "troubleshooting",
    "troubleshoot": "troubleshooting",
    "help": "troubleshooting",
    "support": "troubleshooting",
    "family": "caregiver",
    "son": "caregiver",
    "contact": "caregiver",
    "history": "activity",
    "log": "activity",
    "logs": "activity",
    "home": "chat",
    "conversation": "chat",
}

TRUSTED_RESOURCES: list[dict] = [
    {
        "id": "inhaler_mdi",
        "title": "How to Use a Metered-Dose Inhaler (MDI)",
        "category": "inhaler",
        "device_type": "metered-dose inhaler (puffer / spray)",
        "source": "NIH — National Heart, Lung, and Blood Institute (NHLBI)",
        "kind": "video_and_guide",
        "url": "https://www.nhlbi.nih.gov/resources/how-use-metered-dose-inhaler",
        "summary": "Official NHLBI step-by-step guide and demonstration for standard puff/spray inhalers (with or without a spacer).",
        "steps": [
            "Remove the cap and shake the inhaler well for 5 seconds.",
            "Breathe out slowly and completely, away from the inhaler.",
            "Hold the inhaler upright, seal your lips around the mouthpiece (or spacer), and start breathing in slowly through your mouth.",
            "Press down on the canister once while breathing in slowly and deeply over 3 to 5 seconds.",
            "Hold your breath for up to 10 seconds so the medicine settles in your lungs, then breathe out gently.",
            "Check your prescription label or ask your pharmacist whether to rinse your mouth with water afterward.",
        ],
        "keywords": ["inhaler", "mdi", "puffer", "spray", "spacer", "albuterol", "puff", "canister", "asthma", "copd"],
    },
    {
        "id": "inhaler_dpi",
        "title": "How to Use a Dry Powder Inhaler (Diskus / Turbuhaler)",
        "category": "inhaler",
        "device_type": "dry powder inhaler (Diskus, Ellipta, Turbuhaler, capsule)",
        "source": "NIH — MedlinePlus Medical Encyclopedia",
        "kind": "guide",
        "url": "https://medlineplus.gov/ency/patientinstructions/000041.htm",
        "summary": "Official NIH MedlinePlus instructions for breath-activated dry powder inhalers (do not shake; breathe in quickly and deeply).",
        "steps": [
            "Open the device and load a dose according to your device's lever or twist mechanism. Do not shake a dry powder inhaler.",
            "Turn your head away from the inhaler and breathe out completely. Never blow into a dry powder inhaler.",
            "Seal your lips tightly around the mouthpiece.",
            "Breathe in quickly and deeply through your mouth (a strong, fast breath draws the powder in).",
            "Remove the inhaler and hold your breath for up to 10 seconds, then breathe out slowly.",
            "Close the device and keep it dry. Rinse your mouth and spit out the water if your leaflet recommends it.",
        ],
        "keywords": ["inhaler", "dry powder", "dpi", "diskus", "turbuhaler", "ellipta", "powder", "capsule", "handihaler"],
    },
    {
        "id": "inhaler_soft_mist",
        "title": "How to Use a Soft Mist Inhaler (Respimat)",
        "category": "inhaler",
        "device_type": "soft mist inhaler (Respimat)",
        "source": "American Lung Association",
        "kind": "video_and_guide",
        "url": "https://www.lung.org/lung-health-diseases/lung-disease-lookup/copd/treating/copd-medications/how-to-use-a-soft-mist-inhaler",
        "summary": "Official American Lung Association video and step-by-step instructions for soft mist inhalers.",
        "steps": [
            "Keep the cap closed and turn the clear base in the direction of the arrows until it clicks (half a turn).",
            "Flip the cap fully open until it snaps into place.",
            "Breathe out slowly and completely away from the inhaler.",
            "Close your lips around the mouthpiece without covering the side air vents.",
            "While taking a slow, deep breath through your mouth, press the dose-release button and keep breathing in slowly.",
            "Hold your breath for 10 seconds or as long as comfortable, then close the cap.",
        ],
        "keywords": ["inhaler", "soft mist", "respimat", "mist"],
    },
    {
        "id": "inhaler_general",
        "title": "Official Inhaler Technique Video Library (All Device Types)",
        "category": "inhaler",
        "device_type": "all inhaler types",
        "source": "American Lung Association",
        "kind": "video_and_guide",
        "url": "https://www.lung.org/lung-health-diseases/lung-disease-lookup/asthma/treatment/omalizumab-and-other-biologics/how-to-use-your-inhaler",
        "summary": "Verified instructional videos for metered-dose, dry powder, soft mist, and nebulizer devices.",
        "steps": [
            "Check your inhaler label to see whether it is a metered-dose puffer (spray), a dry powder inhaler (Diskus/Ellipta), or a soft mist inhaler.",
            "Puffers require a slow, steady breath while pressing the canister; dry powder inhalers require a quick, deep breath after loading a dose.",
            "Watch the matching demonstration video for your exact device or ask your pharmacist to check your technique in person.",
        ],
        "keywords": ["inhaler", "use my inhaler", "breathing device", "nebulizer", "how to use inhaler"],
    },
    {
        "id": "sleep_hygiene",
        "title": "A Good Night's Sleep: Healthy Sleep Hygiene Guide",
        "category": "sleep",
        "device_type": "",
        "source": "NIH — National Institute on Aging",
        "kind": "guide",
        "url": "https://www.nia.nih.gov/health/sleep/good-nights-sleep",
        "summary": "Evidence-based NIH sleep-hygiene habits for falling asleep and staying rested without changing medications.",
        "steps": [
            "Follow a regular sleep schedule: go to bed and wake up at the same time every day, even on weekends.",
            "Create a calm wind-down routine 30 to 60 minutes before bed (dim lights, quiet reading, gentle stretching).",
            "Keep your bedroom cool, dark, and quiet, and avoid bright screens (computer, phone, TV) right before sleep.",
            "Avoid caffeine late in the day, large meals right before bedtime, and long naps in the late afternoon.",
            "If you can't fall asleep after 20 minutes, get up and do something quiet in dim light until you feel sleepy.",
        ],
        "keywords": ["sleep", "insomnia", "can't sleep", "cannot sleep", "trouble sleeping", "awake", "bedtime", "tired", "rest", "nap"],
    },
    {
        "id": "blood_pressure",
        "title": "How to Accurately Check Your Blood Pressure at Home",
        "category": "blood_pressure",
        "device_type": "home upper-arm blood pressure monitor",
        "source": "American Heart Association (AHA)",
        "kind": "video_and_guide",
        "url": "https://www.heart.org/en/health-topics/high-blood-pressure/understanding-blood-pressure-readings/monitoring-your-blood-pressure-at-home",
        "summary": "Official American Heart Association instructional video and checklist for accurate home blood pressure readings.",
        "steps": [
            "Avoid caffeine, smoking, or exercise for 30 minutes before measuring, and empty your bladder first.",
            "Sit quietly for at least 5 minutes with your back supported and feet flat on the floor (legs uncrossed).",
            "Rest your arm on a flat surface so the middle of the cuff is at heart level.",
            "Place the cuff directly on bare skin above the bend of the elbow (not over clothing).",
            "Stay still and quiet while the cuff inflates and deflates; take 2 readings 1 minute apart and record both.",
        ],
        "keywords": ["blood pressure", "bp", "cuff", "monitor", "check my blood pressure", "measure blood pressure", "systolic", "diastolic", "hypertension"],
    },
    {
        "id": "medication_safety",
        "title": "Safe Medication Use & Organising Your Schedule",
        "category": "medication_safety",
        "device_type": "",
        "source": "NIH — MedlinePlus",
        "kind": "guide",
        "url": "https://medlineplus.gov/safeuseofmedicines.html",
        "summary": "Official MedlinePlus guidance on reading medicine labels, tracking doses, and talking with your pharmacist.",
        "steps": [
            "Keep an up-to-date list of your medicines and scheduled times.",
            "Always read the official patient leaflet that comes with your prescription.",
            "If you miss a dose or aren't sure how to take a medicine, ask your pharmacist or prescriber before making any change.",
        ],
        "keywords": ["safe medication", "leaflet", "pharmacist", "pill organizer", "medicine guide", "label"],
    },
    {
        "id": "smartpoli_notifications",
        "title": "SmartPoli Reminder & Notification Troubleshooting",
        "category": "troubleshooting",
        "device_type": "desktop & phone notifications",
        "source": "SmartPoli Care Companion Built-in Guide",
        "kind": "internal",
        "url": "https://support.microsoft.com/en-us/windows/change-notification-settings-in-windows-8942c744-6198-fe56-4639-34320cf9444e",
        "summary": "Checklist to restore missing or silent SmartPoli medication reminders (Notifications permission, Do Not Disturb / Focus Assist, and Time Zone).",
        "steps": [
            "Step 1 — Notifications permission: Make sure notifications are turned ON for SmartPoli in system settings.",
            "Step 2 — Do Not Disturb / Focus Assist: Turn off Do Not Disturb (or add SmartPoli as a priority exception).",
            "Step 3 — Time Zone match: Confirm your computer or phone time zone matches the time zone on your SmartPoli schedule.",
        ],
        "keywords": ["reminder isn't working", "reminder not working", "missing reminder", "didn't arrive", "notification", "silent", "do not disturb", "focus assist", "time zone"],
    },
]

_RESOURCE_BY_ID = {r["id"]: r for r in TRUSTED_RESOURCES}
_ALLOWED_URLS = {r["url"] for r in TRUSTED_RESOURCES}


def validate_screen(screen: str) -> str:
    s = re.sub(r"[^a-z_]", "", (screen or "").strip().lower())
    if s in VALID_SCREENS:
        return s
    if s in _SCREEN_ALIASES:
        return _SCREEN_ALIASES[s]
    raise CareError(f"Unknown screen '{screen}'. Valid screens are: {', '.join(VALID_SCREENS)}.")


def find_trusted_resource(topic: str, device_type: str = "") -> dict:
    """Resolve a trusted resource from the allowlist. Never accepts arbitrary URLs."""
    raw_topic = (topic or "").strip().lower()
    raw_dev = (device_type or "").strip().lower()
    if not raw_topic and not raw_dev:
        raise CareError("Please specify a resource topic (e.g. inhaler, sleep_hygiene, blood_pressure, smartpoli_notifications).")

    if raw_topic in _RESOURCE_BY_ID:
        # If generic inhaler_general or "inhaler" was passed with a specific device_type, refine it
        if raw_topic in ("inhaler", "inhaler_general") and raw_dev:
            pass
        else:
            return dict(_RESOURCE_BY_ID[raw_topic])

    combined = f"{raw_topic} {raw_dev}".strip()
    # Inhaler subtype matching
    if "inhaler" in combined or "puffer" in combined or "diskus" in combined or "respimat" in combined or "turbuhaler" in combined:
        if any(k in combined for k in ("dry powder", "dpi", "diskus", "turbuhaler", "ellipta", "powder", "capsule", "handihaler")):
            return dict(_RESOURCE_BY_ID["inhaler_dpi"])
        if any(k in combined for k in ("soft mist", "respimat", "mist")):
            return dict(_RESOURCE_BY_ID["inhaler_soft_mist"])
        if any(k in combined for k in ("mdi", "metered", "puffer", "spray", "spacer", "canister", "puff", "albuterol")):
            return dict(_RESOURCE_BY_ID["inhaler_mdi"])
        return dict(_RESOURCE_BY_ID["inhaler_general"])

    best_score = 0
    best_res = None
    words = set(re.findall(r"[a-z0-9']+", combined))
    for res in TRUSTED_RESOURCES:
        score = 0
        if res["category"] in combined or res["id"] in combined:
            score += 5
        for kw in res["keywords"]:
            if kw in combined:
                score += 4
            else:
                kw_words = set(re.findall(r"[a-z0-9']+", kw))
                overlap = len(words & kw_words)
                if overlap:
                    score += overlap
        if score > best_score:
            best_score = score
            best_res = res

    if not best_res or best_score == 0:
        raise CareError(
            f"No verified resource found for '{topic}'. Available verified topics: "
            "inhaler_mdi, inhaler_dpi, inhaler_soft_mist, inhaler_general, sleep_hygiene, blood_pressure, "
            "medication_safety, smartpoli_notifications."
        )
    return dict(best_res)


def _default_browser_opener(url: str) -> bool:
    """Opens only allowlisted HTTPS URLs in the user's browser without blocking the caller."""
    if url not in _ALLOWED_URLS or not url.startswith("https://"):
        return False
    if os.environ.get("CARE_DISABLE_BROWSER_OPEN") == "1" or "pytest" in sys.modules:
        return False
    try:
        threading.Thread(target=lambda: webbrowser.open(url, new=2), name="smartpoli-browser", daemon=True).start()
        return True
    except Exception:
        return False


def _default_settings_opener() -> bool:
    """Opens Windows Notification Settings (`ms-settings:notifications`) without blocking the caller."""
    if os.environ.get("CARE_DISABLE_BROWSER_OPEN") == "1" or "pytest" in sys.modules:
        return False
    try:
        if sys.platform.startswith("win") and hasattr(os, "startfile"):
            threading.Thread(
                target=lambda: os.startfile("ms-settings:notifications"),  # noqa: S606
                name="smartpoli-settings",
                daemon=True,
            ).start()
            return True
    except Exception:
        pass
    return False


class SmartActionRunner:
    """Executes validated, safe desktop actions and logs them in CareStore."""

    def __init__(
        self,
        store: CareStore,
        browser_opener: Callable[[str], bool] | None = None,
        settings_opener: Callable[[], bool] | None = None,
    ):
        self.store = store
        self.browser_opener = browser_opener or _default_browser_opener
        self.settings_opener = settings_opener or _default_settings_opener

    def list_resources(self) -> list[dict]:
        return [dict(r) for r in TRUSTED_RESOURCES]

    def open_trusted_resource(self, topic: str, device_type: str = "", open_in_browser: bool = True) -> dict:
        res = find_trusted_resource(topic, device_type)
        url = res["url"]
        if url not in _ALLOWED_URLS:
            raise CareError("Refused to open a URL outside the verified SmartPoli resource catalog.")
        opened = False
        if open_in_browser:
            opened = bool(self.browser_opener(url))
        self.store.log_smart_action(
            "resource_opened" if opened else "resource_shared",
            f"{'Opened' if opened else 'Shared'} verified resource: {res['title']} ({res['source']})",
        )
        return {
            "ok": True,
            "resource": res,
            "opened_in_browser": opened,
            "message": (
                f"{'Opened' if opened else 'Prepared'} verified resource '{res['title']}' from {res['source']} "
                f"({url}). Key steps: " + " ".join(f"{i+1}) {s}" for i, s in enumerate(res["steps"][:4]))
            ),
        }

    def show_instructions(self, topic: str, device_type: str = "") -> dict:
        res = find_trusted_resource(topic, device_type)
        self.store.log_smart_action("instructions_shown", f"Displayed instructions: {res['title']}")
        return {
            "ok": True,
            "resource": res,
            "message": f"Displayed step-by-step instructions for '{res['title']}' ({res['source']}): "
            + " ".join(f"{i+1}) {s}" for i, s in enumerate(res["steps"])),
        }

    def navigate_screen(self, screen: str, reason: str = "") -> dict:
        target = validate_screen(screen)
        clean_reason = re.sub(r"\s+", " ", (reason or "").strip())[:120]
        self.store.log_smart_action(
            "screen_navigated",
            f"Navigated companion to '{target}' screen" + (f" ({clean_reason})" if clean_reason else ""),
        )
        return {
            "ok": True,
            "screen": target,
            "reason": clean_reason,
            "message": f"Switched the SmartPoli Care Companion view to the '{target}' screen.",
        }

    def open_notification_settings(self) -> dict:
        opened = bool(self.settings_opener())
        self.store.log_smart_action(
            "settings_opened",
            "Opened system notification settings (ms-settings:notifications)"
            if opened
            else "Provided system notification settings checklist (ms-settings:notifications)",
        )
        res = _RESOURCE_BY_ID["smartpoli_notifications"]
        return {
            "ok": True,
            "opened_system_settings": opened,
            "settings_uri": "ms-settings:notifications",
            "steps": list(res["steps"]),
            "message": (
                "Opened Windows Notification Settings (ms-settings:notifications) and navigated to troubleshooting."
                if opened
                else "Prepared Windows Notification Settings (ms-settings:notifications) and troubleshooting checklist."
            ),
        }
