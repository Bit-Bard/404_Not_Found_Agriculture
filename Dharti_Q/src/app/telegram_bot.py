from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import Settings
from .graph import (
    CropAdvisorGraph,
    ACTION_CROP_RECO,
    ACTION_DIGEST,
    ACTION_MARKET,
    ACTION_SCHEMES,
    ACTION_SET_LANG_PREFIX,
)
from .models import GraphState, ImageAsset
from .store import StateStore

log = logging.getLogger("telegram")

# Default 24h; for testing set DIGEST_INTERVAL_SECONDS=60 in .env
DIGEST_INTERVAL_SECONDS = int(os.getenv("DIGEST_INTERVAL_SECONDS", "86400"))
DIGEST_FIRST_DELAY_SECONDS = int(os.getenv("DIGEST_FIRST_DELAY_SECONDS", "10"))

ACTION_BUY = "__ACTION__:BUY"  # handled by graph.py buy node


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _chat_id_str(update: Update) -> str:
    chat = update.effective_chat
    return str(chat.id) if chat else "unknown"


_UI = {
    "en": {
        "intro_title": "Farm Guide",
        "intro_body": "Send: name, crop+stage, land, location. Upload crop photo for diagnosis.",
        "help": "Example: My name is Ramesh. Rice germination. 2 acres. Pune.\n/start /profile /reset /help /location",
        "profile": "Copy+edit:\n\nMy name is ___\nCrop: ___\nStage: ___\nLand: ___ acres/hectare\nLocation: ___ (or 19.07,72.87)\n",
        "reset_ok": "Session reset. Send your profile again.",
        "err_generic": "Error. Send: name + crop + stage + land + location.",
        "photo_fail": "Image failed. Send a clear photo + short caption (symptoms + days).",
        "ask_location": "Send location: City/Village + District/State OR lat,lon (19.07,72.87)",
        "ask_symptoms": "Symptoms: what you see + since how many days + irrigation frequency",
        "schemes_fail": "Schemes not available now. Set location + crop then try again.",
        "market_fail": "Market snapshot not available now. Try again in a moment.",

        "btn_profile": "Set Profile",
        "btn_location": "Update Location",
        "btn_symptoms": "Report Symptoms",
        "btn_crop_reco": "Crop Suggestions",
        "btn_buy": "Buy Inputs",
        "btn_schemes": "Govt Schemes",
        "btn_market": "Market Prices",

        "btn_share_location": "Share Location",
        "share_location_prompt": "To give hyper-local advice, please share your exact location (tap button).",
        "location_saved": "Location saved. Generating updated advice…",

        "sec_photo": "Photo",
        "sec_do": "Do now",
        "sec_watch": "Watch",
        "sec_schemes": "Govt Schemes",
        "sec_market": "Market Prices",
        "sec_links": "Links",
        "sec_conf": "Conf",

        "stage_sowing": "Sowing",
        "stage_germination": "Germination",
        "stage_vegetative": "Vegetative",
        "stage_flowering": "Flowering",
        "stage_fruiting": "Fruiting",
        "stage_maturity": "Maturity",
        "stage_harvest": "Harvest",

        "digest_title": "Daily update",
        "market_note": "Note: Web snapshot—confirm at your local mandi.",
    },
    "hi": {
        "intro_title": "कृषि मार्गदर्शक",
        "intro_body": "नाम, फसल+चरण, जमीन, स्थान भेजें। फोटो अपलोड करें।",
        "help": "उदाहरण: मेरा नाम रमेश। धान अंकुरण। 2 एकड़। पुणे।\n/start /profile /reset /help /location",
        "profile": "कॉपी+एडिट:\n\nमेरा नाम ___\nफसल: ___\nचरण: ___\nजमीन: ___\nस्थान: ___ (या 19.07,72.87)\n",
        "reset_ok": "सेशन रीसेट। प्रोफाइल फिर भेजें।",
        "err_generic": "त्रुटि। भेजें: नाम + फसल + चरण + जमीन + स्थान।",
        "photo_fail": "फोटो प्रोसेस नहीं हुई। साफ फोटो + छोटा कैप्शन भेजें।",
        "ask_location": "स्थान: शहर/गांव + जिला/राज्य या lat,lon (19.07,72.87)",
        "ask_symptoms": "लक्षण: क्या दिख रहा + कितने दिन + सिंचाई कितनी बार",
        "schemes_fail": "अभी योजना नहीं मिल रही। पहले स्थान + फसल सेट करें।",
        "market_fail": "बाजार जानकारी अभी नहीं मिल रही। थोड़ी देर बाद कोशिश करें।",

        "btn_profile": "प्रोफाइल सेट",
        "btn_location": "स्थान अपडेट",
        "btn_symptoms": "लक्षण रिपोर्ट",
        "btn_crop_reco": "फसल सुझाव",
        "btn_buy": "खरीद लिंक",
        "btn_schemes": "सरकारी योजनाएँ",
        "btn_market": "बाजार भाव",

        "btn_share_location": "लोकेशन भेजें",
        "share_location_prompt": "सटीक सलाह के लिए कृपया अपनी लोकेशन भेजें (बटन दबाएँ)।",
        "location_saved": "लोकेशन सेव हो गई। नया सलाह तैयार कर रहे हैं…",

        "sec_photo": "फोटो",
        "sec_do": "अभी करें",
        "sec_watch": "ध्यान रखें",
        "sec_schemes": "सरकारी योजनाएँ",
        "sec_market": "बाजार भाव",
        "sec_links": "लिंक",
        "sec_conf": "विश्वास",

        "stage_sowing": "बुवाई",
        "stage_germination": "अंकुरण",
        "stage_vegetative": "वृद्धि",
        "stage_flowering": "फूल",
        "stage_fruiting": "फल",
        "stage_maturity": "पकना",
        "stage_harvest": "कटाई",

        "digest_title": "दैनिक अपडेट",
        "market_note": "नोट: वेब स्नैपशॉट—स्थानीय मंडी में पुष्टि करें।",
    },
    "mr": {
        "intro_title": "कृषी मार्गदर्शक",
        "intro_body": "नाव, पीक+अवस्था, जमीन, ठिकाण पाठवा. फोटो अपलोड करा.",
        "help": "उदा: माझं नाव रमेश. भात अंकुरण. 2 एकर. पुणे.\n/start /profile /reset /help /location",
        "profile": "कॉपी+एडिट:\n\nमाझं नाव ___\nपीक: ___\nअवस्था: ___\nजमीन: ___\nठिकाण: ___ (किंवा 19.07,72.87)\n",
        "reset_ok": "सेशन रीसेट. प्रोफाइल पुन्हा पाठवा.",
        "err_generic": "त्रुटी. पाठवा: नाव + पीक + अवस्था + जमीन + ठिकाण.",
        "photo_fail": "फोटो प्रोसेस नाही झाली. स्पष्ट फोटो + छोटं कॅप्शन पाठवा.",
        "ask_location": "ठिकाण: शहर/गाव + जिल्हा/राज्य किंवा lat,lon (19.07,72.87)",
        "ask_symptoms": "लक्षणं: काय दिसतं + किती दिवस + पाणी किती वेळा",
        "schemes_fail": "आत्ता योजना मिळत नाहीत. आधी ठिकाण + पीक सेट करा.",
        "market_fail": "बाजार माहिती आत्ता मिळत नाही. थोड्या वेळाने प्रयत्न करा.",

        "btn_profile": "प्रोफाइल सेट",
        "btn_location": "ठिकाण अपडेट",
        "btn_symptoms": "लक्षणं रिपोर्ट",
        "btn_crop_reco": "पीक सुचना",
        "btn_buy": "खरेदी लिंक",
        "btn_schemes": "सरकारी योजना",
        "btn_market": "बाजार भाव",

        "btn_share_location": "लोकेशन पाठवा",
        "share_location_prompt": "अचूक सल्ल्यासाठी कृपया तुमची लोकेशन पाठवा (बटन दाबा).",
        "location_saved": "लोकेशन सेव झाली. अपडेटेड सल्ला तयार करतोय…",

        "sec_photo": "फोटो",
        "sec_do": "आत्ता करा",
        "sec_watch": "पहा",
        "sec_schemes": "सरकारी योजना",
        "sec_market": "बाजार भाव",
        "sec_links": "लिंक्स",
        "sec_conf": "विश्वास",

        "stage_sowing": "पेरणी",
        "stage_germination": "अंकुरण",
        "stage_vegetative": "वाढ",
        "stage_flowering": "फुलोरा",
        "stage_fruiting": "फळ",
        "stage_maturity": "पक्वता",
        "stage_harvest": "कापणी",

        "digest_title": "दैनिक अपडेट",
        "market_note": "नोट: वेब स्नॅपशॉट—स्थानिक मंडीत पडताळा.",
    },
}


