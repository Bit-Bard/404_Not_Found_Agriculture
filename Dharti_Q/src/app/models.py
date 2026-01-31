from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field, ConfigDict


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class FarmerContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    farmer_name: Optional[str] = None
    crop: Optional[str] = None
    stage: str = "unknown"

    land_size: Optional[float] = None
    land_unit: Optional[str] = None

    location_text: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None

    sowing_date: Optional[str] = None
    irrigation: Optional[str] = None
    soil_type: Optional[str] = None
    notes: Optional[str] = None

    language: str = "en"


class Observation(BaseModel):
    model_config = ConfigDict(extra="ignore")

    symptoms: list[str] = Field(default_factory=list)
    pests_seen: list[str] = Field(default_factory=list)
    urgency: str = "low"  # low | medium | high


class WeatherSnapshot(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fetched_at_utc: str = Field(default_factory=_utc_now_iso)
    summary: str = ""
    alerts: list[str] = Field(default_factory=list)
    daily: list[dict[str, Any]] = Field(default_factory=list)


class WebContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fetched_at_utc: str = Field(default_factory=_utc_now_iso)
    query: str = ""
    snippets: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)


class SchemesContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fetched_at_utc: str = Field(default_factory=_utc_now_iso)
    query: str = ""
    snippets: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)


class MarketContext(BaseModel):
    model_config = ConfigDict(extra="ignore")

    fetched_at_utc: str = Field(default_factory=_utc_now_iso)
    query: str = ""
    location: Optional[str] = None
    crop: Optional[str] = None
    snippets: list[str] = Field(default_factory=list)
    urls: list[str] = Field(default_factory=list)


class ImageAsset(BaseModel):
    model_config = ConfigDict(extra="ignore")

    file_path: str
    telegram_file_id: Optional[str] = None
    caption: Optional[str] = None
    created_at_utc: str = Field(default_factory=_utc_now_iso)


class ImageDiagnosis(BaseModel):
    model_config = ConfigDict(extra="ignore")

    issue: str
    likely_causes: list[str] = Field(default_factory=list)
    actions_now: list[str] = Field(default_factory=list)
    watch_out_for: list[str] = Field(default_factory=list)
    confidence: str = "medium"  # low|medium|high
    needs_human_review: bool = False


class Advisory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    headline: str
    stage: str = "unknown"
    actions_now: list[str] = Field(default_factory=list)
    watch_out_for: list[str] = Field(default_factory=list)
    rationale_brief: str = ""
    safety_notes: list[str] = Field(default_factory=list)
    confidence: str = "medium"  # low|medium|high
    needs_human_review: bool = False


class GraphState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    chat_id: str = ""
    turn_count: int = 0
    last_node: Optional[str] = None

    messages: list[dict[str, Any]] = Field(default_factory=list)

    context: FarmerContext = Field(default_factory=FarmerContext)
    observation: Observation = Field(default_factory=Observation)

    weather: Optional[WeatherSnapshot] = None
    web: Optional[WebContext] = None
    schemes: Optional[SchemesContext] = None
    market: Optional[MarketContext] = None

    last_image: Optional[ImageAsset] = None
    image_diagnosis: Optional[ImageDiagnosis] = None

    advisory: Optional[Advisory] = None

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def add_assistant(self, text: str) -> None:
        self.messages.append({"role": "assistant", "content": text})

    def compact_messages(self, keep_last: int = 16) -> None:
        if len(self.messages) > keep_last:
            self.messages = self.messages[-keep_last:]


def safe_parse_advisory(data: dict[str, Any]) -> Advisory:
    if not isinstance(data, dict):
        raise ValueError("Advisory JSON must be an object.")

    headline = str(data.get("headline") or "").strip()
    if not headline:
        headline = "Advisory update"

    stage = str(data.get("stage") or "unknown").strip().lower()

    actions_now = data.get("actions_now") or []
    if isinstance(actions_now, str):
        actions_now = [x.strip() for x in actions_now.split("\n") if x.strip()]
    if not isinstance(actions_now, list):
        actions_now = []
    actions_now = [str(x).strip() for x in actions_now if str(x).strip()][:5]

    watch = data.get("watch_out_for") or []
    if isinstance(watch, str):
        watch = [x.strip() for x in watch.split("\n") if x.strip()]
    if not isinstance(watch, list):
        watch = []
    watch = [str(x).strip() for x in watch if str(x).strip()][:3]

    conf = str(data.get("confidence") or "medium").strip().lower()
    if conf not in ("low", "medium", "high"):
        conf = "medium"

    needs = data.get("needs_human_review")
    if isinstance(needs, str):
        needs = needs.strip().lower() in ("true", "yes", "1")
    if not isinstance(needs, bool):
        needs = False

    rationale = str(data.get("rationale_brief") or "").strip()
    if len(rationale) > 220:
        rationale = rationale[:220]

    safety = data.get("safety_notes") or []
    if isinstance(safety, str):
        safety = [x.strip() for x in safety.split("\n") if x.strip()]
    if not isinstance(safety, list):
        safety = []
    safety = [str(x).strip() for x in safety if str(x).strip()][:2]

    return Advisory(
        headline=headline,
        stage=stage or "unknown",
        actions_now=actions_now,
        watch_out_for=watch,
        rationale_brief=rationale,
        safety_notes=safety,
        confidence=conf,
        needs_human_review=needs,
    )
