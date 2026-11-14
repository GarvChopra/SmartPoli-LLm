#!/usr/bin/env python3
"""Build voice dialogue clips, combined VO track, subtle UI SFX mix, and timeline.json for SmartPoli Care Navigator."""
import json
import os
import subprocess
import tempfile
import wave
import numpy as np

SR = 48000
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROJ = os.path.dirname(os.path.abspath(__file__))
SFX_DIR = os.path.join(ROOT, "launch-video", "assets", "sfx")
VO_DIR = os.path.join(PROJ, "vo")
CLIPS_DIR = os.path.join(VO_DIR, "clips")
OUT_DIR = os.path.join(PROJ, "out")

DAVID = "Microsoft David Desktop"
ZIRA = "Microsoft Zira Desktop"


def _ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def read_audio(path, ch=1):
    raw = subprocess.run(
        [_ffmpeg(), "-v", "error", "-i", path, "-f", "f32le", "-ac", str(ch), "-ar", str(SR), "-"],
        check=True,
        capture_output=True,
    ).stdout
    arr = np.frombuffer(raw, np.float32).astype(np.float64)
    return arr.reshape(-1, ch) if ch > 1 else arr


def write_wav(path, x, sr=SR):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    x = np.asarray(x, dtype=np.float64)
    if x.ndim == 1:
        x = x[:, None]
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def rms_db(x, win):
    n = len(x) // win
    if n == 0:
        return np.array([-120.0])
    r = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(1) + 1e-12)
    return 20.0 * np.log10(r)


def trim(x, thresh=-42.0, pad=0.035):
    """Trim leading and trailing silence cleanly with safe edge padding."""
    win = int(0.01 * SR)
    db = rms_db(x, win)
    on = np.where(db > max(thresh, db.max() - 40.0))[0]
    if not len(on):
        return x, 0.0
    a = max(0, on[0] * win - int(pad * SR))
    b = min(len(x), (on[-1] + 1) * win + int(pad * SR))
    return x[a:b], a / SR


