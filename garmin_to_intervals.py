"""
Garmin Connect ワークアウト取得 → Intervals.icu スケジュール登録
================================================================
【仕組み】
  Garmin Connect（ワークアウトライブラリ）
      ↓ python-garminconnect でログイン取得
  ワークアウト一覧を種目・強度で自動分類
      ↓ HRV/CTL/ATLでコンディション判定
  Intervals.icu カレンダーにPOST
      ↓ 自動同期
  Garmin デバイスに反映

【セットアップ】
  pip install garminconnect

【使い方】
  python garmin_to_intervals.py --list              # ワークアウト一覧確認
  python garmin_to_intervals.py --preview           # スケジュール確認（アップロードなし）
  python garmin_to_intervals.py                     # 来週分をアップロード
  python garmin_to_intervals.py --days 14           # 2週間分
"""

import json, base64, argparse
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta
from collections import defaultdict
from getpass import getpass
from pathlib import Path

# ============================================================
# 設定
# ============================================================
ATHLETE_ID = "i275804"
API_KEY    = "4bhzoa2udw4gi80pcie1eijuj"
BASE_URL   = "https://intervals.icu/api/v1"
AUTH       = base64.b64encode(f"API_KEY:{API_KEY}".encode()).decode()
ICU_HEADERS = {"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"}

# あなたの閾値（実データ算出済み）
THRESHOLD_PACE_SEC = 288   # 4:48/km
FTP_WATTS          = 223
WEIGHT_KG          = 68.4

# ============================================================
# Garmin Connect ログイン & ワークアウト取得
# ============================================================
def get_garmin_workouts() -> list:
    """
    garth のトークンを使ってワークアウトライブラリを取得。
    事前に garmin_check.py を実行してトークンを保存しておくこと。
    """
    try:
        import garth
    except ImportError:
        print("\n  ❌ garth がインストールされていません")
        print("  pip install garth  を実行してください\n")
        return []

    TOKEN_DIR = Path.home() / ".garth"

    # --- トークンをロード ---
    if not TOKEN_DIR.exists():
        print(f"  ❌ トークンが見つかりません: {TOKEN_DIR}")
        print("  garmin_check.py を先に実行してください")
        return []

    try:
        garth.resume(str(TOKEN_DIR))
        print("  ✅ トークン読み込み成功")
    except Exception as e:
        print(f"  ❌ トークン読み込み失敗: {e}")
        print("  garmin_check.py を再実行してください")
        return []

    # --- ワークアウト取得 ---
    print("  ワークアウトライブラリを取得中...")
    try:
        workouts = []
        start = 0
        limit = 100
        while True:
            resp = garth.connectapi(
                "/workout-service/workouts",
                params={"start": start, "limit": limit, "myWorkoutsOnly": True}
            )
            batch = resp if isinstance(resp, list) else resp.get("workouts", [])
            if not batch:
                break
            workouts.extend(batch)
            if len(batch) < limit:
                break
            start += limit
        print(f"  ✅ {len(workouts)}件取得")
        return workouts
    except Exception as e:
        print(f"  ❌ 取得失敗: {e}")
        print("  トークンが期限切れの可能性があります。garmin_check.py を再実行してください")
        return []


