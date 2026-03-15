"""
garmin_token_setup.py — Garmin Connect 初回認証セットアップ
===========================================================
このスクリプトは「一度だけ」ターミナルで対話的に実行してください。

  python garmin_token_setup.py

MFA（2段階認証）が有効な場合:
  1. Garmin からメールが届く
  2. ターミナルに「Enter MFA code:」が表示される
  3. メールに書かれた6桁のコードを入力してEnter

成功すると ~/.garminconnect/ にトークンが保存され、
以降は smart_plan が自動ログインします。
"""

from pathlib import Path


def setup_garmin_token():
    # garth インストール確認 (garminconnect の依存ライブラリ)
    try:
        import garth
        from garminconnect import Garmin
    except ImportError:
        print("❌  garminconnect が未インストールです。")
        print("    pip install garminconnect")
        return False

    token_dir = Path.home() / ".garminconnect"
    token_str = str(token_dir)

    # 既存トークンを削除（クリーンな状態から開始）
    if token_dir.exists():
        import shutil
        shutil.rmtree(token_dir)
        print(f"🗑️  既存トークンを削除しました: {token_dir}")

    # config.yaml からメール/パスワードを読み込む
    cfg_path = Path(__file__).parent / "config.yaml"
    email = password = ""
    if cfg_path.exists():
        try:
            import yaml
            with open(cfg_path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            garmin_cfg = cfg.get("garmin", {})
            email    = garmin_cfg.get("email", "")
            password = garmin_cfg.get("password", "")
            if email:
                print(f"📋  config.yaml からメール読み込み: {email}")
        except Exception as e:
            print(f"⚠️  config.yaml 読み込みエラー: {e}")

    if not email:
        email = input("Garmin Connect メールアドレス: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("Garmin Connect パスワード: ")

    print(f"\n🔑 Garmin Connect にログイン中... ({email})")
    print("   MFAが有効な場合、まもなく Garmin からメールが届きます。")
    print("   ターミナルに「Enter MFA code:」が表示されたらコードを入力してください。\n")

    # garth を直接使って MFA 対話ログイン
    # garth.login() は内部で input("Enter MFA code: ") を呼ぶ
    try:
        garth.configure(domain="garmin.com")
        garth.login(email, password)           # MFAプロンプトが出たらコード入力
        token_dir.mkdir(parents=True, exist_ok=True)
        garth.save(token_str)                  # トークンを保存
        print(f"\n✅  認証成功！トークンを保存しました: {token_dir}")
        print("    次回から smart_plan は自動ログインします。")
        return True
    except Exception as e:
        print(f"\n❌  ログイン失敗: {e}")
        print("\n原因として考えられること:")
        print("  1. メールアドレス/パスワードが間違っている")
        print("  2. MFAコードを入力しなかった、または間違えた")
        print("  3. ネットワーク接続の問題")
        return False


if __name__ == "__main__":
    setup_garmin_token()
