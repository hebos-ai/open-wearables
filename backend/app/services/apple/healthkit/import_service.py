import hashlib
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
from logging import Logger, getLogger
from typing import Iterable, TypedDict
from uuid import UUID, uuid4

import sentry_sdk
from pydantic import ValidationError

from app.constants.series_types.sdk import (
    SDKMetricType,
    WorkoutStatisticType,
    get_detail_field_from_workout_statistic_type,
    get_series_type_from_metric_type,
    get_series_type_from_workout_statistic_type,
)
from app.constants.series_types.sdk.reproductive_types import (
    MenstrualFlow,
    flow_from_record,
    is_reproductive_type,
    local_days,
    parse_zone_offset,
)
from app.constants.workout_types import get_unified_apple_workout_type_sdk
from app.database import DbSession
from app.models import DataPointSeries, DataSource
from app.repositories.user_connection_repository import UserConnectionRepository
from app.schemas.enums import SeriesType, daily_total_flag, get_series_type_id, get_series_type_unit
from app.schemas.model_crud.activities import (
    EventRecordCreate,
    EventRecordDetailCreate,
    EventRecordMetrics,
    HeartRateSampleCreate,
    StepSampleCreate,
    TimeSeriesSampleCreate,
)
from app.schemas.providers.mobile_sdk import (
    SyncRequest as SDKSyncRequest,
)
from app.schemas.providers.mobile_sdk import (
    WorkoutStatistic,
)
from app.schemas.providers.mobile_sdk.sync_request import (
    MetricRecord,
    SleepRecord,
    SyncRequestData,
    Workout,
)
from app.schemas.responses.upload import UploadDataResponse
from app.services.event_record_service import event_record_service
from app.services.sdk_menstrual_cycle_service import sdk_menstrual_cycle_service
from app.services.timeseries_service import timeseries_service
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

from .device_resolution import extract_device_info
from .sleep_service import handle_sleep_data

# Health Connect's own mg/dL converter uses exactly 18.0, so values written to HC
# in mg/dL round-trip with a ~0.1% offset under this factor.
MMOL_L_TO_MG_DL = Decimal("18.0182")

# Skin temperature readings below this magnitude are deviations from the user's baseline (Health
# Connect deltas, apps that write the deviation); above it they are absolute readings in °C.
SKIN_TEMPERATURE_DEVIATION_LIMIT_C = Decimal("5")
# Wrist temperature: baseline = mean of the previous nights (at least MIN nights in WINDOW days).
WRIST_BASELINE_WINDOW_DAYS = 28
WRIST_BASELINE_MIN_NIGHTS = 3
_WRIST_TEMPERATURE_TYPES = frozenset(
    {SDKMetricType.APPLE_SLEEPING_WRIST_TEMPERATURE.value, SDKMetricType.ANDROID_SKIN_TEMPERATURE.value}
)

# Unit factors to the series unit for nutrition and hydration (SDK units as on a nutrition label).
_MASS_IN_GRAMS: dict[str, Decimal] = {
    "g": Decimal("1"),
    "mg": Decimal("0.001"),
    "mcg": Decimal("0.000001"),
    "µg": Decimal("0.000001"),
    "ug": Decimal("0.000001"),
    "kg": Decimal("1000"),
}
_ENERGY_IN_KCAL: dict[str, Decimal] = {
    "kcal": Decimal("1"),
    "cal": Decimal("1"),  # HealthKit "Cal" is the food calorie (kcal)
    "kj": Decimal("0.239006"),
}
_VOLUME_IN_ML: dict[str, Decimal] = {"ml": Decimal("1"), "l": Decimal("1000")}


def _intake_offset(record_id: str | None) -> timedelta:
    """Sub-millisecond shift, fixed per record, for food and water samples.

    A series keeps one row per (source, instant) and the foods of one meal usually share the
    meal's time, so without it each food overwrites the previous one. The shift depends only on
    the record id: re-sending a record updates its own row.
    """
    if not record_id:
        return timedelta(0)
    digest = hashlib.sha256(record_id.encode()).digest()
    return timedelta(microseconds=int.from_bytes(digest[:4], "big") % 1000)