def _ui(lang: str, key: str) -> str:
    lang = (lang or "en").strip().lower()
    if lang not in _UI:
        lang = "en"
    return _UI[lang].get(key, _UI["en"].get(key, ""))


def _keyboard(lang: str) -> InlineKeyboardMarkup:
    lang = (lang or "en").strip().lower()
    if lang not in _UI:
        lang = "en"

    lang_row = [
        InlineKeyboardButton("English", callback_data="lang:en"),
        InlineKeyboardButton("हिंदी", callback_data="lang:hi"),
        InlineKeyboardButton("मराठी", callback_data="lang:mr"),
    ]

    stage_rows = [
        [
            InlineKeyboardButton(_ui(lang, "stage_sowing"), callback_data="stage:sowing"),
            InlineKeyboardButton(_ui(lang, "stage_germination"), callback_data="stage:germination"),
        ],
        [
            InlineKeyboardButton(_ui(lang, "stage_vegetative"), callback_data="stage:vegetative"),
            InlineKeyboardButton(_ui(lang, "stage_flowering"), callback_data="stage:flowering"),
        ],
        [
            InlineKeyboardButton(_ui(lang, "stage_fruiting"), callback_data="stage:fruiting"),
            InlineKeyboardButton(_ui(lang, "stage_maturity"), callback_data="stage:maturity"),
        ],
        [InlineKeyboardButton(_ui(lang, "stage_harvest"), callback_data="stage:harvest")],
    ]

    action_rows = [
        [InlineKeyboardButton(_ui(lang, "btn_profile"), callback_data="action:profile")],
        [InlineKeyboardButton(_ui(lang, "btn_location"), callback_data="action:location")],
        [InlineKeyboardButton(_ui(lang, "btn_symptoms"), callback_data="action:symptoms")],
        [
            InlineKeyboardButton(_ui(lang, "btn_crop_reco"), callback_data="action:crop_reco"),
            InlineKeyboardButton(_ui(lang, "btn_buy"), callback_data="action:buy"),
        ],
        [
            InlineKeyboardButton(_ui(lang, "btn_schemes"), callback_data="action:schemes"),
            InlineKeyboardButton(_ui(lang, "btn_market"), callback_data="action:market"),
        ],
    ]

    return InlineKeyboardMarkup([lang_row, *stage_rows, *action_rows])


