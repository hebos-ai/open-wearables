from app.utils.exceptions import UnsupportedProviderError

PARTNER_ACCESS_HINT = "pending COROS partner API access (apply via api@coros.com)"


def pending_partner_access(operation: str) -> UnsupportedProviderError:
    """Error raised by every COROS operation whose payload format is not public yet."""
    return UnsupportedProviderError("coros", f"{operation} — {PARTNER_ACCESS_HINT}")