_SDK_ITEM_MODELS = (("records", MetricRecord), ("sleep", SleepRecord), ("workouts", Workout))


class InvalidRecord(TypedDict):
    """A single record dropped by per-record validation (PII-free — only the location, no values)."""

    collection: str  # "records" | "sleep" | "workouts"
    index: int
    loc: str
    msg: str | None
    type: str | None


class LoadDataResult(TypedDict):
    workouts_saved: int
    records_saved: int
    types: list[str]  # series types written
    sleep_saved: int
    dropped: list[InvalidRecord]
    validation_ms: float


def _parse_sync_request(raw: dict) -> tuple[SDKSyncRequest, list[InvalidRecord]]:
    """Parse a raw payload into a SyncRequest, salvaging valid records when items fail.

    Fast path validates in one shot. On failure, valid records are kept and the per-item
    failures returned (PII-free: loc/msg/type). Raises ValidationError when the envelope or
    a container shape is invalid — nothing salvageable, so the batch is rejected (400).
    """
    try:
        return SDKSyncRequest(**raw), []
    except ValidationError:
        pass  # fall through to per-item validation

    # Only individual records are salvageable. A malformed container shape (data not a dict,
    # or a collection not a list) is not — re-validate to re-raise the real ValidationError.
    data = raw.get("data")
    if not isinstance(data, dict) or any(not isinstance(data.get(key, []), list) for key, _ in _SDK_ITEM_MODELS):
        return SDKSyncRequest(**raw), []

    kept: dict[str, list] = {}
    dropped: list[InvalidRecord] = []
    for key, model in _SDK_ITEM_MODELS:
        good: list = []
        for idx, item in enumerate(data.get(key, [])):
            try:
                good.append(model.model_validate(item))
            except ValidationError as exc:
                err = (exc.errors() or [{}])[0]
                dropped.append(
                    {
                        "collection": key,
                        "index": idx,
                        "loc": ".".join(str(x) for x in err.get("loc", [])),
                        "msg": err.get("msg"),
                        "type": err.get("type"),
                    }
                )
        kept[key] = good

    # Reconstruct via model_validate so Pydantic still rejects a bad envelope; `data` now
    # carries only the kept records.
    request = SDKSyncRequest.model_validate(
        {**raw, "data": SyncRequestData(records=kept["records"], sleep=kept["sleep"], workouts=kept["workouts"])}
    )
    return request, dropped


