from app.constants.series_types.sdk.metric_types import ANDROID_METRIC_TYPE_TO_SERIES_TYPE
from app.constants.series_types.sdk.workout_statistics import WORKOUT_STATISTIC_TYPE_TO_SERIES_TYPE
from app.schemas.enums import SeriesType
from app.services.providers.apple.coverage import HEALTH_SCORES, MENSTRUAL_CYCLE_FIELDS, SLEEP_FIELDS, WORKOUT_FIELDS

# Samsung Health Connect emits Android/HC metric types (RMSSD, not SDNN).
TIMESERIES: frozenset[SeriesType] = frozenset(
    {
        *ANDROID_METRIC_TYPE_TO_SERIES_TYPE.values(),
        *WORKOUT_STATISTIC_TYPE_TO_SERIES_TYPE.values(),
        # Not a 1:1 metric mapping: menstrual records become daily flow, and the overnight
        # wrist/skin temperature also yields a deviation series (ImportService).
        SeriesType.menstrual_flow,
        SeriesType.skin_temperature,
        SeriesType.skin_temperature_deviation,
    }
)

__all__ = ["HEALTH_SCORES", "MENSTRUAL_CYCLE_FIELDS", "SLEEP_FIELDS", "TIMESERIES", "WORKOUT_FIELDS"]
