from app.schemas.enums import SeriesType
from app.schemas.enums.health_score_category import HealthScoreCategory
from app.services.fit_parser import FIT_RECORD_SERIES

# Wahoo exposes per-second samples only inside the FIT file (workout_summary.file.url).
# Workout-context only, gated by settings.ingest_workout_samples: workouts.py downloads the
# FIT and stores what its record messages carry (app/services/workout_fit_samples.py).
TIMESERIES: frozenset[SeriesType] = FIT_RECORD_SERIES

# EventRecordDetail fields populated by workouts.py from workout_summary
# (/v1/workouts and the workout_summary webhook).
WORKOUT_FIELDS: frozenset[str] = frozenset(
    {
        "heart_rate_avg",
        "energy_burned",
        "distance",
        "average_speed",
        "average_watts",
        "average_cadence",
        "total_elevation_gain",
        "moving_time_seconds",
    }
)

SLEEP_FIELDS: frozenset[str] = frozenset()

HEALTH_SCORES: frozenset[HealthScoreCategory] = frozenset()
