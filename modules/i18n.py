"""
i18n.py
-------
Whole-app language toggle: a small 🌐 icon (not a big box) that opens a
compact popover where the client picks their language. Selection is stored
in st.session_state.app_language and used by t(key) everywhere else.

HOW THIS IS WIRED (by design, to avoid ever breaking page routing/logic):
  - Internal page identifiers (nav_options, "page == ..." checks throughout
    app.py) stay in ENGLISH always — translation only changes what's
    *displayed* (via format_func on the sidebar radio, and t() calls on
    titles/labels/buttons). Nothing about how the app routes or stores data
    depends on the chosen language, so switching language can never break a
    button, a saved filter, or a page.
  - t(key) always has an English fallback, so any string this pass hasn't
    translated yet still displays correctly (in English) rather than
    crashing or showing a raw key.

SCOPE OF THIS FIRST PASS: navigation menu, page titles/major section
headers, and the most-used chrome (login, logout, save, download, refresh,
common labels). Deep per-widget translation of every table/column/tooltip
across an 11k-line app is a large incremental job — this file is built so
that job is just "add more keys to TRANSLATIONS", nothing structural.
"""

import streamlit as st

LANGUAGES = {
    "en": "English",
    "hi": "हिन्दी (Hindi)",
    "gu": "ગુજરાતી (Gujarati)",
    "mr": "मराठी (Marathi)",
    "ta": "தமிழ் (Tamil)",
    "te": "తెలుగు (Telugu)",
    "bn": "বাংলা (Bengali)",
    "pa": "ਪੰਜਾਬੀ (Punjabi)",
    "kn": "ಕನ್ನಡ (Kannada)",
}

