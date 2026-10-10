"""Wahoo Cloud API workout payloads.

Source: https://cloud-api.wahooligan.com/ (``GET /v1/workouts``, ``GET /v1/workouts/{id}``
and the ``workout_summary`` webhook). Wahoo serialises every numeric summary value as a
decimal *string* (``"24909.71"``); Pydantic coerces them to ``Decimal``. Every summary
field is optional: manual or in-progress workouts may omit any of them.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class WahooFileJSON(BaseModel):
    """Link to the raw FIT file of a workout."""

    model_config = ConfigDict(extra="allow")

    url: str | None = None


class WahooWorkoutSummaryJSON(BaseModel):
    """Aggregated metrics of a completed workout (``workout_summary``)."""

    model_config = ConfigDict(extra="allow")

    id: int | None = None
    ascent_accum: Decimal | None = None  # metres
    cadence_avg: Decimal | None = None  # rpm / spm
    calories_accum: Decimal | None = None  # kcal
    distance_accum: Decimal | None = None  # metres
    duration_active_accum: Decimal | None = None  # seconds (moving time)
    duration_paused_accum: Decimal | None = None  # seconds
    duration_total_accum: Decimal | None = None  # seconds (elapsed)
    heart_rate_avg: Decimal | None = None  # bpm
    power_avg: Decimal | None = None  # watts
    power_bike_np_last: Decimal | None = None  # watts (normalized power)
    power_bike_tss_last: Decimal | None = None  # TSS
    speed_avg: Decimal | None = None  # m/s
    work_accum: Decimal | None = None  # joules
    time_zone: str | None = None  # IANA name, e.g. "America/Denver"
    manual: bool | None = None
    edited: bool | None = None
    fitness_app_id: int | None = None
    file: WahooFileJSON | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WahooWorkoutJSON(BaseModel):
    """A Wahoo workout (``GET /v1/workouts`` item)."""

    model_config = ConfigDict(extra="allow")

    id: int
    starts: datetime
    minutes: int | None = None
    name: str | None = None
    plan_id: int | None = None
    workout_token: str | None = None
    workout_type_id: int | None = None
    workout_summary: WahooWorkoutSummaryJSON | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WahooWorkoutListJSON(BaseModel):
    """Paginated ``GET /v1/workouts`` response."""

    model_config = ConfigDict(extra="allow")

    workouts: list[WahooWorkoutJSON] = []
    total: int | None = None
    page: int | None = None
    per_page: int | None = None
