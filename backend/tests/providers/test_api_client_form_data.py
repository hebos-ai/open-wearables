"""make_authenticated_request: form-encoded bodies (used by planned-workout push)."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.providers.api_client import make_authenticated_request


def _call(**kwargs: object) -> object:
    return make_authenticated_request(
        db=MagicMock(),
        user_id=uuid4(),
        connection_repo=MagicMock(),
        oauth=MagicMock(),
        api_base_url="https://api.example.com",
        provider_name="wahoo",
        endpoint="/v1/plans",
        method="POST",
        **kwargs,  # type: ignore[arg-type]
    )


def test_json_and_form_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _call(json_data={"a": 1}, form_data={"b": 2})


def test_form_data_is_sent_as_request_data() -> None:
    response = MagicMock(status_code=201)
    response.json.return_value = {"id": 1}
    client = MagicMock()
    client.__enter__.return_value.request.return_value = response
    with (
        patch("app.services.providers.api_client._get_valid_token", return_value="tok"),
        patch("app.services.providers.api_client.httpx.Client", return_value=client),
    ):
        assert _call(form_data={"plan[external_id]": "x"}) == {"id": 1}

    kwargs = client.__enter__.return_value.request.call_args.kwargs
    assert kwargs["data"] == {"plan[external_id]": "x"}
    assert kwargs["json"] is None
    assert kwargs["headers"]["Authorization"] == "Bearer tok"