def parse_garmin_workout(w: dict) -> dict:
    """Garminワークアウトを分類・整理"""
    name      = (w.get("workoutName") or "").strip()
    sport_key = (w.get("sportType", {}) or {}).get("sportTypeKey", "") or ""
    workout_id = w.get("workoutId") or w.get("workoutUuid", "")

    # 推定時間（ステップから合算）
    total_sec = 0
    steps = w.get("workoutSegments", [{}])[0].get("workoutSteps", []) if w.get("workoutSegments") else []
    for step in steps:
        end_cond = step.get("endCondition", {}) or {}
        if end_cond.get("conditionTypeKey") == "time":
            total_sec += float(end_cond.get("conditionValue") or 0)
        elif step.get("type") == "RepeatGroupDTO":
            iters = step.get("numberOfIterations", 1)
            for sub in step.get("workoutSteps", []):
                ec = sub.get("endCondition", {}) or {}
                if ec.get("conditionTypeKey") == "time":
                    total_sec += float(ec.get("conditionValue") or 0) * iters

    # 種目マッピング
    sport_map = {
        "running":      "run",
        "cycling":      "bike",
        "swimming":     "swim",
        "strength_training": "strength",
        "yoga":         "yoga",
        "pilates":      "pilates",
        "hiit":         "strength",
        "cardio":       "strength",
    }
    sport = sport_map.get(sport_key.lower(), "other")

    # 名前から補完
    name_lower = name.lower()
    if sport == "other":
        if any(k in name_lower for k in ["run","ラン","走"]):           sport = "run"
        elif any(k in name_lower for k in ["ride","bike","バイク","ライド"]): sport = "bike"
        elif any(k in name_lower for k in ["swim","スイム","水泳"]):    sport = "swim"
        elif any(k in name_lower for k in ["hiit","筋","strength","プッシュ","スクワット","プランク","体幹"]):
            sport = "strength"
        elif any(k in name_lower for k in ["yoga","ヨガ"]):             sport = "yoga"
        elif any(k in name_lower for k in ["pilates","ピラティス"]):    sport = "pilates"

    # 強度・目的判定
    intensity, purpose = classify_intensity_purpose(name_lower, total_sec)

    return {
        "id":           workout_id,
        "name":         name,
        "sport_key":    sport_key,
        "sport":        sport,
        "intensity":    intensity,
        "purpose":      purpose,
        "duration_min": round(total_sec / 60, 1),
        "raw":          w,  # 元データ（アップロード時に使用）
    }


def classify_intensity_purpose(text: str, dur_sec: float):
    """テキストと時間から強度・目的を推定"""
    intensity = "easy"
    purpose   = "general"

    # 目的
    if any(k in text for k in ["interval","インターバル","vo2","rep "]):
        purpose = "vo2max";   intensity = "hard"
    elif any(k in text for k in ["tempo","テンポ","threshold","閾値","sweet spot","スイートスポット"]):
        purpose = "threshold"; intensity = "moderate"
    elif any(k in text for k in ["long","ロング","lsd"]):
        purpose = "endurance"; intensity = "moderate"
    elif any(k in text for k in ["recovery","リカバリー","easy","イージー","active"]):
        purpose = "recovery";  intensity = "recovery"
    elif any(k in text for k in ["hiit","タバタ","tabata","circuit","サーキット"]):
        purpose = "hiit";      intensity = "hard"
    elif any(k in text for k in ["strength","筋","core","体幹","プランク","スクワット","push","プッシュ"]):
        purpose = "strength";  intensity = "moderate"
    elif any(k in text for k in ["brick","ブリック"]):
        purpose = "brick";     intensity = "moderate"
    elif any(k in text for k in ["race","レース","comp"]):
        purpose = "race_prep"; intensity = "hard"

    return intensity, purpose


# ============================================================
# Intervals.icu API
# ============================================================
def icu_get(path, params=None):
    url = f"{BASE_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=ICU_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [ICU GET失敗] {path}: {e}")
        return None