def _location_request_keyboard(lang: str) -> ReplyKeyboardMarkup:
    btn = KeyboardButton(_ui(lang, "btn_share_location"), request_location=True)
    return ReplyKeyboardMarkup(
        [[btn]],
        resize_keyboard=True,
        one_time_keyboard=True,
        input_field_placeholder=_ui(lang, "btn_share_location"),
    )


def _short_intro(lang: str) -> str:
    return f"<b>{_ui(lang,'intro_title')}</b>\n{_ui(lang,'intro_body')}"


def _profile_template(lang: str) -> str:
    return f"<b>{_ui(lang,'btn_profile')}</b>\n{_ui(lang,'profile')}"


def _help_text(lang: str) -> str:
    return f"<b>Help</b>\n{_ui(lang,'help')}"


def _last_user_text(state: GraphState) -> str:
    for m in reversed(state.messages):
        if m.get("role") == "user":
            return str(m.get("content", "") or "").strip()
    return ""


def _render_schemes_only(state: GraphState) -> str:
    lang = state.context.language
    title = f"<b>{_ui(lang,'sec_schemes')}</b>"
    crop = (state.context.crop or "crop").title()
    loc = (state.context.location_text or "").strip() or "—"

    if not state.schemes or not state.schemes.snippets:
        return f"{title}\n<i>{crop} • {loc}</i>\n\n{_ui(lang,'schemes_fail')}"

    lines = [title, f"<i>{crop} • {loc}</i>", ""]
    for s in state.schemes.snippets[:3]:
        lines.append(f"• {s}")
    if state.schemes.urls:
        lines.append("")
        lines.append(f"<i>{_ui(lang,'sec_links')}:</i>")
        for u in state.schemes.urls[:3]:
            lines.append(f"• {u}")
    return "\n".join(lines).strip()


