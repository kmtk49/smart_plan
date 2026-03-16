# Smart Plan — AI トライアスロントレーニングプラン生成ツール

Intervals.icu・Garmin Connect・Googleカレンダーと連携し、HRV・体組成・Readiness・レーススケジュールを自動取得して、最適なトレーニングプランを生成・アップロードするPythonツールです。

---

## できること一覧

### 🔌 データ取得・連携

| 機能 | データソース |
|------|------------|
| FTP / TP（ランペース）/ CSS（スイムペース）自動取得 | Intervals.icu |
| CTL / ATL / フォーム（疲労度）取得 | Intervals.icu |
| HRV・安静時心拍・睡眠 取得 | Intervals.icu wellness |
| 過去90日のアクティビティ履歴・レース結果自動抽出 | Intervals.icu + CSV |
| **朝体重・体内水分%・体脂肪%・筋肉量 取得（過去14日）** | Garmin Connect |
| **Training Readiness 取得（過去14日）** | Garmin Connect |
| **Body Battery 取得（当日）** | Garmin Connect |
| **ハイドレーション解析（トレーニング前後1時間以内の計測）** | Garmin Connect |
| レース・仕事・旅行・スイム予約の自動解析 | Google カレンダー |

---

### 📊 分析・スコアリング

| 機能 | 詳細 |
|------|------|
| **HRVスコア（0〜10）** | HRV変動・睡眠・安静時心拍・フォームから総合判定 |
| **推定グリコーゲン量** | 運動消費×0.55÷1600kcal、睡眠回復モデル 🟢🟡🔴 |
| **体重・体内水分% トレンド** | 朝一番の計測値で前日差表示（日次トレンド用） |
| **Readiness 表示** | 🟢≥67 通常 / 🟡34-66 調整 / 🔴<34 回復優先 |
| **脱水リスク判定** | 体重2%以上減少で警告 |
| **レース目標タイム算出** | 自己ベスト-3% / ライバル / 生理学的推定 |

---

### 💦 ハイドレーション解析（詳細）

- トレーニング**前後1時間以内**に体重計測がある場合に自動表示
- 体重変化・体水分%変化を 🔴減少 / 🟢増加 / ⚪変化なし で表示
- **1日2トレーニング対応**：各トレーニングごとに独立してマッチング
- **中間計測なし対応**：2トレーニング間に計測がない場合は1セッションとして合算（A→①→②→C でも A+C で解析）
- 中間計測（B）がある場合：B は1本目のpost兼2本目のpreとして共用

---

### 📅 プラン生成

| 機能 | 詳細 |
|------|------|
| **6フェーズ対応** | Base / Build / Peak / Taper / Race / Recovery |
| **当日からプラン生成** | 朝の体重計測後すぐ当日メニューを確認可能 |
| **8種目対応** | ラン・バイク・スイム・筋トレ・ブリック・ヨガ・ストレッチ・HIIT |
| **ブリック（バイク→ラン）** | 2種目連続セッション自動生成 |
| **カレンダー制約** | 仕事・出張・スイム予約を自動回避/反映 |
| **強度グラデーション** | Recovery / Easy / Tempo / Threshold / VO2Max |
| **栄養計算** | kcal・タンパク質(g/kg)・糖質・脂質の目標値 |
| **筋トレフェーズ指示** | Base/Build/Peak/Taperごとに種目指示 |

---

### 💬 対話・操作

| 機能 | 詳細 |
|------|------|
| **今日のコンディション入力** | 絶好調〜ボロボロ → HRVスコアに反映 |
| **自由テキストリクエスト** | 「土日朝スイム1.5時間」「木曜削除」「強度高めで」など |
| **ブラウザUI** | `--server` でlocalhost:8765にコーチチャット画面を起動 |
| **プラン確認→修正→再生成ループ** | アップロード前に何度でも調整可能 |

---

### 📤 出力・アップロード

