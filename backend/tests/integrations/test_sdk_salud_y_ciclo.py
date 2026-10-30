"""SDK (Apple HealthKit, Health Connect, Samsung): body composition, nutrition, skin temperature
and menstrual cycles derived from the daily flow."""

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from app.constants.series_types.sdk.reproductive_types import (
    MenstrualFlow,
    flow_from_record,
    local_days,
    parse_zone_offset,
)
from app.models import DataPointSeries, DataSource, EventRecord, MenstrualCycleDetails
from app.schemas.enums import SeriesType, get_series_type_id
from app.services.apple.healthkit.import_service import ImportService
from app.services.outgoing_webhooks import events as outgoing_events
from app.services.sdk_menstrual_cycle_service import FlowDay, build_cycles
from tests.factories import UserFactory

APPLE_SOURCE = {"name": "Health", "bundleIdentifier": "com.apple.Health", "productType": "iPhone15,2"}
HC_SOURCE = {"appId": "com.sec.android.app.shealth", "deviceModel": "SM-R960", "deviceType": "watch"}


@pytest.fixture(autouse=True)
def mock_sleep_redis() -> Any:
    mock_redis = MagicMock()
    mock_redis.get.return_value = None
    with (
        patch("app.services.apple.healthkit.sleep_service.get_redis_client", return_value=mock_redis),
        patch("app.integrations.celery.tasks.finalize_stale_sleep_task.finalize_stale_sleeps"),
    ):
        yield mock_redis


@pytest.fixture
def import_service() -> ImportService:
    return ImportService(log=logging.getLogger("test"))


def _payload(provider: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "provider": provider,
        "sdkVersion": "1.0.0",
        "syncTimestamp": "2026-10-30T10:00:00Z",
        "data": {"records": records},
    }


def _record(
    rid: str, rtype: str, value: float, start: str, end: str | None = None, unit: str | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "id": rid,
        "type": rtype,
        "value": value,
        "unit": unit,
        "startDate": start,
        "endDate": end or start,
        "zoneOffset": "+02:00",
        "source": APPLE_SOURCE,
        **extra,
    }


def _series(db: Session, user_id: UUID, series_type: SeriesType) -> list[DataPointSeries]:
    return (
        db.query(DataPointSeries)
        .join(DataSource, DataPointSeries.data_source_id == DataSource.id)
        .filter(
            DataSource.user_id == user_id,
            DataPointSeries.series_type_definition_id == get_series_type_id(series_type),
        )
        .order_by(DataPointSeries.recorded_at)
        .all()
    )


def _cycles(db: Session, user_id: UUID) -> list[tuple[EventRecord, MenstrualCycleDetails]]:
    return (
        db.query(EventRecord, MenstrualCycleDetails)
        .join(DataSource, EventRecord.data_source_id == DataSource.id)
        .join(MenstrualCycleDetails, MenstrualCycleDetails.record_id == EventRecord.id)
        .filter(DataSource.user_id == user_id, EventRecord.category == "menstrual_cycle")
        .order_by(EventRecord.start_datetime)
        .all()
    )


def _apple_flow_day(rid: str, day: str, value: int) -> dict[str, Any]:
    """An all-day HealthKit menstrual flow sample (local midnight to next midnight, +02:00)."""
    start = datetime.fromisoformat(f"{day}T00:00:00+02:00")
    return _record(
        rid,
        "HKCategoryTypeIdentifierMenstrualFlow",
        value,
        start.isoformat(),
        (start + timedelta(days=1)).isoformat(),
    )


# ---------------------------------------------------------------------------
# Pure mapping helpers
# ---------------------------------------------------------------------------


