from app.constants.series_types.sdk.metric_types import APPLE_METRIC_TYPE_TO_SERIES_TYPE
from app.constants.series_types.sdk.workout_statistics import WORKOUT_STATISTIC_TYPE_TO_SERIES_TYPE
from app.schemas.enums import SeriesType
from app.schemas.enums.health_score_category import HealthScoreCategory

# Apple HealthKit emits only HKQuantityTypeIdentifier... metrics (SDNN, not RMSSD).
TIMESERIES: frozenset[SeriesType] = frozenset(
    {
        *APPLE_METRIC_TYPE_TO_SERIES_TYPE.values(),
        *WORKOUT_STATISTIC_TYPE_TO_SERIES_TYPE.values(),
        # Not a 1:1 metric mapping: menstrual records become daily flow, and the overnight
        # wrist/skin temperature also yields a deviation series (ImportService).
        SeriesType.menstrual_flow,
        SeriesType.skin_temperature,
        SeriesType.skin_temperature_deviation,
    }
)

# EventRecordDetail fields populated by workouts.py (workout records)
WORKOUT_FIELDS: frozenset[str] = frozenset(
    {
        "heart_rate_min",
        "heart_rate_max",
        "heart_rate_avg",
        "steps_count",
        "energy_burned",
        "distance",
        "max_speed",
        "average_speed",
        "average_cadence",
        "moving_time_seconds",
        "total_elevation_gain",
        "elev_high",
        "elev_low",
    }
)

# EventRecordDetail fields populated by the shared healthkit sleep service
SLEEP_FIELDS: frozenset[str] = frozenset(
    {
        "sleep_total_duration_minutes",
        "sleep_time_in_bed_minutes",
        "sleep_efficiency_score",
        "sleep_deep_minutes",
        "sleep_rem_minutes",
        "sleep_light_minutes",
        "sleep_awake_minutes",
        "is_nap",
        "sleep_stages",
    }
)

HEALTH_SCORES: frozenset[HealthScoreCategory] = frozenset()

# Menstrual cycles derived from the daily flow (app/services/sdk_menstrual_cycle_service.py)
MENSTRUAL_CYCLE_FIELDS: frozenset[str] = frozenset(
    {
        "day_in_cycle",
        "current_phase_type",
        "predicted_cycle_length",
        "is_predicted_cycle",
        "cycle_length",
        "last_updated_at",
        "period_length",
        "daily_flow",
    }
)
