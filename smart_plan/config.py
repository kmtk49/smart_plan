"""
config.py — 設定読み込みモジュール

優先順位:
  1. 環境変数 (INTERVALS_ICU_ATHLETE_ID, INTERVALS_ICU_API_KEY など)
  2. config.yaml ファイル
"""

import os
import yaml
from pathlib import Path

CONFIG_FILE = Path(__file__).parent.parent / "config.yaml"

# 環境変数名 → config.yaml パスのマッピング
_ENV_MAP = {
    "INTERVALS_ICU_ATHLETE_ID":  ("athlete", "intervals_icu_athlete_id"),
    "INTERVALS_ICU_API_KEY":     ("athlete", "intervals_icu_api_key"),
    "GOOGLE_CALENDAR_ID":        ("google_calendar", "calendar_id"),
    "GOOGLE_CREDENTIALS_FILE":   ("google_calendar", "credentials_file"),
    "GOOGLE_DRIVE_TOKEN":        ("google_drive", "token"),
}


def load_config() -> dict:
    """設定を読み込む。環境変数が設定されていればconfig.yamlより優先。"""
    cfg: dict = {}

    # config.yaml が存在すれば読み込む
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

    # 環境変数で上書き
    for env_var, (section, key) in _ENV_MAP.items():
        val = os.environ.get(env_var)
        if val:
            cfg.setdefault(section, {})[key] = val

    # 必須キーの確認
    athlete_id  = (cfg.get("athlete") or {}).get("intervals_icu_athlete_id")
    athlete_key = (cfg.get("athlete") or {}).get("intervals_icu_api_key")
    if not athlete_id or not athlete_key:
        raise RuntimeError(
            "Intervals.icu の認証情報が見つかりません。\n"
            "以下のいずれかで設定してください:\n"
            "  - config.yaml に athlete.intervals_icu_athlete_id / intervals_icu_api_key を記載\n"
            "  - 環境変数 INTERVALS_ICU_ATHLETE_ID / INTERVALS_ICU_API_KEY を設定"
        )

    return cfg
