from app.schemas.enums import SeriesType
from app.schemas.enums.health_score_category import HealthScoreCategory

# Nothing is emitted until the COROS partner payloads are mapped.
TIMESERIES: frozenset[SeriesType] = frozenset()
WORKOUT_FIELDS: frozenset[str] = frozenset()
SLEEP_FIELDS: frozenset[str] = frozenset()
HEALTH_SCORES: frozenset[HealthScoreCategory] = frozenset()
