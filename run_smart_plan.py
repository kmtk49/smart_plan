"""
run_smart_plan.py — VS Code F5 / ターミナルから直接実行するランチャー

使い方:
  python run_smart_plan.py              # デフォルト (10日プラン, 対話モード)
  python run_smart_plan.py --preview    # アップロードなしでプレビュー
  python run_smart_plan.py --today      # 当日のみ
  python run_smart_plan.py --days 14    # 14日分
  python run_smart_plan.py --server     # HTMLチャットUIサーバー起動
"""

import sys
from pathlib import Path

# Training_scripts をパスに追加
sys.path.insert(0, str(Path(__file__).parent))

from smart_plan.main import main

if __name__ == "__main__":
    import traceback, sys
    try:
        main()
    except Exception:
        sys.stdout.flush()
        traceback.print_exc()
        sys.exit(1)
