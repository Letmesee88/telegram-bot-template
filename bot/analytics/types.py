# ruff: noqa: N815, TC003
from __future__ import annotations
from abc import ABC, abstractmethod
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, IPvAnyAddress

EventType = Literal[
    # Generic events
    "Start Session",
    "End Session",
    "Revenue",
    "Revenue (Verified)",
    "Revenue (Unverified)",
    "Sign Up",
    "Select Item",
    "View Item",
    "Complete Purchase",
    "Error",
    # FoodAI: custom domain events
    "FoodAI:PreviewShown",
    "FoodAI:SaveClicked",
    "FoodAI:DeleteClicked",
    "FoodAI:EditClicked",
    "FoodAI:EditSubmitted",
    "FoodAI:EditApplied",
    "FoodAI:EditFailed",
    "FoodAI:BackClicked",
    "FoodAI:AdjustCal",
    "FoodAI:AdjustWt",
    "FoodAI:PhotoAnalyzeStarted",
    "FoodAI:PhotoAnalyzeSucceeded",
    "FoodAI:PhotoAnalyzeFailed",
    "FoodAI:TextAnalyzeStarted",
    "FoodAI:TextAnalyzeSucceeded",
    "FoodAI:TextAnalyzeFailed",
    # Vision escalation (UX instrumentation)
    "FoodAI:VisionEscalationStarted",
    "FoodAI:VisionEscalationCompleted",
    # Onboarding
    "Onboarding:Resume",
    "Onboarding:Restart",
    "Onboarding:ActivitySelected",
    "Onboarding:SpeedSelected",
]
PaymentMethod = Literal["Stripe", "PayPal", "Square", "Crypto"]


class UserProperties(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    is_premium: str | None = None
    url: str | None = None


class EventProperties(BaseModel):
    chat_id: int | None = None
    chat_type: str | None = None
    text: str | None = None
    command: str | None = None
    payment_method: PaymentMethod | None = None
    # Vision escalation structured fields for Amplitude segmentation
    esc_chain: str | None = None
    esc_detail_order: str | None = None
    esc_final_model: str | None = None
    esc_steps: int | None = None
    esc_reason: str | None = None
    esc_total_ms: int | None = None


class Plan(BaseModel):
    branch: str | None = None
    source: str | None = None
    version: str | None = None


class BaseEvent(BaseModel):
    user_id: int
    event_type: EventType
    time: int | None = None  # int(datetime.now().timestamp() * 1000)
    user_properties: UserProperties | None = None
    event_properties: EventProperties | None = None
    app_version: str | None = None
    platform: str | None = None
    carrier: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    dma: str | None = None
    language: str | None = None
    price: float | Decimal | None = None
    quantity: int | None = None
    revenue: float | Decimal | None = None
    productId: str | None = None
    revenueType: str | None = None
    location_lat: float | None = None
    location_lng: float | None = None
    ip: IPvAnyAddress | None = None
    plan: Plan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.model_dump(exclude_none=True).items() if value}


class AbstractAnalyticsLogger(ABC):
    @abstractmethod
    async def log_event(self, event: BaseEvent) -> None:
        pass
