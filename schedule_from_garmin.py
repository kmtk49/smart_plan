"""
Garmin登録済みワークアウト取得 → スマートスケジュール生成 & アップロード
=========================================================================
Step 1: Intervals.icuのワークアウトライブラリ（Garmin同期済み）を取得
Step 2: HRV/CTL/ATLを見てその週に最適なワークアウトを自動選択
Step 3: Intervals.icuカレンダーにPOST → Garminに自動同期

使い方:
  python schedule_from_garmin.py --list              # 登録済みワークアウト一覧表示
  python schedule_from_garmin.py --preview           # スケジュール確認（アップロードなし）
  python schedule_from_garmin.py                     # 来週分をアップロード
  python schedule_from_garmin.py --days 14           # 2週間分
  python schedule_from_garmin.py --start 2026-04-01  # 開始日指定
"""

import urllib.request, urllib.parse, urllib.error
import json, base64, argparse
from datetime import datetime, timedelta
from collections import defaultdict

# ============================================================
# 設定
# ============================================================
ATHLETE_ID = "i275804"
API_KEY    = "4bhzoa2udw4gi80pcie1eijuj"
BASE_URL   = "https://intervals.icu/api/v1"
AUTH       = base64.b64encode(f"API_KEY:{API_KEY}".encode()).decode()
HEADERS    = {"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"}

# ============================================================
# API
# ============================================================
def api_get(path, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  [HTTP {e.code}] {path}")
        return None
    except Exception as e:
        print(f"  [エラー] {path}: {e}")
        return None

def api_post(path, body):
    url = f"{BASE_URL}{path}"
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  [HTTP {e.code}] {path}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"  [エラー] {path}: {e}")
        return None

# ============================================================
# ワークアウト取得・分類
# ============================================================
def fetch_all_workouts():
    """Intervals.icuのワークアウトライブラリを全取得"""
    print("  ワークアウトライブラリを取得中...")
    workouts = api_get(f"/athlete/{ATHLETE_ID}/workouts")
    if not workouts:
        print("  ❌ 取得失敗")
        return []
    print(f"  ✅ {len(workouts)}件取得")
    return workouts

def classify_workout(w):
    """ワークアウトをタイプ・強度・目的で自動分類"""
    name  = (w.get("name") or "").lower()
    desc  = (w.get("description") or "").lower()
    wtype = w.get("type") or ""
    load  = float(w.get("icu_training_load") or 0)
    dur_s = float(w.get("moving_time") or 0)
    dur_m = dur_s / 60

    text = name + " " + desc

    # --- 種目判定 ---
    if wtype in ("Run", "VirtualRun"):
        sport = "run"
    elif wtype in ("Ride", "VirtualRide"):
        sport = "bike"
    elif wtype == "Swim":
        sport = "swim"
    elif wtype in ("Yoga",):
        sport = "yoga"
    elif wtype in ("Pilates",):
        sport = "pilates"
    elif wtype in ("HighIntensityIntervalTraining", "WeightTraining"):
        sport = "strength"
    else:
        # 名前から推測
        if any(k in text for k in ["run","ラン","走"]):
            sport = "run"
        elif any(k in text for k in ["ride","bike","バイク","ライド","サイクル"]):
            sport = "bike"
        elif any(k in text for k in ["swim","スイム","水泳"]):
            sport = "swim"
        elif any(k in text for k in ["hiit","筋","strength","プッシュ","スクワット","プランク","体幹","core"]):
            sport = "strength"
        elif any(k in text for k in ["yoga","ヨガ"]):
            sport = "yoga"
        elif any(k in text for k in ["pilates","ピラティス"]):
            sport = "pilates"
        else:
            sport = "other"

    # --- 強度判定 ---
    if load > 0:
        if load < 40:
            intensity = "recovery"
        elif load < 70:
            intensity = "easy"
        elif load < 100:
            intensity = "moderate"
        elif load < 140:
            intensity = "hard"
        else:
            intensity = "very_hard"
    else:
        # テキストから推測
        if any(k in text for k in ["recovery","リカバリー","easy","イージー","z1","zone1"]):
            intensity = "recovery"
        elif any(k in text for k in ["tempo","テンポ","sweet spot","スイートスポット","z3","z4"]):
            intensity = "moderate"
        elif any(k in text for k in ["interval","インターバル","hiit","vo2","z5","z6"]):
            intensity = "hard"
        elif any(k in text for k in ["long","ロング"]):
            intensity = "moderate"
        else:
            intensity = "easy"

    # --- 目的判定 ---
    purpose = "general"
    if any(k in text for k in ["interval","インターバル","vo2","vo₂"]):
        purpose = "vo2max"
    elif any(k in text for k in ["tempo","テンポ","threshold","閾値","sweet spot"]):
        purpose = "threshold"
    elif any(k in text for k in ["long","ロング","lsd"]):
        purpose = "endurance"
    elif any(k in text for k in ["recovery","リカバリー"]):
        purpose = "recovery"
    elif any(k in text for k in ["hiit","タバタ","tabata","circuit","サーキット"]):
        purpose = "hiit"
    elif any(k in text for k in ["strength","筋","core","体幹","プランク","スクワット"]):
        purpose = "strength"
    elif any(k in text for k in ["brick","ブリック","t1","t2","transition"]):
        purpose = "brick"
    elif any(k in text for k in ["race","レース","competition"]):
        purpose = "race_prep"

    return {
        "id":        w.get("id"),
        "name":      w.get("name"),
        "type":      wtype,
        "sport":     sport,
        "intensity": intensity,
        "purpose":   purpose,
        "load":      load,
        "duration_min": round(dur_m, 1),
        "description": w.get("description", ""),
    }