# key -> {lang_code: text}. English is the fallback for any missing key/lang.
TRANSLATIONS = {
    # ---- Sidebar navigation menu (display only — routing stays English) ----
    "nav_connect_data":    {"en": "📥 Connect Data",      "hi": "📥 डेटा जोड़ें",              "gu": "📥 ડેટા જોડો",              "mr": "📥 डेटा जोडा",              "ta": "📥 தரவை இணைக்கவும்",        "te": "📥 డేటాను కనెక్ట్ చేయండి",   "bn": "📥 ডেটা সংযুক্ত করুন",       "pa": "📥 ਡਾਟਾ ਜੋੜੋ",              "kn": "📥 ಡೇಟಾ ಸಂಪರ್ಕಿಸಿ"},
    "nav_raw_analysis":    {"en": "📊 Raw Analysis",      "hi": "📊 रॉ एनालिसिस",              "gu": "📊 રો એનાલિસિસ",            "mr": "📊 रॉ अ‍ॅनालिसिस",           "ta": "📊 மூல பகுப்பாய்வு",         "te": "📊 రా విశ్లేషణ",             "bn": "📊 কাঁচা বিশ্লেষণ",          "pa": "📊 ਰਾਅ ਵਿਸ਼ਲੇਸ਼ਣ",          "kn": "📊 ರಾ ವಿಶ್ಲೇಷಣೆ"},
    "nav_custom_builder":  {"en": "🧩 Custom Builder",    "hi": "🧩 कस्टम बिल्डर",             "gu": "🧩 કસ્ટમ બિલ્ડર",           "mr": "🧩 कस्टम बिल्डर",            "ta": "🧩 விருப்ப உருவாக்கி",       "te": "🧩 కస్టమ్ బిల్డర్",          "bn": "🧩 কাস্টম বিল্ডার",          "pa": "🧩 ਕਸਟਮ ਬਿਲਡਰ",             "kn": "🧩 ಕಸ್ಟಮ್ ಬಿಲ್ಡರ್"},
    "nav_boss_dashboard":  {"en": "⭐ Boss Dashboard",    "hi": "⭐ बॉस डैशबोर्ड",             "gu": "⭐ બોસ ડેશબોર્ડ",            "mr": "⭐ बॉस डॅशबोर्ड",            "ta": "⭐ முதன்மை டாஷ்போர்டு",      "te": "⭐ బాస్ డాష్‌బోర్డ్",         "bn": "⭐ বস ড্যাশবোর্ড",           "pa": "⭐ ਬੌਸ ਡੈਸ਼ਬੋਰਡ",            "kn": "⭐ ಬಾಸ್ ಡ್ಯಾಶ್‌ಬೋರ್ಡ್"},
    "nav_business_insights": {"en": "💡 Business Insights", "hi": "💡 बिज़नेस इनसाइट्स",      "gu": "💡 બિઝનેસ ઇનસાઇટ્સ",        "mr": "💡 बिझनेस इनसाइट्स",         "ta": "💡 வணிக நுண்ணறிவு",          "te": "💡 వ్యాపార అంతర్దృష్టులు",    "bn": "💡 ব্যবসায়িক অন্তর্দৃষ্টি",  "pa": "💡 ਕਾਰੋਬਾਰੀ ਸੂਝ",           "kn": "💡 ವ್ಯಾಪಾರ ಒಳನೋಟಗಳು"},
    "nav_full_analysis":   {"en": "📈 Full Analysis",     "hi": "📈 पूर्ण विश्लेषण",           "gu": "📈 સંપૂર્ણ વિશ્લેષણ",        "mr": "📈 पूर्ण विश्लेषण",          "ta": "📈 முழு பகுப்பாய்வு",        "te": "📈 పూర్తి విశ్లేషణ",          "bn": "📈 সম্পূর্ণ বিশ্লেষণ",       "pa": "📈 ਪੂਰਾ ਵਿਸ਼ਲੇਸ਼ਣ",          "kn": "📈 ಪೂರ್ಣ ವಿಶ್ಲೇಷಣೆ"},
    "nav_data_table":      {"en": "🗂 Data Table",        "hi": "🗂 डेटा टेबल",                "gu": "🗂 ડેટા ટેબલ",              "mr": "🗂 डेटा टेबल",               "ta": "🗂 தரவு அட்டவணை",           "te": "🗂 డేటా టేబుల్",             "bn": "🗂 ডেটা টেবিল",              "pa": "🗂 ਡਾਟਾ ਟੇਬਲ",              "kn": "🗂 ಡೇಟಾ ಟೇಬಲ್"},
    "nav_ai_assistant":    {"en": "🤖 AI Assistant",      "hi": "🤖 एआई असिस्टेंट",            "gu": "🤖 એઆઈ આસિસ્ટન્ટ",          "mr": "🤖 एआय असिस्टंट",            "ta": "🤖 AI உதவியாளர்",            "te": "🤖 AI సహాయకుడు",             "bn": "🤖 এআই সহায়ক",              "pa": "🤖 ਏਆਈ ਸਹਾਇਕ",              "kn": "🤖 AI ಸಹಾಯಕ"},
    "nav_settings":        {"en": "⚙️ Settings",          "hi": "⚙️ सेटिंग्स",                "gu": "⚙️ સેટિંગ્સ",               "mr": "⚙️ सेटिंग्ज",                "ta": "⚙️ அமைப்புகள்",             "te": "⚙️ సెట్టింగ్‌లు",            "bn": "⚙️ সেটিংস",                 "pa": "⚙️ ਸੈਟਿੰਗਾਂ",               "kn": "⚙️ ಸೆಟ್ಟಿಂಗ್‌ಗಳು"},
    "nav_plans":           {"en": "💎 Plans",              "hi": "💎 प्लान्स",                  "gu": "💎 પ્લાન્સ",                "mr": "💎 प्लॅन्स",                 "ta": "💎 திட்டங்கள்",              "te": "💎 ప్లాన్‌లు",               "bn": "💎 প্ল্যান",                 "pa": "💎 ਪਲਾਨ",                   "kn": "💎 ಯೋಜನೆಗಳು"},
    "nav_admin_panel":     {"en": "🔐 Admin Panel",       "hi": "🔐 एडमिन पैनल",               "gu": "🔐 એડમિન પેનલ",             "mr": "🔐 अ‍ॅडमिन पॅनल",            "ta": "🔐 நிர்வாக பலகம்",          "te": "🔐 అడ్మిన్ ప్యానెల్",         "bn": "🔐 অ্যাডমিন প্যানেল",        "pa": "🔐 ਐਡਮਿਨ ਪੈਨਲ",             "kn": "🔐 ಅಡ್ಮಿನ್ ಪ್ಯಾನಲ್"},

    # ---- Common chrome / buttons ----
    "logout":       {"en": "🚪 Logout",  "hi": "🚪 लॉगआउट",  "gu": "🚪 લૉગઆઉટ",  "mr": "🚪 लॉगआउट",  "ta": "🚪 வெளியேறு", "te": "🚪 లాగ్అవుట్", "bn": "🚪 লগআউট", "pa": "🚪 ਲੌਗਆਉਟ", "kn": "🚪 ಲಾಗ್ಔಟ್"},
    "login":        {"en": "Login",      "hi": "लॉगिन",      "gu": "લૉગિન",       "mr": "लॉगिन",       "ta": "உள்நுழை",     "te": "లాగిన్",       "bn": "লগইন",      "pa": "ਲੌਗਇਨ",      "kn": "ಲಾಗಿನ್"},
    "username":     {"en": "Username",   "hi": "यूज़रनेम",    "gu": "યુઝરનેમ",     "mr": "युजरनेम",     "ta": "பயனர்பெயர்",  "te": "యూజర్‌నేమ్",   "bn": "ইউজারনেম",  "pa": "ਯੂਜ਼ਰਨੇਮ",   "kn": "ಬಳಕೆದಾರ ಹೆಸರು"},
    "password":     {"en": "Password",   "hi": "पासवर्ड",     "gu": "પાસવર્ડ",      "mr": "पासवर्ड",      "ta": "கடவுச்சொல்",   "te": "పాస్‌వర్డ్",    "bn": "পাসওয়ার্ড", "pa": "ਪਾਸਵਰਡ",     "kn": "ಪಾಸ್‌ವರ್ಡ್"},
    "invalid_login": {"en": "Invalid username or password.", "hi": "अमान्य यूज़रनेम या पासवर्ड।", "gu": "અમાન્ય યુઝરનેમ અથવા પાસવર્ડ.", "mr": "अवैध युजरनेम किंवा पासवर्ड.", "ta": "தவறான பயனர்பெயர் அல்லது கடவுச்சொல்.", "te": "చెల్లని యూజర్‌నేమ్ లేదా పాస్‌వర్డ్.", "bn": "ভুল ইউজারনেম বা পাসওয়ার্ড।", "pa": "ਗਲਤ ਯੂਜ਼ਰਨੇਮ ਜਾਂ ਪਾਸਵਰਡ।", "kn": "ಅಮಾನ್ಯ ಬಳಕೆದಾರ ಹೆಸರು ಅಥವಾ ಪಾಸ್‌ವರ್ಡ್."},
    "save":         {"en": "Save",       "hi": "सेव करें",    "gu": "સેવ કરો",     "mr": "सेव्ह करा",    "ta": "சேமி",        "te": "సేవ్ చేయండి",  "bn": "সংরক্ষণ করুন", "pa": "ਸੇਵ ਕਰੋ",    "kn": "ಉಳಿಸಿ"},
    "cancel":       {"en": "Cancel",     "hi": "रद्द करें",   "gu": "રદ કરો",       "mr": "रद्द करा",     "ta": "ரத்து செய்",  "te": "రద్దు చేయండి", "bn": "বাতিল করুন", "pa": "ਰੱਦ ਕਰੋ",     "kn": "ರದ್ದುಗೊಳಿಸಿ"},
    "download":     {"en": "Download",   "hi": "डाउनलोड करें", "gu": "ડાઉનલોડ કરો", "mr": "डाउनलोड करा",  "ta": "பதிவிறக்கு",  "te": "డౌన్‌లోడ్ చేయండి", "bn": "ডাউনলোড করুন", "pa": "ਡਾਊਨਲੋਡ ਕਰੋ", "kn": "ಡೌನ್‌ಲೋಡ್ ಮಾಡಿ"},
    "refresh":      {"en": "🔄 Refresh", "hi": "🔄 रिफ्रेश करें", "gu": "🔄 રિફ્રેશ કરો", "mr": "🔄 रिफ्रेश करा", "ta": "🔄 புதுப்பி", "te": "🔄 రిఫ్రెష్ చేయండి", "bn": "🔄 রিফ্রেশ করুন", "pa": "🔄 ਰਿਫ੍ਰੈਸ਼ ਕਰੋ", "kn": "🔄 ರಿಫ್ರೆಶ್ ಮಾಡಿ"},
    "logged_in_as": {"en": "Logged in as", "hi": "लॉग इन:", "gu": "લૉગ ઇન:", "mr": "लॉग इन:", "ta": "உள்நுழைந்தவர்:", "te": "లాగిన్ అయ్యారు:", "bn": "লগ ইন করা আছে:", "pa": "ਲੌਗ ਇਨ:", "kn": "ಲಾಗಿನ್ ಆಗಿದೆ:"},
    "no_data_loaded": {"en": "No data loaded yet.", "hi": "अभी तक कोई डेटा लोड नहीं हुआ।", "gu": "હજુ સુધી કોઈ ડેટા લોડ થયો નથી.", "mr": "अजून कोणताही डेटा लोड झालेला नाही.", "ta": "இன்னும் தரவு ஏற்றப்படவில்லை.", "te": "ఇంకా డేటా లోడ్ కాలేదు.", "bn": "এখনও কোনো ডেটা লোড হয়নি।", "pa": "ਹਾਲੇ ਕੋਈ ਡਾਟਾ ਲੋਡ ਨਹੀਂ ਹੋਇਆ।", "kn": "ಇನ್ನೂ ಯಾವುದೇ ಡೇಟಾ ಲೋಡ್ ಆಗಿಲ್ಲ."},

    # ---- Page titles (main st.title() on each page) ----
    "title_connect_data":     {"en": "📥 Connect Data",  "hi": "📥 डेटा जोड़ें",   "gu": "📥 ડેટા જોડો",  "mr": "📥 डेटा जोडा",   "ta": "📥 தரவை இணைக்கவும்", "te": "📥 డేటాను కనెక్ట్ చేయండి", "bn": "📥 ডেটা সংযুক্ত করুন", "pa": "📥 ਡਾਟਾ ਜੋੜੋ", "kn": "📥 ಡೇಟಾ ಸಂಪರ್ಕಿಸಿ"},
    "title_full_analysis":    {"en": "📈 Full Analysis", "hi": "📈 पूर्ण विश्लेषण", "gu": "📈 સંપૂર્ણ વિશ્લેષણ", "mr": "📈 पूर्ण विश्लेषण", "ta": "📈 முழு பகுப்பாய்வு", "te": "📈 పూర్తి విశ్లేషణ", "bn": "📈 সম্পূর্ণ বিশ্লেষণ", "pa": "📈 ਪੂਰਾ ਵਿਸ਼ਲੇਸ਼ਣ", "kn": "📈 ಪೂರ್ಣ ವಿಶ್ಲೇಷಣೆ"},
    "title_data_table":       {"en": "🗂 Data Table",    "hi": "🗂 डेटा टेबल",    "gu": "🗂 ડેટા ટેબલ",   "mr": "🗂 डेटा टेबल",    "ta": "🗂 தரவு அட்டவணை", "te": "🗂 డేటా టేబుల్", "bn": "🗂 ডেটা টেবিল", "pa": "🗂 ਡਾਟਾ ਟੇਬਲ", "kn": "🗂 ಡೇಟಾ ಟೇಬಲ್"},
    "title_ai_assistant":     {"en": "🤖 AI Assistant",  "hi": "🤖 एआई असिस्टेंट", "gu": "🤖 એઆઈ આસિસ્ટન્ટ", "mr": "🤖 एआय असिस्टंट", "ta": "🤖 AI உதவியாளர்", "te": "🤖 AI సహాయకుడు", "bn": "🤖 এআই সহায়ক", "pa": "🤖 ਏਆਈ ਸਹਾਇਕ", "kn": "🤖 AI ಸಹಾಯಕ"},
    "title_settings":         {"en": "⚙️ Settings",      "hi": "⚙️ सेटिंग्स",    "gu": "⚙️ સેટિંગ્સ",   "mr": "⚙️ सेटिंग्ज",    "ta": "⚙️ அமைப்புகள்", "te": "⚙️ సెట్టింగ్‌లు", "bn": "⚙️ সেটিংস", "pa": "⚙️ ਸੈਟਿੰਗਾਂ", "kn": "⚙️ ಸೆಟ್ಟಿಂಗ್‌ಗಳು"},
    "title_plans":            {"en": "💎 Plans",          "hi": "💎 प्लान्स",     "gu": "💎 પ્લાન્સ",    "mr": "💎 प्लॅन्स",     "ta": "💎 திட்டங்கள்",  "te": "💎 ప్లాన్‌లు",   "bn": "💎 প্ল্যান",   "pa": "💎 ਪਲਾਨ",     "kn": "💎 ಯೋಜನೆಗಳು"},
    "title_admin_panel":      {"en": "🔐 Admin Panel",   "hi": "🔐 एडमिन पैनल",  "gu": "🔐 એડમિન પેનલ", "mr": "🔐 अ‍ॅडमिन पॅनल", "ta": "🔐 நிர்வாக பலகம்", "te": "🔐 అడ్మిన్ ప్యానెల్", "bn": "🔐 অ্যাডমিন প্যানেল", "pa": "🔐 ਐਡਮਿਨ ਪੈਨਲ", "kn": "🔐 ಅಡ್ಮಿನ್ ಪ್ಯಾನಲ್"},

    "language_picker_label": {"en": "🌐 Language", "hi": "🌐 भाषा", "gu": "🌐 ભાષા", "mr": "🌐 भाषा", "ta": "🌐 மொழி", "te": "🌐 భాష", "bn": "🌐 ভাষা", "pa": "🌐 ਭਾਸ਼ਾ", "kn": "🌐 ಭಾಷೆ"},
}


