"""Menstrual records sent by the mobile SDKs (Apple HealthKit, Health Connect, Samsung).

The SDKs put them in ``records[]`` with the usual record shape:

- Apple: ``HKCategoryTypeIdentifierMenstrualFlow`` with the ``HKCategoryValueVaginalBleeding``
  raw value (1 unspecified, 2 light, 3 medium, 4 heavy, 5 none) and
  ``HKCategoryTypeIdentifierIntermenstrualBleeding`` (spotting; value is ignored).
- Health Connect / Samsung (Health Connect record names): ``MENSTRUATION_FLOW`` (also accepted as
  ``MENSTRUATION``) with ``MenstruationFlowRecord.flow`` (0 unknown, 1 light, 2 medium, 3 heavy),
  ``MENSTRUATION_PERIOD`` (one record spanning the period days; value is ignored) and
  ``INTERMENSTRUAL_BLEEDING`` (spotting).

Every record becomes one ``menstrual_flow`` sample per local day, coded with ``MenstrualFlow``.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import IntEnum, StrEnum


class MenstrualFlow(IntEnum):
    """Value stored in the ``menstrual_flow`` series (one sample per day)."""

    NONE = 0
    SPOTTING = 1
    LIGHT = 2
    MEDIUM = 3
    HEAVY = 4
    UNSPECIFIED = 5  # bleeding, amount not given

    @property
    def is_period_bleeding(self) -> bool:
        """Bleeding that counts as a period day (spotting does not)."""
        return self in (MenstrualFlow.LIGHT, MenstrualFlow.MEDIUM, MenstrualFlow.HEAVY, MenstrualFlow.UNSPECIFIED)

    @property
    def label(self) -> str:
        return self.name.lower()


class SDKReproductiveType(StrEnum):
    APPLE_MENSTRUAL_FLOW = "HKCategoryTypeIdentifierMenstrualFlow"
    APPLE_INTERMENSTRUAL_BLEEDING = "HKCategoryTypeIdentifierIntermenstrualBleeding"
    ANDROID_MENSTRUATION_FLOW = "MENSTRUATION_FLOW"
    ANDROID_MENSTRUATION = "MENSTRUATION"
    ANDROID_MENSTRUATION_PERIOD = "MENSTRUATION_PERIOD"
    ANDROID_INTERMENSTRUAL_BLEEDING = "INTERMENSTRUAL_BLEEDING"


REPRODUCTIVE_TYPE_IDENTIFIERS: frozenset[str] = frozenset(t.value for t in SDKReproductiveType)

_APPLE_FLOW: dict[int, MenstrualFlow] = {
    1: MenstrualFlow.UNSPECIFIED,
    2: MenstrualFlow.LIGHT,
    3: MenstrualFlow.MEDIUM,
    4: MenstrualFlow.HEAVY,
    5: MenstrualFlow.NONE,
}

_ANDROID_FLOW: dict[int, MenstrualFlow] = {
    0: MenstrualFlow.UNSPECIFIED,
    1: MenstrualFlow.LIGHT,
    2: MenstrualFlow.MEDIUM,
    3: MenstrualFlow.HEAVY,
}


def is_reproductive_type(record_type: str | None) -> bool:
    return bool(record_type) and record_type in REPRODUCTIVE_TYPE_IDENTIFIERS


def flow_from_record(record_type: str, value: Decimal | float | int | None) -> MenstrualFlow | None:
    """Map an SDK menstrual record to the stored flow code. ``None`` when it cannot be read."""
    match record_type:
        case SDKReproductiveType.APPLE_MENSTRUAL_FLOW:
            return _APPLE_FLOW.get(int(value)) if value is not None else MenstrualFlow.UNSPECIFIED
        case SDKReproductiveType.ANDROID_MENSTRUATION_FLOW | SDKReproductiveType.ANDROID_MENSTRUATION:
            return _ANDROID_FLOW.get(int(value)) if value is not None else MenstrualFlow.UNSPECIFIED
        case SDKReproductiveType.ANDROID_MENSTRUATION_PERIOD:
            return MenstrualFlow.UNSPECIFIED
        case SDKReproductiveType.APPLE_INTERMENSTRUAL_BLEEDING | SDKReproductiveType.ANDROID_INTERMENSTRUAL_BLEEDING:
            return MenstrualFlow.SPOTTING
        case _:
            return None


def parse_zone_offset(zone_offset: str | None) -> timezone:
    """``+02:00`` / ``-0530`` / ``Z`` → tzinfo. UTC when missing or unreadable."""
    if not zone_offset or zone_offset.upper() == "Z":
        return timezone.utc
    raw = zone_offset.replace(":", "")
    try:
        sign = -1 if raw[0] == "-" else 1
        hours, minutes = int(raw[1:3]), int(raw[3:5] or 0)
    except (ValueError, IndexError):
        return timezone.utc
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def local_days(start: datetime, end: datetime, zone_offset: str | None) -> list[date]:
    """Local calendar days covered by ``[start, end)``; a zero-length record covers its start day.

    HealthKit and Health Connect write menstrual days as all-day samples (local midnight to the
    next midnight), so the end instant itself is excluded.
    """
    tz = parse_zone_offset(zone_offset)
    first = start.astimezone(tz).date() if start.tzinfo else start.date()
    if end <= start:
        return [first]
    last_instant = end - timedelta(microseconds=1)
    last = last_instant.astimezone(tz).date() if last_instant.tzinfo else last_instant.date()
    days = (last - first).days
    # Guard against a malformed record spanning months: a period never lasts that long.
    if days > 14:
        return [first]
    return [first + timedelta(days=i) for i in range(days + 1)]
