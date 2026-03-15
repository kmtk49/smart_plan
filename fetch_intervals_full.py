"""
Intervals.icu 全データ取得スクリプト
=====================================
取得するデータ:
  1. activities.csv          - アクティビティ一覧（全フィールド）
  2. activities_detail.csv   - アクティビティ詳細（ICUメトリクス・区間データ含む）
  3. intervals.csv           - 各アクティビティの検出区間データ
  4. wellness.csv            - ウェルネスデータ（体重/HRV/睡眠/歩数等）
  5. athlete.json            - アスリートプロフィール（参照用）

使い方:
  python fetch_intervals_full.py

必要なライブラリ: 標準ライブラリのみ（追加インストール不要）
"""

import urllib.request
import urllib.parse
import json
import csv
import base64
import time
import os
from datetime import datetime, timedelta

# ===== 設定 =====
ATHLETE_ID = "i275804"
API_KEY    = "4bhzoa2udw4gi80pcie1eijuj"
DAYS       = 365        # 取得する過去の日数（最大で全期間）
OUTPUT_DIR = "intervals_data"   # 出力フォルダ名
DETAIL_LIMIT = 999999   # 詳細取得するアクティビティ数上限（多いと時間がかかる）
REQUEST_DELAY = 0.3     # APIレート制限対策（秒）
# ================

BASE_URL = f"https://intervals.icu/api/v1"
AUTH = base64.b64encode(f"API_KEY:{API_KEY}".encode()).decode()
HEADERS = {
    "Authorization": f"Basic {AUTH}",
    "Accept": "application/json",
}

def api_get(path, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read())
    except urllib.error.HTTPError as e:
        print(f"  [警告] HTTP {e.code}: {path}")
        return None
    except Exception as e:
        print(f"  [エラー] {path}: {e}")
        return None

def flatten(d, prefix="", sep="_"):
    """ネストされたdictをフラット化する"""
    items = {}
    if isinstance(d, dict):
        for k, v in d.items():
            key = f"{prefix}{sep}{k}" if prefix else k
            if isinstance(v, dict):
                items.update(flatten(v, key, sep))
            elif isinstance(v, list):
                # リストは JSON文字列として格納
                items[key] = json.dumps(v, ensure_ascii=False) if v else ""
            else:
                items[key] = v if v is not None else ""
    return items

def write_csv(filename, rows, fieldnames=None):
    if not rows:
        print(f"  [スキップ] {filename}: データなし")
        return
    path = os.path.join(OUTPUT_DIR, filename)
    if fieldnames is None:
        # 全行のキーを集めてフィールド名を動的生成
        all_keys = []
        seen = set()
        for row in rows:
            for k in row.keys():
                if k not in seen:
                    all_keys.append(k)
                    seen.add(k)
        fieldnames = all_keys
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    print(f"  [保存] {path} ({len(rows)}件, {len(fieldnames)}フィールド)")

# ==========================================
# メイン処理
# ==========================================
os.makedirs(OUTPUT_DIR, exist_ok=True)

oldest = (datetime.now() - timedelta(days=DAYS)).strftime("%Y-%m-%d")
newest = datetime.now().strftime("%Y-%m-%d")

print("=" * 55)
print("  Intervals.icu 全データ取得スクリプト")
print("=" * 55)
print(f"  アスリートID : {ATHLETE_ID}")
print(f"  期間         : {oldest} 〜 {newest}")
print(f"  出力先       : {OUTPUT_DIR}/")
print("=" * 55)