| 機能 | 詳細 |
|------|------|
| **過去7日ウェルネスサマリー** | kcal・体重・HRV・Readiness・体内水分%・グリコーゲン 一覧表示 |
| **Intervals.icu に直接アップロード** | 既存ワークアウトを削除→新プランを一括登録 |
| **プレビューモード** | `--preview` でアップロードせず内容確認のみ |
| **Garmin ワークアウトエクスポート** | 構造化ワークアウトのJSON出力 |

---

## ディレクトリ構成

```
Training_scripts/
├── run_smart_plan.py              # 実行ランチャー（ここを直接実行）
├── config.yaml                    # APIキー・設定（Gitには含まれない）
├── coach_chat.html                # ブラウザUI（--server モード用）
├── smart_plan/                    # メインパッケージ
│   ├── main.py                    # エントリーポイント
│   ├── config.py                  # 設定読み込み
│   ├── icu_api.py                 # Intervals.icu API通信
│   ├── athlete_model.py           # アスリートデータ取得・HRV・体組成・ハイドレーション解析
│   ├── garmin_fetch_health.py     # Garmin Connect 体重・Readiness・全計測取得
│   ├── result_parser.py           # PDF/Excelリザルト解析
│   ├── session_db.py              # 不足種目検出
│   ├── calendar_parser.py         # Googleカレンダー解析
│   ├── phase_engine.py            # レースフェーズ判定・強度決定
│   ├── workout_builder.py         # ワークアウト生成・説明文
│   ├── strength.py                # 筋トレDB・メニュー生成
│   ├── garmin_export.py           # Garmin Connect JSON生成
│   ├── nutrition.py               # 栄養計算
│   ├── plan_generator.py          # 日別プラン生成
│   ├── plan_output.py             # プラン表示・ウェルネスサマリー・ハイドレーション解析表示
│   ├── upload.py                  # Intervals.icuアップロード
│   ├── gcal_sync.py               # Googleカレンダー同期
│   ├── summary.py                 # レース/ピリオダイゼーション/仕事サマリー
│   └── chat.py                    # CLIチャット・HTTPサーバー
└── .vscode/launch.json            # VS Code デバッグ設定
```

---

## セットアップ

### 1. 依存パッケージのインストール

```bash
pip install pyyaml
pip install garminconnect    # Garmin Connect連携（必須）
pip install PyPDF2           # PDF リザルト解析（任意）
pip install openpyxl         # Excel リザルト解析（任意）
```

### 2. Garmin Connect 初回認証（1回だけ）

```bash
python smart_plan/garmin_fetch_health.py
# → メールアドレス・パスワード入力 → MFAコード入力
# → ~/.garminconnect/ にトークンが保存される（以降は自動）
```

### 3. config.yaml の作成

```yaml
athlete:
  intervals_icu_athlete_id: "iXXXXXX"
  intervals_icu_api_key: "your_api_key"
  ftp_fallback: 223
  tp_fallback: 288
  css_fallback: 125

garmin:
  email: "your@email.com"
  password: "your_password"

google_calendar:
  credentials_file: "credentials.json"
  calendar_id: "primary"
```

---

## 実行方法

```bash
cd Training_scripts

# 通常（10日プラン・対話モード → アップロード）
python run_smart_plan.py

# 当日のみ（朝の体重計測後すぐ確認）
python run_smart_plan.py --today

# プレビューのみ（アップロードなし）
python run_smart_plan.py --preview

# 14日分
python run_smart_plan.py --days 14

# ブラウザUIサーバー起動（localhost:8765）
python run_smart_plan.py --server
```

### VS Code から実行（F5）

| 構成名 | 内容 |
|---|---|
| Smart Plan (preview) | 10日プレビュー |
| Smart Plan (today) | 当日のみ |
| Smart Plan (14日) | 14日プレビュー |
| Smart Plan (chat server) | ブラウザUIサーバー |

---

## 注意事項

- `config.yaml` はAPIキーを含むため `.gitignore` で除外されています
- `*.csv`（activities データ）も除外済みです
- Googleカレンダー連携には `credentials.json`（OAuth2）が必要です
- Garmin Connect トークンは `~/.garminconnect/` に保存されます（リポジトリ外）