def build_library(workouts):
    """分類済みワークアウトをスポーツ×目的でインデックス化"""
    lib = defaultdict(lambda: defaultdict(list))
    for w in workouts:
        c = classify_workout(w)
        lib[c["sport"]][c["purpose"]].append(c)
        lib[c["sport"]]["all"].append(c)
    return lib

def pick_workout(lib, sport, purpose, intensity_pref, exclude_ids=set()):
    """条件に合うワークアウトを選択（被りなし）"""
    candidates = lib.get(sport, {}).get(purpose, [])
    if not candidates:
        candidates = lib.get(sport, {}).get("general", [])
    if not candidates:
        candidates = lib.get(sport, {}).get("all", [])

    # 使用済み除外
    candidates = [c for c in candidates if c["id"] not in exclude_ids]
    if not candidates:
        return None

    # 強度フィルタ（ゆるめに）
    intensity_order = ["recovery", "easy", "moderate", "hard", "very_hard"]
    pref_idx = intensity_order.index(intensity_pref) if intensity_pref in intensity_order else 2
    scored = []
    for c in candidates:
        c_idx = intensity_order.index(c["intensity"]) if c["intensity"] in intensity_order else 2
        diff = abs(c_idx - pref_idx)
        scored.append((diff, c["load"], c))

    scored.sort(key=lambda x: (x[0], -x[1]))
    return scored[0][2] if scored else None

# ============================================================
# コンディション取得
# ============================================================
def get_current_condition():
    """最新のHRV/CTL/ATLを取得してコンディション判定"""
    today     = datetime.now().strftime("%Y-%m-%d")
    week_ago  = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    wellness  = api_get(f"/athlete/{ATHLETE_ID}/wellness", {"oldest": week_ago, "newest": today})

    ctl, atl, form, hrv = 70.5, 97.1, -26.6, 86.0  # デフォルト

    if wellness:
        latest = sorted(wellness, key=lambda x: x.get("id", ""))[-1]
        ctl  = float(latest.get("ctl")  or ctl)
        atl  = float(latest.get("atl")  or atl)
        hrv  = float(latest.get("hrv")  or hrv)
        form = ctl - atl

    # コンディションスコア（0=最悪 〜 10=最高）
    form_score = max(0, min(10, (form + 30) / 5))  # -30〜+20 → 0〜10
    hrv_score  = max(0, min(10, (hrv - 50) / 7))   # 50〜120 → 0〜10
    score      = (form_score * 0.6 + hrv_score * 0.4)

    if score >= 7:
        condition = "peak"       # 絶好調
    elif score >= 5:
        condition = "good"       # 良好
    elif score >= 3:
        condition = "normal"     # 通常
    elif score >= 1.5:
        condition = "fatigued"   # 疲労気味
    else:
        condition = "depleted"   # 要休養

    return {
        "ctl": ctl, "atl": atl, "form": form, "hrv": hrv,
        "score": score, "condition": condition
    }

