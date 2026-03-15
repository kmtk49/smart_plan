"""
Garmin Connect トークン診断 & 修正スクリプト
============================================
実行すると：
1. トークンの保存場所と状態を確認
2. 問題があれば自動修正
3. ワークアウット取得テスト

使い方:
  python garmin_check.py
"""

import garth
from pathlib import Path
from getpass import getpass
import json

# Windowsでよくあるトークン保存先を全部チェック
CANDIDATE_DIRS = [
    Path.home() / ".garth",
    Path.home() / ".garminconnect",
    Path("C:/Users") / Path.home().name / ".garth",
]

def check_token_dirs():
    print("\n[1] トークンファイルの確認")
    print("─" * 50)
    found = None
    for d in CANDIDATE_DIRS:
        exists = d.exists()
        files  = list(d.glob("*.json")) if exists else []
        status = f"✅ {len(files)}ファイル" if files else ("📁 空" if exists else "❌ なし")
        print(f"  {d} : {status}")
        if files and found is None:
            found = d
            for f in files:
                size = f.stat().st_size
                print(f"    └─ {f.name} ({size}bytes)")
    return found

def try_resume(token_dir):
    print(f"\n[2] トークン読み込みテスト ({token_dir})")
    print("─" * 50)
    try:
        garth.resume(str(token_dir))
        print("  garth.resume() : ✅ 成功")
    except Exception as e:
        print(f"  garth.resume() : ❌ {e}")
        return False

    try:
        profile = garth.connectapi("/userprofile-service/socialProfile")
        name = profile.get("displayName") or profile.get("userName", "?")
        print(f"  API接続テスト  : ✅ ログイン済み ({name})")
        return True
    except Exception as e:
        print(f"  API接続テスト  : ❌ {e}")
        return False

def fresh_login(token_dir):
    print("\n[3] 新規ログイン（トークン再取得）")
    print("─" * 50)
    print("  ※ MFAコードが届いたら入力してください")
    email    = input("  メールアドレス: ")
    password = getpass("  パスワード: ")
    try:
        garth.login(email, password)
        token_dir.mkdir(parents=True, exist_ok=True)
        garth.save(str(token_dir))
        print(f"  ✅ ログイン成功！トークンを保存: {token_dir}")
        return True
    except Exception as e:
        print(f"  ❌ ログイン失敗: {e}")
        return False

def test_workouts():
    print("\n[4] ワークアウト取得テスト")
    print("─" * 50)
    try:
        resp = garth.connectapi(
            "/workout-service/workouts",
            params={"start": 0, "limit": 5, "myWorkoutsOnly": True}
        )
        workouts = resp if isinstance(resp, list) else resp.get("workouts", [])
        print(f"  ✅ {len(workouts)}件取得（最初の5件）")
        for w in workouts:
            name = w.get("workoutName", "?")
            sport = w.get("sportType", {}).get("sportTypeKey", "?")
            print(f"    - [{sport}] {name}")
        return True
    except Exception as e:
        print(f"  ❌ 取得失敗: {e}")
        return False

# ============================================================
# メイン
# ============================================================
print("=" * 50)
print("  Garmin Connect トークン診断")
print("=" * 50)

TOKEN_DIR = Path.home() / ".garth"

# 1. 既存トークン確認
found_dir = check_token_dirs()

# 2. トークンがあれば読み込みテスト
if found_dir:
    ok = try_resume(found_dir)
else:
    print("\n  トークンが見つかりません。ログインします。")
    ok = False

# 3. 失敗なら再ログイン
if not ok:
    ok = fresh_login(TOKEN_DIR)

# 4. ワークアウット取得テスト
if ok:
    test_workouts()
    print("\n" + "=" * 50)
    print("  ✅ セットアップ完了！")
    print("  以降は garmin_to_intervals.py を実行するだけです")
    print("=" * 50)
else:
    print("\n" + "=" * 50)
    print("  ❌ セットアップ失敗")
    print("  Garmin Connectのメール/パスワードを確認してください")
    print("=" * 50)
