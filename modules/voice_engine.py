"""
voice_engine.py
----------------
Backs the "🎤 <name>" voice assistant that appears on Full Analysis, Business
Insights, and Boss Dashboard.

THREE SEPARATE PIECES, each using a different (free) mechanism:

  1. LISTENING (speech-to-text) — streamlit_mic_recorder.speech_to_text().
     This runs entirely in the BROWSER (Chrome's built-in Web Speech API
     under the hood) — no server call, no API key, no cost. Started/stopped
     by a button click (push-to-talk), not a hands-free "always listening"
     wake word — a browser tab can't reliably stay "always listening" in
     the background the way a phone's built-in assistant can, so a button
     is the honest, reliable version of this rather than promising
     something that would work inconsistently.

  2. THINKING (answering the question) — reuses ai_chat.ask() EXACTLY as
     the existing 🤖 AI Assistant page does: real SQL run against the
     actual loaded data, not a guess. No new AI key needed — the same
     OpenRouter key already configured for the text AI Assistant is reused
     here.

  3. SPEAKING (text-to-speech) — the browser's built-in speechSynthesis API,
     injected via a tiny <script> (see tts_html()). Also entirely free, no
     key, no server call.

LANGUAGE NOTE (be upfront about this): browser speech support varies.
English and Hindi work reasonably well on Chrome/Edge. Gujarati recognition
and voice availability depends on the specific browser/OS/device — it may
be less accurate or, on some setups, unavailable. This is a browser/OS
limitation, not something an API key can fix.
"""

import time

LANG_CODES = {"English": "en-IN", "Hindi": "hi-IN", "Gujarati": "gu-IN"}
DEFAULT_ASSISTANT_NAME = "री"


# ---------------------------------------------------------------------------
# ASSISTANT NAME — "री" on Free, customizable per-workspace on Standard
# ---------------------------------------------------------------------------
def get_assistant_name(plan: str, custom_name: str = None) -> str:
    """Free plan always hears/says 'री' - no per-account customization,
    that's a Standard-plan perk (same pattern as branding/logo). Standard
    with nothing set yet also defaults to 'री' until the client picks
    something else in Settings."""
    if plan == "standard" and custom_name and custom_name.strip():
        return custom_name.strip()
    return DEFAULT_ASSISTANT_NAME


# ---------------------------------------------------------------------------
# TEXT-TO-SPEECH — tiny JS snippet, rendered via st.components.v1.html
# ---------------------------------------------------------------------------
def tts_html(text: str, lang_code: str, height: int = 0) -> str:
    """Speaks `text` aloud once, in the browser, using speechSynthesis.
    Picks a voice matching lang_code if the browser has one installed;
    otherwise falls back to the browser's default voice (still sets the
    lang code, which some browsers use even without a matching voice)."""
    safe_text = (text or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f"""
    <script>
    (function() {{
        if (!window.speechSynthesis) return;
        const utter = new SpeechSynthesisUtterance("{safe_text}");
        utter.lang = "{lang_code}";
        const pick = () => {{
            const voices = window.speechSynthesis.getVoices();
            const match = voices.find(v => v.lang === "{lang_code}") ||
                          voices.find(v => v.lang.startsWith("{lang_code.split('-')[0]}"));
            if (match) utter.voice = match;
            window.speechSynthesis.cancel();
            window.speechSynthesis.speak(utter);
        }};
        if (window.speechSynthesis.getVoices().length) {{
            pick();
        }} else {{
            window.speechSynthesis.onvoiceschanged = pick;
        }}
    }})();
    </script>
    """


# ---------------------------------------------------------------------------
# GUIDED WALKTHROUGH SCRIPTS — built from data ALREADY computed on each page
# (never invents numbers; each segment is generated from real values passed
# in by the caller, same discipline as the rest of the app)
# ---------------------------------------------------------------------------
def walkthrough_full_analysis(missing_cols: list, kpi_lines: list, narrative: str = None) -> list:
    segments = []
    if missing_cols:
        segments.append({
            "title": "Behtar analysis ke liye",
            "text": "Behtar analysis ke liye ye columns add karne se madad milegi: "
                    + ", ".join(missing_cols) + ". Filhaal jo data hai usi se analysis chal raha hai."
        })
    segments.append({
        "title": "Data Quality",
        "text": "Sabse pehle data quality dekhte hain — kitni rows hain, kitne columns, "
                "aur kahin missing ya duplicate values to nahi."
    })
    if kpi_lines:
        segments.append({
            "title": "Key Numbers",
            "text": "Ab main numbers: " + ". ".join(kpi_lines) + "."
        })
    if narrative:
        segments.append({"title": "Past se Future tak", "text": narrative})
    segments.append({
        "title": "Kahan focus karein",
        "text": "Ab neeche jo Management Decisions section hai, wahi bataata hai kahan scale karna hai, "
                "kahan optimize, aur kahan kam karna hai — sab evidence ke saath."
    })
    return segments


def walkthrough_business_insights(top_sport: dict, top_code: dict, top_day: dict,
                                   health: dict, n_decisions: int) -> list:
    segments = [{
        "title": "Shuru",
        "text": "Ye Business Insights hai — Payment Page Title ko Sport, Code aur Day me tod ke dikhata hai."
    }]
    if top_sport:
        segments.append({"title": "Top Sport",
                          "text": f"Sabse zyada revenue {top_sport.get('name')} se aaya hai — "
                                  f"₹{top_sport.get('revenue', 0):,.0f}."})
    if top_code:
        segments.append({"title": "Top Location",
                          "text": f"Sabse zyada revenue wala Code/Location hai {top_code.get('name')} — "
                                  f"₹{top_code.get('revenue', 0):,.0f}."})
    if top_day:
        segments.append({"title": "Best Day",
                          "text": f"Sabse achha din hai {top_day.get('name')} — "
                                  f"₹{top_day.get('revenue', 0):,.0f} revenue ke saath."})
    if health and health.get("score") is not None:
        segments.append({"title": "Health Score",
                          "text": f"Overall Health Score hai {health['score']} — matlab {health['label']}."})
    if n_decisions:
        segments.append({"title": "Decisions",
                          "text": f"Neeche {n_decisions} management decisions hain — kahan scale karna hai, "
                                  "kahan optimize, sab evidence ke saath."})
    return segments


def walkthrough_boss_dashboard(pinned_kpi_lines: list, n_charts: int) -> list:
    segments = [{"title": "Shuru", "text": "Ye aapka Boss Dashboard hai — jo bhi pin kiya hai wahi yahan hai."}]
    if pinned_kpi_lines:
        segments.append({"title": "Pinned KPIs", "text": "Pinned KPIs: " + ". ".join(pinned_kpi_lines) + "."})
    else:
        segments.append({"title": "Pinned KPIs", "text": "Abhi koi KPI pin nahi kiya gaya hai."})
    if n_charts:
        segments.append({"title": "Charts", "text": f"{n_charts} charts pin kiye gaye hain neeche."})
    segments.append({"title": "Poochiye", "text": "Kuch bhi poochna ho to mic dabaake seedha poochh sakte hain."})
    return segments
