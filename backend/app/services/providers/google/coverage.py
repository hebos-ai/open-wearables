from app.constants.series_types.sdk.metric_types import ANDROID_METRIC_TYPE_TO_SERIES_TYPE
from app.constants.series_types.sdk.workout_statistics import WORKOUT_STATISTIC_TYPE_TO_SERIES_TYPE
from app.schemas.enums import SeriesType
from app.services.providers.apple.coverage import HEALTH_SCORES, MENSTRUAL_CYCLE_FIELDS, SLEEP_FIELDS, WORKOUT_FIELDS
from app.services.providers.google.health_api.metrics import METRICS
from app.services.providers.google.health_api.nutrition import NUTRITION_SERIES

# Google data arrives through two paths under one provider identity:
#   - Health Connect SDK: Android/HC metric types (RMSSD, not SDNN)
#   - Health API cloud: series from the unified rollUp + list metric registry, plus the
#     dietary_* series read from nutrition-log entries
HEALTH_API_SERIES: frozenset[SeriesType] = frozenset(
    {*(s for m in METRICS for s in m.series_types()), *NUTRITION_SERIES}
)

TIMESERIES: frozenset[SeriesType] = frozenset(
    {
        *ANDROID_METRIC_TYPE_TO_SERIES_TYPE.values(),
        *WORKOUT_STATISTIC_TYPE_TO_SERIES_TYPE.values(),
        # Not a 1:1 metric mapping: menstrual records become daily flow, and the overnight
        # wrist/skin temperature also yields a deviation series (ImportService).
        SeriesType.menstrual_flow,
        SeriesType.skin_temperature,
        SeriesType.skin_temperature_deviation,
        *HEALTH_API_SERIES,
    }
)

__all__ = [
    "HEALTH_API_SERIES",
    "HEALTH_SCORES",
    "MENSTRUAL_CYCLE_FIELDS",
    "SLEEP_FIELDS",
    "TIMESERIES",
    "WORKOUT_FIELDS",
]
