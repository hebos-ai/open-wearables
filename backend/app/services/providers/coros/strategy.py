from app.config import settings
from app.services.providers.base_strategy import BaseProviderStrategy, ProviderCapabilities, ProviderCoverage
from app.services.providers.coros.coverage import HEALTH_SCORES, SLEEP_FIELDS, TIMESERIES, WORKOUT_FIELDS
from app.services.providers.coros.oauth import CorosOAuth


class CorosStrategy(BaseProviderStrategy):
    """COROS scaffold. See ``app/services/providers/coros/__init__.py`` for status."""

    def __init__(self):
        super().__init__()
        self.oauth = CorosOAuth(
            user_repo=self.user_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
        )
        # Workout / 24-7 handlers are added once the partner payloads are documented; leaving
        # them unset makes the vendor routes answer 501 and the sync task skip COROS.
        self.workouts = None
        self.data_247 = None
        self.webhooks = None
        # Scaffold only: wire it (``self.planned_workouts = CorosPlannedWorkouts(...)``) once
        # translate/send are implemented, so ``supports_planned_workouts`` stays truthful.
        self.planned_workouts = None

    @property
    def name(self) -> str:
        return "coros"

    @property
    def display_name(self) -> str:
        return "COROS"

    @property
    def api_base_url(self) -> str:
        return (settings.coros_api_base_url or "").rstrip("/")

    @property
    def coverage(self) -> ProviderCoverage:
        return ProviderCoverage(
            timeseries=TIMESERIES,
            workout_fields=WORKOUT_FIELDS,
            sleep_fields=SLEEP_FIELDS,
            health_scores=HEALTH_SCORES,
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        # No delivery mode is declared until the partner API is mapped: the scheduler must not
        # pull and historical sync must report "unsupported" rather than fail per user.
        return ProviderCapabilities()
