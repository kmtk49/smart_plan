"""
garmin_token_setup.py — Garmin Connect 初回認証セットアップ
===========================================================
このスクリプトは「一度だけ」ターミナルで対話的に実行してください。

  python garmin_token_setup.py

Garmin アカウントに MFA（2段階認証）が設定されている場合、
Garmin から届くメールのコードを入力してください。

成功すると ~/.garminconnect/ にトークンが保存され、
以降は smart_plan が自動ログインします。
"""

from pathlib import Path


def setup_garmin_token():
    # garminconnect インストール確認
    try:
        from garminconnect import Garmin
    except ImportError:
        print("❌  garminconnect が未インストールです。")
        print("    pip install garminconnect")
        return False

    token_dir = Path.home() / ".garminconnect"
    token_str = str(token_dir)

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
        except Exception as e:
            print(f"⚠️  config.yaml 読み込みエラー: {e}")

    if not email:
        email    = input("Garmin Connect メールアドレス: ").strip()
    if not password:
        import getpass
        password = getpass.getpass("Garmin Connect パスワード: ")

    print(f"\n🔑 Garmin Connect にログイン中... ({email})")
    print("   MFAが有効な場合、Garmin からメールが届きます。\n")

    try:
        garmin = Garmin(email=email, password=password)
        garmin.login()
        token_dir.mkdir(parents=True, exist_ok=True)
        garmin.garth.dump(token_str)
        print(f"\n✅  認証成功！トークンを保存しました: {token_dir}")
        print("    次回から smart_plan は自動ログインします。")
        return True
    except Exception as e:
        print(f"\n❌  ログイン失敗: {e}")
        print("\n原因として考えられること:")
        print("  1. メールアドレス/パスワードが間違っている")
        print("  2. MFAコードを入力しなかった（Garminメールを確認してください）")
        print("  3. ネットワーク接続の問題")
        return False


if __name__ == "__main__":
    setup_garmin_token()
