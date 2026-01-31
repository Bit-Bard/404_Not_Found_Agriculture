from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from langgraph.graph import END, StateGraph
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from .config import Settings
from .models import (
    Advisory,
    FarmerContext,
    GraphState,
    ImageDiagnosis,
    Observation,
    safe_parse_advisory,
)
from .tools import ToolBundle, ToolError, extract_lat_lon

log = logging.getLogger("graph")


_LANG_NAME = {"en": "English", "hi": "Hindi", "mr": "Marathi"}

ACTION_PREFIX = "__ACTION__:"
ACTION_SCHEMES = "__ACTION__:SCHEMES"
ACTION_MARKET = "__ACTION__:MARKET"
ACTION_DIGEST = "__ACTION__:DIGEST"
ACTION_CROP_RECO = "__ACTION__:CROP_RECO"
ACTION_BUY = "__ACTION__:BUY"
ACTION_SET_LANG_PREFIX = "__ACTION__:SET_LANG:"  # __ACTION__:SET_LANG:hi

_ALLOWED_STAGES = {
    "unknown",
    "pre_sowing",
    "sowing",
    "germination",
    "vegetative",
    "flowering",
    "fruiting",
    "maturity",
    "harvest",
    "post_harvest",
}

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)

_STAGE_UPDATE_RE = re.compile(r"^\s*my\s+stage\s+is\s+([a-z_]+)\.?\s*$", re.IGNORECASE)
_STAGE_INLINE_RE = re.compile(r"^\s*stage\s*:\s*([a-z_]+)\s*$", re.IGNORECASE)


class IntakeExtraction(BaseModel):
    farmer_name: Optional[str] = None
    land_size: Optional[float] = None
    land_unit: Optional[str] = None
    crop: Optional[str] = None
    stage: Optional[str] = None
    location_text: Optional[str] = None
    sowing_date: Optional[str] = None
    irrigation: Optional[str] = None
    soil_type: Optional[str] = None
    notes: Optional[str] = None
    symptoms: list[str] = Field(default_factory=list)
    pests_seen: list[str] = Field(default_factory=list)
    urgency: Optional[str] = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _last_user_text(state: GraphState) -> str:
    for m in reversed(state.messages):
        if m.get("role") == "user":
            return str(m.get("content", "") or "").strip()
    return ""


def _is_action_message(text: str) -> bool:
    return bool(text) and text.startswith(ACTION_PREFIX)


def _extract_stage_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    m = _STAGE_UPDATE_RE.match(text) or _STAGE_INLINE_RE.match(text)
    if not m:
        return None
    stage = (m.group(1) or "").strip().lower()
    return stage if stage in _ALLOWED_STAGES else None


def _is_stage_update_message(text: str) -> bool:
    return _extract_stage_from_text(text) is not None


