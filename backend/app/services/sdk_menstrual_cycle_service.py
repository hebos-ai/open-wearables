"""Menstrual cycles derived from the daily flow the mobile SDKs send (Apple, Health Connect, Samsung).

Garmin sends whole cycle summaries; the phone platforms only send what the user logged per day
(flow, spotting). Every SDK import stores one ``menstrual_flow`` sample per day and then rebuilds
the user's cycles for that provider from all the stored flow:

- A period is a run of bleeding days (light, medium, heavy or unspecified; spotting does not
  count). A bleeding day more than ``NEW_PERIOD_GAP_DAYS`` after the previous one starts a new
  period.
- A cycle goes from the first day of one period to the day before the next one. The last cycle is
  open: it ends on the last day with data and has no ``cycle_length`` yet.
- Nothing is predicted except ``predicted_cycle_length`` on the open cycle (mean of the last
  completed cycles) and nothing is inferred about ovulation: only the menstrual phase is known.

Each cycle is an ``event_record`` (category ``menstrual_cycle``) with ``external_id``
``<provider>:cycle:<start date>``. Only the cycles whose content changed are rewritten, so the
``menstrual_cycle.created`` webhook fires once per new or changed cycle.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from logging import Logger, getLogger
from statistics import mean
from typing import Any
from uuid import UUID, uuid4

from app.constants.series_types.sdk.reproductive_types import MenstrualFlow, parse_zone_offset
from app.database import DbSession
from app.models import DataPointSeries, DataSource, EventRecord, MenstrualCycleDetails
from app.schemas.enums import SeriesType, get_series_type_id
from app.schemas.model_crud.activities import EventRecordCreate, MenstrualCycleDetailCreate
from app.services.event_record_service import event_record_service

NEW_PERIOD_GAP_DAYS = 7
LOOKBACK_DAYS = 730
MAX_CYCLES_FOR_PREDICTION = 6


@dataclass
class FlowDay:
    day: date
    flow: MenstrualFlow
    zone_offset: str | None = None
    source: str | None = None
    device_model: str | None = None


@dataclass
class DerivedCycle:
    start: date
    end_exclusive: date
    period_length: int
    cycle_length: int | None
    day_in_cycle: int
    current_phase_type: str | None
    predicted_cycle_length: int | None
    daily_flow: list[dict[str, Any]] = field(default_factory=list)
    zone_offset: str | None = None
    source: str | None = None
    device_model: str | None = None

    def signature(self) -> tuple:
        return (
            self.start,
            self.end_exclusive,
            self.period_length,
            self.cycle_length,
            self.day_in_cycle,
            self.current_phase_type,
            self.predicted_cycle_length,
            tuple((d["date"], d["flow"]) for d in self.daily_flow),
        )


def build_cycles(days: dict[date, FlowDay]) -> list[DerivedCycle]:
    """Group daily flow into cycles. Pure function (no DB)."""
    if not days:
        return []
    bleeding = sorted(d for d, fd in days.items() if fd.flow.is_period_bleeding)
    if not bleeding:
        return []

    periods: list[tuple[date, date]] = []
    start = prev = bleeding[0]
    for d in bleeding[1:]:
        if (d - prev).days > NEW_PERIOD_GAP_DAYS:
            periods.append((start, prev))
            start = d
        prev = d
    periods.append((start, prev))

    last_day = max(days)
    cycles: list[DerivedCycle] = []
    completed_lengths: list[int] = []
    for i, (p_start, p_end) in enumerate(periods):
        next_start = periods[i + 1][0] if i + 1 < len(periods) else None
        period_length = (p_end - p_start).days + 1
        if next_start is not None:
            cycle_length = (next_start - p_start).days
            end_exclusive = next_start
            day_in_cycle = cycle_length
            phase = None
            predicted = None
            completed_lengths.append(cycle_length)
        else:
            cycle_length = None
            end_exclusive = max(last_day, p_end) + timedelta(days=1)
            day_in_cycle = (end_exclusive - p_start).days
            phase = "menstrual" if day_in_cycle <= period_length else None
            recent = completed_lengths[-MAX_CYCLES_FOR_PREDICTION:]
            predicted = round(mean(recent)) if recent else None

        in_cycle = [days[d] for d in sorted(days) if p_start <= d < end_exclusive]
        first = days[p_start]
        cycles.append(
            DerivedCycle(
                start=p_start,
                end_exclusive=end_exclusive,
                period_length=period_length,
                cycle_length=cycle_length,
                day_in_cycle=day_in_cycle,
                current_phase_type=phase,
                predicted_cycle_length=predicted,
                daily_flow=[
                    {
                        "date": fd.day.isoformat(),
                        "day_in_cycle": (fd.day - p_start).days + 1,
                        "flow": fd.flow.label,
                    }
                    for fd in in_cycle
                ],
                zone_offset=first.zone_offset,
                source=first.source,
                device_model=first.device_model,
            )
        )
    return cycles


class SDKMenstrualCycleService:
    def __init__(self, log: Logger | None = None) -> None:
        self.log = log or getLogger(__name__)

    def _load_flow_days(self, db: DbSession, user_id: UUID, provider: str) -> dict[date, FlowDay]:
        since = datetime.now().astimezone() - timedelta(days=LOOKBACK_DAYS)
        rows = (
            db.query(DataPointSeries, DataSource)
            .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
            .filter(
                DataSource.user_id == user_id,
                DataSource.provider == provider,
                DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.menstrual_flow),
                DataPointSeries.recorded_at >= since,
            )
            .order_by(DataPointSeries.recorded_at)
            .all()
        )
        days: dict[date, FlowDay] = {}
        for sample, data_source in rows:
            try:
                flow = MenstrualFlow(int(sample.value))
            except ValueError:
                continue
            day = sample.recorded_at.astimezone(parse_zone_offset(sample.zone_offset)).date()
            current = days.get(day)
            # Several records for one day (two apps, flow + spotting): keep the strongest bleeding.
            if current is None or _rank(flow) > _rank(current.flow):
                days[day] = FlowDay(
                    day=day,
                    flow=flow,
                    zone_offset=sample.zone_offset,
                    source=data_source.source,
                    device_model=data_source.device_model,
                )
        return days

    def _load_existing(
        self, db: DbSession, user_id: UUID, provider: str
    ) -> dict[str, tuple[EventRecord, MenstrualCycleDetails | None]]:
        rows = (
            db.query(EventRecord, MenstrualCycleDetails)
            .join(DataSource, EventRecord.data_source_id == DataSource.id)
            .outerjoin(MenstrualCycleDetails, MenstrualCycleDetails.record_id == EventRecord.id)
            .filter(
                DataSource.user_id == user_id,
                DataSource.provider == provider,
                EventRecord.category == "menstrual_cycle",
                EventRecord.external_id.like(f"{provider}:cycle:%"),
            )
            .all()
        )
        return {record.external_id: (record, detail) for record, detail in rows if record.external_id}

    @staticmethod
    def _existing_signature(record: EventRecord, detail: MenstrualCycleDetails | None) -> tuple | None:
        if detail is None:
            return None
        flow = tuple((d.get("date"), d.get("flow")) for d in (detail.daily_flow or []))
        tz = parse_zone_offset(record.zone_offset)
        return (
            record.start_datetime.astimezone(tz).date(),
            record.end_datetime.astimezone(tz).date(),
            detail.period_length,
            detail.cycle_length,
            detail.day_in_cycle,
            detail.current_phase_type,
            detail.predicted_cycle_length,
            flow,
        )

    def rebuild(self, db: DbSession, user_id: UUID, provider: str) -> int:
        """Rebuild the user's cycles for ``provider``. Returns how many cycles were (re)written.

        The caller commits; webhooks fire on commit.
        """
        cycles = build_cycles(self._load_flow_days(db, user_id, provider))
        existing = self._load_existing(db, user_id, provider)

        wanted: dict[str, DerivedCycle] = {f"{provider}:cycle:{c.start.isoformat()}": c for c in cycles}
        to_delete: list[UUID] = []
        to_write: list[tuple[str, DerivedCycle]] = []
        for external_id, (record, detail) in existing.items():
            cycle = wanted.get(external_id)
            if cycle is None or self._existing_signature(record, detail) != cycle.signature():
                to_delete.append(record.id)
        for external_id, cycle in wanted.items():
            current = existing.get(external_id)
            if current is None or current[0].id in to_delete:
                to_write.append((external_id, cycle))

        if to_delete:
            db.query(EventRecord).filter(EventRecord.id.in_(to_delete)).delete(synchronize_session=False)
            db.flush()
        if not to_write:
            return 0

        records: list[EventRecordCreate] = []
        details: list[MenstrualCycleDetailCreate] = []
        for external_id, cycle in to_write:
            tz = parse_zone_offset(cycle.zone_offset)
            start_dt = datetime.combine(cycle.start, datetime.min.time(), tzinfo=tz)
            end_dt = datetime.combine(cycle.end_exclusive, datetime.min.time(), tzinfo=tz)
            record = EventRecordCreate(
                id=uuid4(),
                category="menstrual_cycle",
                type="menstrual",
                source_name=(cycle.source or provider)[:64],
                device_model=cycle.device_model,
                duration_seconds=int((end_dt - start_dt).total_seconds()),
                start_datetime=start_dt,
                end_datetime=end_dt,
                zone_offset=cycle.zone_offset,
                external_id=external_id,
                source=cycle.source,
                provider=provider,
                user_id=user_id,
            )
            records.append(record)
            details.append(
                MenstrualCycleDetailCreate(
                    record_id=record.id,
                    day_in_cycle=cycle.day_in_cycle,
                    current_phase_type=cycle.current_phase_type,
                    cycle_length=cycle.cycle_length,
                    predicted_cycle_length=cycle.predicted_cycle_length,
                    is_predicted_cycle=False,
                    period_length=cycle.period_length,
                    last_updated_at=datetime.now().astimezone(),
                    daily_flow=cycle.daily_flow,
                )
            )

        inserted = set(event_record_service.bulk_create(db, records))
        db.flush()
        details = [d for d in details if d.record_id in inserted]
        if details:
            event_record_service.bulk_create_details(db, details, detail_type="menstrual_cycle")  # ty:ignore[invalid-argument-type]
        return len(details)


def _rank(flow: MenstrualFlow) -> int:
    """Order for picking one flow per day: heavier bleeding wins, then any bleeding, spotting, none."""
    return {
        MenstrualFlow.NONE: 0,
        MenstrualFlow.SPOTTING: 1,
        MenstrualFlow.UNSPECIFIED: 2,
        MenstrualFlow.LIGHT: 3,
        MenstrualFlow.MEDIUM: 4,
        MenstrualFlow.HEAVY: 5,
    }[flow]


sdk_menstrual_cycle_service = SDKMenstrualCycleService()