# ============================================================
# 週間スケジュール生成
# ============================================================
WEEKLY_TEMPLATES = {
    # condition → [(曜日offset, sport, purpose, intensity)]
    "peak": [
        (0, "run",      "threshold",  "hard"),
        (1, "bike",     "vo2max",     "hard"),
        (2, "strength", "hiit",       "moderate"),
        (3, "run",      "endurance",  "moderate"),
        (4, "bike",     "threshold",  "hard"),
        (5, "run",      "vo2max",     "hard"),
        (6, "bike",     "endurance",  "moderate"),
    ],
    "good": [
        (0, "run",      "endurance",  "moderate"),
        (1, "bike",     "threshold",  "moderate"),
        (2, "strength", "strength",   "moderate"),
        (3, "run",      "threshold",  "moderate"),
        (4, "bike",     "endurance",  "easy"),
        (5, "run",      "vo2max",     "moderate"),
        (6, "bike",     "endurance",  "moderate"),
    ],
    "normal": [
        (0, "run",      "endurance",  "easy"),
        (1, "bike",     "endurance",  "moderate"),
        (2, "strength", "strength",   "easy"),
        (3, "run",      "threshold",  "moderate"),
        (4, "yoga",     "recovery",   "recovery"),
        (5, "bike",     "threshold",  "moderate"),
        (6, "run",      "endurance",  "moderate"),
    ],
    "fatigued": [
        (0, "run",      "recovery",   "recovery"),
        (1, "bike",     "endurance",  "easy"),
        (2, "yoga",     "recovery",   "recovery"),
        (3, "run",      "endurance",  "easy"),
        (4, "strength", "strength",   "recovery"),
        (5, "bike",     "endurance",  "easy"),
        (6, "run",      "endurance",  "easy"),
    ],
    "depleted": [
        (0, "yoga",     "recovery",   "recovery"),
        (1, "run",      "recovery",   "recovery"),
        (2, "yoga",     "recovery",   "recovery"),
        (3, "bike",     "recovery",   "recovery"),
        (4, "yoga",     "recovery",   "recovery"),
        (5, "run",      "endurance",  "easy"),
        (6, "yoga",     "recovery",   "recovery"),
    ],
}

SPORT_LABEL = {
    "run": "🏃 Run", "bike": "🚴 Ride", "swim": "🏊 Swim",
    "strength": "💪 Strength", "yoga": "🧘 Yoga",
    "pilates": "🤸 Pilates", "other": "🏋️ Other",
}

def generate_schedule(lib, condition_info, start_date, days=7):
    cond      = condition_info["condition"]
    template  = WEEKLY_TEMPLATES.get(cond, WEEKLY_TEMPLATES["normal"])
    used_ids  = set()
    schedule  = []

    for offset, sport, purpose, intensity in template[:days]:
        date = (start_date + timedelta(days=offset)).strftime("%Y-%m-%d")
        w    = pick_workout(lib, sport, purpose, intensity, exclude_ids=used_ids)

        if w:
            used_ids.add(w["id"])
            schedule.append({
                "date":     date,
                "sport":    sport,
                "workout":  w,
                "purpose":  purpose,
                "intensity": intensity,
            })
        else:
            # 該当なし → スキップまたは休養
            schedule.append({
                "date":    date,
                "sport":   sport,
                "workout": None,
                "purpose": purpose,
                "intensity": intensity,
            })

    return schedule

# ============================================================
# アップロード
# ============================================================
def upload_schedule(schedule, dry_run=False):
    success = 0
    for item in schedule:
        date    = item["date"]
        w       = item["workout"]
        sport   = item["sport"]
        purpose = item["purpose"]

        sport_label = SPORT_LABEL.get(sport, sport)

        if w is None:
            print(f"\n  {date} {sport_label} — 該当ワークアウトなし（スキップ）")
            continue

        # Intervals.icu API: events に workout_id で登録
        payload = {
            "start_date_local": f"{date}T00:00:00",
            "category":   "WORKOUT",
            "type":       w["type"] or sport.capitalize(),
            "name":       w["name"],
            "workout_id": w["id"],   # ← ライブラリのIDを紐付け
            "description": w["description"],
            "moving_time": int((w["duration_min"] or 30) * 60),
            "icu_training_load": int(w["load"]) if w["load"] else None,
        }
        # None フィールドを除去
        payload = {k: v for k, v in payload.items() if v is not None}

        tag = "[DRY RUN] " if dry_run else ""
        print(f"\n  {tag}{date} {sport_label}")
        print(f"    📋 {w['name']}")
        print(f"    ⏱  {w['duration_min']}分  💥 Load={w['load']:.0f}  🎯 {purpose}/{item['intensity']}")
        if w["description"]:
            # 最初の2行だけ表示
            for line in w["description"].split("\n")[:2]:
                if line.strip():
                    print(f"    {line.strip()}")

        if dry_run:
            success += 1
            continue

        result = api_post(f"/athlete/{ATHLETE_ID}/events", payload)
        if result:
            print(f"    ✅ アップロード成功 (event id: {result.get('id')})")
            success += 1
        else:
            print(f"    ❌ 失敗")

    return success

# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="Garminワークアウトからスマートスケジュール生成")
    parser.add_argument("--list",    action="store_true", help="登録済みワークアウト一覧を表示")
    parser.add_argument("--preview", action="store_true", help="スケジュール確認（アップロードなし）")
    parser.add_argument("--days",    type=int, default=7, help="生成日数（デフォルト7）")
    parser.add_argument("--start",   type=str, default=None, help="開始日 YYYY-MM-DD")
    args = parser.parse_args()

    print("=" * 60)
    print("  Garminワークアウト → スマートスケジュール")
    print("=" * 60)

    # Step 1: ワークアウト取得
    print("\n[1/3] ワークアウトライブラリを取得...")
    workouts = fetch_all_workouts()
    if not workouts:
        print("  ワークアウトが見つかりません。")
        print("  Garmin ConnectのワークアウトがIntervals.icuに同期されているか確認してください。")
        return

    if args.list:
        # 一覧表示
        print(f"\n{'─'*60}")
        print(f"  登録済みワークアウト ({len(workouts)}件)")
        print(f"{'─'*60}")
        by_type = defaultdict(list)
        for w in workouts:
            c = classify_workout(w)
            by_type[c["sport"]].append(c)
        for sport, items in sorted(by_type.items()):
            print(f"\n  {SPORT_LABEL.get(sport, sport)} ({len(items)}件)")
            for item in sorted(items, key=lambda x: x["load"] or 0, reverse=True):
                print(f"    [{item['intensity']:10s}|{item['purpose']:12s}] "
                      f"{item['name']:<35} "
                      f"Load={item['load']:5.0f} {item['duration_min']:.0f}min")
        return

    lib = build_library(workouts)
    sport_counts = {s: len(lib[s]["all"]) for s in lib}
    print(f"  スポーツ別: { {SPORT_LABEL.get(k,k): v for k,v in sport_counts.items()} }")

    # Step 2: コンディション取得
    print("\n[2/3] 現在のコンディションを確認...")
    cond = get_current_condition()
    cond_emoji = {"peak":"🔥","good":"✅","normal":"😐","fatigued":"😓","depleted":"🛑"}
    print(f"  CTL={cond['ctl']:.1f}  ATL={cond['atl']:.1f}  "
          f"Form={cond['form']:.1f}  HRV={cond['hrv']:.0f}")
    print(f"  コンディション: {cond_emoji.get(cond['condition'],'?')} "
          f"{cond['condition'].upper()} (スコア {cond['score']:.1f}/10)")

    # Step 3: スケジュール生成
    print(f"\n[3/3] スケジュール生成...")
    start_date = datetime.now() + timedelta(days=1)
    if args.start:
        start_date = datetime.fromisoformat(args.start)

    schedule = generate_schedule(lib, cond, start_date, days=args.days)

    print(f"\n{'─'*60}")
    mode = "プレビュー" if args.preview else "アップロード"
    print(f"  {mode} — {start_date.strftime('%Y-%m-%d')}から{args.days}日間")
    print(f"{'─'*60}")

    n = upload_schedule(schedule, dry_run=args.preview)

    print(f"\n{'='*60}")
    if args.preview:
        print(f"  プレビュー完了（{n}/{len(schedule)}件）")
        print(f"  実際にアップロード: python {__file__.split('/')[-1]}")
    else:
        print(f"  完了！ {n}/{len(schedule)}件アップロード成功")
        print(f"  ✅ Intervals.icuカレンダーを確認してください")
        print(f"  ✅ GarminデバイスをConnectに同期すると反映されます")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
