"""Google Health API nutrition-log → dietary_* time series.

Each nutrition-log DataPoint (a logged food or meal, a Session type) becomes one sample per
nutrient at the entry's start time, in the series' canonical unit (kcal, g, mg, mcg). There
is no meal model in this fork: the series are summed per day by consumers.

Field names follow upstream Open Wearables (#1689), checked against the Google Health API
reference: top-level ``energy``/``energyFromFat`` (``kcal``) and ``totalCarbohydrate``/
``totalFat`` (``grams``), plus a ``nutrients`` list of ``{nutrient, quantity: {grams}}``.
Google reports every entry of ``nutrients`` in grams, so it is scaled to the series unit.

Needs the ``googlehealth.nutrition.readonly`` scope (in the default scope set). Health scopes
go through Google's OAuth app verification.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.schemas.enums import SeriesType, get_series_type_unit
from app.services.providers.google.health_api.helpers import parse_interval, read_number, zone_offset_from

NUTRITION_DATA_TYPE = "nutrition-log"
NUTRITION_VALUE_KEY = "nutritionLog"

# NutritionLog top-level fields: (field, quantity subfield, series).
NUTRITION_PRIMARY_FIELDS: tuple[tuple[str, str, SeriesType], ...] = (
    ("energy", "kcal", SeriesType.dietary_energy_consumed),
    ("energyFromFat", "kcal", SeriesType.dietary_energy_from_fat),
    ("totalCarbohydrate", "grams", SeriesType.dietary_carbohydrates),
    ("totalFat", "grams", SeriesType.dietary_fat_total),
)

# NutrientQuantity.nutrient enum -> series.
NUTRIENT_FIELDS: dict[str, SeriesType] = {
    "BIOTIN": SeriesType.dietary_biotin,
    "CAFFEINE": SeriesType.dietary_caffeine,
    "CALCIUM": SeriesType.dietary_calcium,
    "CARBOHYDRATES": SeriesType.dietary_carbohydrates,
    "CHLORIDE": SeriesType.dietary_chloride,
    "CHOLESTEROL": SeriesType.dietary_cholesterol,
    "CHROMIUM": SeriesType.dietary_chromium,
    "COPPER": SeriesType.dietary_copper,
    "DIETARY_FIBER": SeriesType.dietary_fiber,
    "FOLATE": SeriesType.dietary_folate,
    "FOLIC_ACID": SeriesType.dietary_folic_acid,
    "IODINE": SeriesType.dietary_iodine,
    "IRON": SeriesType.dietary_iron,
    "MAGNESIUM": SeriesType.dietary_magnesium,
    "MANGANESE": SeriesType.dietary_manganese,
    "MOLYBDENUM": SeriesType.dietary_molybdenum,
    "MONOUNSATURATED_FAT": SeriesType.dietary_fat_monounsaturated,
    "NIACIN": SeriesType.dietary_niacin,
    "PANTOTHENIC_ACID": SeriesType.dietary_pantothenic_acid,
    "PHOSPHORUS": SeriesType.dietary_phosphorus,
    "POLYUNSATURATED_FAT": SeriesType.dietary_fat_polyunsaturated,
    "POTASSIUM": SeriesType.dietary_potassium,
    "PROTEIN": SeriesType.dietary_protein,
    "RIBOFLAVIN": SeriesType.dietary_riboflavin,
    "SATURATED_FAT": SeriesType.dietary_fat_saturated,
    "SELENIUM": SeriesType.dietary_selenium,
    "SODIUM": SeriesType.dietary_sodium,
    "SUGAR": SeriesType.dietary_sugar,
    "THIAMIN": SeriesType.dietary_thiamin,
    "TRANS_FAT": SeriesType.dietary_fat_trans,
    "UNSATURATED_FAT": SeriesType.dietary_fat_unsaturated,
    "VITAMIN_A": SeriesType.dietary_vitamin_a,
    "VITAMIN_B12": SeriesType.dietary_vitamin_b12,
    "VITAMIN_B6": SeriesType.dietary_vitamin_b6,
    "VITAMIN_C": SeriesType.dietary_vitamin_c,
    "VITAMIN_D": SeriesType.dietary_vitamin_d,
    "VITAMIN_E": SeriesType.dietary_vitamin_e,
    "VITAMIN_K": SeriesType.dietary_vitamin_k,
    "ZINC": SeriesType.dietary_zinc,
}

NUTRITION_SERIES: frozenset[SeriesType] = frozenset(
    {*(series for _, _, series in NUTRITION_PRIMARY_FIELDS), *NUTRIENT_FIELDS.values()}
)

_GRAMS_TO_UNIT: dict[str, Decimal] = {"g": Decimal(1), "mg": Decimal(1_000), "mcg": Decimal(1_000_000)}


def nutrients_of(nutrition: dict[str, Any]) -> dict[SeriesType, Decimal]:
    """Every nutrient Google returned for one entry, scaled to the series unit.

    Top-level totals win over the same nutrient repeated in ``nutrients``.
    """
    values: dict[SeriesType, Decimal] = {}
    for field, subfield, series_type in NUTRITION_PRIMARY_FIELDS:
        value = read_number(nutrition, field, subfield=subfield)
        if value is not None:
            values[series_type] = value
    for entry in nutrition.get("nutrients") or []:
        if not isinstance(entry, dict) or (series_type := NUTRIENT_FIELDS.get(entry.get("nutrient") or "")) is None:
            continue
        scale = _GRAMS_TO_UNIT[get_series_type_unit(series_type)]
        value = read_number(entry, "quantity", subfield="grams", scale=scale)
        if value is not None:
            values.setdefault(series_type, value)
    return values


def parse_nutrition_point(
    point: dict[str, Any], start_time: datetime, end_time: datetime
) -> tuple[datetime, str | None, dict[SeriesType, Decimal]] | None:
    """(start, zone_offset, nutrients) of one nutrition-log DataPoint inside the window, else None."""
    nutrition = point.get(NUTRITION_VALUE_KEY)
    if not isinstance(nutrition, dict):
        return None
    interval = nutrition.get("interval") or {}
    start, _end = parse_interval(interval)
    if start is None or not (start_time <= start < end_time):
        return None
    nutrients = nutrients_of(nutrition)
    if not nutrients:
        return None
    return start, zone_offset_from(interval.get("startUtcOffset")), nutrients