def _render_market_only(state: GraphState) -> str:
    lang = state.context.language
    title = f"<b>{_ui(lang,'sec_market')}</b>"
    crop = (state.context.crop or "crop").title()
    loc = (state.context.location_text or "").strip() or "—"

    if not state.market or not state.market.snippets:
        return f"{title}\n<i>{crop} • {loc}</i>\n\n{_ui(lang,'market_fail')}"

    lines = [title, f"<i>{crop} • {loc}</i>", ""]
    for s in state.market.snippets[:3]:
        lines.append(f"• {s}")
    if state.market.urls:
        lines.append("")
        lines.append(f"<i>{_ui(lang,'sec_links')}:</i>")
        for u in state.market.urls[:3]:
            lines.append(f"• {u}")
    lines.append("")
    lines.append(f"<i>{_ui(lang,'market_note')}</i>")
    return "\n".join(lines).strip()


def _format_advisory(state: GraphState, *, digest: bool = False) -> str:
    lang = state.context.language
    last_user = _last_user_text(state)

    if last_user == ACTION_SCHEMES:
        return _render_schemes_only(state)
    if last_user == ACTION_MARKET:
        return _render_market_only(state)

    if not state.advisory:
        for m in reversed(state.messages):
            if m.get("role") == "assistant":
                return str(m.get("content", "") or _ui(lang, "err_generic"))
        return _ui(lang, "err_generic")

    adv = state.advisory
    crop = (state.context.crop or "Crop").title()
    stage = adv.stage.replace("_", " ").title()

    parts: list[str] = []
    if digest:
        parts.append(f"<b>{_ui(lang,'digest_title')}</b>")

    parts.append(f"<b>{adv.headline}</b>")
    parts.append(f"<i>{crop} • {stage}</i>")

    if state.weather and state.weather.summary:
        parts.append(f"<i>Weather:</i> {state.weather.summary}")

    if state.image_diagnosis:
        d = state.image_diagnosis
        parts.append(f"\n<b>{_ui(lang,'sec_photo')}</b>")
        parts.append(f"• {d.issue}")

    if adv.actions_now:
        parts.append(f"\n<b>{_ui(lang,'sec_do')}</b>")
        for a in adv.actions_now[:6]:
            parts.append(f"• {a}")

    if adv.watch_out_for:
        parts.append(f"\n<b>{_ui(lang,'sec_watch')}</b>")
        for w in adv.watch_out_for[:3]:
            parts.append(f"• {w}")

    parts.append(f"\n<i>{_ui(lang,'sec_conf')}:</i> {adv.confidence.upper()}")
    if adv.needs_human_review or (state.image_diagnosis and state.image_diagnosis.needs_human_review):
        parts[-1] += " • <b>Expert review</b>"

    return "\n".join(parts).strip()