# ------------------------------------------
# 1. アスリートプロフィール
# ------------------------------------------
print("\n[1/5] アスリートプロフィール取得...")
athlete = api_get(f"/athlete/{ATHLETE_ID}")
if athlete:
    path = os.path.join(OUTPUT_DIR, "athlete.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(athlete, f, ensure_ascii=False, indent=2)
    print(f"  [保存] {path}")

# ------------------------------------------
# 2. アクティビティ一覧（全フィールド）
# ------------------------------------------
print("\n[2/5] アクティビティ一覧取得...")
activities = api_get(f"/athlete/{ATHLETE_ID}/activities", {
    "oldest": oldest,
    "newest": newest,
    "limit": 10000,
})
if not activities:
    activities = []
print(f"  {len(activities)} 件取得")

act_rows = []
for a in activities:
    act_rows.append(flatten(a))
write_csv("activities.csv", act_rows)

# ------------------------------------------
# 3. アクティビティ詳細 + 区間データ
# ------------------------------------------
print(f"\n[3/5] アクティビティ詳細取得（最大{min(len(activities), DETAIL_LIMIT)}件）...")
print("  ※件数が多い場合は時間がかかります。Ctrl+Cで中断可能です。")

detail_rows = []
interval_rows = []
total = min(len(activities), DETAIL_LIMIT)

for i, act in enumerate(activities[:DETAIL_LIMIT]):
    act_id = act.get("id")
    if not act_id:
        continue

    if (i + 1) % 10 == 0 or i == 0:
        print(f"  進捗: {i+1}/{total}件", end="\r")

    detail = api_get(f"/activity/{act_id}", {"intervals": "true"})
    time.sleep(REQUEST_DELAY)

    if not detail:
        continue

    # 区間データを別ファイルに分離
    icu_intervals = detail.pop("icu_intervals", []) or []
    streams = detail.pop("streams", None)  # ストリームデータは別途大きすぎるため除外

    # 詳細フラット化
    row = flatten(detail)
    detail_rows.append(row)

    # 区間データ
    for iv in icu_intervals:
        iv_row = {"activity_id": act_id, "activity_date": act.get("start_date_local", "")}
        iv_row.update(flatten(iv))
        interval_rows.append(iv_row)

print(f"\n  詳細取得完了: {len(detail_rows)}件, 区間データ: {len(interval_rows)}件")
write_csv("activities_detail.csv", detail_rows)
write_csv("intervals.csv", interval_rows)

# ------------------------------------------
# 4. ウェルネスデータ
# ------------------------------------------
print("\n[4/5] ウェルネスデータ取得...")
wellness = api_get(f"/athlete/{ATHLETE_ID}/wellness", {
    "oldest": oldest,
    "newest": newest,
})
if not wellness:
    wellness = []
print(f"  {len(wellness)} 件取得")

wellness_rows = [flatten(w) for w in wellness]
write_csv("wellness.csv", wellness_rows)

# ------------------------------------------
# 5. カレンダー・イベント（計画ワークアウト）
# ------------------------------------------
print("\n[5/5] カレンダーイベント取得...")
events = api_get(f"/athlete/{ATHLETE_ID}/events", {
    "oldest": oldest,
    "newest": newest,
})
if not events:
    events = []
print(f"  {len(events)} 件取得")

event_rows = []
for e in events:
    # workout_docはネストが深いため除外
    e.pop("workout_doc", None)
    event_rows.append(flatten(e))
write_csv("events.csv", event_rows)

# ------------------------------------------
# 完了サマリー
# ------------------------------------------
print("\n" + "=" * 55)
print("  取得完了！")
print("=" * 55)
files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith((".csv", ".json"))]
total_size = sum(os.path.getsize(os.path.join(OUTPUT_DIR, f)) for f in files)
for f in sorted(files):
    size = os.path.getsize(os.path.join(OUTPUT_DIR, f))
    print(f"  {f:<35} {size/1024:>7.1f} KB")
print(f"  {'合計':<35} {total_size/1024:>7.1f} KB")
print("=" * 55)
print(f"\n  出力フォルダ: {os.path.abspath(OUTPUT_DIR)}")
print("  このフォルダごとClaudeにアップロードして分析してもらってください！")
print("  （各CSVファイルを個別にアップロードしてもOKです）")
