"""Body-measurement metrics (weight, height, body fat, core temp, nightly skin temp, glucose).

Value fields confirmed against the live API / the public DataPoint reference. Weight is
reported in grams (scaled to kg) and height in millimeters (scaled to cm).
core-body-temperature also carries Min/Max alongside the Avg we take (future multi-series).

daily-sleep-temperature-derivations carries the nightly skin temperature and the user's
baseline, never the difference: the absolute nightly value goes to ``skin_temperature``
(never ``body_temperature``) and the deviation is derived as nightly - baseline into
``skin_temperature_deviation``. A deviation of exactly 0 is a real measurement and is kept.

Lean/bone/water mass are not Google Health API data types (only weight, body-fat, height),
so they are not mapped here; they reach OW from Health Connect through the SDK.
"""

from decimal import Decimal

from app.schemas.enums import SeriesType
from app.schemas.providers.google import DataTypeMetric, DerivedSeriesField, ListSpec, RollupSpec, TimeShape

_G_TO_KG = Decimal("0.001")
_MM_TO_CM = Decimal("0.1")

BODY_METRICS: tuple[DataTypeMetric, ...] = (
    DataTypeMetric(
        "weight",
        SeriesType.weight,
        value_key="weight",
        rollup_spec=RollupSpec("weightGramsAvg", scale=_G_TO_KG),
        list_spec=ListSpec("weightGrams", TimeShape.SAMPLE, scale=_G_TO_KG),
    ),
    DataTypeMetric(
        "height",
        SeriesType.height,
        value_key="height",
        list_spec=ListSpec("heightMillimeters", TimeShape.SAMPLE, scale=_MM_TO_CM),
    ),
    DataTypeMetric(
        "body-fat",
        SeriesType.body_fat_percentage,
        value_key="bodyFat",
        rollup_spec=RollupSpec("bodyFatPercentageAvg"),
        list_spec=ListSpec("percentage", TimeShape.SAMPLE),
    ),
    DataTypeMetric(
        "core-body-temperature",
        SeriesType.body_temperature,
        value_key="coreBodyTemperature",
        rollup_spec=RollupSpec("temperatureCelsiusAvg"),
        list_spec=ListSpec("celsius", TimeShape.SAMPLE),
    ),
    DataTypeMetric(
        "daily-sleep-temperature-derivations",
        SeriesType.skin_temperature,
        value_key="dailySleepTemperatureDerivations",
        list_spec=ListSpec(
            "nightlyTemperatureCelsius",
            TimeShape.DATE,
            is_daily_total=True,
            derived=(
                DerivedSeriesField(
                    SeriesType.skin_temperature_deviation,
                    "nightlyTemperatureCelsius",
                    against="baselineTemperatureCelsius",
                ),
            ),
        ),
    ),
    DataTypeMetric(
        "blood-glucose",
        SeriesType.blood_glucose,
        value_key="bloodGlucose",
        rollup_spec=RollupSpec("bloodGlucoseMilligramsPerDeciliterAvg"),
        list_spec=ListSpec("bloodGlucoseMilligramsPerDeciliter", TimeShape.SAMPLE),
    ),
)