class ImportService:
    def __init__(
        self,
        log: Logger,
    ):
        self.log = log
        self.event_record_service = event_record_service
        self.timeseries_service = timeseries_service
        self.user_connection_repo = UserConnectionRepository()

    def _dec(self, value: float | int | Decimal | None) -> Decimal | None:
        return None if value is None else Decimal(str(value))

    def _build_workout_bundles(
        self,
        request: SDKSyncRequest,
        user_id: str,
    ) -> Iterable[tuple[EventRecordCreate, EventRecordDetailCreate, list[TimeSeriesSampleCreate]]]:
        """
        Given the parsed SDKSyncRequest, yield tuples of
        (EventRecordCreate, EventRecordDetailCreate) ready to insert into your ORM session.
        """
        user_uuid = UUID(user_id)
        provider = request.provider

        for wjson in request.data.workouts:
            workout_id = uuid4()
            external_id = wjson.id if wjson.id else None

            device_model, software_version, original_source_name = extract_device_info(wjson.source)

            metrics, time_series_samples, duration = self._extract_metrics_from_workout_stats(
                wjson.values,
                user_uuid,
                device_model,
                software_version,
                wjson.endDate,
                wjson.zoneOffset,
                provider,
                original_source_name,
            )

            if duration is None:
                duration = int((wjson.endDate - wjson.startDate).total_seconds())

            workout_type = wjson.type.lower() if wjson.type else None
            type = get_unified_apple_workout_type_sdk(workout_type).value if workout_type else None

            record = EventRecordCreate(
                category="workout",
                type=type,
                source_name=original_source_name or "unknown",
                device_model=device_model,
                duration_seconds=int(duration),
                start_datetime=wjson.startDate,
                end_datetime=wjson.endDate,
                zone_offset=wjson.zoneOffset,
                id=workout_id,
                external_id=external_id,
                source=original_source_name,
                software_version=software_version,
                provider=provider,
                user_id=user_uuid,
            )

            detail = EventRecordDetailCreate(
                record_id=workout_id,
                **metrics,
            )

            yield record, detail, time_series_samples

    def _normalize_unit(self, series_type: SeriesType, value: Decimal, provider: str | None = None) -> Decimal:
        match series_type:
            # meters → cm
            case SeriesType.height | SeriesType.walking_step_length:
                return value * 100
            # 0-1 fraction → percent (body_fat only for Apple; Health Connect already reports percent)
            case SeriesType.body_fat_percentage if provider == "apple":
                return value * 100
            # HealthKit walking metrics returned as 0-1 fraction, DB stores percent
            # apple-only metrics, so no need to check if provider is apple
            case (
                SeriesType.walking_double_support_percentage
                | SeriesType.walking_asymmetry_percentage
                | SeriesType.walking_steadiness
            ):
                return value * 100
            case _:
                return value

    @staticmethod
    def _convert_intake_unit(series_type: SeriesType, value: Decimal, unit: str | None) -> Decimal:
        """Bring nutrition and hydration values to the series unit (g/mg/mcg, kcal, mL)."""
        if not unit:
            return value
        src = unit.strip().lower()
        target = get_series_type_unit(series_type).lower()
        for table in (_MASS_IN_GRAMS, _ENERGY_IN_KCAL, _VOLUME_IN_ML):
            if src in table and target in table:
                return value * table[src] / table[target]
        return value

    def _build_flow_samples(self, rjson: MetricRecord, user_uuid: UUID, provider: str) -> list[TimeSeriesSampleCreate]:
        """One ``menstrual_flow`` sample per local day covered by an SDK menstrual record."""
        flow = flow_from_record(rjson.type or "", rjson.value)
        if flow is None:
            return []
        device_model, software_version, original_source_name = extract_device_info(rjson.source)
        tz = parse_zone_offset(rjson.zoneOffset)
        samples = []
        for day in local_days(rjson.startDate, rjson.endDate, rjson.zoneOffset):
            samples.append(
                TimeSeriesSampleCreate(
                    id=uuid4(),
                    external_id=f"{rjson.id}:{day.isoformat()}" if rjson.id else None,
                    user_id=user_uuid,
                    source=original_source_name,
                    device_model=device_model,
                    software_version=software_version,
                    provider=provider,
                    recorded_at=datetime.combine(day, datetime.min.time(), tzinfo=tz),
                    zone_offset=rjson.zoneOffset,
                    value=Decimal(int(flow)),
                    series_type=SeriesType.menstrual_flow,
                    is_daily_total=daily_total_flag(SeriesType.menstrual_flow, is_daily=True),
                )
            )
        return samples

    @staticmethod
    def _keep_flow_amounts(
        db_session: DbSession,
        samples: list[TimeSeriesSampleCreate],
    ) -> list[TimeSeriesSampleCreate]:
        """Drop "bleeding, amount not given" days that would overwrite a known amount.

        Health Connect writes a period record (no amount) over the same days as the per-day flow
        records, and both land on the same series row (one per day). Without this, whichever comes
        last wins, so a re-sent period record would wipe out "heavy" or "light".
        """
        unspecified = Decimal(int(MenstrualFlow.UNSPECIFIED))
        amounts = {Decimal(int(f)) for f in (MenstrualFlow.LIGHT, MenstrualFlow.MEDIUM, MenstrualFlow.HEAVY)}
        flow = [s for s in samples if s.series_type == SeriesType.menstrual_flow]

        # Days that already have an amount: in this batch or stored for this user and provider.
        with_amount = {s.recorded_at for s in flow if Decimal(str(s.value)) in amounts}
        candidates = [s for s in flow if Decimal(str(s.value)) == unspecified]
        if candidates:
            rows = (
                db_session.query(DataPointSeries.recorded_at)
                .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
                .filter(
                    DataSource.user_id == candidates[0].user_id,
                    DataSource.provider == candidates[0].provider,
                    DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.menstrual_flow),
                    DataPointSeries.recorded_at.in_({s.recorded_at for s in candidates}),
                    DataPointSeries.value.in_(amounts),
                )
                .all()
            )
            with_amount.update(recorded_at for (recorded_at,) in rows)

        return [
            s
            for s in samples
            if not (
                s.series_type == SeriesType.menstrual_flow
                and Decimal(str(s.value)) == unspecified
                and s.recorded_at in with_amount
            )
        ]

    def _derive_wrist_temperature_deviation(
        self,
        db_session: DbSession,
        samples: list[TimeSeriesSampleCreate],
    ) -> list[TimeSeriesSampleCreate]:
        """Deviation of each absolute wrist-temperature reading from the user's previous nights.

        Apple stores the overnight wrist temperature in °C and the Health app shows it as a
        deviation from the user's baseline; only the deviation is comparable across people. The
        baseline is the mean of one value per earlier night (at least WRIST_BASELINE_MIN_NIGHTS in
        the last WRIST_BASELINE_WINDOW_DAYS). Without enough history no deviation is written.
        """
        wrist = [s for s in samples if s.series_type == SeriesType.skin_temperature]
        if not wrist:
            return []
        user_id = wrist[0].user_id
        provider = wrist[0].provider
        since = min(s.recorded_at for s in wrist) - timedelta(days=WRIST_BASELINE_WINDOW_DAYS + 1)
        until = max(s.recorded_at for s in wrist)
        rows = (
            db_session.query(DataPointSeries.recorded_at, DataPointSeries.value)
            .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
            .filter(
                DataSource.user_id == user_id,
                DataSource.provider == provider,
                DataPointSeries.series_type_definition_id == get_series_type_id(SeriesType.skin_temperature),
                DataPointSeries.recorded_at >= since,
                DataPointSeries.recorded_at <= until,
            )
            .all()
        )
        history = [(recorded_at, Decimal(str(value))) for recorded_at, value in rows]
        # The batch itself also counts as history for later nights in the same upload.
        history += [(s.recorded_at, Decimal(str(s.value))) for s in wrist]

        derived: list[TimeSeriesSampleCreate] = []
        for sample in wrist:
            window_start = sample.recorded_at - timedelta(days=WRIST_BASELINE_WINDOW_DAYS)
            nights: dict[object, list[Decimal]] = {}
            for recorded_at, value in history:
                # Earlier nights only: at least 12 h before this reading.
                if window_start <= recorded_at <= sample.recorded_at - timedelta(hours=12):
                    nights.setdefault(recorded_at.date(), []).append(value)
            if len(nights) < WRIST_BASELINE_MIN_NIGHTS:
                continue
            per_night = [sum(v, Decimal(0)) / len(v) for v in nights.values()]
            baseline = sum(per_night, Decimal(0)) / len(per_night)
            derived.append(
                sample.model_copy(
                    update={
                        "id": uuid4(),
                        "external_id": f"{sample.external_id}:deviation" if sample.external_id else None,
                        "series_type": SeriesType.skin_temperature_deviation,
                        "value": (Decimal(str(sample.value)) - baseline).quantize(Decimal("0.001")),
                    }
                )
            )
        return derived

    def _build_statistic_bundles(
        self,
        request: SDKSyncRequest,
        user_id: str,
    ) -> list[HeartRateSampleCreate | StepSampleCreate | TimeSeriesSampleCreate]:
        time_series_samples: list[HeartRateSampleCreate | StepSampleCreate | TimeSeriesSampleCreate] = []
        user_uuid = UUID(user_id)
        provider = request.provider

        for rjson in request.data.records:
            record_type = rjson.type or ""
            if is_reproductive_type(record_type):
                time_series_samples.extend(self._build_flow_samples(rjson, user_uuid, provider))
                continue

            value = Decimal(str(rjson.value))
            series_type = get_series_type_from_metric_type(record_type)

            if not series_type:
                continue
            value = self._normalize_unit(series_type, value, provider)

            # Skin / wrist temperature: small values are deviations from baseline, large values
            # are absolute readings. Never stored as body (core) temperature.
            if record_type in _WRIST_TEMPERATURE_TYPES:
                series_type = (
                    SeriesType.skin_temperature_deviation
                    if abs(value) < SKIN_TEMPERATURE_DEVIATION_LIMIT_C
                    else SeriesType.skin_temperature
                )

            recorded_at = rjson.startDate
            if series_type == SeriesType.hydration or series_type.value.startswith("dietary_"):
                value = self._convert_intake_unit(series_type, value, rjson.unit)
                recorded_at = recorded_at + _intake_offset(rjson.id)

            # Health Connect reports blood glucose in mmol/L; the series unit is mg/dL.
            if series_type == SeriesType.blood_glucose and (rjson.unit or "").lower().startswith("mmol"):
                value = value * MMOL_L_TO_MG_DL

            # Extract device info
            device_model, software_version, original_source_name = extract_device_info(rjson.source)

            sample = TimeSeriesSampleCreate(
                id=uuid4(),
                external_id=rjson.id,
                user_id=user_uuid,
                source=original_source_name,
                device_model=device_model,
                software_version=software_version,
                provider=provider,
                recorded_at=recorded_at,
                zone_offset=rjson.zoneOffset,
                value=value,
                series_type=series_type,
                is_daily_total=daily_total_flag(series_type, is_daily=False),
            )

            match series_type:
                case SeriesType.heart_rate:
                    time_series_samples.append(HeartRateSampleCreate(**sample.model_dump()))
                case SeriesType.steps:
                    time_series_samples.append(StepSampleCreate(**sample.model_dump()))
                case _:
                    time_series_samples.append(sample)

        return time_series_samples

    def _compute_aggregates(self, values: list[Decimal]) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
        if not values:
            return None, None, None
        min_v = min(values)
        max_v = max(values)
        avg_v = sum(values, Decimal("0")) / Decimal(len(values))
        return min_v, max_v, avg_v

    def _extract_metrics_from_workout_stats(
        self,
        stats: list[WorkoutStatistic] | None,
        user_uuid: UUID,
        device_model: str | None,
        software_version: str | None,
        end_date: datetime,
        zone_offset: str | None,
        provider: str,
        source_name: str | None,
    ) -> tuple[EventRecordMetrics, list[TimeSeriesSampleCreate], int | float | None]:
        """
        Returns a tuple with the metrics, time series samples, and duration.
        """
        if stats is None:
            return EventRecordMetrics(), [], None

        stats_dict: dict[str, Decimal | int] = {}
        stats_dict["energy_burned"] = Decimal("0")
        time_series_samples: list[TimeSeriesSampleCreate] = []
        duration: float | None = None

        for stat in stats:
            value = self._dec(stat.value)
            if value is None or stat.type is None:
                continue

            series_type = get_series_type_from_workout_statistic_type(stat.type)
            if series_type:
                time_series_samples.append(
                    TimeSeriesSampleCreate(
                        id=uuid4(),
                        external_id=None,
                        user_id=user_uuid,
                        source=source_name,
                        device_model=device_model,
                        software_version=software_version,
                        provider=provider,
                        recorded_at=end_date,
                        zone_offset=zone_offset,
                        value=value,
                        series_type=series_type,
                        is_daily_total=daily_total_flag(series_type, is_daily=False),
                    )
                )
                continue

            match stat.type:
                # duration is sent as a workout statistic but stored separately
                case WorkoutStatisticType.DURATION | WorkoutStatisticType.TOTAL_DURATION:
                    duration = float(value) / 1000 if stat.unit == "ms" else float(value)
                case (
                    WorkoutStatisticType.ACTIVE_ENERGY_BURNED
                    | WorkoutStatisticType.BASAL_ENERGY_BURNED
                    | WorkoutStatisticType.CALORIES
                    | WorkoutStatisticType.TOTAL_CALORIES
                ):
                    stats_dict["energy_burned"] += value
                case _:
                    detail_field = get_detail_field_from_workout_statistic_type(stat.type)
                    if detail_field:
                        # Apple SDK may send fractional Decimals for integer fields (e.g. stepCount)
                        if detail_field in ("steps_count", "moving_time_seconds"):
                            value = int(value)
                        stats_dict[detail_field] = value

        return EventRecordMetrics(**stats_dict), time_series_samples, duration

    def load_data(
        self,
        db_session: DbSession,
        raw: dict,
        user_id: str,
        batch_id: str | None = None,
    ) -> LoadDataResult:
        """
        Load data into database and return counts of saved items plus per-record failures.
        """
        # Per-record validation: keep valid records, drop+report the invalid ones instead
        # of failing the whole batch. Raises ValidationError only if the envelope is invalid.
        # validation_ms measures the parse/per-record pass so we can watch the cost of a
        # malformed payload in the logs.
        started = time.perf_counter()
        request, dropped = _parse_sync_request(raw)
        validation_ms = round((time.perf_counter() - started) * 1000, 1)
        workouts_saved = 0
        records_saved = 0
        sleep_saved = 0
        types: set[str] = set()

        # Process workouts in batch
        workout_bundles = list(self._build_workout_bundles(request, user_id))
        if workout_bundles:
            records = [record for record, _, _ in workout_bundles]
            details_by_id = {detail.record_id: detail for _, detail, _ in workout_bundles}
            # Flatten all time series samples from all workouts into a single list
            time_series_samples = [sample for _, _, samples in workout_bundles for sample in samples]

            # Bulk create records - returns only IDs that were actually inserted
            inserted_ids = self.event_record_service.bulk_create(db_session, records)
            db_session.flush()

            # Filter details to only those records that were actually inserted (avoid FK violation)
            details_to_insert = [details_by_id[rid] for rid in inserted_ids if rid in details_by_id]

            # Bulk create details (requires event_record to exist due to FK)
            if details_to_insert:
                self.event_record_service.bulk_create_details(db_session, details_to_insert, detail_type="workout")
            workouts_saved = len(inserted_ids)

            # Bulk create time series samples
            if time_series_samples:
                self.timeseries_service.bulk_create_samples(db_session, time_series_samples)
                records_saved += len(time_series_samples)
                types.update(sample.series_type.value for sample in time_series_samples)

        # Process time series samples (records)
        samples = self._build_statistic_bundles(request, user_id)
        has_menstrual_flow = any(sample.series_type == SeriesType.menstrual_flow for sample in samples)
        if has_menstrual_flow:
            samples = self._keep_flow_amounts(db_session, samples)
        if samples:
            self.timeseries_service.bulk_create_samples(db_session, samples)
            records_saved += len(samples)
            types.update(sample.series_type.value for sample in samples)

            deviations = self._derive_wrist_temperature_deviation(db_session, samples)
            if deviations:
                self.timeseries_service.bulk_create_samples(db_session, deviations)
                records_saved += len(deviations)
                types.add(SeriesType.skin_temperature_deviation.value)

        # Commit all workout and timeseries changes in one transaction
        db_session.commit()

        # Menstrual flow → cycles (event_record + menstrual_cycle_details, menstrual_cycle.created)
        if has_menstrual_flow:
            sdk_menstrual_cycle_service.rebuild(db_session, UUID(user_id), request.provider)
            db_session.commit()

        # Process sleep (count sleep segments from input)
        if request.data.sleep:
            handle_sleep_data(db_session, request, user_id)
            sleep_saved = len(request.data.sleep)

        return {
            "workouts_saved": workouts_saved,
            "records_saved": records_saved,
            "types": sorted(types),
            "sleep_saved": sleep_saved,
            "dropped": dropped,
            "validation_ms": validation_ms,
        }

    def import_data_from_request(
        self,
        db_session: DbSession,
        request_content: str,
        content_type: str,
        user_id: str,
        batch_id: str | None = None,
    ) -> UploadDataResponse:
        provider = "unknown"
        try:
            # Parse content based on type
            if "multipart/form-data" in content_type:
                data = self._parse_multipart_content(request_content)
            else:
                data = self._parse_json_content(request_content)

            if not data:
                log_structured(
                    self.log,
                    "warning",
                    "No valid data found in request",
                    action="sdk_validate_data",
                    batch_id=batch_id,
                    user_id=user_id,
                )
                return UploadDataResponse(status_code=400, response="No valid data found", user_id=user_id)

            # Extract incoming counts (best-effort; invalid types must reach validation
            # below rather than raising TypeError here)
            provider = data.get("provider", "unknown")
            inner_data = data.get("data")
            if not isinstance(inner_data, dict):
                inner_data = {}
            records = inner_data.get("records")
            workouts = inner_data.get("workouts")
            sleep = inner_data.get("sleep")
            incoming_records = len(records) if isinstance(records, list) else 0
            incoming_workouts = len(workouts) if isinstance(workouts, list) else 0
            incoming_sleep = len(sleep) if isinstance(sleep, list) else 0

            # Load data and get saved counts
            saved_counts = self.load_data(db_session, data, user_id=user_id, batch_id=batch_id)

            connection = self.user_connection_repo.get_by_user_and_provider(db_session, UUID(user_id), provider)
            if connection:
                self.user_connection_repo.update_last_synced_at(db_session, connection)

            # Log detailed processing results
            log_structured(
                self.log,
                "info",
                f"{provider.capitalize()} data import completed",
                provider=f"{provider}",
                action=f"{provider}_sdk_import_complete",
                batch_id=batch_id,
                user_id=user_id,
                incoming_records=incoming_records,
                incoming_workouts=incoming_workouts,
                incoming_sleep=incoming_sleep,
                records_saved=saved_counts["records_saved"],
                workouts_saved=saved_counts["workouts_saved"],
                sleep_saved=saved_counts["sleep_saved"],
                validation_ms=saved_counts["validation_ms"],
            )

            dropped = saved_counts.get("dropped") or []
            if dropped:
                # Partial success: some records failed per-record validation. The good
                # ones are already saved above; report the exact field errors to Sentry
                # (PII-free: loc/msg/type) so we keep full visibility into what was lost.
                with sentry_sdk.push_scope() as scope:
                    scope.set_level("warning")
                    scope.set_context(
                        "dropped_records",
                        {
                            "batch_id": batch_id,
                            "user_id": user_id,
                            "provider": provider,
                            "dropped_count": len(dropped),
                            "errors": dropped[:20],
                        },
                    )
                    sentry_sdk.capture_message(f"{provider} SDK payload: dropped invalid records, kept the rest")
                # Compose the full location (collection[index].field) so the log one-liner
                # says which record failed, not just the field — the per-record `loc` is
                # relative to a single record. The full breakdown is in the Sentry context.
                first = dropped[0]
                first_loc = f"{first['collection']}[{first['index']}]"
                if first.get("loc"):
                    first_loc += f".{first['loc']}"
                log_structured(
                    self.log,
                    "warning",
                    f"{provider.capitalize()} SDK dropped invalid records",
                    provider=f"{provider}",
                    action=f"{provider}_sdk_records_dropped",
                    batch_id=batch_id,
                    user_id=user_id,
                    dropped_count=len(dropped),
                    first_error_loc=first_loc,
                    first_error_msg=first["msg"],
                )

            return UploadDataResponse(
                status_code=200,
                response="Import successful",
                user_id=user_id,
                dropped_count=len(dropped),
                records_saved=saved_counts["records_saved"],
                types=saved_counts["types"],
                workouts_saved=saved_counts["workouts_saved"],
                sleep_saved=saved_counts["sleep_saved"],
            )

        except ValidationError as e:
            # Reached ONLY when the envelope itself is invalid (provider/sdkVersion/
            # syncTimestamp/data shape) — _parse_sync_request re-raised because nothing was
            # salvageable. Per-record failures never reach here: they are collected inside
            # load_data and handled above as a partial success. Report + 400 the whole batch.
            errors = e.errors()
            first = errors[0] if errors else {}
            # Drop `input` (raw record value — may contain health data/PII) and `url`
            # before sending to Sentry; keep loc/msg/type. Preserve the 20-error cap.
            safe_errors = [{k: v for k, v in err.items() if k not in ("input", "url")} for err in errors[:20]]
            log_and_capture_error(
                e,
                self.log,
                f"{provider} SDK payload failed validation for user {user_id}",
                extra={
                    "user_id": user_id,
                    "batch_id": batch_id,
                    "provider": provider,
                    "error_count": len(errors),
                    "errors": safe_errors,
                },
            )
            log_structured(
                self.log,
                "warning",
                f"{provider.capitalize()} SDK payload validation failed",
                provider=f"{provider}",
                action=f"{provider}_sdk_validation_failed",
                batch_id=batch_id,
                user_id=user_id,
                error_count=len(errors),
                first_error_loc=".".join(str(x) for x in first.get("loc", [])),
                first_error_msg=first.get("msg"),
            )
            return UploadDataResponse(
                status_code=400,
                response=f"Validation failed: {first.get('msg', 'invalid payload')}",
                user_id=user_id,
            )

        except Exception as e:
            log_and_capture_error(
                e,
                self.log,
                f"Import failed for user {user_id}",
                extra={"user_id": user_id, "batch_id": batch_id, "provider": provider},
            )
            log_structured(
                self.log,
                "error",
                f"Import failed for user {user_id}: {e}",
                provider=f"{provider}",
                action=f"{provider}_sdk_import_failed",
                batch_id=batch_id,
                user_id=user_id,
                error_type=type(e).__name__,
            )
            return UploadDataResponse(
                status_code=400,
                response=f"Import failed: {str(e)}",
                user_id=user_id,
            )

    def _parse_multipart_content(self, content: str) -> dict | None:
        """Parse multipart form data to extract JSON."""
        # Try to find JSON start with various field patterns
        json_start = content.find('{\n  "data"')
        if json_start == -1:
            json_start = content.find('{"data"')
        if json_start == -1:
            return None

        brace_count = 0
        json_end = json_start
        for i, char in enumerate(content[json_start:], json_start):
            if char == "{":
                brace_count += 1
            elif char == "}":
                brace_count -= 1
                if brace_count == 0:
                    json_end = i
                    break

        if brace_count != 0:
            return None

        json_str = content[json_start : json_end + 1]
        return json.loads(json_str)

    def _parse_json_content(self, content: str) -> dict | None:
        """Parse JSON content directly."""
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None


import_service = ImportService(log=getLogger(__name__))