class TestReproductiveMapping:
    @pytest.mark.parametrize(
        ("rtype", "value", "expected"),
        [
            ("HKCategoryTypeIdentifierMenstrualFlow", 1, MenstrualFlow.UNSPECIFIED),
            ("HKCategoryTypeIdentifierMenstrualFlow", 2, MenstrualFlow.LIGHT),
            ("HKCategoryTypeIdentifierMenstrualFlow", 3, MenstrualFlow.MEDIUM),
            ("HKCategoryTypeIdentifierMenstrualFlow", 4, MenstrualFlow.HEAVY),
            ("HKCategoryTypeIdentifierMenstrualFlow", 5, MenstrualFlow.NONE),
            ("HKCategoryTypeIdentifierIntermenstrualBleeding", 0, MenstrualFlow.SPOTTING),
            ("MENSTRUATION_FLOW", 0, MenstrualFlow.UNSPECIFIED),
            ("MENSTRUATION_FLOW", 1, MenstrualFlow.LIGHT),
            ("MENSTRUATION", 3, MenstrualFlow.HEAVY),
            ("MENSTRUATION_PERIOD", 1, MenstrualFlow.UNSPECIFIED),
            ("INTERMENSTRUAL_BLEEDING", 1, MenstrualFlow.SPOTTING),
        ],
    )
    def test_flow_codes(self, rtype: str, value: int, expected: MenstrualFlow) -> None:
        assert flow_from_record(rtype, value) is expected

    def test_unknown_values_are_ignored(self) -> None:
        assert flow_from_record("HKCategoryTypeIdentifierMenstrualFlow", 9) is None
        assert flow_from_record("SOMETHING_ELSE", 1) is None

    def test_spotting_is_not_a_period_day(self) -> None:
        assert not MenstrualFlow.SPOTTING.is_period_bleeding
        assert not MenstrualFlow.NONE.is_period_bleeding
        assert MenstrualFlow.UNSPECIFIED.is_period_bleeding

    def test_all_day_sample_covers_one_local_day(self) -> None:
        start = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)  # 00:00 at +02:00
        assert local_days(start, start + timedelta(days=1), "+02:00") == [date(2026, 10, 2)]

    def test_period_record_covers_every_day(self) -> None:
        start = datetime(2026, 10, 1, 0, 0, tzinfo=parse_zone_offset("+02:00"))
        days = local_days(start, start + timedelta(days=4), "+02:00")
        assert days == [date(2026, 10, 1) + timedelta(days=i) for i in range(4)]

    def test_zone_offset_parsing(self) -> None:
        assert parse_zone_offset("-0530").utcoffset(None) == -timedelta(hours=5, minutes=30)
        assert parse_zone_offset(None) is timezone.utc
        assert parse_zone_offset("rubbish") is timezone.utc


class TestBuildCycles:
    @staticmethod
    def _days(spec: dict[str, MenstrualFlow]) -> dict[date, FlowDay]:
        return {date.fromisoformat(d): FlowDay(day=date.fromisoformat(d), flow=f) for d, f in spec.items()}

    def test_two_periods_make_one_closed_and_one_open_cycle(self) -> None:
        days = self._days(
            {
                "2026-09-01": MenstrualFlow.MEDIUM,
                "2026-09-02": MenstrualFlow.HEAVY,
                "2026-09-03": MenstrualFlow.LIGHT,
                "2026-09-04": MenstrualFlow.NONE,
                "2026-09-15": MenstrualFlow.SPOTTING,
                "2026-09-29": MenstrualFlow.LIGHT,
                "2026-09-30": MenstrualFlow.MEDIUM,
            }
        )
        closed, current = build_cycles(days)

        assert closed.start == date(2026, 9, 1)
        assert closed.cycle_length == 28
        assert closed.period_length == 3
        assert closed.end_exclusive == date(2026, 9, 29)
        assert closed.current_phase_type is None
        assert [d["flow"] for d in closed.daily_flow] == ["medium", "heavy", "light", "none", "spotting"]
        assert closed.daily_flow[-1] == {"date": "2026-09-15", "day_in_cycle": 15, "flow": "spotting"}

        assert current.start == date(2026, 9, 29)
        assert current.cycle_length is None
        assert current.period_length == 2
        assert current.day_in_cycle == 2
        assert current.current_phase_type == "menstrual"
        assert current.predicted_cycle_length == 28

    def test_short_gap_stays_in_the_same_period(self) -> None:
        days = self._days(
            {
                "2026-10-01": MenstrualFlow.HEAVY,
                "2026-10-03": MenstrualFlow.LIGHT,
                "2026-10-06": MenstrualFlow.LIGHT,
            }
        )
        (cycle,) = build_cycles(days)
        assert cycle.period_length == 6

    def test_spotting_alone_is_not_a_cycle(self) -> None:
        assert build_cycles(self._days({"2026-10-01": MenstrualFlow.SPOTTING})) == []
        assert build_cycles({}) == []


