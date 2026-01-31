from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator


CropStage = Literal[
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
]


class FarmerContext(BaseModel):
    crop: Optional[str] = Field(default=None, description="Crop name, e.g., cotton, tomato")
    stage: CropStage = Field(default="unknown", description="Current crop stage")
    location_text: Optional[str] = Field(default=None, description="Free-text location")
    lat: Optional[float] = Field(default=None, ge=-90, le=90)
    lon: Optional[float] = Field(default=None, ge=-180, le=180)
    sowing_date: Optional[str] = Field(default=None, description="ISO-like date or free text")
    irrigation: Optional[str] = Field(default=None, description="e.g., drip, flood, rainfed")
    soil_type: Optional[str] = Field(default=None, description="e.g., black, red, sandy, loam")
    notes: Optional[str] = Field(default=None, description="Other farmer-provided info")


class Observation(BaseModel):
    symptoms: list[str] = Field(default_factory=list, description="Observed symptoms")
    pests_seen: list[str] = Field(default_factory=list, description="Observed pests")
    images_hint: Optional[str] = Field(default=None, description="Text hint: images were shared (future)")
    urgency: Literal["low", "medium", "high"] = Field(default="low")


class WeatherSnapshot(BaseModel):
    source: Literal["openweather"] = "openweather"
    fetched_at_utc: str
    summary: str = Field(default="")
    alerts: list[str] = Field(default_factory=list)
    daily: list[dict[str, Any]] = Field(default_factory=list, description="Raw daily forecast subset")
    hourly: list[dict[str, Any]] = Field(default_factory=list, description="Raw hourly subset")


class WebContext(BaseModel):
    source: Literal["tavily"] = "tavily"
    fetched_at_utc: str
    query: str
    snippets: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)


class Advisory(BaseModel):
    """
    Final validated output we send to the farmer.
    Keep it small + actionable + stage-aware.
    """

    headline: str = Field(..., min_length=3, max_length=120)
    stage: CropStage = Field(default="unknown")

    actions_now: list[str] = Field(default_factory=list, description="Do these now (3-7 bullets)")
    watch_out_for: list[str] = Field(default_factory=list, description="What to monitor next (2-5 bullets)")
    next_questions: list[str] = Field(default_factory=list, description="Ask farmer for missing info (0-3)")

    rationale_brief: str = Field(default="", max_length=600)
    confidence: Literal["low", "medium", "high"] = Field(default="medium")

    safety_notes: list[str] = Field(default_factory=list, description="Safety disclaimers or escalation note")
    needs_human_review: bool = Field(default=False, description="Flag when escalation recommended")

    @field_validator("actions_now")
    @classmethod
    def _limit_actions(cls, v: list[str]) -> list[str]:
        # Keep output crisp for Telegram UI
        v = [x.strip() for x in v if x and x.strip()]
        return v[:7]

    @field_validator("watch_out_for")
    @classmethod
    def _limit_watch(cls, v: list[str]) -> list[str]:
        v = [x.strip() for x in v if x and x.strip()]
        return v[:5]

    @field_validator("next_questions")
    @classmethod
    def _limit_questions(cls, v: list[str]) -> list[str]:
        v = [x.strip() for x in v if x and x.strip()]
        return v[:3]


class GuardrailResult(BaseModel):
    ok: bool = True
    reasons: list[str] = Field(default_factory=list)
    needs_human_review: bool = False


class GraphState(BaseModel):
    """
    LangGraph state.
    Keep as one object; store per Telegram chat_id.
    """

    # Identity
    chat_id: str

    # Conversation
    messages: list[dict[str, Any]] = Field(
        default_factory=list,
        description="OpenAI-style messages: {role, content}",
    )

    # Structured memory
    context: FarmerContext = Field(default_factory=FarmerContext)
    observation: Observation = Field(default_factory=Observation)

    # Tool context
    weather: Optional[WeatherSnapshot] = None
    web: Optional[WebContext] = None

    # Latest output
    advisory: Optional[Advisory] = None

    # Control flags
    last_node: Optional[str] = None
    turn_count: int = 0

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})

    def compact_messages(self, max_messages: int = 16) -> None:
        # Minimal memory strategy: keep last N messages
        if len(self.messages) > max_messages:
            self.messages = self.messages[-max_messages:]


def safe_parse_advisory(data: Any) -> Advisory:
    """
    Convert model output into Advisory or raise with details.
    """
    if isinstance(data, Advisory):
        return data
    return Advisory.model_validate(data)
