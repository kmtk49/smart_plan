"""
smart_plan package — トレーニングプラン生成モジュール群
smart_plan_v10.py を機能別に分割したパッケージ
"""

from .config import load_config, CONFIG_FILE
from .icu_api import icu_headers, icu_get, icu_post, icu_delete
from .result_parser import (
    fetch_weather_for_race, fetch_gdrive_pdf_via_api,
    parse_split_times_from_text, resolve_result_path,
    parse_result_file, load_race_splits_from_csv,
    _find_activities_csv, extract_venue_coords,
)
from .athlete_model import (
    _pace_to_icu, _fmt_pace, _swim_pace, _swim_pace_icu,
    run_pace_zones, _fmt_time, calc_hrv_score,
    fetch_athlete_data, calc_goal_targets,
)
from .session_db import (
    IDEAL_WEEKLY, detect_deficient_sports,
    SHORT_SESSIONS, pick_short_session,
)
from .calendar_parser import (
    RACE_DISTANCE_DEFS, parse_gcal_day, build_directive_template,
    _detect_race_type, _detect_race_distance, _parse_race_priority,
    parse_training_directive, apply_trip_adjacency,
)
from .phase_engine import (
    get_race_phase, calc_strength_progression,
    INTENSITY_ORDER, PHASE_TEMPLATES, PHASE_INTENSITY,
    COND_OVERRIDE, decide_intensity,
    PHASE_MOTIVATIONS, INTENSITY_LABELS,
)
from .workout_builder import build_workout, session_desc
from .strength import STRENGTH_DB, gen_strength_menu
from .garmin_export import (
    _garmin_step, _garmin_repeat, _pace_to_ms,
    build_garmin_workout, export_garmin_workout_json, build_workout_both,
)
from .nutrition import calc_nutrition
from .plan_generator import (
    EMOJI, SHORT_THRESH, generate_days,
    _build_brick_session, consistency_check, generate_week,
)
from .plan_output import print_calorie_summary, print_plan, _print_body_comp_status
from .upload import upload_plan
from .gcal_sync import (
    WORK_KWS, FLIGHT_KWS, TRIP_KWS, RACE_KWS,
    parse_gcal_events_to_days,
)
from .summary import (
    PHASE_JP, RACE_TYPE_JP, DIST_JP,
    print_race_schedule_summary, print_periodization_summary,
    print_work_schedule_summary,
)
from .chat import (
    _cli_chat_session, _apply_feeling_to_cond, _apply_requests_to_gcal,
    _override_intensity, run_chat_server, _server_chat_turn,
    _generate_plan_from_context, _plan_to_api_json,
    _fetch_gcal_events_auto, _inject_cal_rival,
)
from .main import main

__all__ = [
    "load_config", "CONFIG_FILE",
    "icu_headers", "icu_get", "icu_post", "icu_delete",
    "fetch_weather_for_race", "fetch_gdrive_pdf_via_api",
    "parse_split_times_from_text", "resolve_result_path",
    "parse_result_file", "load_race_splits_from_csv",
    "_find_activities_csv", "extract_venue_coords",
    "_pace_to_icu", "_fmt_pace", "_swim_pace", "_swim_pace_icu",
    "run_pace_zones", "_fmt_time", "calc_hrv_score",
    "fetch_athlete_data", "calc_goal_targets",
    "IDEAL_WEEKLY", "detect_deficient_sports",
    "SHORT_SESSIONS", "pick_short_session",
    "RACE_DISTANCE_DEFS", "parse_gcal_day", "build_directive_template",
    "_detect_race_type", "_detect_race_distance", "_parse_race_priority",
    "parse_training_directive", "apply_trip_adjacency",
    "get_race_phase", "calc_strength_progression",
    "INTENSITY_ORDER", "PHASE_TEMPLATES", "PHASE_INTENSITY",
    "COND_OVERRIDE", "decide_intensity",
    "PHASE_MOTIVATIONS", "INTENSITY_LABELS",
    "build_workout", "session_desc",
    "STRENGTH_DB", "gen_strength_menu",
    "_garmin_step", "_garmin_repeat", "_pace_to_ms",
    "build_garmin_workout", "export_garmin_workout_json", "build_workout_both",
    "calc_nutrition",
    "EMOJI", "SHORT_THRESH", "generate_days",
    "_build_brick_session", "consistency_check", "generate_week",
    "print_calorie_summary", "print_plan", "_print_body_comp_status",
    "upload_plan",
    "WORK_KWS", "FLIGHT_KWS", "TRIP_KWS", "RACE_KWS",
    "parse_gcal_events_to_days",
    "PHASE_JP", "RACE_TYPE_JP", "DIST_JP",
    "print_race_schedule_summary", "print_periodization_summary",
    "print_work_schedule_summary",
    "_cli_chat_session", "_apply_feeling_to_cond", "_apply_requests_to_gcal",
    "_override_intensity", "run_chat_server", "_server_chat_turn",
    "_generate_plan_from_context", "_plan_to_api_json",
    "_fetch_gcal_events_auto", "_inject_cal_rival",
    "main",
]