def _user_wants_crop_reco(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower()
    if t == ACTION_CROP_RECO:
        return True
    # English
    if any(k in t for k in ("recommend crop", "suggest crop", "which crop", "what crop", "crop suggestion", "crop recommendations")):
        return True
    # Hindi
    if any(k in t for k in ("कौन सी फसल", "फसल सुझाव", "फसल बताओ", "फसल recommend")):
        return True
    # Marathi
    if any(k in t for k in ("कोणते पीक", "पीक सुचवा", "पीक recommendation", "पीक सुचना")):
        return True
    return False


def _wants_schemes(state: GraphState) -> bool:
    t = _last_user_text(state)
    return t in (ACTION_SCHEMES, ACTION_DIGEST)


def _wants_market(state: GraphState) -> bool:
    t = _last_user_text(state)
    return t in (ACTION_MARKET, ACTION_DIGEST)


def _wants_buy(state: GraphState) -> bool:
    return _last_user_text(state) == ACTION_BUY


def _is_weather_stale(state: GraphState, *, max_age_hours: int = 6) -> bool:
    if not state.weather:
        return True
    dt = _parse_iso_dt(state.weather.fetched_at_utc)
    return True if not dt else (_utc_now() - dt) > timedelta(hours=max_age_hours)


def _is_web_stale(state: GraphState, *, max_age_hours: int = 24) -> bool:
    if not state.web:
        return True
    dt = _parse_iso_dt(state.web.fetched_at_utc)
    return True if not dt else (_utc_now() - dt) > timedelta(hours=max_age_hours)


def _is_schemes_stale(state: GraphState, *, max_age_days: int = 7) -> bool:
    if not state.schemes:
        return True
    dt = _parse_iso_dt(state.schemes.fetched_at_utc)
    return True if not dt else (_utc_now() - dt) > timedelta(days=max_age_days)


def _is_market_stale(state: GraphState, *, max_age_hours: int = 12) -> bool:
    if not state.market:
        return True
    dt = _parse_iso_dt(state.market.fetched_at_utc)
    return True if not dt else (_utc_now() - dt) > timedelta(hours=max_age_hours)


def _extract_json_object(text: str) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    m = _JSON_OBJ_RE.search(text)
    return m.group(0) if m else None


def _split_lines_to_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        parts = re.split(r"[\n•\-]+", v.strip())
        return [p.strip() for p in parts if p.strip()]
    return []


def _coerce_image_diagnosis(data: dict[str, Any]) -> ImageDiagnosis:
    issue = None
    for k in ("issue", "problem", "disease", "diagnosis", "issue_detected", "observation"):
        v = data.get(k)
        if isinstance(v, str) and v.strip():
            issue = v.strip()
            break
    if not issue:
        issue = "Unclear issue from image (needs clearer photo or more details)."

    likely_causes = _split_lines_to_list(data.get("likely_causes") or data.get("causes"))[:3]
    actions_now = _split_lines_to_list(data.get("actions_now") or data.get("actions") or data.get("remedy"))[:3]
    watch_out_for = _split_lines_to_list(data.get("watch_out_for") or data.get("precautions") or data.get("watch"))[:2]

    conf = str(data.get("confidence") or "medium").strip().lower()
    if conf not in ("low", "medium", "high"):
        conf = "medium"

    needs = data.get("needs_human_review")
    if isinstance(needs, str):
        needs = needs.strip().lower() in ("true", "yes", "1")
    if not isinstance(needs, bool):
        needs = False

    return ImageDiagnosis(
        issue=issue,
        likely_causes=likely_causes,
        actions_now=actions_now,
        watch_out_for=watch_out_for,
        confidence=conf,
        needs_human_review=needs,
    )


async def _llm_json(
    client: AsyncOpenAI,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.2,
    max_tries: int = 2,
) -> dict[str, Any]:
    last_err: Optional[Exception] = None
    for attempt in range(1, max_tries + 1):
        try:
            resp = await client.responses.create(
                model=model,
                input=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=temperature,
            )
            text = (resp.output_text or "").strip()
            js = _extract_json_object(text) or ""
            data = json.loads(js)
            if isinstance(data, dict):
                return data
            raise ValueError("Model JSON was not an object.")
        except Exception as e:
            last_err = e
            log.warning("LLM JSON parse attempt %s/%s failed: %s", attempt, max_tries, str(e))
    raise RuntimeError("LLM JSON parsing failed.") from last_err


def _merge_context(old: FarmerContext, upd: IntakeExtraction) -> FarmerContext:
    data = old.model_dump(mode="json")

    if upd.farmer_name and upd.farmer_name.strip():
        data["farmer_name"] = upd.farmer_name.strip()

    if upd.land_size is not None:
        data["land_size"] = float(upd.land_size)

    if upd.land_unit and upd.land_unit.strip():
        data["land_unit"] = upd.land_unit.strip()

    if upd.crop and upd.crop.strip():
        data["crop"] = upd.crop.strip().lower()

    if upd.stage and upd.stage.strip():
        stage = upd.stage.strip().lower()
        data["stage"] = stage if stage in _ALLOWED_STAGES else data.get("stage", "unknown")

    if upd.location_text and upd.location_text.strip():
        data["location_text"] = upd.location_text.strip()

    if upd.sowing_date and upd.sowing_date.strip():
        data["sowing_date"] = upd.sowing_date.strip()

    if upd.irrigation and upd.irrigation.strip():
        data["irrigation"] = upd.irrigation.strip()

    if upd.soil_type and upd.soil_type.strip():
        data["soil_type"] = upd.soil_type.strip()

    if upd.notes and upd.notes.strip():
        data["notes"] = upd.notes.strip()

    return FarmerContext.model_validate(data)


def _merge_observation(old: Observation, upd: IntakeExtraction) -> Observation:
    symptoms = list(old.symptoms)
    pests = list(old.pests_seen)

    for s in upd.symptoms or []:
        s = (s or "").strip()
        if s and s.lower() not in {x.lower() for x in symptoms}:
            symptoms.append(s)

    for p in upd.pests_seen or []:
        p = (p or "").strip()
        if p and p.lower() not in {x.lower() for x in pests}:
            pests.append(p)

    urgency = old.urgency
    if upd.urgency:
        u = upd.urgency.strip().lower()
        if u in {"low", "medium", "high"}:
            order = {"low": 0, "medium": 1, "high": 2}
            urgency = u if order[u] > order[urgency] else urgency

    return Observation(symptoms=symptoms, pests_seen=pests, urgency=urgency)


def _needs_profile_questions(state: GraphState) -> bool:
    c = state.context
    if not c.farmer_name:
        return True
    if not (c.lat and c.lon) and not (c.location_text and c.location_text.strip()):
        return True
    if not c.crop:
        return True
    if c.stage == "unknown":
        return True
    if c.land_size is None:
        return True
    return False


def _has_location(state: GraphState) -> bool:
    c = state.context
    return bool((c.lat and c.lon) or (c.location_text and c.location_text.strip()))


def _route(state: GraphState) -> str:
    c = state.context
    last_user = _last_user_text(state)

    # Buy should override everything (button action)
    if last_user == ACTION_BUY:
        return "buy"

    # Crop reco button should work anytime if location exists
    if last_user == ACTION_CROP_RECO and _has_location(state):
        return "crop_reco"

    # Stage-only update → go straight to advice
    if _is_stage_update_message(last_user):
        return "advice"

    # If crop missing but location exists: auto recommend once
    if (not c.crop) and _has_location(state) and last_user != ACTION_DIGEST:
        if state.last_node != "crop_reco" or _user_wants_crop_reco(last_user):
            return "crop_reco"
        return "ask"

    if state.last_image and not state.image_diagnosis:
        return "vision"

    if _needs_profile_questions(state) and last_user != ACTION_DIGEST:
        return "ask"

    if c.lat and c.lon and _is_weather_stale(state):
        return "weather"

    if (state.observation.symptoms and _is_web_stale(state)) or (last_user == ACTION_DIGEST and _is_web_stale(state)):
        return "web"

    if _wants_schemes(state) and _is_schemes_stale(state):
        return "schemes"

    if _wants_market(state) and _is_market_stale(state):
        return "market"

    return "advice"


def _data_url_from_file(path: str) -> str:
    p = Path(path)
    mime, _ = mimetypes.guess_type(str(p))
    if not mime:
        mime = "image/jpeg"
    b = p.read_bytes()
    b64 = base64.b64encode(b).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _normalize_to_jsonable(x: Any) -> Any:
    if isinstance(x, BaseModel):
        return x.model_dump(mode="json")
    if isinstance(x, dict):
        return {k: _normalize_to_jsonable(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_normalize_to_jsonable(v) for v in x]
    return x


def _deep_merge(base: dict[str, Any], upd: dict[str, Any]) -> dict[str, Any]:
    for k, v in upd.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            base[k] = _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _t(lang: str, en: str, hi: str, mr: str) -> str:
    if lang == "hi":
        return hi
    if lang == "mr":
        return mr
    return en


def _build_compiled_graph(*, settings: Settings, client: AsyncOpenAI, tools: ToolBundle):
    sg = StateGraph(GraphState)

    async def intake_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "intake"

        last_user = _last_user_text(s)

        # Deterministic stage update
        stage_direct = _extract_stage_from_text(last_user)
        if stage_direct:
            s.context = s.context.model_copy(update={"stage": stage_direct})
            return {"context": s.context, "last_node": s.last_node}

        # Action messages: skip extraction
        if _is_action_message(last_user):
            return {"context": s.context, "observation": s.observation, "last_node": s.last_node}

        # Lat/lon extraction from text
        lat, lon = extract_lat_lon(last_user)
        ctx_data = s.context.model_dump(mode="json")
        if lat is not None and lon is not None:
            ctx_data["lat"] = ctx_data.get("lat") or lat
            ctx_data["lon"] = ctx_data.get("lon") or lon
        s.context = FarmerContext.model_validate(ctx_data)

        lang = s.context.language
        system = (
            "Extract farmer + crop context.\n"
            "Return ONLY JSON object.\n"
            f"Allowed stages: {', '.join(sorted(_ALLOWED_STAGES))}.\n"
            "Urgency: low|medium|high.\n"
            f"Responding language: {_LANG_NAME.get(lang, 'English')}.\n"
        )

        user = (
            f"Known context:\n{json.dumps(s.context.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            f"Known observation:\n{json.dumps(s.observation.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            f"New message:\n{last_user}\n\n"
            "Extract keys: farmer_name, land_size, land_unit, crop, stage, location_text, sowing_date, irrigation, soil_type, notes, symptoms, pests_seen, urgency."
        )

        try:
            data = await _llm_json(
                client,
                model=settings.openai_model,
                system=system,
                user=user,
                temperature=0.1,
                max_tries=2,
            )
            upd = IntakeExtraction.model_validate(data)
        except Exception:
            log.exception("Intake extraction failed.")
            return {"context": s.context, "observation": s.observation, "last_node": s.last_node}

        return {
            "context": _merge_context(s.context, upd),
            "observation": _merge_observation(s.observation, upd),
            "last_node": s.last_node,
        }

    def plan_node(state: GraphState) -> dict[str, Any]:
        return {"last_node": "plan"}

    def ask_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "ask"
        c = s.context
        lang = c.language

        if not c.farmer_name:
            msg = "Name?" if lang == "en" else ("नाम?" if lang == "hi" else "नाव?")
        elif not (c.lat and c.lon) and not (c.location_text and c.location_text.strip()):
            msg = (
                "Location (city/village or lat,lon)?"
                if lang == "en"
                else ("स्थान (गांव/शहर या lat,lon)?" if lang == "hi" else "ठिकाण (गाव/शहर किंवा lat,lon)?")
            )
        elif not c.crop:
            msg = (
                "Pick one crop from the suggestions (reply: Crop: ___)."
                if lang == "en"
                else (
                    "सुझाव से एक फसल चुनें (उत्तर: Crop: ___)."
                    if lang == "hi"
                    else "सुचनांमधून एक पीक निवडा (उत्तर: Crop: ___)."
                )
            )
        elif c.stage == "unknown":
            msg = (
                "Stage? (sowing/germination/vegetative/flowering/fruiting/maturity/harvest)"
                if lang == "en"
                else (
                    "चरण? (बुवाई/अंकुरण/वृद्धि/फूल/फल/पकना/कटाई)"
                    if lang == "hi"
                    else "अवस्था? (पेरणी/अंकुरण/वाढ/फुलोरा/फळ/पक्वता/कापणी)"
                )
            )
        elif c.land_size is None:
            msg = (
                "Land size? (acres/hectare)"
                if lang == "en"
                else ("जमीन? (एकड़/हेक्टेयर)" if lang == "hi" else "जमीन? (एकर/हेक्टर)")
            )
        else:
            msg = "Send symptoms or photo." if lang == "en" else ("लक्षण या फोटो भेजें।" if lang == "hi" else "लक्षणं किंवा फोटो पाठवा.")

        s.add_assistant(msg)
        s.compact_messages()
        return {"messages": s.messages, "last_node": s.last_node, "advisory": None}

    async def crop_reco_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "crop_reco"

        c = s.context
        if not (c.lat and c.lon) and c.location_text:
            try:
                lat, lon, resolved = await tools.geocode(c.location_text)
                if lat is not None and lon is not None:
                    s.context = s.context.model_copy(update={"lat": lat, "lon": lon, "location_text": resolved})
                    c = s.context
            except ToolError:
                log.exception("Geocoding failed in crop_reco.")

        loc_text = (c.location_text or "").strip()
        if not loc_text and c.lat and c.lon:
            loc_text = f"{float(c.lat):.4f},{float(c.lon):.4f}"

        weather_summary = None
        if c.lat and c.lon and _is_weather_stale(s):
            try:
                s.weather = await tools.weather(float(c.lat), float(c.lon))
            except ToolError:
                log.exception("Weather failed in crop_reco.")
        if s.weather:
            weather_summary = s.weather.summary

        soil_snips: list[str] = []
        crop_snips: list[str] = []
        try:
            soil_ctx = await tools.web(f"typical soil pH in {loc_text} agriculture", time_range="year")
            soil_snips = (soil_ctx.snippets or [])[:3]
        except ToolError:
            log.exception("Soil pH web search failed in crop_reco.")
        try:
            crops_ctx = await tools.web(f"best crops suitable for climate in {loc_text} India", time_range="year")
            crop_snips = (crops_ctx.snippets or [])[:4]
            s.web = crops_ctx
        except ToolError:
            log.exception("Crop suitability web search failed in crop_reco.")

        lang = s.context.language
        lang_name = _LANG_NAME.get(lang, "English")

        system = (
            "You are an agronomy assistant.\n"
            "Goal: Recommend crops for the farmer's location.\n"
            "Return ONLY JSON that matches the Advisory schema.\n"
            "Constraints:\n"
            "- stage MUST be 'pre_sowing'\n"
            "- headline must mention location and estimated soil pH RANGE (approx)\n"
            "- actions_now must include: (1) 5-7 crop options (list), (2) 2-3 next steps to validate soil/pH locally\n"
            "- watch_out_for: 2-3 risks\n"
            "- safety_notes: 0-2\n"
            "- No pesticide dosage/mixing ratios.\n"
            f"Respond in {lang_name}.\n"
        )

        schema = Advisory.model_json_schema()
        user = (
            f"Schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"Location: {loc_text}\n"
            f"Weather summary: {weather_summary}\n"
            f"Soil pH web snippets: {json.dumps(soil_snips, ensure_ascii=False)}\n"
            f"Crop suitability web snippets: {json.dumps(crop_snips, ensure_ascii=False)}\n"
            f"Known farmer land size: {s.context.land_size} {s.context.land_unit}\n"
            "Now produce crop recommendations."
        )

        try:
            data = await _llm_json(
                client,
                model=settings.openai_model,
                system=system,
                user=user,
                temperature=0.2,
                max_tries=2,
            )
            adv = safe_parse_advisory(data)
        except Exception:
            log.exception("crop_reco advisory generation failed.")
            s.add_assistant("Share location and ask: recommend crops.")
            return {"messages": s.messages, "advisory": None, "last_node": s.last_node}

        s.advisory = adv
        s.add_assistant(adv.headline)
        s.compact_messages()
        return {
            "context": s.context,
            "weather": s.weather,
            "web": s.web,
            "advisory": s.advisory,
            "messages": s.messages,
            "last_node": s.last_node,
        }

    async def buy_node(state: GraphState) -> dict[str, Any]:
        """
        Deterministic (no LLM): fetch Tavily buying links and package into an Advisory.
        Shows ONLY when the Buy button is clicked.
        """
        s = state.model_copy(deep=True)
        s.last_node = "buy"

        lang = s.context.language
        crop = (s.context.crop or "").strip().lower()
        loc = (s.context.location_text or "").strip()

        # Make sure we have a usable location string
        if not loc and s.context.lat and s.context.lon:
            loc = f"{float(s.context.lat):.4f},{float(s.context.lon):.4f}"

        if not crop:
            msg = _t(
                lang,
                "Set crop first (reply: Crop: rice) then tap Buy Inputs.",
                "पहले फसल सेट करें (उत्तर: Crop: rice) फिर खरीद लिंक दबाएँ।",
                "आधी पीक सेट करा (उत्तर: Crop: rice) मग खरेदी लिंक दाबा.",
            )
            data = {
                "headline": msg,
                "stage": s.context.stage or "pre_sowing",
                "actions_now": [],
                "watch_out_for": [],
                "safety_notes": [],
                "rationale_brief": "",
                "confidence": "high",
                "needs_human_review": False,
            }
            adv = safe_parse_advisory(data)
            s.advisory = adv
            s.add_assistant(adv.headline)
            s.compact_messages()
            return {"advisory": s.advisory, "messages": s.messages, "last_node": s.last_node}

        if not loc:
            msg = _t(
                lang,
                "Set location first (send city/village or share GPS), then tap Buy Inputs.",
                "पहले स्थान सेट करें (शहर/गाँव या GPS भेजें) फिर खरीद लिंक दबाएँ।",
                "आधी ठिकाण सेट करा (शहर/गाव किंवा GPS) मग खरेदी लिंक दाबा.",
            )
            data = {
                "headline": msg,
                "stage": s.context.stage or "pre_sowing",
                "actions_now": [],
                "watch_out_for": [],
                "safety_notes": [],
                "rationale_brief": "",
                "confidence": "high",
                "needs_human_review": False,
            }
            adv = safe_parse_advisory(data)
            s.advisory = adv
            s.add_assistant(adv.headline)
            s.compact_messages()
            return {"advisory": s.advisory, "messages": s.messages, "last_node": s.last_node}

        try:
            ctx = await tools.buy_inputs(loc, crop)
        except ToolError:
            log.exception("buy_inputs failed.")
            msg = _t(
                lang,
                "Buying links not available right now. Try again in a minute.",
                "खरीद लिंक अभी उपलब्ध नहीं। 1 मिनट बाद फिर प्रयास करें।",
                "खरेदी लिंक्स आत्ता मिळत नाहीत. 1 मिनिटाने पुन्हा प्रयत्न करा.",
            )
            data = {
                "headline": msg,
                "stage": s.context.stage or "pre_sowing",
                "actions_now": [],
                "watch_out_for": [],
                "safety_notes": [],
                "rationale_brief": "",
                "confidence": "medium",
                "needs_human_review": False,
            }
            adv = safe_parse_advisory(data)
            s.advisory = adv
            s.add_assistant(adv.headline)
            s.compact_messages()
            return {"advisory": s.advisory, "messages": s.messages, "last_node": s.last_node}

        # Build short output: 4–6 bullets max
        seed_lbl = _t(lang, "Seeds", "बीज", "बियाणे")
        fert_lbl = _t(lang, "Fertilizer", "उर्वरक", "खते")
        prot_lbl = _t(lang, "Crop protection", "फसल सुरक्षा", "पीक संरक्षण")
        note_lbl = _t(lang, "Tip: compare prices + seller ratings.", "टिप: कीमत + रेटिंग तुलना करें।", "टिप: किंमत + रेटिंग तुलना करा.")

        urls = (ctx.urls or [])[:6]
        bullets: list[str] = []

        # Simple grouping: first 2 as seeds, next 2 fertilizer, next 2 protection (works well enough)
        if len(urls) >= 1:
            bullets.append(f"{seed_lbl}: {urls[0]}")
        if len(urls) >= 2:
            bullets.append(f"{seed_lbl}: {urls[1]}")
        if len(urls) >= 3:
            bullets.append(f"{fert_lbl}: {urls[2]}")
        if len(urls) >= 4:
            bullets.append(f"{fert_lbl}: {urls[3]}")
        if len(urls) >= 5:
            bullets.append(f"{prot_lbl}: {urls[4]}")
        if len(urls) >= 6:
            bullets.append(f"{prot_lbl}: {urls[5]}")

        headline = _t(
            lang,
            f"Buy inputs for {crop.title()} ({loc})",
            f"{crop.title()} के लिए खरीद लिंक ({loc})",
            f"{crop.title()} साठी खरेदी लिंक्स ({loc})",
        )

        data = {
            "headline": headline,
            "stage": s.context.stage or "pre_sowing",
            "actions_now": bullets[:6] or [note_lbl],
            "watch_out_for": [
                _t(lang, "Avoid unknown sellers; check expiry date.", "अनजान विक्रेता से बचें; एक्सपायरी देखें।", "अनोळखी विक्रेते टाळा; एक्सपायरी तपासा.")
            ],
            "safety_notes": [],
            "rationale_brief": note_lbl,
            "confidence": "high",
            "needs_human_review": False,
        }

        adv = safe_parse_advisory(data)
        s.advisory = adv
        s.add_assistant(adv.headline)
        s.compact_messages()
        return {"advisory": s.advisory, "messages": s.messages, "last_node": s.last_node}

    async def weather_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "weather"
        c = s.context

        if not (c.lat and c.lon) and c.location_text:
            try:
                lat, lon, resolved = await tools.geocode(c.location_text)
                if lat is not None and lon is not None:
                    s.context = s.context.model_copy(update={"lat": lat, "lon": lon, "location_text": resolved})
            except ToolError:
                log.exception("Geocoding failed.")

        if not (s.context.lat and s.context.lon):
            return {"context": s.context, "weather": s.weather, "last_node": s.last_node}

        try:
            snap = await tools.weather(float(s.context.lat), float(s.context.lon))
            return {"weather": snap, "context": s.context, "last_node": s.last_node}
        except ToolError:
            log.exception("Weather failed.")
            return {"weather": s.weather, "context": s.context, "last_node": s.last_node}

    async def web_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "web"

        crop = s.context.crop or ""
        stage = s.context.stage
        loc = (s.context.location_text or "").strip()
        symptoms = ", ".join(s.observation.symptoms[:2]) if s.observation.symptoms else ""

        if not crop:
            q = f"best farming practices {loc} seasonal crops kharif rabi soil pH".strip()
        else:
            q = f"{crop} {stage} symptoms {symptoms} best practice {loc}".strip()

        try:
            ctx = await tools.web(q, time_range="month")
            return {"web": ctx, "last_node": s.last_node}
        except ToolError:
            log.exception("Web search failed.")
            return {"web": s.web, "last_node": s.last_node}

    async def schemes_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "schemes"
        if not s.context.location_text:
            return {"schemes": s.schemes, "last_node": s.last_node}
        try:
            ctx = await tools.schemes(s.context.location_text, s.context.crop)
            return {"schemes": ctx, "last_node": s.last_node}
        except ToolError:
            log.exception("Schemes search failed.")
            return {"schemes": s.schemes, "last_node": s.last_node}

    async def market_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "market"
        try:
            ctx = await tools.market_prices(s.context.location_text or "", s.context.crop)
            return {"market": ctx, "last_node": s.last_node}
        except ToolError:
            log.exception("Market search failed.")
            return {"market": s.market, "last_node": s.last_node}

    async def vision_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "vision"

        if not s.last_image:
            return {"image_diagnosis": None, "last_node": s.last_node}

        try:
            data_url = _data_url_from_file(s.last_image.file_path)
        except Exception:
            log.exception("Reading image failed.")
            return {"image_diagnosis": None, "last_node": s.last_node}

        caption = s.last_image.caption or ""
        crop = s.context.crop or "crop"
        stage = s.context.stage
        lang = s.context.language
        lang_name = _LANG_NAME.get(lang, "English")

        prompt = (
            f"Analyze this crop image.\nCrop: {crop}\nStage: {stage}\nCaption: {caption}\n\n"
            "Return ONLY JSON keys exactly:\n"
            "issue, likely_causes(list), actions_now(list), watch_out_for(list), confidence(low|medium|high), needs_human_review(boolean).\n"
            "Keep short: max 3 causes, 3 actions, 2 watch.\n"
            "No pesticide dosage/mixing ratios.\n"
            f"Respond in {lang_name}."
        )

        try:
            resp = await client.responses.create(
                model=settings.openai_model,
                input=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": data_url},
                        ],
                    }
                ],
                temperature=0.2,
            )
            txt = (resp.output_text or "").strip()
            js = _extract_json_object(txt) or "{}"
            raw = json.loads(js)
            if not isinstance(raw, dict):
                raw = {"issue": txt[:140]}
            diag = _coerce_image_diagnosis(raw)
            return {"image_diagnosis": diag, "last_node": s.last_node}
        except Exception:
            log.exception("Vision failed.")
            return {"image_diagnosis": None, "last_node": s.last_node}

    async def advice_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "advice"

        lang = s.context.language
        lang_name = _LANG_NAME.get(lang, "English")

        ctx = s.context.model_dump(mode="json")
        obs = s.observation.model_dump(mode="json")

        weather = {"summary": s.weather.summary, "alerts": s.weather.alerts[:2]} if s.weather else None
        web = {"snippets": s.web.snippets[:2]} if s.web else None
        schemes = {"snippets": s.schemes.snippets[:2]} if (_wants_schemes(s) and s.schemes) else None
        market = {"snippets": s.market.snippets[:2]} if (_wants_market(s) and s.market) else None

        last_user = _last_user_text(s)
        include_image = bool(s.image_diagnosis) and (not _is_stage_update_message(last_user)) and (not _is_action_message(last_user))
        image_diag = s.image_diagnosis.model_dump(mode="json") if include_image else None

        system = (
            "Return ONLY JSON for Advisory.\n"
            "Keep concise.\n"
            "actions_now: 3-5, watch_out_for: 2-3, safety_notes: 0-2, rationale_brief <= 200 chars.\n"
            "No pesticide dosage/mixing ratios.\n"
            f"Respond in {lang_name}."
        )

        schema = Advisory.model_json_schema()
        user = (
            f"Schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"Context:{json.dumps(ctx, ensure_ascii=False)}\n"
            f"Obs:{json.dumps(obs, ensure_ascii=False)}\n"
            f"Weather:{json.dumps(weather, ensure_ascii=False)}\n"
            f"Web:{json.dumps(web, ensure_ascii=False)}\n"
            f"Schemes:{json.dumps(schemes, ensure_ascii=False)}\n"
            f"Market:{json.dumps(market, ensure_ascii=False)}\n"
            f"Image:{json.dumps(image_diag, ensure_ascii=False)}\n"
        )

        try:
            data = await _llm_json(
                client,
                model=settings.openai_model,
                system=system,
                user=user,
                temperature=0.25,
                max_tries=2,
            )
            adv = safe_parse_advisory(data)
        except Exception:
            log.exception("Advice failed.")
            s.add_assistant("Send crop + stage + location (or upload a clear photo).")
            return {"messages": s.messages, "advisory": None, "last_node": s.last_node}

        s.add_assistant(adv.headline)
        s.compact_messages()
        return {"advisory": adv, "messages": s.messages, "last_node": s.last_node}

    sg.add_node("intake", intake_node)
    sg.add_node("plan", plan_node)
    sg.add_node("ask", ask_node)
    sg.add_node("crop_reco", crop_reco_node)
    sg.add_node("buy", buy_node)
    sg.add_node("vision", vision_node)
    sg.add_node("weather", weather_node)
    sg.add_node("web", web_node)
    sg.add_node("schemes", schemes_node)
    sg.add_node("market", market_node)
    sg.add_node("advice", advice_node)

    sg.set_entry_point("intake")
    sg.add_edge("intake", "plan")

    sg.add_conditional_edges(
        "plan",
        _route,
        {
            "ask": "ask",
            "crop_reco": "crop_reco",
            "buy": "buy",
            "vision": "vision",
            "weather": "weather",
            "web": "web",
            "schemes": "schemes",
            "market": "market",
            "advice": "advice",
        },
    )

    sg.add_edge("crop_reco", END)
    sg.add_edge("buy", END)
    sg.add_edge("vision", "plan")
    sg.add_edge("weather", "plan")
    sg.add_edge("web", "plan")
    sg.add_edge("schemes", "plan")
    sg.add_edge("market", "plan")
    sg.add_edge("ask", END)
    sg.add_edge("advice", END)

    compiled = sg.compile()
    if compiled is None:
        raise RuntimeError("LangGraph compilation returned None.")
    return compiled


@dataclass
class CropAdvisorGraph:
    settings: Settings
    client: AsyncOpenAI
    tools: ToolBundle
    graph: Any = None

    @classmethod
    def create(cls, settings: Settings) -> "CropAdvisorGraph":
        client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        tools = ToolBundle(
            openweather_api_key=settings.openweather_api_key,
            openweather_units=settings.openweather_units,
            tavily_api_key=settings.tavily_api_key,
            tavily_max_results=settings.tavily_max_results,
        )
        compiled = _build_compiled_graph(settings=settings, client=client, tools=tools)
        return cls(settings=settings, client=client, tools=tools, graph=compiled)

    def _ensure_graph(self) -> None:
        if self.graph is None:
            self.graph = _build_compiled_graph(settings=self.settings, client=self.client, tools=self.tools)

    async def run_turn(self, state: GraphState, user_text: str) -> GraphState:
        """
        Robust state persistence:
        - LangGraph may return a full GraphState or a partial dict.
        - We always merge output into current state to prevent missing fields.
        """
        self._ensure_graph()

        s = state.model_copy(deep=True)
        s.add_user(user_text)
        s.turn_count += 1
        s.compact_messages()

        out = await self.graph.ainvoke(s)

        if isinstance(out, GraphState):
            return out

        if isinstance(out, dict):
            base = s.model_dump(mode="json")
            upd = _normalize_to_jsonable(out)
            merged = _deep_merge(base, upd)
            return GraphState.model_validate(merged)

        return GraphState.model_validate(_normalize_to_jsonable(out))
