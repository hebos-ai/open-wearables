"""Wahoo ``workout_type_id`` to unified WorkoutType mapping.

Source: https://cloud-api.wahooligan.com/#workout-types (table of id / name / location / family).
Ids missing from the public table (e.g. 24, 48, 50-55, 57-60) fall back to OTHER.
"""

from app.schemas.enums import WorkoutType

WAHOO_WORKOUT_TYPE_MAPPINGS: list[tuple[int, str, WorkoutType]] = [
    (0, "BIKING", WorkoutType.CYCLING),
    (1, "RUNNING", WorkoutType.RUNNING),
    (2, "FE", WorkoutType.FITNESS_EQUIPMENT),
    (3, "RUNNING_TRACK", WorkoutType.RUNNING),
    (4, "RUNNING_TRAIL", WorkoutType.TRAIL_RUNNING),
    (5, "RUNNING_TREADMILL", WorkoutType.TREADMILL),
    (6, "WALKING", WorkoutType.WALKING),
    (7, "WALKING_SPEED", WorkoutType.WALKING),
    (8, "WALKING_NORDIC", WorkoutType.WALKING),
    (9, "HIKING", WorkoutType.HIKING),
    (10, "MOUNTAINEERING", WorkoutType.MOUNTAINEERING),
    (11, "BIKING_CYCLECROSS", WorkoutType.CYCLOCROSS),
    (12, "BIKING_INDOOR", WorkoutType.INDOOR_CYCLING),
    (13, "BIKING_MOUNTAIN", WorkoutType.MOUNTAIN_BIKING),
    (14, "BIKING_RECUMBENT", WorkoutType.CYCLING),
    (15, "BIKING_ROAD", WorkoutType.CYCLING),
    (16, "BIKING_TRACK", WorkoutType.CYCLING),
    (17, "BIKING_MOTOCYCLING", WorkoutType.MOTORCYCLING),
    (18, "FE_GENERAL", WorkoutType.FITNESS_EQUIPMENT),
    (19, "FE_TREADMILL", WorkoutType.TREADMILL),
    (20, "FE_ELLIPTICAL", WorkoutType.ELLIPTICAL),
    (21, "FE_BIKE", WorkoutType.INDOOR_CYCLING),
    (22, "FE_ROWER", WorkoutType.ROWING_MACHINE),
    (23, "FE_CLIMBER", WorkoutType.STAIR_CLIMBING),
    (25, "SWIMMING_LAP", WorkoutType.POOL_SWIMMING),
    (26, "SWIMMING_OPEN_WATER", WorkoutType.OPEN_WATER_SWIMMING),
    (27, "SNOWBOARDING", WorkoutType.SNOWBOARDING),
    (28, "SKIING", WorkoutType.ALPINE_SKIING),
    (29, "SKIING_DOWNHILL", WorkoutType.DOWNHILL_SKIING),
    (30, "SKIINGCROSS_COUNTRY", WorkoutType.CROSS_COUNTRY_SKIING),
    (31, "SKATING", WorkoutType.SKATING),
    (32, "SKATING_ICE", WorkoutType.ICE_SKATING),
    (33, "SKATING_INLINE", WorkoutType.INLINE_SKATING),
    (34, "LONG_BOARDING", WorkoutType.SKATEBOARDING),
    (35, "SAILING", WorkoutType.SAILING),
    (36, "WINDSURFING", WorkoutType.WINDSURFING),
    (37, "CANOEING", WorkoutType.CANOEING),
    (38, "KAYAKING", WorkoutType.KAYAKING),
    (39, "ROWING", WorkoutType.ROWING),
    (40, "KITEBOARDING", WorkoutType.KITESURFING),
    (41, "STAND_UP_PADDLE_BOARD", WorkoutType.STAND_UP_PADDLEBOARDING),
    (42, "WORKOUT", WorkoutType.STRENGTH_TRAINING),
    (43, "CARDIO_CLASS", WorkoutType.CARDIO_TRAINING),
    (44, "STAIR_CLIMBER", WorkoutType.STAIR_CLIMBING),
    (45, "WHEELCHAIR", WorkoutType.OTHER),
    (46, "GOLFING", WorkoutType.GOLF),
    (47, "OTHER", WorkoutType.OTHER),
    (49, "BIKING_INDOOR_CYCLING_CLASS", WorkoutType.INDOOR_CYCLING),
    (56, "WALKING_TREADMILL", WorkoutType.WALKING),
    (61, "BIKING_INDOOR_TRAINER", WorkoutType.INDOOR_CYCLING),
    (62, "MULTISPORT", WorkoutType.MULTISPORT),
    (63, "TRANSITION", WorkoutType.MULTISPORT),
    (64, "EBIKING", WorkoutType.E_BIKING),
    (65, "TICKR_OFFLINE", WorkoutType.OTHER),
    (66, "YOGA", WorkoutType.YOGA),
    (67, "RUNNING_RACE", WorkoutType.RUNNING),
    (68, "BIKING_INDOOR_VIRTUAL", WorkoutType.INDOOR_CYCLING),
    (69, "MENTAL_STRENGTH", WorkoutType.MEDITATION),
    (70, "HANDCYCLING", WorkoutType.CYCLING),
    (71, "RUNNING_INDOOR_VIRTUAL", WorkoutType.TREADMILL),
    (255, "UNKNOWN", WorkoutType.OTHER),
]

WAHOO_ID_TO_UNIFIED: dict[int, WorkoutType] = {type_id: unified for type_id, _, unified in WAHOO_WORKOUT_TYPE_MAPPINGS}

WAHOO_ID_TO_NAME: dict[int, str] = {type_id: name for type_id, name, _ in WAHOO_WORKOUT_TYPE_MAPPINGS}


def get_unified_workout_type(wahoo_workout_type_id: int | None) -> WorkoutType:
    """Map a Wahoo ``workout_type_id`` to the unified WorkoutType (OTHER when unknown)."""
    if wahoo_workout_type_id is None:
        return WorkoutType.OTHER
    return WAHOO_ID_TO_UNIFIED.get(wahoo_workout_type_id, WorkoutType.OTHER)


def get_activity_name(wahoo_workout_type_id: int | None) -> str:
    """Wahoo's own name for a ``workout_type_id`` (``"UNKNOWN"`` when not in the public table)."""
    if wahoo_workout_type_id is None:
        return "UNKNOWN"
    return WAHOO_ID_TO_NAME.get(wahoo_workout_type_id, "UNKNOWN")
