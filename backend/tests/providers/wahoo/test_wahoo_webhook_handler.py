"""Wahoo webhook: token verification, async dispatch and workout persistence."""

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy.orm import Session

from app.config import settings
from app.models import EventRecord
from app.services.providers.wahoo.strategy import WahooStrategy
from tests.factories import UserConnectionFactory, UserFactory

TOKEN = "wahoo-test-token"


@pytest.fixture
def configured_token() -> Any:
    with patch.object(settings, "wahoo_webhook_token", SecretStr(TOKEN)):
        yield


class TestVerifySignature:
    def test_accepts_matching_token(
        self, wahoo_strategy: WahooStrategy, webhook_payload: dict, configured_token: None
    ) -> None:
        assert wahoo_strategy.webhooks is not None
        body = json.dumps(webhook_payload).encode()
        assert wahoo_strategy.webhooks.verify_signature(MagicMock(), body) is True

    @pytest.mark.parametrize("token", ["wrong", "", None, 123])
    def test_rejects_wrong_or_missing_token(
        self, wahoo_strategy: WahooStrategy, webhook_payload: dict, configured_token: None, token: Any
    ) -> None:
        assert wahoo_strategy.webhooks is not None
        webhook_payload["webhook_token"] = token
        body = json.dumps(webhook_payload).encode()
        assert wahoo_strategy.webhooks.verify_signature(MagicMock(), body) is False

    def test_rejects_everything_when_token_not_configured(
        self, wahoo_strategy: WahooStrategy, webhook_payload: dict
    ) -> None:
        assert wahoo_strategy.webhooks is not None
        with patch.object(settings, "wahoo_webhook_token", None):
            body = json.dumps(webhook_payload).encode()
            assert wahoo_strategy.webhooks.verify_signature(MagicMock(), body) is False

    def test_rejects_non_json(self, wahoo_strategy: WahooStrategy, configured_token: None) -> None:
        assert wahoo_strategy.webhooks is not None
        assert wahoo_strategy.webhooks.verify_signature(MagicMock(), b"not json") is False
        assert wahoo_strategy.webhooks.verify_signature(MagicMock(), b"[]") is False


class TestDispatch:
    def test_enqueues_without_token(self, wahoo_strategy: WahooStrategy, webhook_payload: dict) -> None:
        handler = wahoo_strategy.webhooks
        assert handler is not None
        event = handler.parse_payload(json.dumps(webhook_payload).encode())
        with (
            patch("app.services.providers.wahoo.webhook_handler.celery_app") as celery,
            patch("app.services.providers.wahoo.webhook_handler.store_raw_payload") as store,
        ):
            celery.send_task.return_value.id = "task-1"
            result = handler.dispatch(MagicMock(), event)

        assert result == {"status": "accepted"}
        args = celery.send_task.call_args.kwargs["args"]
        assert args[0] == "wahoo"
        assert "webhook_token" not in args[1]
        assert args[1]["user"]["id"] == 60462
        assert "webhook_token" not in store.call_args.kwargs["payload"]

    def test_invalid_body_is_400(self, wahoo_strategy: WahooStrategy) -> None:
        assert wahoo_strategy.webhooks is not None
        with pytest.raises(HTTPException) as exc:
            wahoo_strategy.webhooks.parse_payload(b'{"event_type": "workout_summary"}')
        assert exc.value.status_code == 400


class TestProcessPayload:
    def test_saves_workout_for_connected_user(
        self, db: Session, wahoo_strategy: WahooStrategy, webhook_payload: dict
    ) -> None:
        user = UserFactory()
        UserConnectionFactory(user=user, provider="wahoo", provider_user_id="60462")
        assert wahoo_strategy.webhooks is not None
        raw = wahoo_strategy.webhooks.parse_payload(json.dumps(webhook_payload).encode()).model_dump(mode="json")

        result = wahoo_strategy.webhooks.process_payload(db, raw, "trace")

        assert result["status"] == "processed"
        assert result["records_saved"] == 1
        record = db.query(EventRecord).filter(EventRecord.external_id == "56519").one()
        assert record.duration_seconds == 275

    def test_unknown_user(self, db: Session, wahoo_strategy: WahooStrategy, webhook_payload: dict) -> None:
        assert wahoo_strategy.webhooks is not None
        result = wahoo_strategy.webhooks.process_payload(db, webhook_payload, "trace")
        assert result == {"status": "user_not_found", "wahoo_user_id": 60462}

    def test_other_event_types_are_ignored(self, db: Session, wahoo_strategy: WahooStrategy) -> None:
        assert wahoo_strategy.webhooks is not None
        result = wahoo_strategy.webhooks.process_payload(
            db, {"event_type": "route_created", "user": {"id": 1}}, "trace"
        )
        assert result["status"] == "ignored"

    def test_extract_user_id(self, wahoo_strategy: WahooStrategy, webhook_payload: dict) -> None:
        assert wahoo_strategy.webhooks is not None
        assert wahoo_strategy.webhooks.extract_user_id(webhook_payload) == "60462"
        assert wahoo_strategy.webhooks.extract_user_id({}) is None