# ---------------------------------------------------------------------------
# Import: menstrual flow → series + cycles
# ---------------------------------------------------------------------------


class TestMenstrualImport:
    def test_apple_flow_creates_series_and_cycles(self, db: Session, import_service: ImportService) -> None:
        user = UserFactory()
        records = [
            _apple_flow_day("A1", "2026-09-01", 3),
            _apple_flow_day("A2", "2026-09-02", 4),
            _apple_flow_day("A3", "2026-09-03", 2),
            _apple_flow_day("A4", "2026-09-29", 1),
        ]
        import_service.load_data(db, _payload("apple", records), str(user.id))

        flow = _series(db, user.id, SeriesType.menstrual_flow)
        assert [int(s.value) for s in flow] == [3, 4, 2, 5]

        cycles = _cycles(db, user.id)
        assert len(cycles) == 2
        (first, first_detail), (second, second_detail) = cycles
        assert first.external_id == "apple:cycle:2026-09-01"
        assert first.type == "menstrual"
        assert first_detail.cycle_length == 28
        assert first_detail.period_length == 3
        assert first_detail.daily_flow[0] == {"date": "2026-09-01", "day_in_cycle": 1, "flow": "medium"}
        assert second_detail.cycle_length is None
        assert second_detail.current_phase_type == "menstrual"
        assert second_detail.predicted_cycle_length == 28
        assert second.zone_offset == "+02:00"

    def test_reimport_is_idempotent_and_only_the_open_cycle_changes(
        self, db: Session, import_service: ImportService
    ) -> None:
        user = UserFactory()
        base = [
            _apple_flow_day("B1", "2026-09-01", 3),
            _apple_flow_day("B2", "2026-09-29", 3),
        ]
        import_service.load_data(db, _payload("apple", base), str(user.id))
        ids_before = {r.external_id: r.id for r, _ in _cycles(db, user.id)}

        import_service.load_data(db, _payload("apple", base), str(user.id))
        assert {r.external_id: r.id for r, _ in _cycles(db, user.id)} == ids_before

        import_service.load_data(db, _payload("apple", [_apple_flow_day("B3", "2026-09-30", 2)]), str(user.id))
        after = {r.external_id: (r.id, d) for r, d in _cycles(db, user.id)}
        assert after["apple:cycle:2026-09-01"][0] == ids_before["apple:cycle:2026-09-01"]
        assert after["apple:cycle:2026-09-29"][0] != ids_before["apple:cycle:2026-09-29"]
        assert after["apple:cycle:2026-09-29"][1].period_length == 2

    def test_health_connect_period_and_flow(self, db: Session, import_service: ImportService) -> None:
        user = UserFactory()
        start = datetime.fromisoformat("2026-10-01T00:00:00+02:00")
        records = [
            {
                **_record("P1", "MENSTRUATION_PERIOD", 1, start.isoformat(), (start + timedelta(days=4)).isoformat()),
                "source": HC_SOURCE,
            },
            {**_record("F1", "MENSTRUATION_FLOW", 3, start.isoformat()), "source": HC_SOURCE},
        ]
        import_service.load_data(db, _payload("samsung", records), str(user.id))

        ((record, detail),) = _cycles(db, user.id)
        assert record.external_id == "samsung:cycle:2026-10-01"
        assert detail.period_length == 4
        assert detail.daily_flow[0]["flow"] == "heavy"  # the heavier record of the day wins
        assert [d["flow"] for d in detail.daily_flow[1:]] == ["unspecified"] * 3

    def test_period_record_never_hides_the_flow_amount(self, db: Session, import_service: ImportService) -> None:
        """Health Connect writes a period record over the days that also carry flow records. Whatever
        the order (same batch, or the period re-sent in a later sync), a day keeps its amount."""
        user = UserFactory()
        start = datetime.fromisoformat("2026-10-01T00:00:00+02:00")
        period = {
            **_record("P2", "MENSTRUATION_PERIOD", 1, start.isoformat(), (start + timedelta(days=3)).isoformat()),
            "source": HC_SOURCE,
        }
        flow = {**_record("F2", "MENSTRUATION_FLOW", 3, start.isoformat()), "source": HC_SOURCE}
        # Flow first, period last in the same batch
        import_service.load_data(db, _payload("samsung", [flow, period]), str(user.id))
        assert [int(s.value) for s in _series(db, user.id, SeriesType.menstrual_flow)] == [4, 5, 5]

        # The period comes again in a later sync: day 1 stays heavy
        import_service.load_data(db, _payload("samsung", [period]), str(user.id))
        assert [int(s.value) for s in _series(db, user.id, SeriesType.menstrual_flow)] == [4, 5, 5]
        ((_, detail),) = _cycles(db, user.id)
        assert [d["flow"] for d in detail.daily_flow] == ["heavy", "unspecified", "unspecified"]

    def test_flow_amount_can_still_be_corrected(self, db: Session, import_service: ImportService) -> None:
        user = UserFactory()
        import_service.load_data(db, _payload("apple", [_apple_flow_day("C1", "2026-10-01", 4)]), str(user.id))
        import_service.load_data(db, _payload("apple", [_apple_flow_day("C1", "2026-10-01", 2)]), str(user.id))
        assert [int(s.value) for s in _series(db, user.id, SeriesType.menstrual_flow)] == [2]

    def test_menstrual_webhook_carries_daily_flow(self) -> None:
        sent: list[dict[str, Any]] = []
        with patch.object(outgoing_events, "_dispatch", side_effect=lambda _t, payload, **_k: sent.append(payload)):
            outgoing_events.on_menstrual_cycle_created(
                record_id=UUID(int=1),
                user_id=UUID(int=2),
                provider="apple",
                device=None,
                start_time="2026-09-01T00:00:00+02:00",
                end_time="2026-09-29T00:00:00+02:00",
                zone_offset="+02:00",
                current_phase_type=None,
                day_in_cycle=28,
                cycle_length=28,
                is_predicted_cycle=False,
                period_length=3,
                daily_flow=[{"date": "2026-09-01", "day_in_cycle": 1, "flow": "medium"}],
            )
        data = sent[0]["data"]
        assert data["period_length"] == 3
        assert data["daily_flow"] == [{"date": "2026-09-01", "day_in_cycle": 1, "flow": "medium"}]
        assert data["source"] == {"provider": "apple", "device": None}


