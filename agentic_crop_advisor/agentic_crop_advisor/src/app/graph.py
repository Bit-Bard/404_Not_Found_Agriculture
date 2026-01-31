from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from langgraph.graph import StateGraph, END

from .config import Settings
from .models import Advisory, FarmerContext, GraphState, GuardrailResult, Observation, safe_parse_advisory
from .tools import ToolBundle, ToolError, extract_lat_lon

log = logging.getLogger("graph")


# ---------------------------
# Small schemas for LLM steps
# ---------------------------

class IntakeExtraction(BaseModel):
    crop: Optional[str] = None
    stage: Optional[str] = None
    location_text: Optional[str] = None
    sowing_date: Optional[str] = None
    irrigation: Optional[str] = None
    soil_type: Optional[str] = None
    notes: Optional[str] = None

    symptoms: list[str] = Field(default_factory=list)
    pests_seen: list[str] = Field(default_factory=list)
    urgency: Optional[str] = None  # low/medium/high


# ---------------------------
# Helpers
# ---------------------------

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat(timespec="seconds")


def _parse_iso_dt(s: str) -> Optional[datetime]:
    try:
        # Handles "2026-01-31T12:00:00+00:00"
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _is_weather_stale(state: GraphState, *, max_age_hours: int = 6) -> bool:
    if not state.weather:
        return True
    dt = _parse_iso_dt(state.weather.fetched_at_utc)
    if not dt:
        return True
    return (_utc_now() - dt) > timedelta(hours=max_age_hours)


def _is_web_stale(state: GraphState, *, max_age_hours: int = 24) -> bool:
    if not state.web:
        return True
    dt = _parse_iso_dt(state.web.fetched_at_utc)
    if not dt:
        return True
    return (_utc_now() - dt) > timedelta(hours=max_age_hours)


def _first_nonempty(*vals: Optional[str]) -> Optional[str]:
    for v in vals:
        if v and v.strip():
            return v.strip()
    return None


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(text: str) -> Optional[str]:
    if not text:
        return None
    text = text.strip()
    if text.startswith("{") and text.endswith("}"):
        return text
    m = _JSON_OBJ_RE.search(text)
    if not m:
        return None
    return m.group(0)