def icu_post(path, body):
    url  = f"{BASE_URL}{path}"
    data = json.dumps(body, ensure_ascii=False).encode()
    req  = urllib.request.Request(url, data=data, headers=ICU_HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  [ICU POST失敗] HTTP {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"  [ICU POST失敗] {e}")
        return None


def get_condition():
    """Intervals.icuから最新コンディションを取得"""
    today    = datetime.now().strftime("%Y-%m-%d")
    week_ago = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    wellness = icu_get(f"/athlete/{ATHLETE_ID}/wellness", {"oldest": week_ago, "newest": today})

    ctl, atl, form, hrv = 70.5, 97.1, -26.6, 86.0
    if wellness:
        latest = sorted(wellness, key=lambda x: x.get("id", ""))[-1]
        ctl  = float(latest.get("ctl")  or ctl)
        atl  = float(latest.get("atl")  or atl)
        hrv  = float(latest.get("hrv")  or hrv)
        form = ctl - atl

    # スコア計算
    form_score = max(0, min(10, (form + 30) / 5))
    hrv_score  = max(0, min(10, (hrv - 50) / 7))
    score      = form_score * 0.6 + hrv_score * 0.4

    if   score >= 7:   cond = "peak"
    elif score >= 5:   cond = "good"
    elif score >= 3:   cond = "normal"
    elif score >= 1.5: cond = "fatigued"
    else:              cond = "depleted"

    # HRV単独でも判定（急低下時）
    if hrv < 70 and cond not in ("fatigued", "depleted"):
        cond = "fatigued"
        print(f"  ⚠️  HRV={hrv:.0f} < 70 → 強度を自動軽減")

    return {"ctl": ctl, "atl": atl, "form": form, "hrv": hrv,
            "score": score, "condition": cond}


# ============================================================
# スケジューリング
# ============================================================
WEEKLY_TEMPLATES = {
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

INTENSITY_ORDER = ["recovery", "easy", "moderate", "hard", "very_hard"]
SPORT_EMOJI = {
    "run": "🏃", "bike": "🚴", "swim": "🏊",
    "strength": "💪", "yoga": "🧘", "pilates": "🤸", "other": "🏋️"
}


def build_library(parsed_workouts: list) -> dict:
    lib = defaultdict(lambda: defaultdict(list))
    for w in parsed_workouts:
        lib[w["sport"]][w["purpose"]].append(w)
        lib[w["sport"]]["all"].append(w)
    return lib


def pick_best(lib, sport, purpose, intensity_pref, used_ids):
    candidates = lib.get(sport, {}).get(purpose, [])
    if not candidates:
        candidates = lib.get(sport, {}).get("general", [])
    if not candidates:
        candidates = lib.get(sport, {}).get("all", [])
    candidates = [c for c in candidates if c["id"] not in used_ids]
    if not candidates:
        return None

    pref_idx = INTENSITY_ORDER.index(intensity_pref) if intensity_pref in INTENSITY_ORDER else 2
    scored = sorted(
        candidates,
        key=lambda c: abs(INTENSITY_ORDER.index(c["intensity"]) - pref_idx)
        if c["intensity"] in INTENSITY_ORDER else 99
    )
    return scored[0]


def generate_schedule(lib, cond_info, start_date, days):
    template = WEEKLY_TEMPLATES.get(cond_info["condition"], WEEKLY_TEMPLATES["normal"])
    used_ids = set()
    schedule = []

    for offset, sport, purpose, intensity in template[:days]:
        date = (start_date + timedelta(days=offset)).strftime("%Y-%m-%d")
        w    = pick_best(lib, sport, purpose, intensity, used_ids)
        if w:
            used_ids.add(w["id"])
        schedule.append({
            "date": date, "sport": sport,
            "purpose": purpose, "intensity": intensity,
            "workout": w,
        })
    return schedule


# ============================================================
# Intervals.icu にPOST（Garminワークアウト形式でdescriptionに埋め込み）
# ============================================================
def upload_to_icu(schedule, dry_run=False):
    success = 0
    for item in schedule:
        date    = item["date"]
        w       = item["workout"]
        sport   = item["sport"]
        emoji   = SPORT_EMOJI.get(sport, "🏋️")
        cond_label = f"{item['purpose']} / {item['intensity']}"

        if w is None:
            print(f"\n  {date} {emoji} — 該当ワークアウトなし（スキップ）")
            print(f"    ※ Garmin Connectに {sport}/{item['purpose']} のワークアウトを登録してください")
            continue

        # Garminワークアウトの種目キーをICU typeに変換
        sport_to_icu_type = {
            "run": "Run", "bike": "Ride", "swim": "Swim",
            "strength": "WeightTraining", "yoga": "Yoga",
            "pilates": "Pilates", "other": "Workout",
        }
        icu_type = sport_to_icu_type.get(sport, "Workout")

        payload = {
            "start_date_local": f"{date}T00:00:00",
            "category":         "WORKOUT",
            "type":             icu_type,
            "name":             w["name"],
            "description":      f"[Garmin workout ID: {w['id']}]\n{cond_label}",
            "moving_time":      int((w["duration_min"] or 30) * 60),
        }

        tag = "[DRY RUN] " if dry_run else ""
        print(f"\n  {tag}{date} {emoji} {w['name']}")
        print(f"    ⏱  {w['duration_min']:.0f}分  🎯 {cond_label}")
        print(f"    🔗 Garmin ID: {w['id']}")

        if dry_run:
            success += 1
            continue

        result = icu_post(f"/athlete/{ATHLETE_ID}/events", payload)
        if result:
            print(f"    ✅ Intervals.icu登録成功 (event: {result.get('id')})")
            success += 1
        else:
            print(f"    ❌ 登録失敗")

    return success


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list",    action="store_true", help="ワークアウト一覧を表示")
    parser.add_argument("--preview", action="store_true", help="スケジュール確認のみ")
    parser.add_argument("--days",    type=int, default=7)
    parser.add_argument("--start",   type=str, default=None)
    parser.add_argument("--email",   type=str, default=None, help="Garmin Connect メールアドレス")
    args = parser.parse_args()

    print("=" * 60)
    print("  Garmin Connect → Intervals.icu スケジューラー")
    print("=" * 60)

    # Step 1: Garminからワークアウト取得
    print("\n[1/3] Garmin Connect からワークアウト取得...")
    print("  ※ 初回は garmin_login.py を先に実行してください")
    raw_workouts = get_garmin_workouts()
    if not raw_workouts:
        return

    # パース・分類
    parsed = [parse_garmin_workout(w) for w in raw_workouts]

    if args.list:
        print(f"\n{'─'*60}")
        print(f"  登録済みワークアウト ({len(parsed)}件)")
        print(f"{'─'*60}")
        by_sport = defaultdict(list)
        for p in parsed:
            by_sport[p["sport"]].append(p)
        for sport, items in sorted(by_sport.items()):
            print(f"\n  {SPORT_EMOJI.get(sport,'?')} {sport.upper()} ({len(items)}件)")
            for item in items:
                print(f"    [{item['intensity']:10s}|{item['purpose']:12s}] "
                      f"{item['name']:<40} {item['duration_min']:.0f}min")
        return

    lib = build_library(parsed)
    sport_counts = {s: len(lib[s]["all"]) for s in lib}
    print(f"  種目別: { {SPORT_EMOJI.get(k,k)+k: v for k,v in sport_counts.items()} }")

    # Step 2: コンディション確認
    print("\n[2/3] Intervals.icu からコンディション取得...")
    cond = get_condition()
    cond_icon = {"peak":"🔥","good":"✅","normal":"😐","fatigued":"😓","depleted":"🛑"}
    print(f"  CTL={cond['ctl']:.1f}  ATL={cond['atl']:.1f}  "
          f"Form={cond['form']:.1f}  HRV={cond['hrv']:.0f}")
    print(f"  コンディション: {cond_icon.get(cond['condition'],'?')} "
          f"{cond['condition'].upper()} (スコア {cond['score']:.1f}/10)")

    # Step 3: スケジュール生成＆アップロード
    print(f"\n[3/3] スケジュール生成...")
    start_date = datetime.now() + timedelta(days=1)
    if args.start:
        start_date = datetime.fromisoformat(args.start)

    schedule = generate_schedule(lib, cond, start_date, args.days)

    print(f"\n{'─'*60}")
    mode = "プレビュー" if args.preview else "アップロード"
    print(f"  {mode} — {start_date.strftime('%Y-%m-%d')}から{args.days}日間")
    print(f"{'─'*60}")

    n = upload_to_icu(schedule, dry_run=args.preview)

    print(f"\n{'='*60}")
    if args.preview:
        print(f"  プレビュー完了 ({n}/{len(schedule)}件)")
        print(f"  アップロードするには --preview を外して実行してください")
    else:
        print(f"  完了！ {n}/{len(schedule)}件 Intervals.icuに登録")
        print(f"  ✅ Intervals.icuのカレンダーを確認してください")
        print(f"  ✅ GarminをConnectに同期すると反映されます")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