# ---------------------------------------------------------------------------
# Import: temperature, body composition, nutrition
# ---------------------------------------------------------------------------


class TestTemperatureImport:
    def test_apple_wrist_temperature_is_skin_and_gets_a_deviation(
        self, db: Session, import_service: ImportService
    ) -> None:
        user = UserFactory()
        wrist = "HKQuantityTypeIdentifierAppleSleepingWristTemperature"
        nights = [
            _record(f"W{i}", wrist, v, f"2026-10-0{i}T03:00:00+02:00", unit="degC")
            for i, v in [(1, 35.0), (2, 35.2), (3, 35.4), (4, 35.9)]
        ]
        import_service.load_data(db, _payload("apple", nights), str(user.id))

        assert _series(db, user.id, SeriesType.body_temperature) == []
        assert [float(s.value) for s in _series(db, user.id, SeriesType.skin_temperature)] == [35.0, 35.2, 35.4, 35.9]
        (deviation,) = _series(db, user.id, SeriesType.skin_temperature_deviation)
        assert deviation.value == Decimal("0.700")  # 35.9 - mean(35.0, 35.2, 35.4)

    def test_wrist_value_that_is_already_a_deviation(self, db: Session, import_service: ImportService) -> None:
        user = UserFactory()
        record = _record("D1", "HKQuantityTypeIdentifierAppleSleepingWristTemperature", -0.4, "2026-10-01T03:00:00Z")
        import_service.load_data(db, _payload("apple", [record]), str(user.id))
        assert _series(db, user.id, SeriesType.skin_temperature) == []
        assert [float(s.value) for s in _series(db, user.id, SeriesType.skin_temperature_deviation)] == [-0.4]

    def test_health_connect_skin_temperature_delta(self, db: Session, import_service: ImportService) -> None:
        user = UserFactory()
        record = {**_record("S1", "SKIN_TEMPERATURE", 0.3, "2026-10-01T03:00:00Z", unit="degC"), "source": HC_SOURCE}
        import_service.load_data(db, _payload("google", [record]), str(user.id))
        assert [float(s.value) for s in _series(db, user.id, SeriesType.skin_temperature_deviation)] == [0.3]


