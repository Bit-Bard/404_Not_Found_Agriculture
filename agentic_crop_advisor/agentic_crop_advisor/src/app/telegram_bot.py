from __future__ import annotations

import logging
from typing import Optional

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
from .graph import CropAdvisorGraph
from .models import Advisory, GraphState
from .store import StateStore

log = logging.getLogger("telegram")


# ---------------------------
# Premium-ish Telegram UI (no "AI look")
# ---------------------------

STAGE_BUTTONS = [
    ("Sowing", "stage:sowing"),
    ("Germination", "stage:germination"),
    ("Vegetative", "stage:vegetative"),
    ("Flowering", "stage:flowering"),
    ("Fruiting", "stage:fruiting"),
    ("Maturity", "stage:maturity"),
    ("Harvest", "stage:harvest"),
]


def _stage_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(STAGE_BUTTONS), 2):
        row = [
            InlineKeyboardButton(STAGE_BUTTONS[i][0], callback_data=STAGE_BUTTONS[i][1])
        ]
        if i + 1 < len(STAGE_BUTTONS):
            row.append(InlineKeyboardButton(STAGE_BUTTONS[i + 1][0], callback_data=STAGE_BUTTONS[i + 1][1]))
        rows.append(row)
    rows.append([InlineKeyboardButton("Update Location", callback_data="action:location")])
    rows.append([InlineKeyboardButton("Report Symptoms", callback_data="action:symptoms")])
    return InlineKeyboardMarkup(rows)


def _format_advisory(state: GraphState) -> str:
    """
    Telegram-rich formatting (HTML) while staying readable on low-end devices.
    """
    adv = state.advisory
    if not adv:
        # If advisory missing, fallback to last assistant message if present.
        for m in reversed(state.messages):
            if m.get("role") == "assistant":
                return str(m.get("content", "") or "Tell me your crop and stage.")
        return "Tell me your crop and stage."

    crop = (state.context.crop or "your crop").title()
    stage = adv.stage.replace("_", " ").title()

    header = f"<b>{adv.headline}</b>\n"
    meta = f"<i>{crop} • {stage}</i>\n"
    if state.context.location_text:
        meta += f"<i>Location:</i> {state.context.location_text}\n"
    if state.weather:
        meta += f"<i>Weather:</i> {state.weather.summary}\n"
        if state.weather.alerts:
            meta += f"<b>Alerts:</b> " + ", ".join(state.weather.alerts[:3]) + "\n"

    parts = [header, meta.strip(), ""]

    if adv.actions_now:
        parts.append("<b>Do now</b>")
        for a in adv.actions_now:
            parts.append(f"• {a}")
        parts.append("")

    if adv.watch_out_for:
        parts.append("<b>Watch next</b>")
        for w in adv.watch_out_for:
            parts.append(f"• {w}")
        parts.append("")

    if adv.next_questions:
        parts.append("<b>Quick questions (to refine)</b>")
        for q in adv.next_questions:
            parts.append(f"• {q}")
        parts.append("")

    if adv.rationale_brief:
        parts.append("<b>Why this</b>")
        parts.append(adv.rationale_brief.strip())
        parts.append("")

    if adv.safety_notes:
        parts.append("<b>Safety</b>")
        for s in adv.safety_notes[:6]:
            parts.append(f"• {s}")
        parts.append("")

    footer = f"<i>Confidence:</i> {adv.confidence.upper()}"
    if adv.needs_human_review:
        footer += " • <b>Recommend local expert review</b>"
    parts.append(footer)

    return "\n".join([p for p in parts if p is not None]).strip()


def _is_allowed(settings: Settings, update: Update) -> bool:
    if not settings.telegram_allowed_user_ids:
        return True
    user = update.effective_user
    if not user:
        return False
    return user.id in settings.telegram_allowed_user_ids


def _chat_id_str(update: Update) -> str:
    chat = update.effective_chat
    return str(chat.id) if chat else "unknown"


def _short_intro() -> str:
    return (
        "<b>Farm Guide</b>\n"
        "Tell me:\n"
        "• Crop (e.g., cotton)\n"
        "• Stage (or tap a stage button)\n"
        "• Location (village/district/state OR lat,lon)\n"
        "• Symptoms (if any)\n\n"
        "<i>I will keep updating your plan as weather and stage changes.</i>"
    )