async def _llm_json(
    client: AsyncOpenAI,
    *,
    model: str,
    system: str,
    user: str,
    temperature: float = 0.2,
    max_tries: int = 2,
) -> dict[str, Any]:
    """
    Ask the model for STRICT JSON. We do lightweight repair by extracting the first {...}.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, max_tries + 1):
        try:
            resp = await client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
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
    if upd.crop and upd.crop.strip():
        data["crop"] = upd.crop.strip().lower()
    if upd.stage and upd.stage.strip():
        # We keep stage mapping conservative; invalid stages become "unknown"
        stage = upd.stage.strip().lower()
        allowed = {
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
        data["stage"] = stage if stage in allowed else "unknown"
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


def _guardrails(advisory: Advisory, state: GraphState) -> GuardrailResult:
    """
    Minimal, practical hackathon guardrails:
    - avoid dosage / mixing instructions
    - raise human review on severe/unsafe signals
    """
    reasons: list[str] = []
    needs_human = False

    risky_patterns = [
        r"\b\d+(\.\d+)?\s*(ml|mL|l|L|g|gm|kg)\b",
        r"\b(per|/)\s*(l|L|liter|litre|kg)\b",
        r"\bmix\b",
        r"\bdose\b",
        r"\bppm\b",
    ]

    joined = " ".join([advisory.headline] + advisory.actions_now + advisory.watch_out_for).lower()
    for pat in risky_patterns:
        if re.search(pat, joined, flags=re.IGNORECASE):
            reasons.append("Contains potentially unsafe dosage/mixing-style details.")
            needs_human = True
            break

    if state.observation.urgency == "high":
        reasons.append("High urgency reported; recommend expert verification.")
        needs_human = True

    if state.weather and state.weather.alerts:
        reasons.append("Weather alerts present; recommend extra caution.")
        needs_human = True

    return GuardrailResult(ok=(not needs_human), reasons=reasons, needs_human_review=needs_human)


def _sanitize_advisory(advisory: Advisory, guard: GuardrailResult) -> Advisory:
    """
    If guardrails trigger, remove potentially unsafe lines and replace with safe escalation guidance.
    """
    if not guard.needs_human_review:
        return advisory

    # Remove any action lines that look like dosing/mixing.
    def is_risky_line(line: str) -> bool:
        if not line:
            return False
        return bool(
            re.search(r"\b\d+(\.\d+)?\s*(ml|mL|l|L|g|gm|kg)\b", line, flags=re.IGNORECASE)
            or re.search(r"\bmix\b|\bdose\b|\bppm\b", line, flags=re.IGNORECASE)
        )

    safe_actions = [a for a in advisory.actions_now if not is_risky_line(a)]
    if len(safe_actions) == 0:
        safe_actions = ["Consult a local agriculture officer/extension worker for treatment options."]

    safety_notes = list(advisory.safety_notes)
    safety_notes.append("Avoid chemical dosage/mixing without local expert guidance.")
    safety_notes.extend(guard.reasons[:2])

    return advisory.model_copy(
        update={
            "actions_now": safe_actions[:7],
            "safety_notes": safety_notes[:6],
            "needs_human_review": True,
            "confidence": "low" if advisory.confidence != "low" else "low",
        }
    )


def _ask_message_for_missing(state: GraphState) -> str:
    """
    Ask only the next most important question (keeps loop tight).
    """
    c = state.context
    if not c.lat or not c.lon:
        if not (c.location_text and c.location_text.strip()):
            return (
                "To guide you accurately, share your location:\n"
                "• Village/City + District/State, OR\n"
                "• Coordinates like: 19.07,72.87"
            )
    if not (c.crop and c.crop.strip()):
        return "Which crop are you growing? (e.g., cotton, wheat, tomato)"
    if c.stage == "unknown":
        return (
            "Which crop stage are you in?\n"
            "Options: sowing, germination, vegetative, flowering, fruiting, maturity, harvest"
        )

    # If we reach here, we likely want more detail for symptoms-based help
    if state.observation.symptoms and not state.web:
        return "Can you share 1–2 clear symptoms and how many days you’ve noticed them? (Also mention irrigation frequency.)"

    return "Share any recent changes (rain/irrigation/fertilizer) and I’ll update your next actions."


def _route(state: GraphState) -> str:
    """
    Router for LangGraph conditional edges.
    """
    c = state.context

    # Must have at least one location representation; lat/lon preferred for weather.
    if not (c.lat and c.lon) and not (c.location_text and c.location_text.strip()):
        return "ask"
    if not (c.crop and c.crop.strip()):
        return "ask"
    if c.stage == "unknown":
        return "ask"

    # Tools
    if c.lat and c.lon and _is_weather_stale(state):
        return "weather"

    # Web for symptoms context or local practices; keep it optional but helpful.
    if state.observation.symptoms and _is_web_stale(state):
        return "web"

    return "advice"


# ---------------------------
# Graph runtime
# ---------------------------

@dataclass
class CropAdvisorGraph:
    settings: Settings
    client: AsyncOpenAI
    tools: ToolBundle
    graph: Any  # compiled langgraph

    @classmethod
    def create(cls, settings: Settings) -> "CropAdvisorGraph":
        client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
        tools = ToolBundle(
            openweather_api_key=settings.openweather_api_key,
            openweather_units=settings.openweather_units,
            tavily_api_key=settings.tavily_api_key,
            tavily_max_results=settings.tavily_max_results,
        )

        compiled = _build_compiled_graph(settings=settings, client=client, tools=tools)
        return cls(settings=settings, client=client, tools=tools, graph=compiled)

    async def run_turn(self, state: GraphState, user_text: str) -> GraphState:
        """
        One interactive loop step:
        - appends user message
        - runs graph
        - returns updated state
        """
        s = state.model_copy(deep=True)
        s.add_user(user_text)
        s.turn_count += 1
        s.compact_messages()

        out = await self.graph.ainvoke(s)

        # LangGraph may return dict or GraphState depending on version; normalize.
        if isinstance(out, GraphState):
            return out
        if isinstance(out, dict):
            return GraphState.model_validate(out)
        return GraphState.model_validate(out)


def _build_compiled_graph(*, settings: Settings, client: AsyncOpenAI, tools: ToolBundle):
    """
    Build LangGraph agents:
      - intake (LLM extraction)
      - plan/router (rule-based)
      - weather (tool)
      - web (tool)
      - advice (LLM + guardrails)
      - ask (deterministic)
    """
    sg = StateGraph(GraphState)

    async def intake_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "intake"

        last_user = ""
        # Find the last user message for deterministic lat/lon extraction too.
        for m in reversed(s.messages):
            if m.get("role") == "user":
                last_user = str(m.get("content", "") or "")
                break

        # 1) deterministic coordinate extraction (fast + reliable)
        lat, lon = extract_lat_lon(last_user)
        ctx_data = s.context.model_dump(mode="json")
        if lat is not None and lon is not None:
            ctx_data["lat"] = ctx_data.get("lat") or lat
            ctx_data["lon"] = ctx_data.get("lon") or lon

        # 2) if location_text missing, try to set from user text (light heuristic)
        if not ctx_data.get("location_text") and last_user and len(last_user) <= 120:
            # Only set if it looks like a place string, not a long paragraph
            ctx_data["location_text"] = last_user.strip()

        s.context = FarmerContext.model_validate(ctx_data)

        # 3) LLM extraction for crop/stage/symptoms/practices
        system = (
            "You extract structured farm context from farmer messages.\n"
            "Return ONLY valid JSON object. No markdown.\n"
            "If unsure, use null or omit.\n"
            "Allowed stages: unknown, pre_sowing, sowing, germination, vegetative, flowering, fruiting, maturity, harvest, post_harvest.\n"
            "Urgency must be one of: low, medium, high."
        )

        user = (
            f"Known context:\n{json.dumps(s.context.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            f"Known observation:\n{json.dumps(s.observation.model_dump(mode='json'), ensure_ascii=False)}\n\n"
            f"New farmer message:\n{last_user}\n\n"
            "Extract updates with keys: crop, stage, location_text, sowing_date, irrigation, soil_type, notes, symptoms, pests_seen, urgency."
        )

        try:
            data = await _llm_json(
                client,
                model=settings.openai_model,
                system=system,
                user=user,
                temperature=0.15,
                max_tries=2,
            )
            upd = IntakeExtraction.model_validate(data)
        except Exception:
            # If extraction fails, keep deterministic updates only (never crash conversation)
            log.exception("Intake extraction failed; continuing with existing context.")
            return {"context": s.context, "observation": s.observation, "last_node": s.last_node}

        new_ctx = _merge_context(s.context, upd)
        new_obs = _merge_observation(s.observation, upd)

        # If upd provides location_text and we still lack lat/lon, keep location_text;
        # geocoding will happen later via weather/web routing if needed.
        return {"context": new_ctx, "observation": new_obs, "last_node": s.last_node}

    def plan_node(state: GraphState) -> dict[str, Any]:
        # Rule-based (fast, stable). Router uses _route(state) for next edge.
        return {"last_node": "plan"}

    def ask_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "ask"
        msg = _ask_message_for_missing(s)
        s.add_assistant(msg)
        s.compact_messages()
        return {"messages": s.messages, "last_node": s.last_node, "advisory": None}

    async def weather_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "weather"

        c = s.context
        if not (c.lat and c.lon):
            # Try geocode if we have location_text
            if c.location_text:
                try:
                    lat, lon, resolved = await tools.geocode(c.location_text)
                    if lat is not None and lon is not None:
                        s.context = s.context.model_copy(update={"lat": lat, "lon": lon})
                        if resolved and not s.context.location_text:
                            s.context = s.context.model_copy(update={"location_text": resolved})
                except ToolError:
                    log.exception("Geocoding failed.")

        if not (s.context.lat and s.context.lon):
            # Can't fetch weather without coords; route back to ask.
            return {"context": s.context, "weather": None, "last_node": s.last_node}

        try:
            snap = await tools.weather(float(s.context.lat), float(s.context.lon))
            return {"weather": snap, "context": s.context, "last_node": s.last_node}
        except ToolError:
            log.exception("Weather tool failed.")
            return {"weather": s.weather, "context": s.context, "last_node": s.last_node}

    async def web_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "web"

        crop = s.context.crop or "crop"
        stage = s.context.stage
        loc = _first_nonempty(s.context.location_text, "")
        symptoms = ", ".join(s.observation.symptoms[:3]) if s.observation.symptoms else ""
        q_parts = [crop, stage, "farming", "best practices"]
        if symptoms:
            q_parts += ["symptoms", symptoms]
        if loc:
            q_parts += ["in", loc]
        query = " ".join([p for p in q_parts if p and str(p).strip()])

        try:
            ctx = await tools.web(query, time_range="month")
            return {"web": ctx, "last_node": s.last_node}
        except ToolError:
            log.exception("Web tool failed.")
            return {"web": s.web, "last_node": s.last_node}

    async def advice_node(state: GraphState) -> dict[str, Any]:
        s = state.model_copy(deep=True)
        s.last_node = "advice"

        # Build compact context for the model
        ctx = s.context.model_dump(mode="json")
        obs = s.observation.model_dump(mode="json")

        weather = None
        if s.weather:
            weather = {
                "summary": s.weather.summary,
                "alerts": s.weather.alerts,
                "daily": s.weather.daily[:3],
            }

        web = None
        if s.web:
            web = {
                "query": s.web.query,
                "snippets": s.web.snippets[:5],
            }

        system = (
            "You are a crop advisory assistant for small farmers.\n"
            "Return ONLY valid JSON matching the given schema. No markdown.\n"
            "Be practical, stage-aware, and safe.\n"
            "NEVER provide pesticide dosage/mixing ratios or guaranteed outcomes.\n"
            "If risk is high, recommend consulting a local agriculture officer/extension worker.\n"
            "Keep actions short and feasible."
        )

        schema = Advisory.model_json_schema()
        user = (
            f"Advisory JSON schema:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"Farmer context:\n{json.dumps(ctx, ensure_ascii=False)}\n\n"
            f"Observation:\n{json.dumps(obs, ensure_ascii=False)}\n\n"
            f"Weather (if present):\n{json.dumps(weather, ensure_ascii=False)}\n\n"
            f"Web context (if present):\n{json.dumps(web, ensure_ascii=False)}\n\n"
            "Generate an Advisory.\n"
            "Guidelines:\n"
            "- actions_now: 3–7 bullet items\n"
            "- watch_out_for: 2–5 items\n"
            "- next_questions: 0–3 items only if truly needed\n"
            "- rationale_brief: <= 600 chars\n"
            "- safety_notes: include safety disclaimers and escalation notes"
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
            log.exception("Advice generation failed; falling back to safe ask.")
            msg = (
                "I couldn’t generate a reliable plan from the current details.\n"
                "Please share crop + stage + location (village/district) and 1–2 symptoms."
            )
            s.add_assistant(msg)
            return {"messages": s.messages, "advisory": None, "last_node": s.last_node}

        guard = _guardrails(adv, s)
        adv2 = _sanitize_advisory(adv, guard)

        # Save to state + add a concise assistant message for conversation memory
        summary_lines = [f"{adv2.headline}"]
        if adv2.actions_now:
            summary_lines.append("Actions: " + "; ".join(adv2.actions_now[:4]))
        if adv2.watch_out_for:
            summary_lines.append("Watch: " + "; ".join(adv2.watch_out_for[:3]))
        if adv2.next_questions:
            summary_lines.append("Next: " + "; ".join(adv2.next_questions[:2]))
        if adv2.needs_human_review:
            summary_lines.append("Note: Recommend local expert review.")

        s.add_assistant("\n".join(summary_lines))
        s.compact_messages()

        return {
            "advisory": adv2,
            "messages": s.messages,
            "last_node": s.last_node,
        }

    # Nodes
    sg.add_node("intake", intake_node)
    sg.add_node("plan", plan_node)
    sg.add_node("ask", ask_node)
    sg.add_node("weather", weather_node)
    sg.add_node("web", web_node)
    sg.add_node("advice", advice_node)

    # Edges
    sg.set_entry_point("intake")
    sg.add_edge("intake", "plan")

    sg.add_conditional_edges(
        "plan",
        _route,  # returns: ask/weather/web/advice
        {
            "ask": "ask",
            "weather": "weather",
            "web": "web",
            "advice": "advice",
        },
    )

    # After tools, plan again (iterative loop)
    sg.add_edge("weather", "plan")
    sg.add_edge("web", "plan")

    # Endpoints
    sg.add_edge("ask", END)
    sg.add_edge("advice", END)

    return sg.compile()