class TestBodyAndNutritionImport:
    def test_health_connect_bone_and_water_mass(self, db: Session, import_service: ImportService) -> None:
        user = UserFactory()
        records = [
            {**_record("BM", "BONE_MASS", 3.1, "2026-10-01T07:00:00Z", unit="kg"), "source": HC_SOURCE},
            {**_record("WM", "BODY_WATER_MASS", 41.5, "2026-10-01T07:00:00Z", unit="kg"), "source": HC_SOURCE},
        ]
        import_service.load_data(db, _payload("samsung", records), str(user.id))
        assert [float(s.value) for s in _series(db, user.id, SeriesType.bone_mass)] == [3.1]
        assert [float(s.value) for s in _series(db, user.id, SeriesType.body_water_mass)] == [41.5]

    def test_apple_dietary_units(self, db: Session, import_service: ImportService) -> None:
        user = UserFactory()
        records = [
            _record("N1", "HKQuantityTypeIdentifierDietaryEnergyConsumed", 650, "2026-10-01T13:00:00Z", unit="Cal"),
            _record("N2", "HKQuantityTypeIdentifierDietaryProtein", 32, "2026-10-01T13:00:00Z", unit="g"),
            _record("N3", "HKQuantityTypeIdentifierDietarySodium", 0.8, "2026-10-01T13:00:00Z", unit="g"),
            _record("N4", "HKQuantityTypeIdentifierDietaryWater", 0.5, "2026-10-01T13:00:00Z", unit="L"),
        ]
        import_service.load_data(db, _payload("apple", records), str(user.id))
        assert [float(s.value) for s in _series(db, user.id, SeriesType.dietary_energy_consumed)] == [650.0]
        assert [float(s.value) for s in _series(db, user.id, SeriesType.dietary_protein)] == [32.0]
        assert [float(s.value) for s in _series(db, user.id, SeriesType.dietary_sodium)] == [800.0]  # g → mg
        assert [float(s.value) for s in _series(db, user.id, SeriesType.hydration)] == [500.0]  # L → mL

    def test_health_connect_nutrition(self, db: Session, import_service: ImportService) -> None:
        user = UserFactory()
        records = [
            {**_record("H1", "DIETARY_ENERGY", 2100, "2026-10-01T20:00:00Z", unit="kJ"), "source": HC_SOURCE},
            {**_record("H2", "DIETARY_TOTAL_FAT", 18, "2026-10-01T20:00:00Z", unit="g"), "source": HC_SOURCE},
        ]
        import_service.load_data(db, _payload("google", records), str(user.id))
        (energy,) = _series(db, user.id, SeriesType.dietary_energy_consumed)
        assert float(energy.value) == pytest.approx(501.913, abs=0.01)  # kJ → kcal
        assert [float(s.value) for s in _series(db, user.id, SeriesType.dietary_fat_total)] == [18.0]
