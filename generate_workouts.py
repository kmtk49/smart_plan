"""
Intervals.icu トレーニングメニュー生成 & アップロード
=====================================================
・ランニング → ペース指定（閾値ペース4:48/km基準）
・バイク     → パワー指定（FTP 223W基準）
・HRV/CTL/ATLに基づいて強度を自動調整
・Intervals.icuカレンダーにPOSTしてGarminに自動同期

使い方:
  python generate_workouts.py             # 来週分を生成してアップロード
  python generate_workouts.py --preview   # アップロードせずに内容確認のみ
  python generate_workouts.py --days 14   # 2週間分生成
"""

import urllib.request, urllib.parse, urllib.error
import json, base64, argparse
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Optional

# ============================================================
# 設定
# ============================================================
ATHLETE_ID   = "i275804"
API_KEY      = "4bhzoa2udw4gi80pcie1eijuj"
BASE_URL     = "https://intervals.icu/api/v1"

# ---- あなたの閾値（実データから算出）----
THRESHOLD_PACE_SEC = 4 * 60 + 48   # 4:48/km = 288秒/km（m/s: 1000/288）
FTP_WATTS          = 223            # rolling FTP
WEIGHT_KG          = 68.4

# ---- ランニング ペースゾーン（秒/km）----
TP = THRESHOLD_PACE_SEC
RUN_ZONES = {
    "Z1": (int(TP * 1.30), int(TP * 1.50)),  # リカバリー   6:14 - 7:12
    "Z2": (int(TP * 1.15), int(TP * 1.29)),  # 有酸素      5:32 - 6:13
    "Z3": (int(TP * 1.05), int(TP * 1.14)),  # テンポ      5:02 - 5:29
    "Z4": (int(TP * 0.97), int(TP * 1.04)),  # 閾値        4:40 - 5:00
    "Z5": (int(TP * 0.88), int(TP * 0.96)),  # VO2max      4:14 - 4:36
}

# ---- バイク パワーゾーン（%FTP）----
BIKE_ZONES = {
    "Z1": (0.45, 0.55),    # アクティブリカバリー
    "Z2": (0.56, 0.75),    # 有酸素
    "Z3": (0.76, 0.90),    # テンポ
    "Z4": (0.91, 1.05),    # 閾値
    "Z5": (1.06, 1.20),    # VO2max
    "Z6": (1.21, 1.50),    # 無酸素
}

AUTH = base64.b64encode(f"API_KEY:{API_KEY}".encode()).decode()
HEADERS = {"Authorization": f"Basic {AUTH}", "Content-Type": "application/json"}

# ============================================================
# ユーティリティ
# ============================================================
def pace_fmt(sec_per_km: int) -> str:
    m, s = divmod(sec_per_km, 60)
    return f"{m}:{s:02d}"

def pace_to_ms(sec_per_km: int) -> float:
    return 1000 / sec_per_km