def _ensure_digest_job(context: ContextTypes.DEFAULT_TYPE, chat_id: str) -> None:
    name = f"digest:{chat_id}"
    if context.application.job_queue.get_jobs_by_name(name):
        return
    context.application.job_queue.run_repeating(
        _digest_job,
        interval=DIGEST_INTERVAL_SECONDS,
        first=DIGEST_FIRST_DELAY_SECONDS,
        name=name,
        data={"chat_id": chat_id},
    )


async def _digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    store: StateStore = context.application.bot_data["store"]
    graph: CropAdvisorGraph = context.application.bot_data["graph"]

    data = getattr(context.job, "data", {}) or {}
    chat_id = str(data.get("chat_id", "")).strip()
    if not chat_id:
        return

    try:
        state = store.load(chat_id)
        new_state = await graph.run_turn(state, user_text=ACTION_DIGEST)
        store.save(new_state)

        await context.bot.send_message(
            chat_id=int(chat_id),
            text=_format_advisory(new_state, digest=True),
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(new_state.context.language),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Digest job failed for chat_id=%s", chat_id)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: StateStore = context.application.bot_data["store"]
    chat_id = _chat_id_str(update)
    state = store.load(chat_id)
    lang = state.context.language

    _ensure_digest_job(context, chat_id)

    await update.effective_message.reply_text(
        _ui(lang, "share_location_prompt"),
        reply_markup=_location_request_keyboard(lang),
        disable_web_page_preview=True,
    )

    await update.effective_message.reply_text(
        _short_intro(lang),
        parse_mode=ParseMode.HTML,
        reply_markup=_keyboard(lang),
        disable_web_page_preview=True,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: StateStore = context.application.bot_data["store"]
    chat_id = _chat_id_str(update)
    state = store.load(chat_id)
    lang = state.context.language

    _ensure_digest_job(context, chat_id)

    await update.effective_message.reply_text(
        _help_text(lang),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def profile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: StateStore = context.application.bot_data["store"]
    chat_id = _chat_id_str(update)
    state = store.load(chat_id)
    lang = state.context.language

    _ensure_digest_job(context, chat_id)

    await update.effective_message.reply_text(
        _profile_template(lang),
        parse_mode=ParseMode.HTML,
        reply_markup=_keyboard(lang),
        disable_web_page_preview=True,
    )


async def location_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: StateStore = context.application.bot_data["store"]
    chat_id = _chat_id_str(update)
    state = store.load(chat_id)
    lang = state.context.language

    await update.effective_message.reply_text(
        _ui(lang, "share_location_prompt"),
        reply_markup=_location_request_keyboard(lang),
        disable_web_page_preview=True,
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store: StateStore = context.application.bot_data["store"]
    chat_id = _chat_id_str(update)
    store.save(GraphState(chat_id=chat_id))
    await update.effective_message.reply_text(
        _ui("en", "reset_ok"),
        reply_markup=_keyboard("en"),
        disable_web_page_preview=True,
    )


async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.location:
        return

    settings: Settings = context.application.bot_data["settings"]
    store: StateStore = context.application.bot_data["store"]
    graph: CropAdvisorGraph = context.application.bot_data["graph"]

    chat_id = _chat_id_str(update)
    _ensure_digest_job(context, chat_id)

    state = store.load(chat_id)
    lang = state.context.language

    lat = float(msg.location.latitude)
    lon = float(msg.location.longitude)

    state.context = state.context.model_copy(
        update={
            "lat": lat,
            "lon": lon,
            "location_text": state.context.location_text or f"{lat:.5f},{lon:.5f}",
        }
    )
    store.save(state)

    await msg.reply_text(
        _ui(lang, "location_saved"),
        reply_markup=ReplyKeyboardRemove(),
        disable_web_page_preview=True,
    )

    try:
        new_state = await graph.run_turn(state, user_text=f"{lat:.5f},{lon:.5f}")
        store.save(new_state)
        await msg.reply_text(
            _format_advisory(new_state),
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(new_state.context.language),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Location turn failed chat_id=%s", chat_id)
        await msg.reply_text(_ui(lang, "err_generic"), reply_markup=_keyboard(lang))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.text:
        return

    chat_id = _chat_id_str(update)
    store: StateStore = context.application.bot_data["store"]
    graph: CropAdvisorGraph = context.application.bot_data["graph"]

    _ensure_digest_job(context, chat_id)

    state = store.load(chat_id)
    lang = state.context.language

    try:
        new_state = await graph.run_turn(state, user_text=msg.text.strip())
        store.save(new_state)
        await msg.reply_text(
            _format_advisory(new_state),
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(new_state.context.language),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Turn failed for chat_id=%s", chat_id)
        await msg.reply_text(
            _ui(lang, "err_generic"),
            reply_markup=_keyboard(lang),
            disable_web_page_preview=True,
        )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or not msg.photo:
        return

    settings: Settings = context.application.bot_data["settings"]
    store: StateStore = context.application.bot_data["store"]
    graph: CropAdvisorGraph = context.application.bot_data["graph"]

    chat_id = _chat_id_str(update)
    _ensure_digest_job(context, chat_id)

    state = store.load(chat_id)
    lang = state.context.language

    caption = (msg.caption or "").strip()
    photo = msg.photo[-1]
    file_id = photo.file_id

    try:
        tg_file = await context.bot.get_file(file_id)
        chat_dir = Path(settings.media_dir) / chat_id
        chat_dir.mkdir(parents=True, exist_ok=True)
        local_path = chat_dir / f"{photo.file_unique_id}_{int(datetime.now().timestamp())}.jpg"
        await tg_file.download_to_drive(custom_path=str(local_path))

        store.save_image_record(
            chat_id=chat_id,
            file_path=str(local_path),
            caption=caption or None,
            telegram_file_id=file_id,
        )

        state.last_image = ImageAsset(
            file_path=str(local_path),
            telegram_file_id=file_id,
            caption=caption or None,
            created_at_utc=_utc_now_iso(),
        )
        state.image_diagnosis = None
        store.save(state)

        user_text = caption if caption else "Analyze this crop photo and suggest safe remedy steps."
        new_state = await graph.run_turn(state, user_text=user_text)
        store.save(new_state)

        await msg.reply_text(
            _format_advisory(new_state),
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(new_state.context.language),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Photo processing failed chat_id=%s", chat_id)
        await msg.reply_text(
            _ui(lang, "photo_fail"),
            reply_markup=_keyboard(lang),
            disable_web_page_preview=True,
        )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data:
        return
    await q.answer()

    store: StateStore = context.application.bot_data["store"]
    graph: CropAdvisorGraph = context.application.bot_data["graph"]

    chat_id = _chat_id_str(update)
    _ensure_digest_job(context, chat_id)

    state = store.load(chat_id)
    lang = state.context.language
    data = q.data

    if data.startswith("lang:"):
        new_lang = data.split(":", 1)[1].strip().lower()
        state.context = state.context.model_copy(update={"language": new_lang})
        store.save(state)
        try:
            new_state = await graph.run_turn(state, user_text=f"{ACTION_SET_LANG_PREFIX}{new_lang}")
            store.save(new_state)
            out = _format_advisory(new_state)
            kb = _keyboard(new_state.context.language)
        except Exception:
            log.exception("Language switch failed chat_id=%s", chat_id)
            out = "Language updated."
            kb = _keyboard(new_lang)

        await q.message.reply_text(out, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
        return

    if data.startswith("stage:"):
        stage = data.split(":", 1)[1].strip().lower()
        state.context = state.context.model_copy(update={"stage": stage})
        state.advisory = None
        store.save(state)
        try:
            new_state = await graph.run_turn(state, user_text=f"My stage is {stage}.")
            store.save(new_state)
            await q.message.reply_text(
                _format_advisory(new_state),
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(new_state.context.language),
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Stage update failed chat_id=%s", chat_id)
            await q.message.reply_text(_ui(lang, "err_generic"), reply_markup=_keyboard(lang))
        return

    if data == "action:profile":
        await q.message.reply_text(
            _profile_template(state.context.language),
            parse_mode=ParseMode.HTML,
            reply_markup=_keyboard(state.context.language),
            disable_web_page_preview=True,
        )
        return

    if data == "action:location":
        await q.message.reply_text(
            _ui(lang, "share_location_prompt"),
            reply_markup=_location_request_keyboard(lang),
            disable_web_page_preview=True,
        )
        await q.message.reply_text(_ui(lang, "ask_location"), reply_markup=_keyboard(lang))
        return

    if data == "action:symptoms":
        await q.message.reply_text(_ui(lang, "ask_symptoms"), reply_markup=_keyboard(lang))
        return

    if data == "action:crop_reco":
        try:
            state.advisory = None
            store.save(state)
            new_state = await graph.run_turn(state, user_text=ACTION_CROP_RECO)
            store.save(new_state)
            await q.message.reply_text(
                _format_advisory(new_state),
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(new_state.context.language),
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Crop reco failed chat_id=%s", chat_id)
            await q.message.reply_text(_ui(lang, "err_generic"), reply_markup=_keyboard(lang))
        return

    if data == "action:buy":
        # ✅ NOW calls graph buy node
        try:
            state.advisory = None
            store.save(state)
            new_state = await graph.run_turn(state, user_text=ACTION_BUY)
            store.save(new_state)
            await q.message.reply_text(
                _format_advisory(new_state),
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(new_state.context.language),
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Buy inputs failed chat_id=%s", chat_id)
            await q.message.reply_text(_ui(lang, "err_generic"), reply_markup=_keyboard(lang))
        return

    if data == "action:schemes":
        try:
            state.advisory = None
            store.save(state)
            new_state = await graph.run_turn(state, user_text=ACTION_SCHEMES)
            store.save(new_state)
            await q.message.reply_text(
                _format_advisory(new_state),
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(new_state.context.language),
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Schemes failed chat_id=%s", chat_id)
            await q.message.reply_text(_ui(lang, "schemes_fail"), reply_markup=_keyboard(lang))
        return

    if data == "action:market":
        try:
            state.advisory = None
            store.save(state)
            new_state = await graph.run_turn(state, user_text=ACTION_MARKET)
            store.save(new_state)
            await q.message.reply_text(
                _format_advisory(new_state),
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(new_state.context.language),
                disable_web_page_preview=True,
            )
        except Exception:
            log.exception("Market failed chat_id=%s", chat_id)
            await q.message.reply_text(_ui(lang, "market_fail"), reply_markup=_keyboard(lang))
        return


def build_telegram_app(settings: Settings) -> Application:
    store = StateStore.from_settings(settings)
    graph = CropAdvisorGraph.create(settings)

    app = Application.builder().token(settings.telegram_bot_token).build()

    app.bot_data["settings"] = settings
    app.bot_data["store"] = store
    app.bot_data["graph"] = graph

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("profile", profile_cmd))
    app.add_handler(CommandHandler("location", location_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))

    app.add_handler(CallbackQueryHandler(on_button))

    # location handler MUST be before text handler
    app.add_handler(MessageHandler(filters.LOCATION, on_location))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app
