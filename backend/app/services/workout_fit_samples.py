"""Per-sample data of ONE workout taken from its FIT file.

Some providers (Wahoo, COROS) expose the per-second samples of a workout only inside
the FIT file linked from the workout payload. This module downloads that file, parses
it with :func:`app.services.fit_parser.parse_fit_file` and stores the samples in
``data_point_series`` exactly like the providers that already push samples (Garmin
FIT, Strava streams), so ``GET /users/{id}/events/workouts/{id}/stream`` serves them.

Guarantees:

* gated by ``settings.ingest_workout_samples`` (same flag as Garmin / Strava samples);
* failure-isolated: a download / parse / insert error is logged and returns 0, the
  workout itself is never lost;
* the provider's OAuth token is sent only when the file lives on the provider's own API
  host; CDN / pre-signed links (Wahoo ``cdn.wahooligan.com``) get no credentials;
* bounded: files bigger than ``settings.workout_fit_max_bytes`` are dropped, HTTP 429
  is retried a few times honouring ``Retry-After``; callers that backfill many
  workouts cap the number of downloads per run (see each provider).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from urllib.parse import urlparse
from uuid import UUID

import httpx

from app.config import settings
from app.database import DbSession
from app.repositories import UserConnectionRepository
from app.services.fit_parser import parse_fit_file
from app.services.providers.api_client import _get_valid_token
from app.services.providers.templates.base_oauth import BaseOAuthTemplate
from app.services.raw_payload_storage import store_fit_file
from app.services.timeseries_service import timeseries_service
from app.utils.sentry_helpers import log_and_capture_error
from app.utils.structured_logging import log_structured

logger = logging.getLogger(__name__)

#: Retries on HTTP 429 (rate limited) before giving up on one file.
MAX_429_RETRIES = 3
#: Upper bound for one wait, whatever ``Retry-After`` says.
MAX_RETRY_WAIT_SECONDS = 60.0
_BASE_RETRY_WAIT_SECONDS = 5.0


class FitTooLargeError(Exception):
    """The FIT file exceeds ``settings.workout_fit_max_bytes``."""


def _same_host(url: str, api_base_url: str | None) -> bool:
    if not api_base_url:
        return False
    a, b = urlparse(url), urlparse(api_base_url)
    return a.scheme == "https" and a.hostname is not None and a.hostname == b.hostname


def _retry_wait(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            return min(max(float(raw), 0.0), MAX_RETRY_WAIT_SECONDS)
        except ValueError:
            pass
    return min(_BASE_RETRY_WAIT_SECONDS * (2**attempt), MAX_RETRY_WAIT_SECONDS)


def download_fit(
    url: str,
    headers: dict[str, str] | None = None,
    *,
    max_bytes: int | None = None,
    client: httpx.Client | None = None,
    sleep: Callable[[float], object] = time.sleep,
) -> bytes:
    """GET a FIT file with a size cap and 429 back-off. Raises on any other HTTP error."""
    limit = max_bytes if max_bytes is not None else settings.workout_fit_max_bytes
    own_client = client is None
    http = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        for attempt in range(MAX_429_RETRIES + 1):
            with http.stream("GET", url, headers=headers or {}) as response:
                if response.status_code == 429 and attempt < MAX_429_RETRIES:
                    wait = _retry_wait(response, attempt)
                    log_structured(logger, "warning", "FIT download rate limited, retrying", wait_seconds=wait)
                    sleep(wait)
                    continue
                response.raise_for_status()
                declared = response.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise FitTooLargeError(f"FIT file declares {declared} bytes (limit {limit})")
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise FitTooLargeError(f"FIT file exceeds {limit} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)
        raise httpx.HTTPError("FIT download still rate limited after retries")
    finally:
        if own_client:
            http.close()


def ingest_workout_fit(
    db: DbSession,
    *,
    provider: str,
    user_id: UUID,
    fit_url: str | None,
    activity_id: str,
    zone_offset: str | None = None,
    device_model: str | None = None,
    api_base_url: str | None = None,
    oauth: BaseOAuthTemplate | None = None,
    connection_repo: UserConnectionRepository | None = None,
) -> int:
    """Download + parse + store the samples of one workout's FIT file. Returns samples stored."""
    if not settings.ingest_workout_samples or not fit_url:
        return 0

    context = {"provider": provider, "user_id": str(user_id), "activity_id": activity_id}
    try:
        headers: dict[str, str] = {}
        if oauth is not None and connection_repo is not None and _same_host(fit_url, api_base_url):
            token = _get_valid_token(db, user_id, provider, connection_repo, oauth)
            headers["Authorization"] = f"Bearer {token}"
        fit_bytes = download_fit(fit_url, headers)
    except Exception as exc:
        log_and_capture_error(exc, logger, "Failed to download workout FIT file, skipping samples", extra=context)
        return 0

    store_fit_file(provider=provider, fit_bytes=fit_bytes, user_id=str(user_id), activity_id=activity_id)

    try:
        parsed = parse_fit_file(fit_bytes, user_id, source=provider)
    except Exception as exc:
        log_and_capture_error(exc, logger, "Failed to parse workout FIT file, skipping samples", extra=context)
        return 0

    samples = [
        s.model_copy(update={"provider": provider, "zone_offset": zone_offset, "device_model": device_model})
        for s in parsed.samples
    ]
    if not samples:
        return 0

    # Savepoint: a failed insert rolls back only the samples, not the caller's workout.
    nested = db.begin_nested()
    try:
        timeseries_service.bulk_create_samples(db, samples)
        nested.commit()
    except Exception as exc:
        nested.rollback()
        log_and_capture_error(
            exc, logger, "Workout FIT sample ingestion failed; continuing", extra={**context, "samples": len(samples)}
        )
        return 0

    log_structured(logger, "info", "Stored workout FIT samples", samples=len(samples), **context)
    return len(samples)


__all__ = ["FitTooLargeError", "download_fit", "ingest_workout_fit"]
