"""
phase_engine.py — フェーズ判定・強度決定
smart_plan_v10.py line 2278-2403 から抽出
"""

from datetime import date, datetime


# ============================================================
# レースフェーズ
# ============================================================
def get_race_phase(races, target_date):
    """
    優先度（A/B/C）を考慮してフェーズを決定する。
    - Aレースに向けてピーキングする
    - Bレースはテーパーなしで通過（ピーク期の練習レース扱い）
    - Cレースはフェーズ計算に影響しない

    戻り値には "all_upcoming" も含め、サマリ表示時にB/Cレースも見えるようにする。
    """
    if isinstance(target_date, datetime): target_date = target_date.date()
    upcoming = sorted([r for r in races
                       if date.fromisoformat(r["date"]) >= target_date],
                      key=lambda r: r["date"])
    a_races  = [r for r in upcoming if r.get("priority","B")=="A"]
    b_races  = [r for r in upcoming if r.get("priority","B")=="B"]

    # フェーズ計算の基準: Aレースを最優先、なければ全レースの最近傍
    nearest  = (a_races or upcoming or [None])[0]
    if not nearest: return {"phase":"base","weeks_to_race":999,"race":None,
                            "all_upcoming":upcoming,"a_races":a_races}
    weeks = (date.fromisoformat(nearest["date"])-target_date).days//7

    # Aレースに向けてフェーズ決定
    if   weeks>16: phase="base"
    elif weeks>8:  phase="build"
    elif weeks>3:  phase="peak"
    elif weeks>1:  phase="taper"
    elif weeks==0: phase="race_week"
    else:          phase="recovery"

    # Bレースが直近にある場合: フェーズをそのままにしてテーパーしない
    # (練習レースとして消化する)
    next_b = (b_races or [None])[0]
    weeks_to_b = (date.fromisoformat(next_b["date"])-target_date).days//7 if next_b else 999

    return {"phase": phase,
            "weeks_to_race": weeks,
            "race": nearest,
            "next_b_race": next_b if weeks_to_b <= 2 else None,
            "all_upcoming": upcoming,
            "a_races": a_races}


# ============================================================
# 筋トレ進捗
# ============================================================
def calc_strength_progression(cfg):
    a = cfg["athlete"]
    goal_muscle = float(a.get("goal_muscle_kg") or 0)
    goal_date_str = a.get("goal_muscle_date","")
    if not goal_muscle or not goal_date_str:
        return {"level":"base","weeks_to_goal":0,"goal_muscle_kg":0,"goal_date":""}
    try: goal_date = date.fromisoformat(goal_date_str)
    except: return {"level":"base","weeks_to_goal":0,"goal_muscle_kg":goal_muscle,"goal_date":goal_date_str}
    weeks = max(0,(goal_date-date.today()).days//7)
    level = "base" if weeks>20 else "build" if weeks>10 else "peak" if weeks>4 else "maintenance"
    return {"level":level,"weeks_to_goal":weeks,"goal_muscle_kg":goal_muscle,"goal_date":goal_date_str}


# ============================================================
# 強度・セッション決定
# ============================================================
INTENSITY_ORDER = ["recovery","easy","moderate","hard","very_hard"]

# テンプレートは7日ローテーション。スイムはGCal予約（forced_sessions）で
# 検出した実際の予約日に自動挿入される。
PHASE_TEMPLATES = {
    "base":     [("run","long",90),("bike","endurance",120),("run","easy",50),
                 ("strength","core",30),("bike","endurance",75),("run","easy",40),("strength","upper",30)],
    "build":    [("run","tempo",60),("bike","sweetspot",90),("strength","full",40),
                 ("run","long",90),("bike","endurance",75),("run","interval",60),("bike","threshold",75)],
    "peak":     [("run","interval",60),("bike","threshold",90),("strength","core",30),
                 ("run","long",90),("bike","race_sim",60),("run","tempo",60),("bike","sweetspot",60)],
    "taper":    [("run","easy",40),("bike","easy",45),("run","strides",30),
                 ("rest",None,0),("bike","easy",30),("run","race_prep",20),("rest",None,0)],
    "race_week":[("run","easy",30),("bike","easy",30),("rest",None,0),
                 ("run","strides",20),("rest",None,0),("run","race_prep",15),("rest",None,0)],
    "recovery": [("yoga",None,30),("run","easy",30),("yoga",None,30),
                 ("bike","easy",40),("yoga",None,30),("run","easy",40),("rest",None,0)],
}

PHASE_INTENSITY = {
    "base":      {"run":"easy",     "bike":"easy",     "strength":"base"},
    "build":     {"run":"moderate", "bike":"moderate", "strength":"build"},
    "peak":      {"run":"hard",     "bike":"hard",     "strength":"build"},
    "taper":     {"run":"easy",     "bike":"easy",     "strength":"base"},
    "race_week": {"run":"recovery", "bike":"recovery", "strength":None},
    "recovery":  {"run":"recovery", "bike":"recovery", "strength":"base"},
}

COND_OVERRIDE = {"fatigued":"easy","depleted":"recovery"}

def decide_intensity(phase, sport, cond):
    base = PHASE_INTENSITY.get(phase,{}).get(sport,"easy")
    over = COND_OVERRIDE.get(cond)
    if over and sport not in ("strength",):
        bi = INTENSITY_ORDER.index(base) if base in INTENSITY_ORDER else 1
        oi = INTENSITY_ORDER.index(over) if over in INTENSITY_ORDER else 1
        return INTENSITY_ORDER[min(bi,oi)]
    return base


# ============================================================
# セッション説明文（目的・位置づけ・モチベ文付き）
# ============================================================

# フェーズ別モチベーションメッセージ
PHASE_MOTIVATIONS = {
    "base":      "基礎期：焦らず有酸素基盤を積み上げる時期。今の地味な積み上げがレース当日の余裕を作ります。",
    "build":     "",
    "peak":      "ピーク期：最後の強度上げ。残り数週間、全力で仕上げましょう。",
    "taper":     "テーパー期：強度を落として体を整える。焦りは禁物——体は今まさに準備中です。",
    "race_week": "レース週：今週は休むことが最高のトレーニング。自分を信じてスタートラインに立ちましょう。",
    "recovery":  "回復期：体と向き合い、次のサイクルへの土台を作る大切な時期です。",
}

# 強度別の説明テンプレート
INTENSITY_LABELS = {
    "recovery": ("リカバリー", "疲労回復と血流改善が目的。会話できる楽なペースを維持してください。"),
    "easy":     ("Z2有酸素",  "脂肪燃焼効率と有酸素基盤を高める最重要ゾーン。ここを丁寧に積み上げることが全ての土台になります。"),
    "moderate": ("テンポ/Z3", "乳酸閾値を押し上げる強度。苦しいが続けられるギリギリのペースが適切です。"),
    "hard":     ("閾値/VO2max","最大酸素摂取量と閾値を同時に鍛えます。このセッションが1番タイムを縮めます。"),
}
