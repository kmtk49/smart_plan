"""
nutrition.py — 栄養計算
smart_plan_v10.py line 4571-4640 から抽出
"""


def calc_nutrition(cfg, athlete, cond, phase, train_h, sport=None):
    """
    1日の目標摂取カロリーとPFCバランスを計算する。
    sport を渡すと種目別METs で消費カロリーをより正確に算出できる。
    """
    w   = athlete["weight"]
    h   = float(cfg["athlete"].get("height_cm", 170))
    age = int(cfg["athlete"].get("age", 35))
    goal = cfg["athlete"].get("goal", "performance")

    # 基礎代謝 (Mifflin-St Jeor) gender は config.athlete.gender から取得
    gender  = cfg.get("athlete",{}).get("gender","male") if cfg else "male"
    sex_adj = 5 if gender == "male" else -161
    bmr = 10 * w + 6.25 * h - 5 * age + sex_adj

    # 非運動TDEE（生活活動量のみ）
    neat = {"base":1.45,"build":1.50,"peak":1.55,
            "taper":1.35,"race_week":1.30,"recovery":1.35}.get(phase, 1.45)
    tdee_base = bmr * neat

    # 種目別 MET（中強度基準）で運動消費カロリーを計算
    METS = {"swim":9.0,"bike":7.5,"run":10.0,"brick":8.5,
            "strength":4.5,"yoga":2.5,"stretch":2.0,
            "hiit":8.0,"rest":0,"race":11.0}
    met = METS.get(sport, 6.0) if sport else 6.0
    exercise_kcal = met * w * train_h

    # 目標別カロリー調整（config から読み込み）
    n_cfg   = cfg.get("nutrition", {}) if cfg else {}
    cal_adj = n_cfg.get("calorie_adj", {})
    adj = cal_adj.get(goal, cal_adj.get("default", 0))
    # 2h超の高負荷日は追加補給
    extra_per_h = float(n_cfg.get("extra_kcal_per_hour_over2h", 150))
    if train_h > 2.0:
        adj += round((train_h - 2.0) * extra_per_h)

    kcal = round(tdee_base + exercise_kcal + adj)

    # タンパク質目標 (g/kg) — config から読み込み
    p_cfg = n_cfg.get("protein_per_kg", {})
    p_r   = float(p_cfg.get(goal, p_cfg.get("default", 1.8)))
    if train_h > 1.5: p_r += 0.2
    if sport in ("swim", "strength"): p_r += 0.1

    fat_ratio = float(n_cfg.get("fat_ratio", 0.25))
    min_fat   = int(n_cfg.get("min_fat_g",  40))
    min_carb  = int(n_cfg.get("min_carb_g", 50))
    prot = round(w * p_r)
    fat  = max(min_fat,  round(kcal * fat_ratio / 9))
    carb = max(min_carb, round((kcal - prot * 4 - fat * 9) / 4))

    notes = []
    if phase == "taper":
        notes.append("テーパー期：炭水化物多めでグリコーゲン蓄積")
    elif phase == "build":
        notes.append("ビルド期：練習後30分以内にタンパク質摂取")
    if not cfg.get("nutrition", {}).get("uses_protein_supplement"):
        notes.append("鶏むね・卵・魚・豆腐・納豆でタンパク補給")
    if cond["condition"] in ("fatigued", "depleted"):
        notes.append("疲労回復中：青魚・ベリー・ショウガ（抗炎症）")

    return {
        "kcal": kcal, "prot": prot, "carb": carb, "fat": fat,
        "p_per_kg": round(p_r, 1),
        "exercise_kcal": round(exercise_kcal),
        "notes": notes,
    }
