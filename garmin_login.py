"""
【Step 1】Garmin Connect ログイン & トークン保存
================================================
MFA（2段階認証）対応。これは最初の1回だけ実行すれば OK。
トークンは1年間有効で自動更新されます。

使い方:
  pip install garth
  python garmin_login.py
"""

import garth
from getpass import getpass
from pathlib import Path

TOKEN_DIR = Path.home() / ".garth"

print("=" * 50)
print("  Garmin Connect ログイン（初回トークン取得）")
print("=" * 50)
print(f"\n  トークン保存先: {TOKEN_DIR}")
print("  ※ MFAコードの入力を求められた場合は")
print("    メール/SMS に届いたコードを入力してください\n")

email    = input("  Garmin Connect メールアドレス: ")
password = getpass("  Garmin Connect パスワード: ")

try:
    # garth.login() は MFA プロンプトを自動処理する
    garth.login(email, password)
    garth.save(str(TOKEN_DIR))
    print(f"\n  ✅ ログイン成功！トークンを保存しました")
    print(f"  保存場所: {TOKEN_DIR}")
    print(f"\n  次回からは garmin_to_intervals.py を実行するだけです")
    print(f"  （パスワード入力不要）")
except Exception as e:
    print(f"\n  ❌ ログイン失敗: {e}")
    print("\n  よくある原因:")
    print("  - メールアドレス/パスワードが間違っている")
    print("  - MFAコードの入力タイムアウト（再実行してください）")
