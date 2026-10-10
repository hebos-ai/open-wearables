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
"""

from app.services.providers.coros.strategy import CorosStrategy

__all__ = ["CorosStrategy"]