def t(key, default=None):
    """Translate `key` into the currently selected app language. Falls back
    to English, then to `default` (or the key itself) if not found — so a
    string that hasn't been added to TRANSLATIONS yet never crashes the
    page, it just shows in English."""
    lang = st.session_state.get("app_language", "en")
    entry = TRANSLATIONS.get(key)
    if not entry:
        return default if default is not None else key
    return entry.get(lang) or entry.get("en") or default or key


# Maps the exact English strings used as nav_options / page identifiers in
# app.py to their TRANSLATIONS key, so the sidebar radio can show a
# translated label via format_func while routing internally stays English
# (nothing else in app.py has to change or ever risks breaking).
NAV_KEY_MAP = {
    "📥 Connect Data": "nav_connect_data",
    "📊 Raw Analysis": "nav_raw_analysis",
    "🧩 Custom Builder": "nav_custom_builder",
    "⭐ Boss Dashboard": "nav_boss_dashboard",
    "💡 Business Insights": "nav_business_insights",
    "📈 Full Analysis": "nav_full_analysis",
    "🗂 Data Table": "nav_data_table",
    "🤖 AI Assistant": "nav_ai_assistant",
    "⚙️ Settings": "nav_settings",
    "💎 Plans": "nav_plans",
    "🔐 Admin Panel": "nav_admin_panel",
}


def nav_label(nav_option_en: str) -> str:
    """format_func for the sidebar nav radio — translates the DISPLAYED
    label only; the value st.radio returns (used for routing) stays the
    original English string untouched."""
    key = NAV_KEY_MAP.get(nav_option_en)
    return t(key, default=nav_option_en) if key else nav_option_en


def language_toggle(key_suffix=""):
    """Small 🌐 icon that opens a compact popover with the language list —
    NOT a big box. Call once per page render (sidebar and/or login screen).
    Changing the selection reruns the app immediately so every t() call
    picks up the new language on the very next render."""
    current = st.session_state.get("app_language", "en")
    with st.popover("🌐", help=t("language_picker_label"), use_container_width=False):
        st.caption(t("language_picker_label"))
        for code, label in LANGUAGES.items():
            is_current = code == current
            if st.button(("✅ " if is_current else "") + label,
                         key=f"lang_pick_{code}{key_suffix}",
                         use_container_width=True,
                         disabled=is_current):
                st.session_state.app_language = code
                st.rerun()