def _help_text() -> str:
    return (
        "<b>Help</b>\n"
        "Send a message like:\n"
        "• “Cotton, vegetative stage, near Wardha Maharashtra, yellowing leaves”\n"
        "Or location as:\n"
        "• “19.07,72.87”\n\n"
        "Commands:\n"
        "/start — intro\n"
        "/reset — clear your saved session\n"
        "/help — this help"
    )


# ---------------------------
# Handlers
# ---------------------------

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(settings, update):
        return

    await update.effective_message.reply_text(
        _short_intro(),
        parse_mode=ParseMode.HTML,
        reply_markup=_stage_keyboard(),
        disable_web_page_preview=True,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(settings, update):
        return

    await update.effective_message.reply_text(
        _help_text(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(settings, update):
        return

    store: StateStore = context.application.bot_data["store"]
    chat_id = _chat_id_str(update)

    # Overwrite session with fresh GraphState
    store.save(GraphState(chat_id=chat_id))

    await update.effective_message.reply_text(
        "Session reset. Send crop + stage + location to start again.",
        reply_markup=_stage_keyboard(),
        disable_web_page_preview=True,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(settings, update):
        return

    msg = update.effective_message
    if not msg or not msg.text:
        return

    chat_id = _chat_id_str(update)
    user_text = msg.text.strip()

    store: StateStore = context.application.bot_data["store"]
    graph: CropAdvisorGraph = context.application.bot_data["graph"]

    state = store.load(chat_id)

    try:
        new_state = await graph.run_turn(state, user_text=user_text)
        store.save(new_state)

        text = _format_advisory(new_state)
        await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=_stage_keyboard(),
            disable_web_page_preview=True,
        )
    except Exception:
        log.exception("Turn failed for chat_id=%s", chat_id)
        await msg.reply_text(
            "Something went wrong while generating advice. Please try again with crop + stage + location.",
            reply_markup=_stage_keyboard(),
            disable_web_page_preview=True,
        )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed(settings, update):
        return

    q = update.callback_query
    if not q or not q.data:
        return

    await q.answer()
    chat_id = _chat_id_str(update)

    store: StateStore = context.application.bot_data["store"]
    graph: CropAdvisorGraph = context.application.bot_data["graph"]

    data = q.data

    # Stage quick-set
    if data.startswith("stage:"):
        stage = data.split(":", 1)[1].strip().lower()
        state = store.load(chat_id)
        state.context = state.context.model_copy(update={"stage": stage})
        store.save(state)

        # Immediately run a turn that prompts for missing info / refreshes advice
        try:
            new_state = await graph.run_turn(state, user_text=f"My current stage is {stage}.")
            store.save(new_state)
            text = _format_advisory(new_state)
        except Exception:
            log.exception("Stage update turn failed for chat_id=%s", chat_id)
            text = "Stage updated. Now send crop + location (village/district/state or lat,lon)."

        await q.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=_stage_keyboard(),
            disable_web_page_preview=True,
        )
        return

    # Action prompts (UX)
    if data == "action:location":
        await q.message.reply_text(
            "Send your location as:\n• Village/City + District/State\nOR\n• lat,lon (example: 19.07,72.87)",
            reply_markup=_stage_keyboard(),
            disable_web_page_preview=True,
        )
        return

    if data == "action:symptoms":
        await q.message.reply_text(
            "Describe symptoms briefly:\n• What you see (spots/yellowing/wilting)\n• Since how many days\n• Irrigation frequency",
            reply_markup=_stage_keyboard(),
            disable_web_page_preview=True,
        )
        return


# ---------------------------
# App builder
# ---------------------------

def build_telegram_app(settings: Settings) -> Application:
    """
    Build Telegram app, with bot_data containing:
      - settings
      - store
      - graph
    """
    store = StateStore.from_settings(settings)
    graph = CropAdvisorGraph.create(settings)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .build()
    )

    app.bot_data["settings"] = settings
    app.bot_data["store"] = store
    app.bot_data["graph"] = graph

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    return app
