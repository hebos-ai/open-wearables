"""COROS provider scaffold (pending partner API access).

COROS does not publish its API. Access is granted per partner (application via
https://support.coros.com/hc/en-us/articles/17085887816340 / api@coros.com) and the endpoint
and payload documentation is shared only after approval. Until then this package provides:

* identity, configuration-driven OAuth endpoints (``COROS_AUTHORIZE_URL`` / ``COROS_TOKEN_URL``
  / ``COROS_API_BASE_URL``) and registration in the factory;
* no workout / 24-7 handlers and an unwired planned-workout scaffold
  (``planned_workouts.py``), so no payload field is guessed.

Publicly stated by COROS (not yet verifiable against docs): OAuth 2.0, push notifications of
completed workouts, FIT download, structured-workout and training-plan push to athletes.

Per-sample data: COROS, like Wahoo, carries the samples only in the workout's FIT file. The
workout handler, once mapped, stores them with
``app.services.workout_fit_samples.ingest_workout_fit(db, provider="coros", fit_url=...)``
(download with size cap and 429 back-off, ``fit_parser``, ``data_point_series``) *before*
``create_detail`` emits ``workout.created``, and caps backfills with
``settings.workout_fit_downloads_per_sync``; then ``coverage.TIMESERIES`` becomes
``fit_parser.FIT_RECORD_SERIES``. The path is already covered by tests with ``provider="coros"``.
"""

from app.services.providers.coros.strategy import CorosStrategy

__all__ = ["CorosStrategy"]
