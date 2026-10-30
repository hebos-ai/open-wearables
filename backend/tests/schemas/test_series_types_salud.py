"""New series types for body composition, nutrition and max heart rate (Hebos health inventory)."""

import pytest

from app.constants.webhooks.events import SERIES_TYPE_TO_GRANULAR_EVENT, SERIES_TYPE_TO_GROUP_EVENT
from app.constants.webhooks.test_payloads import get_test_payload
from app.schemas.enums import (
    SERIES_TYPE_DEFINITIONS,
    SeriesType,
    get_series_type_from_id,
    get_series_type_id,
    get_series_type_unit,
)
from app.schemas.enums.aggregation_method import AggregationMethod, daily_total_flag, get_aggregation_method
from app.schemas.enums.series_types import SERIES_TYPE_CATEGORY_BY_ENUM
from app.schemas.webhooks.event_types import EVENT_TYPE_DESCRIPTIONS, EVENT_TYPE_GROUPS, WebhookEventType


class TestSeriesTypeDefinitions:
    def test_ids_are_unique(self) -> None:
        ids = [type_id for type_id, _, _ in SERIES_TYPE_DEFINITIONS]
        assert len(ids) == len(set(ids))

    def test_every_enum_member_has_a_definition(self) -> None:
        defined = {series_type for _, series_type, _ in SERIES_TYPE_DEFINITIONS}
        assert set(SeriesType) == defined

    @pytest.mark.parametrize(
        ("series_type", "type_id", "unit"),
        [
            (SeriesType.max_heart_rate, 8, "bpm"),
            (SeriesType.bone_mass, 52, "kg"),
            (SeriesType.body_water_mass, 53, "kg"),
            (SeriesType.dietary_energy_consumed, 260, "kcal"),
            (SeriesType.dietary_carbohydrates, 261, "g"),
            (SeriesType.dietary_protein, 269, "g"),
            (SeriesType.dietary_fat_total, 264, "g"),
            (SeriesType.dietary_caffeine, 297, "mg"),
            (SeriesType.dietary_folic_acid, 301, "mcg"),
        ],
    )
    def test_new_types_keep_upstream_ids_and_units(self, series_type: SeriesType, type_id: int, unit: str) -> None:
        assert get_series_type_id(series_type) == type_id
        assert get_series_type_from_id(type_id) is series_type
        assert get_series_type_unit(series_type) == unit

    def test_new_codes_fit_the_db_column(self) -> None:
        new = [SeriesType.max_heart_rate, SeriesType.bone_mass, SeriesType.body_water_mass] + [
            s for s in SeriesType if s.value.startswith("dietary_")
        ]
        assert all(len(s.value) <= 32 for s in new)

    def test_categories(self) -> None:
        assert SERIES_TYPE_CATEGORY_BY_ENUM[SeriesType.dietary_sodium] == "Nutrition"
        assert SERIES_TYPE_CATEGORY_BY_ENUM[SeriesType.bone_mass] == "Body Composition"
        assert SERIES_TYPE_CATEGORY_BY_ENUM[SeriesType.max_heart_rate] == "Heart & Cardiovascular"


class TestWebhookEventsForNewTypes:
    def test_body_composition_group(self) -> None:
        assert SERIES_TYPE_TO_GROUP_EVENT["bone_mass"] == WebhookEventType.BODY_COMPOSITION_CREATED
        assert SERIES_TYPE_TO_GROUP_EVENT["body_water_mass"] == WebhookEventType.BODY_COMPOSITION_CREATED
        assert SERIES_TYPE_TO_GRANULAR_EVENT["bone_mass"] == WebhookEventType.SERIES_BONE_MASS

    def test_max_heart_rate_is_a_heart_rate_event(self) -> None:
        assert SERIES_TYPE_TO_GROUP_EVENT["max_heart_rate"] == WebhookEventType.HEART_RATE_CREATED
        assert SERIES_TYPE_TO_GRANULAR_EVENT["max_heart_rate"] == WebhookEventType.SERIES_MAX_HEART_RATE

    def test_every_dietary_type_emits_the_nutrition_group(self) -> None:
        dietary = [s for s in SeriesType if s.value.startswith("dietary_")]
        assert len(dietary) == 42
        for series_type in dietary:
            assert SERIES_TYPE_TO_GROUP_EVENT[series_type.value] == WebhookEventType.NUTRITION_CREATED

    def test_granular_nutrition_events_belong_to_the_group(self) -> None:
        granular = set(EVENT_TYPE_GROUPS[WebhookEventType.NUTRITION_CREATED])
        assert WebhookEventType.SERIES_DIETARY_ENERGY_CONSUMED in granular
        for slug, event in SERIES_TYPE_TO_GRANULAR_EVENT.items():
            if slug.startswith("dietary_"):
                assert event in granular

    def test_new_events_have_descriptions_and_examples(self) -> None:
        for evt in (
            WebhookEventType.NUTRITION_CREATED,
            WebhookEventType.SERIES_BONE_MASS,
            WebhookEventType.SERIES_MAX_HEART_RATE,
            WebhookEventType.SERIES_DIETARY_PROTEIN,
        ):
            assert evt in EVENT_TYPE_DESCRIPTIONS
            assert get_test_payload(evt)["type"] == evt


class TestAggregationForNewTypes:
    def test_intake_adds_up_over_the_day(self) -> None:
        for series_type in SeriesType:
            if series_type.value.startswith("dietary_"):
                assert get_aggregation_method(series_type) is AggregationMethod.SUM
        assert get_aggregation_method(SeriesType.hydration) is AggregationMethod.SUM
        assert daily_total_flag(SeriesType.dietary_protein, is_daily=False) is False

    def test_body_and_heart_are_not_summed(self) -> None:
        assert get_aggregation_method(SeriesType.bone_mass) is AggregationMethod.AVG
        assert get_aggregation_method(SeriesType.body_water_mass) is AggregationMethod.AVG
        assert get_aggregation_method(SeriesType.max_heart_rate) is AggregationMethod.MAX