def api_get(path):
    url = f"{BASE_URL}{path}"
    req = urllib.request.Request(url, headers={**HEADERS, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [GET失敗] {path}: {e}")
        return None

def api_post(path, body):
    url = f"{BASE_URL}{path}"
    data = json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  [POST失敗] {path}: HTTP {e.code} {e.read().decode()[:200]}")
        return None
    except Exception as e:
        print(f"  [POST失敗] {path}: {e}")
        return None

# ============================================================
# ワークアウト定義
# ============================================================
@dataclass
class Step:
    name: str
    duration_sec: int
    target: str        # ゾーン名 e.g. "Z2" / "Z4"
    sport: str         # "run" or "bike"
    repeat: int = 1

@dataclass
class Workout:
    name: str
    sport_type: str    # "Run" or "Ride"
    description: str
    steps: List[Step]
    date: str          # YYYY-MM-DD
    training_load: Optional[int] = None

# ============================================================
# ICU ワークアウトテキスト生成
# ============================================================
def step_to_icu_text(step: Step) -> str:
    """Intervals.icu ワークアウト記法に変換"""
    dur_min = step.duration_sec / 60
    if dur_min == int(dur_min):
        dur_str = f"{int(dur_min)}m"
    else:
        dur_str = f"{int(step.duration_sec)}s"

    if step.sport == "run":
        lo, hi = RUN_ZONES[step.target]
        lo_ms = pace_to_ms(hi)   # 遅い方がlow speed
        hi_ms = pace_to_ms(lo)   # 速い方がhigh speed
        # ICU形式: pace は m/s で指定
        target_str = f"{lo_ms:.3f}-{hi_ms:.3f}m/s"
        pace_label = f"({pace_fmt(lo)}-{pace_fmt(hi)}/km)"
        return f"- {step.name} {dur_str} {target_str} #{step.target} {pace_label}"
    else:
        lo_pct, hi_pct = BIKE_ZONES[step.target]
        lo_w = int(FTP_WATTS * lo_pct)
        hi_w = int(FTP_WATTS * hi_pct)
        return f"- {step.name} {dur_str} {lo_w}-{hi_w}w #{step.target} ({int(lo_pct*100)}-{int(hi_pct*100)}%FTP)"

def workout_to_icu_description(workout: Workout) -> str:
    lines = []
    i = 0
    steps = workout.steps
    while i < len(steps):
        s = steps[i]
        if s.repeat > 1:
            # リピートブロック
            repeat_steps = []
            j = i
            while j < len(steps) and steps[j].repeat == s.repeat:
                repeat_steps.append(steps[j])
                j += 1
            lines.append(f"{s.repeat}x")
            for rs in repeat_steps:
                lines.append("  " + step_to_icu_text(rs))
            i = j
        else:
            lines.append(step_to_icu_text(s))
            i += 1
    return "\n".join(lines)

# ============================================================
# トレーニングメニュー定義
# ============================================================
def make_workouts(start_date: datetime, days: int, form: float, atl: float) -> List[Workout]:
    """
    form  = CTL - ATL (TSB)
    atl   = 直近疲労
    form < -20  → 負荷軽減
    form -20~0  → 通常
    form > 0    → 積極的
    """
    workouts = []

    # 強度係数
    if form < -20:
        intensity = "low"
    elif form < 0:
        intensity = "normal"
    else:
        intensity = "high"

    print(f"\n  Form={form:.1f} ATL={atl:.1f} → 強度設定: {intensity}")

    # 7日間テンプレート（週のパターン）
    weekly_plan = [
        # (曜日オフセット, 種目, メニュー名, intensity_map)
        (0, "Run",  "Easy Run"),
        (1, "Ride", "Endurance Ride"),
        (2, "Run",  "Tempo Run"),
        (3, "Ride", "Sweet Spot"),
        (4, "Run",  "Interval Run"),
        (5, "Ride", "Long Ride"),
        (6, "Run",  "Long Run"),
    ]

    for offset, sport, menu_name in weekly_plan[:days]:
        date = (start_date + timedelta(days=offset)).strftime("%Y-%m-%d")

        if sport == "Run":
            w = make_run_workout(menu_name, date, intensity)
        else:
            w = make_bike_workout(menu_name, date, intensity)

        workouts.append(w)

    return workouts


def make_run_workout(name: str, date: str, intensity: str) -> Workout:
    if name == "Easy Run":
        dur = 40 if intensity == "low" else 50
        steps = [
            Step("ウォームアップ", 5*60, "Z1", "run"),
            Step("イージーラン", dur*60, "Z2", "run"),
            Step("クールダウン", 5*60, "Z1", "run"),
        ]
        desc = f"リカバリー重視のイージーラン。{pace_fmt(RUN_ZONES['Z2'][0])}〜{pace_fmt(RUN_ZONES['Z2'][1])}/kmで会話できるペースを維持。"
        load = 35 if intensity == "low" else 50

    elif name == "Tempo Run":
        reps = 2 if intensity == "low" else 3
        tempo_min = 8 if intensity == "low" else 10
        steps = [
            Step("ウォームアップ", 10*60, "Z2", "run"),
        ]
        for _ in range(reps):
            steps.append(Step("テンポ", tempo_min*60, "Z4", "run", repeat=reps))
            steps.append(Step("リカバリー", 3*60, "Z1", "run", repeat=reps))
        steps.append(Step("クールダウン", 5*60, "Z1", "run"))
        desc = f"テンポランx{reps}本。{pace_fmt(RUN_ZONES['Z4'][0])}〜{pace_fmt(RUN_ZONES['Z4'][1])}/km（閾値ペース）。"
        load = 60 if intensity == "low" else 85

    elif name == "Interval Run":
        if intensity == "low":
            reps, work_sec, rest_sec = 4, 3*60, 2*60
        elif intensity == "normal":
            reps, work_sec, rest_sec = 5, 4*60, 2*60
        else:
            reps, work_sec, rest_sec = 6, 4*60, 90

        steps = [
            Step("ウォームアップ", 10*60, "Z2", "run"),
        ]
        for _ in range(reps):
            steps.append(Step(f"インターバル", work_sec, "Z5", "run", repeat=reps))
            steps.append(Step("リカバリー", rest_sec, "Z1", "run", repeat=reps))
        steps.append(Step("クールダウン", 5*60, "Z1", "run"))
        desc = f"VO2maxインターバル {reps}x{work_sec//60}分。{pace_fmt(RUN_ZONES['Z5'][0])}〜{pace_fmt(RUN_ZONES['Z5'][1])}/km。"
        load = 70 if intensity == "low" else 95

    elif name == "Long Run":
        dist_target = 25 if intensity == "low" else 30 if intensity == "normal" else 35
        # Z2メインで最後にZ3ビルドアップ
        easy_min = int(dist_target / (1000 / RUN_ZONES["Z2"][1]) * 1000 / 60 * 0.8)
        steps = [
            Step("ウォームアップ", 5*60, "Z1", "run"),
            Step("有酸素ラン", easy_min*60, "Z2", "run"),
            Step("ビルドアップ", 15*60, "Z3", "run"),
            Step("クールダウン", 5*60, "Z1", "run"),
        ]
        desc = f"ロングラン〜{dist_target}km想定。Z2でLSD、最後15分Z3にビルドアップ。"
        load = 100 if intensity == "low" else 130

    else:
        steps = [Step("イージーラン", 30*60, "Z2", "run")]
        desc = "リカバリーラン"
        load = 30

    return Workout(
        name=name, sport_type="Run",
        description=desc, steps=steps,
        date=date, training_load=load
    )


def make_bike_workout(name: str, date: str, intensity: str) -> Workout:
    if name == "Endurance Ride":
        dur = 60 if intensity == "low" else 75 if intensity == "normal" else 90
        steps = [
            Step("ウォームアップ", 10*60, "Z1", "bike"),
            Step("エンデュランス", dur*60, "Z2", "bike"),
            Step("クールダウン", 5*60, "Z1", "bike"),
        ]
        lo, hi = BIKE_ZONES["Z2"]
        desc = f"有酸素エンデュランス{dur}分。{int(lo*FTP_WATTS)}〜{int(hi*FTP_WATTS)}W（FTPの{int(lo*100)}-{int(hi*100)}%）。"
        load = 50 if intensity == "low" else 70

    elif name == "Sweet Spot":
        if intensity == "low":
            reps, work_min, rest_min = 2, 12, 5
        elif intensity == "normal":
            reps, work_min, rest_min = 3, 12, 4
        else:
            reps, work_min, rest_min = 3, 15, 4

        ss_lo = int(FTP_WATTS * 0.88)
        ss_hi = int(FTP_WATTS * 0.95)
        steps = [
            Step("ウォームアップ", 10*60, "Z2", "bike"),
        ]
        for _ in range(reps):
            steps.append(Step("スイートスポット", work_min*60, "Z4", "bike", repeat=reps))
            steps.append(Step("リカバリー", rest_min*60, "Z2", "bike", repeat=reps))
        steps.append(Step("クールダウン", 5*60, "Z1", "bike"))
        desc = f"スイートスポット {reps}x{work_min}分 @ {ss_lo}-{ss_hi}W（FTPの88-95%）。CTL向上に最効率。"
        load = 70 if intensity == "low" else 95

    elif name == "Long Ride":
        dur = 120 if intensity == "low" else 150 if intensity == "normal" else 180
        steps = [
            Step("ウォームアップ", 10*60, "Z1", "bike"),
            Step("エンデュランス", int(dur * 0.7)*60, "Z2", "bike"),
            Step("テンポ", int(dur * 0.2)*60, "Z3", "bike"),
            Step("クールダウン", 10*60, "Z1", "bike"),
        ]
        desc = f"ロングライド{dur}分。有酸素基盤構築。後半Z3でテンポ走。"
        load = 110 if intensity == "low" else 145

    else:
        steps = [Step("リカバリーライド", 30*60, "Z1", "bike")]
        desc = "アクティブリカバリー"
        load = 25

    return Workout(
        name=name, sport_type="Ride",
        description=desc, steps=steps,
        date=date, training_load=load
    )

# ============================================================
# Intervals.icu へ POST
# ============================================================
def upload_workout(workout: Workout, dry_run: bool = False) -> bool:
    desc_text = workout_to_icu_description(workout)
    full_desc = f"{workout.description}\n\n{desc_text}"

    payload = {
        "start_date_local": f"{workout.date}T00:00:00",
        "category": "WORKOUT",
        "type": workout.sport_type,
        "name": workout.name,
        "description": full_desc,
        "moving_time": sum(s.duration_sec for s in workout.steps),
        "icu_training_load": workout.training_load,
    }

    print(f"\n  {'[DRY RUN] ' if dry_run else ''}📅 {workout.date} [{workout.sport_type}] {workout.name}")
    print(f"     負荷: {workout.training_load}  時間: {sum(s.duration_sec for s in workout.steps)//60}分")
    print(f"     {workout.description}")
    # ステップ表示
    for line in desc_text.split("\n"):
        print(f"     {line}")

    if dry_run:
        return True

    result = api_post(f"/athlete/{ATHLETE_ID}/events", payload)
    if result:
        print(f"     ✅ アップロード成功 (id: {result.get('id')})")
        return True
    return False

# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true", help="アップロードせずに内容確認")
    parser.add_argument("--days", type=int, default=7, help="生成する日数（デフォルト7日）")
    parser.add_argument("--start", type=str, default=None, help="開始日 YYYY-MM-DD（デフォルト明日）")
    args = parser.parse_args()

    dry_run = args.preview

    # 現在のフィットネス状態を取得
    print("=" * 55)
    print("  トレーニングメニュー生成 & Intervals.icuアップロード")
    print("=" * 55)
    print(f"\n  FTP: {FTP_WATTS}W ({FTP_WATTS/WEIGHT_KG:.2f}W/kg)")
    print(f"  閾値ペース: {pace_fmt(THRESHOLD_PACE_SEC)}/km")
    print("\n  Intervals.icuから最新コンディションを取得中...")

    athlete_data = api_get(f"/athlete/{ATHLETE_ID}")
    form = -26.6  # デフォルト（取得失敗時）
    atl  = 97.1

    if athlete_data:
        # 最新のウェルネスから CTL/ATL を取得
        today = datetime.now().strftime("%Y-%m-%d")
        week_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
        wellness = api_get(f"/athlete/{ATHLETE_ID}/wellness?oldest={week_ago}&newest={today}")
        if wellness:
            latest_w = sorted(wellness, key=lambda x: x.get('id',''))[-1]
            ctl = float(latest_w.get('ctl') or 70.5)
            atl = float(latest_w.get('atl') or 97.1)
            form = ctl - atl
            hrv = float(latest_w.get('hrv') or 0)
            print(f"  CTL={ctl:.1f}  ATL={atl:.1f}  Form={form:.1f}", end="")
            if hrv > 0:
                print(f"  HRV={hrv:.0f}", end="")
            print()
            # HRVが低い場合はさらに強度を下げる
            if hrv > 0 and hrv < 70:
                print(f"  ⚠️  HRV={hrv:.0f} < 70：強度を自動軽減します")
                form = min(form, -25)  # 強制的にlow強度

    start_date = datetime.now() + timedelta(days=1)
    if args.start:
        start_date = datetime.fromisoformat(args.start)

    print(f"\n  生成期間: {start_date.strftime('%Y-%m-%d')} から {args.days}日間")

    workouts = make_workouts(start_date, args.days, form, atl)

    print(f"\n{'─'*55}")
    print(f"  {'プレビュー' if dry_run else 'アップロード'}開始（{len(workouts)}件）")
    print(f"{'─'*55}")

    success = 0
    for w in workouts:
        ok = upload_workout(w, dry_run=dry_run)
        if ok:
            success += 1

    print(f"\n{'='*55}")
    if dry_run:
        print(f"  プレビュー完了（{success}/{len(workouts)}件）")
        print(f"  実際にアップロードするには --preview を外して実行してください")
    else:
        print(f"  完了！ {success}/{len(workouts)}件アップロード成功")
        print(f"  Intervals.icuのカレンダーを確認し、")
        print(f"  GarminデバイスをGarmin Connectに同期すると反映されます。")
    print(f"{'='*55}")

if __name__ == "__main__":
    main()
