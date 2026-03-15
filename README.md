# Smart Plan — AI トライアスロントレーニングプラン生成ツール

Intervals.icu・Googleカレンダーと連携し、HRV・フィットネス状態・レーススケジュールを自動取得して、最適なトレーニングプランを生成・アップロードするPythonツールです。

---

## 主な機能

- **Intervals.icu 連携** — FTP / TP / CSS / CTL / ATL / HRV を自動取得
- **Googleカレンダー解析** — レース・仕事・旅行・スイム予約を自動検出
- **HRVスコアリング** — 睡眠・安静時心拍・HRV 7日平均からコンディション判定
- **ピリオダイゼーション** — Base / Build / Peak / Race / Recovery フェーズ自動切替
- **ワークアウト自動生成** — スイム・バイク・ラン・筋トレ・ブリックのメニューを生成
- **Garmin Connect 対応** — Garmin ワークアウト JSON 形式でエクスポート
- **栄養計算** — 練習強度・体重・フェーズに応じた摂取カロリー・糖質量を算出
- **対話モード（CLI）** — コーチとチャットしてからプラン生成
- **ブラウザUI** — `--server` でローカルHTMLチャットサーバーを起動

---

## ディレクトリ構成

```
Training_scripts/
├── run_smart_plan.py          # 実行ランチャー（ここを直接実行）
├── config.yaml                # APIキー・設定（Gitには含まれない）
├── coach_chat.html            # ブラウザUI（--server モード用）
├── smart_plan/                # メインパッケージ
│   ├── main.py                # エントリーポイント（main関数）
│   ├── config.py              # 設定読み込み
│   ├── icu_api.py             # Intervals.icu API通信
│   ├── athlete_model.py       # アスリートデータ取得・HRV・ゾーン計算
│   ├── result_parser.py       # PDF/Excelリザルト解析・天気取得
│   ├── session_db.py          # 短時間SessionDB・不足種目検出
│   ├── calendar_parser.py     # Googleカレンダー解析・ディレクティブ解析
│   ├── phase_engine.py        # レースフェーズ判定・強度決定
│   ├── workout_builder.py     # ワークアウト生成・セッション説明文
│   ├── strength.py            # 筋トレDB・メニュー生成
│   ├── garmin_export.py       # Garmin Connect JSON生成
│   ├── nutrition.py           # 栄養計算
│   ├── plan_generator.py      # 日別プラン生成（generate_days）
│   ├── plan_output.py         # プラン表示・カロリーサマリー
│   ├── upload.py              # Intervals.icuアップロード
│   ├── gcal_sync.py           # Googleカレンダー同期・変換
│   ├── summary.py             # レース/ピリオダイゼーション/仕事サマリー
│   └── chat.py                # CLIチャット・HTTPサーバー
├── smart_plan_v10.py          # 旧モノリシック版（参照用）
└── .vscode/launch.json        # VS Code デバッグ設定
```

---

## セットアップ

### 1. 依存パッケージのインストール

```bash
pip install pyyaml
pip install PyPDF2       # PDF リザルト解析（任意）
pip install openpyxl     # Excel リザルト解析（任意）
pip install pandas       # Excel 解析補助（任意）
```

### 2. config.yaml の作成

`config.yaml.example` を参考に `config.yaml` を作成してください（APIキーを含むためGitには含まれません）。

```yaml
athlete:
  intervals_icu_athlete_id: "iXXXXXX"
  intervals_icu_api_key: "your_api_key"
  ftp_fallback: 223
  tp_fallback: 288
  css_fallback: 125

google_calendar:
  credentials_file: "credentials.json"
  calendar_id: "primary"
```

---

## 実行方法

```bash
cd Training_scripts

# 10日分プレビュー（アップロードなし）
python run_smart_plan.py --preview

# 当日のトレーニングのみ表示
python run_smart_plan.py --today

# 14日分プレビュー
python run_smart_plan.py --days 14 --preview

# プラン生成 → Intervals.icu にアップロード
python run_smart_plan.py

# ブラウザUIサーバー起動（localhost:8765）
python run_smart_plan.py --server
```

### VS Code から実行

`F5` キーを押して実行構成を選択：

| 構成名 | 内容 |
|---|---|
| Smart Plan (preview) | 10日プレビュー |
| Smart Plan (today) | 当日のみ |
| Smart Plan (14日) | 14日プレビュー |
| Smart Plan (chat server) | ブラウザUIサーバー |

---

## モジュール別のトークン効率

Claude Code で作業する場合、関係するモジュールだけ読み込むことでトークンを節約できます。

| 作業内容 | 対象ファイル |
|---|---|
| Garmin JSON のバグ修正 | `smart_plan/garmin_export.py` |
| 栄養計算の調整 | `smart_plan/nutrition.py` |
| チャット機能の改修 | `smart_plan/chat.py` |
| ワークアウト内容の変更 | `smart_plan/workout_builder.py` |
| カレンダー解析の調整 | `smart_plan/calendar_parser.py` |
| フェーズ・強度ロジック | `smart_plan/phase_engine.py` |

---

## 注意事項

- `config.yaml` はAPIキーを含むため `.gitignore` で除外しています
- `*.csv`（activities データ）も除外済みです
- Googleカレンダー連携には `credentials.json`（OAuth2）が必要です
