from app.services.providers.base_strategy import BaseProviderStrategy, ProviderCapabilities, ProviderCoverage
from app.services.providers.wahoo.coverage import HEALTH_SCORES, SLEEP_FIELDS, TIMESERIES, WORKOUT_FIELDS
from app.services.providers.wahoo.oauth import WahooOAuth
from app.services.providers.wahoo.planned_workouts import WahooPlannedWorkouts
from app.services.providers.wahoo.webhook_handler import WahooWebhookHandler
from app.services.providers.wahoo.workouts import WahooWorkouts


class WahooStrategy(BaseProviderStrategy):
    """Wahoo (ELEMNT, KICKR, TICKR, Wahoo app) via the Wahoo Cloud API."""

    def __init__(self):
        super().__init__()
        self.oauth = WahooOAuth(
            user_repo=self.user_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
        )
        self.workouts = WahooWorkouts(
            workout_repo=self.workout_repo,
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )
        self.data_247 = None  # Wahoo exposes workouts only (no sleep / daily data)
        self.webhooks = WahooWebhookHandler(workouts=self.workouts)
        self.planned_workouts = WahooPlannedWorkouts(
            connection_repo=self.connection_repo,
            provider_name=self.name,
            api_base_url=self.api_base_url,
            oauth=self.oauth,
        )

    @property
    def name(self) -> str:
        return "wahoo"

    @property
    def api_base_url(self) -> str:
        return "https://api.wahooligan.com"

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
        # REST backfill + full workout_summary pushed in every webhook. Subscriptions are
        # configured in the Wahoo developer portal (no registration API).
        return ProviderCapabilities(rest_pull=True, webhook_stream=True)
