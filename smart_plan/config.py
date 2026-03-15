"""
config.py — 設定読み込みモジュール
smart_plan_v10.py line 41-43 から抽出
"""

import yaml
from pathlib import Path

CONFIG_FILE = Path(__file__).parent.parent / "config.yaml"


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)