def compress_pauses(x, max_pause=0.26, thresh=-48.0):
    """Shorten overly long internal TTS pauses (> max_pause) while preserving natural cadence."""
    win = int(0.005 * SR)
    db = rms_db(x, win)
    silent = db < max(thresh, db.max() - 45.0)
    n_win = len(silent)
    parts = []
    pause_centers = []
    i = 0
    last_copy = 0
    out_len = 0
    max_wins = int(max_pause / 0.005)

    while i < n_win:
        if silent[i]:
            j = i
            while j < n_win and silent[j]:
                j += 1
            if i > 0 and j < n_win and (j - i) >= 24:  # >= 120ms meaningful internal pause
                if (j - i) > max_wins:
                    left_end = (i + max_wins // 2) * win
                    right_start = (j - (max_wins - max_wins // 2)) * win
                    if right_start > left_end:
                        seg = x[last_copy:left_end]
                        parts.append(seg)
                        out_len += len(seg)
                        pause_centers.append(out_len / SR)
                        xf = min(int(0.01 * SR), len(parts[-1]), len(x) - right_start)
                        if xf > 0:
                            r = np.linspace(1.0, 0.0, xf)
                            parts[-1][-xf:] *= r
                            bridge = x[right_start : right_start + xf].copy() * (1.0 - r)
                            parts.append(bridge)
                            out_len += len(bridge)
                            last_copy = right_start + xf
                        else:
                            last_copy = right_start
                else:
                    center_sample = ((i + j) // 2) * win
                    pause_centers.append((out_len + (center_sample - last_copy)) / SR)
            i = j
        else:
            i += 1
    parts.append(x[last_copy:])
    return np.concatenate(parts), pause_centers


def fade(x, ms=12):
    n = min(len(x) // 2, int(ms / 1000.0 * SR))
    if n:
        x = x.copy()
        r = np.linspace(0.0, 1.0, n)
        x[:n] *= r
        x[-n:] *= r[::-1]
    return x


def level(x, target=-16.0):
    """Normalize active-speech RMS to target dBFS with transparent soft peak limiting."""
    db = rms_db(x, int(0.05 * SR))
    act = db[db > db.max() - 25.0]
    cur = np.percentile(act, 80) if len(act) else -30.0
    y = x * 10.0 ** ((target - cur) / 20.0)
    over = np.abs(y) > 0.89
    y[over] = np.sign(y[over]) * (0.89 + 0.1 * np.tanh((np.abs(y[over]) - 0.89) / 0.1))
    return y


def active_rms_db(x):
    db = rms_db(x, int(0.05 * SR))
    act = db[db > db.max() - 25.0]
    return float(np.percentile(act, 80)) if len(act) else -120.0


def total_rms_db(x):
    return float(20.0 * np.log10(np.sqrt((x ** 2).mean()) + 1e-12))


def peak_db(x):
    return float(20.0 * np.log10(np.abs(x).max() + 1e-12))


SEGMENTS_SPEC = [
    {
        "id": "intro",
        "chapter": "Hovering Avatar",
        "badge": "Desktop Companion",
        "speaker": "narrator",
        "subtitle": "Meet SmartPoli Care Navigator: tap the hovering desktop avatar anytime to open your live medication companion.",
        "pre": 0.22,
        "post": 0.20,
        "turns": [
            {
                "clip": "00_intro.wav",
                "speaker": "narrator",
                "voice": DAVID,
                "rate": 3,
                "text": "Meet SmartPoli Care Navigator: tap the hovering desktop avatar anytime to open your live medication companion.",
                "tts": "Meet SmartPoli Care Navigator: tap the hovering desktop avatar anytime to open your live medication companion.",
            }
        ],
    },
    {
        "id": "kb",
        "chapter": "Touch & Talk",
        "badge": "Grounded Policy KB",
        "speaker": "narrator",
        "subtitle": "Touch and talk right in the floating companion\u2014it answers strictly from verified policy and refuses unsafe dosing guesses.",
        "pre": 0.18,
        "post": 0.20,
        "turns": [
            {
                "clip": "01_kb.wav",
                "speaker": "narrator",
                "voice": DAVID,
                "rate": 3,
                "text": "Touch and talk right in the floating companion\u2014it answers strictly from verified policy and refuses unsafe dosing guesses.",
                "tts": "Touch and talk right in the floating companion, it answers strictly from verified policy, and refuses unsafe dosing guesses.",
            }
        ],
    },
    {
        "id": "workflow_stats",
        "chapter": "Calls Your Phone",
        "badge": "4 Clinical Workflows",
        "speaker": "narrator",
        "subtitle": "Tap Unwell to request a check-in, and the companion immediately calls your phone across four grounded clinical workflows.",
        "pre": 0.18,
        "post": 0.22,
        "turns": [
            {
                "clip": "02_workflow_stats.wav",
                "speaker": "narrator",
                "voice": DAVID,
                "rate": 3,
                "text": "Tap Unwell to request a check-in, and the companion immediately calls your phone across four grounded clinical workflows.",
                "tts": "Tap Unwell to request a check-in, and the companion immediately calls your phone across four grounded clinical workflows.",
            }
        ],
    },
    {
        "id": "voice_bp",
        "chapter": "Live Voice Guidance",
        "badge": "Voice Call · BP Steps",
        "speaker": "dialogue",
        "subtitle": "Caller: Hi, how do I check my blood pressure, and can you send that to my WhatsApp? Agent: Rest five minutes with feet flat, place the cuff on your bare upper arm at heart level, and take two readings one minute apart. I'm sending that checklist to your WhatsApp.",
        "pre": 1.35,
        "post": 0.20,
        "turn_gap": 0.28,
        "turns": [
            {
                "clip": "03a_voice_bp_caller.wav",
                "speaker": "caller",
                "voice": DAVID,
                "rate": 2,
                "text": "Hi, how do I check my blood pressure, and can you send that to my WhatsApp?",
                "tts": "Hi, how do I check my blood pressure, and can you send that to my WhatsApp?",
            },
            {
                "clip": "03b_voice_bp_agent.wav",
                "speaker": "agent",
                "voice": ZIRA,
                "rate": 1,
                "text": "Rest five minutes with feet flat, place the cuff on your bare upper arm at heart level, and take two readings one minute apart. I'm sending that checklist to your WhatsApp.",
                "tts": "Rest five minutes with feet flat, place the cuff on your bare upper arm at heart level, and take two readings one minute apart. I'm sending that checklist to your WhatsApp.",
            },
        ],
    },
    {
        "id": "voice_urgent",
        "chapter": "Urgent Safety Triage",
        "badge": "Emergency Triage · SP-1042",
        "speaker": "dialogue",
        "subtitle": "Caller: Wait\u2014I'm also having severe chest pain and heart palpitations right now. Agent: This may be an emergency. Please call 112 or 911 immediately. I have logged urgent ticket SP-1042 and I am sending the details on WhatsApp right now.",
        "pre": 0.16,
        "post": 0.22,
        "turn_gap": 0.24,
        "turns": [
            {
                "clip": "04a_voice_urgent_caller.wav",
                "speaker": "caller",
                "voice": DAVID,
                "rate": 2,
                "text": "Wait\u2014I'm also having severe chest pain and heart palpitations right now.",
                "tts": "Wait, I'm also having severe chest pain and heart palpitations right now.",
            },
            {
                "clip": "04b_voice_urgent_agent.wav",
                "speaker": "agent",
                "voice": ZIRA,
                "rate": 1,
                "text": "This may be an emergency. Please call 112 or 911 immediately. I have logged urgent ticket SP-1042 and I am sending the details on WhatsApp right now.",
                "tts": "This may be an emergency. Please call 1 1 2 or 9 1 1 immediately. I have logged urgent ticket S P 10 42 and I am sending the details on WhatsApp right now.",
            },
        ],
    },
    {
        "id": "whatsapp",
        "chapter": "WhatsApp Dispatch",
        "badge": "WhatsApp Delivery",
        "speaker": "narrator",
        "subtitle": "On WhatsApp, the patient immediately receives the blood pressure checklist and urgent ticket SP-1042 with a direct portal link.",
        "pre": 0.24,
        "post": 0.20,
        "turns": [
            {
                "clip": "05_whatsapp.wav",
                "speaker": "narrator",
                "voice": DAVID,
                "rate": 3,
                "text": "On WhatsApp, the patient immediately receives the blood pressure checklist and urgent ticket SP-1042 with a direct portal link.",
                "tts": "On WhatsApp, the patient immediately receives the blood pressure checklist and urgent ticket S P 10 42 with a direct portal link.",
            }
        ],
    },
    {
        "id": "portal",
        "chapter": "Patient Ticket Portal",
        "badge": "Ticket Portal · SP-1042",
        "speaker": "narrator",
        "subtitle": "Clicking the ticket opens the live clinical record with delivery confirmations and one-tap caregiver and emergency actions.",
        "pre": 0.18,
        "post": 0.24,
        "turns": [
            {
                "clip": "06_portal.wav",
                "speaker": "narrator",
                "voice": DAVID,
                "rate": 3,
                "text": "Clicking the ticket opens the live clinical record with delivery confirmations and one-tap caregiver and emergency actions.",
                "tts": "Clicking the ticket opens the live clinical record with delivery confirmations and one-tap caregiver and emergency actions.",
            }
        ],
    },
    {
        "id": "outro",
        "chapter": "Clinical Safety Notice",
        "badge": "Triage & Support Only",
        "speaker": "narrator",
        "subtitle": "SmartPoli Care Navigator: a clinical triage and support flag, not a medical diagnosis.",
        "pre": 0.20,
        "post": 1.25,
        "turns": [
            {
                "clip": "07_outro.wav",
                "speaker": "narrator",
                "voice": DAVID,
                "rate": 3,
                "text": "SmartPoli Care Navigator: a clinical triage and support flag, not a medical diagnosis.",
                "tts": "SmartPoli Care Navigator: a clinical triage and support flag, not a medical diagnosis.",
            }
        ],
    },
]


def synthesize_all():
    """Synthesize all turns with Windows SAPI SpeechSynthesizer and return processed clip data."""
    os.makedirs(CLIPS_DIR, exist_ok=True)
    # Remove any legacy uXX.wav clips from earlier drafts
    for fname in os.listdir(CLIPS_DIR):
        if fname.endswith(".wav"):
            os.remove(os.path.join(CLIPS_DIR, fname))

    flat_turns = [t for seg in SEGMENTS_SPEC for t in seg["turns"]]

    with tempfile.TemporaryDirectory() as td:
        ps_lines = [
            "$ErrorActionPreference = 'Stop'",
            "Add-Type -AssemblyName System.Speech",
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        ]
        raw_paths = []
        for idx, t in enumerate(flat_turns):
            raw_p = os.path.join(td, f"raw_{idx:02d}.wav")
            raw_paths.append(raw_p)
            safe_txt = t["tts"].replace('"', '`"')
            ps_lines.append(f'$s.SelectVoice("{t["voice"]}")')
            ps_lines.append(f'$s.Rate = {t["rate"]}')
            ps_lines.append(f'$s.SetOutputToWaveFile("{raw_p}")')
            ps_lines.append(f'$s.Speak("{safe_txt}")')
        ps_lines.append("$s.SetOutputToNull()")
        ps_lines.append("$s.Dispose()")

        ps_path = os.path.join(td, "synth_all.ps1")
        with open(ps_path, "w", encoding="utf-8") as f:
            f.write("\n".join(ps_lines))
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps_path], check=True)

        processed = {}
        manifest = {}
        print("=== PROCESSED CLIPS ===")
        for idx, (t, raw_p) in enumerate(zip(flat_turns, raw_paths)):
            x = read_audio(raw_p, ch=1)
            xt, cut = trim(x, thresh=-42.0, pad=0.035)
            xc, pauses = compress_pauses(xt, max_pause=0.26, thresh=-48.0)
            xp = fade(level(xc, target=-16.0), ms=12)

            out_p = os.path.join(CLIPS_DIR, t["clip"])
            write_wav(out_p, xp, sr=SR)

            dur = len(xp) / SR
            processed[t["clip"]] = {
                "audio": xp,
                "dur": dur,
                "pauses": pauses,
                "act_rms": active_rms_db(xp),
                "tot_rms": total_rms_db(xp),
                "peak_db": peak_db(xp),
            }
            manifest[t["clip"]] = {
                "speaker": t["speaker"],
                "voice": t["voice"],
                "rate": t["rate"],
                "text": t["text"],
                "tts": t["tts"],
                "file": os.path.relpath(out_p, PROJ),
                "duration": round(dur, 3),
                "active_rms_db": round(processed[t["clip"]]["act_rms"], 2),
                "peak_db": round(processed[t["clip"]]["peak_db"], 2),
            }
            print(
                f"  {t['clip']:26s} {t['speaker']:8s} rate={t['rate']} "
                f"dur={dur:5.2f}s act_rms={processed[t['clip']]['act_rms']:6.2f}dBFS "
                f"tot_rms={processed[t['clip']]['tot_rms']:6.2f}dBFS peak={processed[t['clip']]['peak_db']:6.2f}dBFS"
            )

        with open(os.path.join(VO_DIR, "clips.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

    return processed


def build_timeline_and_mix(processed):
    """Assemble vo.wav, place subtle UI/WhatsApp SFX into stereo mix.wav, and save timeline.json."""
    t = 0.0
    segments = []
    placed_vo = []
    seg_lookup = {}

    for seg in SEGMENTS_SPEC:
        seg_start = t
        cur = seg_start + seg["pre"]
        turns_out = []
        for k, turn in enumerate(seg["turns"]):
            if k > 0:
                cur += seg.get("turn_gap", 0.25)
            info = processed[turn["clip"]]
            t_start = cur
            t_end = cur + info["dur"]
            placed_vo.append((t_start, info["audio"]))
            turns_out.append(
                {
                    "speaker": turn["speaker"],
                    "start": round(t_start, 3),
                    "end": round(t_end, 3),
                    "text": turn["text"],
                }
            )
            cur = t_end
        seg_end = cur + seg["post"]
        seg_obj = {
            "id": seg["id"],
            "chapter": seg["chapter"],
            "badge": seg["badge"],
            "start": round(seg_start, 3),
            "end": round(seg_end, 3),
            "speaker": seg["speaker"],
            "subtitle": seg["subtitle"],
            "turns": turns_out,
        }
        segments.append(seg_obj)
        seg_lookup[seg["id"]] = (seg_obj, [processed[tr["clip"]]["pauses"] for tr in seg["turns"]])
        t = seg_end

    duration = round(t, 3)
    assert duration <= 59.5, f"Duration {duration}s exceeds 59.5s ceiling!"

    # Build combined mono vo.wav
    n_samples = int(round(duration * SR))
    vo_track = np.zeros(n_samples, dtype=np.float64)
    for start_s, clip_audio in placed_vo:
        s0 = int(round(start_s * SR))
        L = min(len(clip_audio), n_samples - s0)
        if L > 0:
            vo_track[s0 : s0 + L] += clip_audio[:L]

    vo_path = os.path.join(VO_DIR, "vo.wav")
    write_wav(vo_path, vo_track, sr=SR)

    # Subtle UI & WhatsApp SFX events ONLY (zero background music / noise)
    def seg_t(sid):
        return seg_lookup[sid][0]

    def pause_at(sid, turn_idx, pause_idx, fallback):
        s_obj, p_lists = seg_lookup[sid]
        t_start = s_obj["turns"][turn_idx]["start"]
        pauses = p_lists[turn_idx]
        return round(t_start + pauses[pause_idx], 3) if pause_idx < len(pauses) else round(t_start + fallback, 3)

    sfx_events = [
        # Seg 0 (intro): click hovering desktop avatar to open floating SmartPoli Companion
        {"name": "click", "at": 2.55, "gain": 0.32},
        # Seg 1 (kb): user touches/talks in floating companion; policy citation badge pops in
        {"name": "click", "at": round(seg_t("kb")["start"] + 0.06, 3), "gain": 0.30},
        {"name": "pop", "at": pause_at("kb", 0, 1, 2.5), "gain": 0.26},
        # Seg 2 (workflow_stats): tap Unwell -> outbound call + 4 clinical workflow stats appear
        {"name": "click", "at": round(seg_t("workflow_stats")["start"] + 0.06, 3), "gain": 0.34},
        {"name": "pop", "at": pause_at("workflow_stats", 0, 0, 2.2), "gain": 0.26},
        # Seg 3 (voice_bp): Phone Link ringing opens, user clicks Accept Call (+1.15s) before Caller talks (+1.35s) + WhatsApp BP checklist dispatched
        {"name": "click", "at": round(seg_t("voice_bp")["start"] + 1.15, 3), "gain": 0.34},
        {"name": "pop", "at": round(seg_t("voice_bp")["turns"][1]["end"] + 0.04, 3), "gain": 0.30},
        # Seg 4 (voice_urgent): emergency alert triggered + urgent ticket SP-1042 logged
        {"name": "pop", "at": pause_at("voice_urgent", 1, 1, 3.6), "gain": 0.28},
        {"name": "pop", "at": round(seg_t("voice_urgent")["turns"][1]["end"] + 0.04, 3), "gain": 0.30},
        # Seg 5 (whatsapp): WhatsApp message arrival chime + urgent ticket bubble
        {"name": "ding", "at": round(seg_t("whatsapp")["start"] + 0.05, 3), "gain": 0.36},
        {"name": "pop", "at": round(seg_t("whatsapp")["turns"][0]["end"] + 0.03, 3), "gain": 0.28},
        # Seg 6 (portal): clicking the ticket link opens live clinical record + delivery confirmation card
        {"name": "click", "at": round(seg_t("portal")["start"] + 0.06, 3), "gain": 0.34},
        {"name": "pop", "at": round(seg_t("portal")["turns"][0]["end"] + 0.03, 3), "gain": 0.28},
    ]

    # Build stereo mix.wav (silence = true digital zero; voices centered; subtle SFX overlaid)
    os.makedirs(OUT_DIR, exist_ok=True)
    mix_track = np.stack([vo_track, vo_track], axis=1)

    sfx_cache = {}
    for ev in sfx_events:
        name = ev["name"]
        if name not in sfx_cache:
            sfx_cache[name] = read_audio(os.path.join(SFX_DIR, f"{name}.wav"), ch=2)
        sx = sfx_cache[name]
        s0 = int(round(ev["at"] * SR))
        L = min(len(sx), n_samples - s0)
        if s0 >= 0 and L > 0:
            mix_track[s0 : s0 + L] += sx[:L] * ev["gain"]

    # Local transparent soft-limiting only on extreme peaks (> 0.92), keeping -16 dBFS active speech linear
    over = np.abs(mix_track) > 0.92
    if np.any(over):
        mix_track[over] = np.sign(mix_track[over]) * (
            0.92 + 0.06 * np.tanh((np.abs(mix_track[over]) - 0.92) / 0.06)
        )

    mix_path = os.path.join(OUT_DIR, "mix.wav")
    write_wav(mix_path, mix_track, sr=SR)

    timeline = {
        "duration": duration,
        "mix": "out/mix.wav",
        "vo": "vo/vo.wav",
        "sample_rate": SR,
        "channels": 2,
        "segments": segments,
        "sfx_events": sfx_events,
    }
    tl_path = os.path.join(PROJ, "timeline.json")
    with open(tl_path, "w", encoding="utf-8") as f:
        json.dump(timeline, f, indent=2)

    return timeline, vo_path, mix_path


def verify_outputs(timeline, vo_path, mix_path):
    """Verify exact WAV format, duration <= 59.5s, RMS/peaks, silence floor, and print report."""
    with wave.open(vo_path, "rb") as wv:
        vo_ch, vo_sw, vo_sr, vo_nf = wv.getnchannels(), wv.getsampwidth(), wv.getframerate(), wv.getnframes()
        vo_arr = np.frombuffer(wv.readframes(vo_nf), dtype="<i2").astype(np.float64) / 32767.0
    vo_dur = vo_nf / vo_sr

    with wave.open(mix_path, "rb") as wm:
        mx_ch, mx_sw, mx_sr, mx_nf = wm.getnchannels(), wm.getsampwidth(), wm.getframerate(), wm.getnframes()
        mx_arr = np.frombuffer(wm.readframes(mx_nf), dtype="<i2").astype(np.float64) / 32767.0
        mx_arr = mx_arr.reshape(-1, mx_ch)
    mx_dur = mx_nf / mx_sr

    assert vo_sr == 48000, f"vo.wav sr={vo_sr}"
    assert mx_sr == 48000 and mx_ch == 2, f"mix.wav sr={mx_sr} ch={mx_ch}"
    assert mx_dur <= 59.5 and timeline["duration"] <= 59.5, f"Duration {mx_dur} exceeds 59.5s!"
    assert abs(mx_dur - timeline["duration"]) < 0.005, "mix.wav duration mismatch with timeline.json"

    # Check tail hold silence floor (last 0.9s of outro hold)
    tail_samples = mx_arr[-int(0.9 * mx_sr) :]
    tail_max = float(np.abs(tail_samples).max())

    print("\n=== VERIFIED TIMELINE SEGMENTS ===")
    print(f"{'Idx':>3} | {'ID':<14} | {'Start':>7} | {'End':>7} | {'Dur':>6} | {'Speaker':<8} | {'Chapter'}")
    print("-" * 78)
    for idx, seg in enumerate(timeline["segments"]):
        dur = seg["end"] - seg["start"]
        print(f"{idx:>3} | {seg['id']:<14} | {seg['start']:7.3f} | {seg['end']:7.3f} | {dur:6.3f} | {seg['speaker']:<8} | {seg['chapter']}")
        if len(seg["turns"]) > 1:
            for ti, tr in enumerate(seg["turns"]):
                print(f"    -> turn {idx}{chr(97+ti)} ({tr['speaker']:<6}): {tr['start']:7.3f} - {tr['end']:7.3f} ({tr['end']-tr['start']:5.3f}s)")

    print("\n=== SFX EVENTS ===")
    for ev in timeline["sfx_events"]:
        print(f"  at={ev['at']:6.3f}s  name={ev['name']:<6}  gain={ev['gain']:.2f}")

    print("\n=== WAV METRICS ===")
    print(
        f"  vo.wav : ch={vo_ch} sr={vo_sr} sw={vo_sw*8}-bit dur={vo_dur:.3f}s "
        f"act_rms={active_rms_db(vo_arr):.2f} dBFS tot_rms={total_rms_db(vo_arr):.2f} dBFS peak={peak_db(vo_arr):.2f} dBFS"
    )
    print(
        f"  mix.wav: ch={mx_ch} sr={mx_sr} sw={mx_sw*8}-bit dur={mx_dur:.3f}s "
        f"act_rms={active_rms_db(mx_arr[:,0]):.2f} dBFS tot_rms={total_rms_db(mx_arr):.2f} dBFS "
        f"peak={peak_db(mx_arr):.2f} dBFS tail_noise_peak={tail_max:.6f}"
    )


def load_existing_clips():
    """Load already-synthesized clips from vo/clips/ to rebuild timeline and mix without re-running TTS."""
    processed = {}
    flat_turns = [t for seg in SEGMENTS_SPEC for t in seg["turns"]]
    for t in flat_turns:
        p = os.path.join(CLIPS_DIR, t["clip"])
        xp = read_audio(p, ch=1)
        _, pauses = compress_pauses(xp, max_pause=0.26, thresh=-48.0)
        processed[t["clip"]] = {
            "audio": xp,
            "dur": len(xp) / SR,
            "pauses": pauses,
            "act_rms": active_rms_db(xp),
            "tot_rms": total_rms_db(xp),
            "peak_db": peak_db(xp),
        }
    return processed


if __name__ == "__main__":
    import sys
    clips = load_existing_clips() if "--reuse-clips" in sys.argv else synthesize_all()
    tl, vp, mp = build_timeline_and_mix(clips)
    verify_outputs(tl, vp, mp)
