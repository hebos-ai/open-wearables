"""Provider-agnostic planned (structured) workout, and the push request/result around it.

``PlannedWorkout`` is the canonical shape every provider translator consumes (``name`` /
``sport`` / ``steps[]`` with ``kind``, ``durationSec`` | ``distanceM``, an optional
``target`` and optional ``name`` / ``note``), so the same JSON can be sent to any provider.
``steps[]`` may also hold repeat groups ``{"kind": "repeat", "repetitions": n, "steps": [...]}``
(one level deep), which providers map to their native repeats instead of flattening.
Both ``snake_case`` and ``camelCase`` keys are accepted.

Units (absolute values only, so translators never need athlete thresholds):

* ``duration_sec``: seconds
* ``distance_m``: metres
* ``target.type == "power"``: watts
* ``target.type == "hr"``: beats per minute
* ``target.type == "pace"``: speed in metres per second (same convention as Garmin Training API)
"""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

_CANONICAL_CONFIG = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

StepKind = Literal["warmup", "interval", "recovery", "cooldown", "rest"]
TargetType = Literal["power", "hr", "pace"]


class PlannedWorkoutTarget(BaseModel):
    """Intensity target of a step. At least one bound is required; a single bound means "hold this"."""

    model_config = _CANONICAL_CONFIG

    type: TargetType
    low: float | None = Field(None, gt=0)
    high: float | None = Field(None, gt=0)

    @model_validator(mode="after")
    def _check_bounds(self) -> "PlannedWorkoutTarget":
        if self.low is None and self.high is None:
            raise ValueError("target needs at least one of low/high")
        if self.low is not None and self.high is not None and self.low > self.high:
            raise ValueError("target.low must be <= target.high")
        return self

    @property
    def bounds(self) -> tuple[float, float]:
        """(low, high) with a missing bound filled from the other one."""
        low = self.low if self.low is not None else self.high
        high = self.high if self.high is not None else self.low
        if low is None or high is None:  # unreachable: _check_bounds requires one bound
            raise ValueError("target needs at least one of low/high")
        return low, high


class PlannedWorkoutStep(BaseModel):
    """One step. Ends after ``duration_sec`` or ``distance_m``; with neither it is an open (lap-button) step."""

    model_config = _CANONICAL_CONFIG

    kind: StepKind
    duration_sec: int | None = Field(None, gt=0)
    distance_m: float | None = Field(None, gt=0)
    target: PlannedWorkoutTarget | None = None
    name: str | None = Field(None, min_length=1, max_length=100, description="Step label, e.g. 'Work', 'Jog'")
    note: str | None = Field(None, min_length=1, max_length=500, description="Free-text note for the step")

    @model_validator(mode="after")
    def _single_end_condition(self) -> "PlannedWorkoutStep":
        if self.duration_sec is not None and self.distance_m is not None:
            raise ValueError("a step ends either by duration_sec or by distance_m, not both")
        return self


class PlannedWorkoutRepeat(BaseModel):
    """A block of steps done ``repetitions`` times in order (e.g. 5 × (3:30 work + 3:00 jog)). One level deep."""

    model_config = _CANONICAL_CONFIG

    kind: Literal["repeat"]
    repetitions: int = Field(ge=1, le=99)
    steps: list[PlannedWorkoutStep] = Field(min_length=1, max_length=50)


PlannedWorkoutItem = Annotated[PlannedWorkoutRepeat | PlannedWorkoutStep, Field(discriminator="kind")]


class PlannedWorkout(BaseModel):
    """Canonical structured workout (provider-agnostic)."""

    model_config = _CANONICAL_CONFIG

    name: str = Field(min_length=1, max_length=100)
    # Free-form sport key. Translators recognise at least: run, bike, mtb, swim, strength
    # (and common synonyms such as running / cycling); unsupported sports are rejected per provider.
    sport: str = Field(min_length=1, max_length=40)
    steps: list[PlannedWorkoutItem] = Field(min_length=1, max_length=200)

    def flat_steps(self) -> list[PlannedWorkoutStep]:
        """Every step in execution order, with repeat groups expanded."""
        out: list[PlannedWorkoutStep] = []
        for item in self.steps:
            if isinstance(item, PlannedWorkoutRepeat):
                for _ in range(item.repetitions):
                    out.extend(item.steps)
            else:
                out.append(item)
        return out


class PlannedWorkoutPushRequest(BaseModel):
    """Body of ``POST /providers/{provider}/users/{user_id}/planned-workouts``."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    workout: PlannedWorkout
    scheduled_at: datetime | None = Field(
        None,
        description="When the athlete should do it (timezone-aware). Omit to only add it to the athlete's library.",
    )
    external_id: str | None = Field(
        None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description=(
            "Caller's stable id for this workout. Re-sending the same id REPLACES the workout (content and date) "
            "on the provider instead of creating another one, and it is the key used to delete it."
        ),
    )
    description: str | None = Field(None, max_length=1000)

    @model_validator(mode="after")
    def _scheduled_at_is_aware(self) -> "PlannedWorkoutPushRequest":
        if self.scheduled_at is not None and self.scheduled_at.tzinfo is None:
            raise ValueError("scheduled_at must include a timezone offset")
        return self


class PlannedWorkoutPushResult(BaseModel):
    """What the provider created. ``provider_*`` ids let the caller reconcile or delete later."""

    provider: str
    status: Literal["created", "scheduled"]
    external_id: str
    provider_plan_id: str | None = None
    provider_workout_id: str | None = None
    scheduled_at: datetime | None = None


class PlannedWorkoutDeleteResult(BaseModel):
    """Result of removing a previously pushed workout. ``deleted`` is false when nothing was known for that id."""

    provider: str
    external_id: str
    deleted: bool
