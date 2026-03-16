"""
plan_output.py — print_plan・各サマリ表示
smart_plan_v10.py line 5152-5535 から抽出
"""

from datetime import date

from .plan_generator import EMOJI
from .athlete_model import _fmt_pace
from .session_db import detect_deficient_sports


def print_calorie_summary(plan, cfg, athlete=None):
    """
    過去7日間の実績サマリーを表示する（プラン表示直前に呼び出す）。
    Intervals.icu の wellness データから:
      - 一日あたりの総消費カロリー（Intervals.icu activityカロリー合計）
      - 体重（前日差分）
      - HRV（前日変動）
      - Training Readiness スコア
    を表示する。
    athlete dict が渡されない / wellnessデータがない場合はスキップ。
    """
    if athlete is None:
        return
    wellness = athlete.get("wellness_history", [])
    if not wellness:
        return

    # 直近7日分のwellnessを取得
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    week_days = [(today - _td(days=i)).isoformat() for i in range(6, -1, -1)]  # 古い順

    # wellnessをdate → dictのマップに変換
    well_map = {}
    for w in wellness:
        wid = w.get("id","")
        if wid:
            well_map[wid] = w

    # 総カロリー・水分量の取得元メモ
    acts_90     = athlete.get("_acts_90") or []
    _a_cfg      = athlete  # weight/height/age は athlete dict から参照

    # ── アクティビティ別消費kcal を日付別に集計 (グリコーゲン推算用) ──
    _act_kcal_by_day = {}
    for a in acts_90:
        d = (a.get("start_date_local") or a.get("start_date",""))[:10]
        if d:
            _act_kcal_by_day[d] = _act_kcal_by_day.get(d, 0.0) + float(a.get("calories") or 0)

    # HRV平均（readiness フォールバック推算用）
    _hrv_all = [float(w.get("hrv") or 0) for w in wellness if w.get("hrv")]
    _hrv_mean = sum(_hrv_all) / len(_hrv_all) if _hrv_all else 0.0

    rows = []
    prev_weight    = None
    prev_hrv       = None
    prev_hydration = None
    # グリコーゲン推算の初期状態 (85%: 通常の休養後)
    # モデル根拠: Bergström & Hultman 1966, Coyle 1992
    #   最大容量: ~400g (1600kcal相当, 競技トレーニング済み男性)
    #   消費係数: アクティビティkcal × 0.55 (混合強度での糖質利用率)
    #   回復係数: 睡眠1時間あたり最大値の約7%回復 (十分な糖質摂取前提)
    _GLYCOGEN_MAX_KCAL = 1600.0
    glycogen_pct = 85.0

    for day_str in week_days:
        w = well_map.get(day_str, {})
        if not w:
            continue

        weight_raw = w.get("weight") or w.get("icu_weight")
        weight_kg  = float(weight_raw) if weight_raw else None
        hrv        = float(w.get("hrv") or 0) or None
        rhr        = float(w.get("restingHR") or 0) or None
        sleep_h    = float(w.get("sleepSecs") or 0) / 3600 or None
        readiness  = (w.get("trainingReadiness") or
                      w.get("training_readiness") or
                      w.get("training_readiness_score") or
                      w.get("icu_training_readiness"))
        # HRVベース推算（APIデータがない場合のフォールバック）
        if not readiness and hrv and _hrv_mean > 0:
            readiness = max(0.0, min(100.0, 50.0 + (hrv / _hrv_mean - 1.0) * 100.0))

        # ── 総カロリー計算 (優先順位付き) ─────────────────────────
        # 1) Garmin 同期 "totalKilocalories"
        total_kcal = float(w.get("totalKilocalories") or w.get("totalCalories") or 0) or None
        if not total_kcal:
            # 2) BMRカロリー + アクティビティカロリー
            bmr_w  = float(w.get("bmrKilocalories") or w.get("bmrCalories") or 0)
            act_w  = float(w.get("kcal") or w.get("activityCalories") or 0)
            if not act_w:
                act_w = _act_kcal_by_day.get(day_str, 0.0)
            if bmr_w > 0 and act_w > 0:
                total_kcal = bmr_w + act_w
            elif act_w > 0:
                # 3) アクティビティkcal + BMR推算
                _wt  = float(athlete.get("weight", 68.4))
                _cfg = cfg.get("athlete", {}) if cfg else {}
                _h   = float(_cfg.get("height_cm", 170))
                _age = int(_cfg.get("age", 35))
                _sex = 5 if _cfg.get("gender","male") == "male" else -161
                _bmr = round(10 * _wt + 6.25 * _h - 5 * _age + _sex)
                total_kcal = act_w + _bmr

        # ── 水分量 ───────────────────────────────────────────────
        hydration_ml = float(w.get("hydration") or
                             w.get("hydrationMilliliters") or
                             w.get("hydrationIntakeInMilliliters") or 0) or None

        # ── グリコーゲン推算 (running state) ─────────────────────
        # その日のアクティビティ消費 (wellness["kcal"] or acts集計)
        _day_act_kcal = (float(w.get("kcal") or w.get("activityCalories") or 0)
                         or _act_kcal_by_day.get(day_str, 0.0))
        # 消費: アクティビティkcal の55%がグリコーゲン由来 (混合強度の平均)
        depletion_pct = (_day_act_kcal * 0.55 / _GLYCOGEN_MAX_KCAL) * 100
        # 翌朝の回復: 不足分の50%を上限、かつ睡眠1hあたり最大5.5% 回復
        #   例) 1100kcal 消費(depletion 37.8%) + 7h睡眠
        #       → min(37.8×0.5, 7×5.5) = min(18.9, 38.5) = 18.9% 回復
        #       → 差し引き -18.9% (翌朝はやや低い)
        _sl           = sleep_h or 7.0
        deficit_after = 100.0 - (glycogen_pct - depletion_pct)
        recovery_pct  = min(deficit_after * 0.5, _sl * 5.5)
        glycogen_pct  = max(10.0, min(100.0,
                            glycogen_pct - depletion_pct + recovery_pct))

        # 前日差分
        dw  = (weight_kg  - prev_weight)    if (weight_kg    and prev_weight)    else None
        dhrv= (hrv        - prev_hrv)       if (hrv          and prev_hrv)       else None
        dh  = (hydration_ml - prev_hydration) if (hydration_ml and prev_hydration) else None

        rows.append({
            "date": day_str, "weight": weight_kg, "dw": dw,
            "hrv": hrv, "dhrv": dhrv,
            "rhr": rhr, "sleep_h": sleep_h,
            "total_kcal": total_kcal,
            "hydration_ml": hydration_ml, "dh": dh,
            "readiness": readiness,
            "glycogen_pct": round(glycogen_pct),
        })
        if weight_kg:    prev_weight    = weight_kg
        if hrv:          prev_hrv       = hrv
        if hydration_ml: prev_hydration = hydration_ml

    if not rows:
        return

    # 水分データが1件でもあるか判定（列表示の有無に使用）
    has_hydration = any(r["hydration_ml"] for r in rows)

    W = 96  # テーブル幅
    print(f"\n{'─'*W}")
    print(f"  📊 過去7日間 ウェルネス実績サマリー (Intervals.icu)")
    print(f"{'─'*W}")
    hdr_water = f"{'水分(±)':>12}" if has_hydration else ""
    print(f"  {'日付':<10}{'総kcal':>8}  {'体重(±)':>12}  {'HRV(±)':>8}  "
          f"{'RHR':>4}  {'睡眠':>5}  {hdr_water}  {'Readiness':>9}  {'グリコ':>6}")
    print(f"  {'─'*W}")

    for r in rows:
        date_fmt  = r["date"][5:]
        kcal_str  = f"{r['total_kcal']:.0f}" if r["total_kcal"] else "N/A"
        wt_str    = (f"{r['weight']:.1f}kg"
                     + (f"({r['dw']:+.1f})" if r["dw"] is not None else "")) if r["weight"] else "N/A"
        hrv_str   = (f"{r['hrv']:.0f}"
                     + (f"({r['dhrv']:+.0f})" if r["dhrv"] is not None else "")) if r["hrv"] else "N/A"
        rhr_str   = f"{r['rhr']:.0f}"  if r["rhr"]   else "N/A"
        sl_str    = f"{r['sleep_h']:.1f}h" if r["sleep_h"] else "N/A"

        # 水分: L表示 + 前日差分 (mL単位)
        if has_hydration:
            if r["hydration_ml"]:
                _hl = r["hydration_ml"] / 1000
                _dh_str = (f"({r['dh']:+.0f}mL)" if r["dh"] is not None else "")
                water_str = f"{_hl:.1f}L{_dh_str}"
            else:
                water_str = "N/A"
        else:
            water_str = ""

        # Readiness
        rd_val = r["readiness"]
        if rd_val is not None:
            rd_int  = int(float(rd_val))
            rd_icon = "🟢" if rd_int >= 67 else ("🟡" if rd_int >= 34 else "🔴")
            rd_str  = f"{rd_icon}{rd_int}"
        else:
            rd_str = "N/A"

        # グリコーゲン
        gp      = r["glycogen_pct"]
        gp_icon = "🟢" if gp >= 75 else ("🟡" if gp >= 50 else "🔴")
        gp_str  = f"{gp_icon}{gp}%"

        water_col = f"{water_str:>12}  " if has_hydration else ""
        print(f"  {date_fmt:<10}{kcal_str:>8}  {wt_str:>12}  {hrv_str:>8}  "
              f"{rhr_str:>4}  {sl_str:>5}  {water_col}{rd_str:>9}  {gp_str:>6}")

    print(f"  {'─'*W}")
    kcal_src = athlete.get("total_kcal_src", "BMR+アクティビティ")
    print(f"  ※ 総kcal = BMR + アクティビティ消費  [{kcal_src}]")
    if has_hydration:
        print(f"  ※ 水分(±): 前日比 mL差  [Garmin/Apple Health 同期値]")
    print(f"  ※ グリコーゲン推算: 運動消費×0.55÷1600kcal基準 | 睡眠で回復  "
          f"🟢≥75% 充分  🟡50-74% やや不足  🔴<50% 要補給(糖質60-90g)")
    print(f"  ※ Readiness 🟢≥67 通常OK  🟡34-66 強度調整  🔴<34 回復優先")
    print(f"{'─'*W}\n")


def print_plan(plan, race_info, cond_info, athlete, goal_targets, cfg=None, today_mode=False, str_prog=None, gcal_days=None, num_days=10):
    # GCalスケジュールサマリは対話モード開始前（main()側）で表示済み
    # --today モード or 明示的な今日プラン表示時のみここで表示
    from .summary import print_work_schedule_summary, print_periodization_summary
    if today_mode and gcal_days is not None:
        print_work_schedule_summary(gcal_days, date.today(), num_days=num_days)

    # ── グランドプラン(年間ピリオダイゼーション)を冒頭に表示 ──
    all_races = race_info.get("all_races") or ([race_info["race"]] if race_info.get("race") else [])
    if all_races:
        print_periodization_summary(all_races)
    phase=race_info["phase"]; race=race_info["race"]; ci=cond_info
    icons={"peak":"🔥","good":"✅","normal":"😐","fatigued":"😓","depleted":"🛑"}

    if not today_mode:
        print(f"\n  コンディション: {icons.get(ci['condition'],'')} {ci['condition'].upper()}"
              f"  スコア {ci['score']}/10")
        print(f"  CTL={athlete['ctl']:.1f} ATL={athlete['atl']:.1f}"
              f" Form={athlete['form']:.1f} HRV={athlete['hrv']:.0f}"
              f"(7d:{athlete['hrv_7d_avg']:.0f}) 睡眠={athlete['sleep_h']:.1f}h")
        for r in ci.get("reasons",[]): print(f"  ⚠️  {r}")

        # ── レース情報（優先度付き） ─────────────────────────────
        all_upcoming = race_info.get("all_upcoming", [race] if race else [])
        if all_upcoming:
            print()
            prio_icons = {"A": "🅐", "B": "🅑", "C": "🅒"}
            for r_ in all_upcoming[:4]:
                pri = r_.get("priority","B")
                r_date = r_.get("date","")
                weeks_to = (date.fromisoformat(r_date) - date.today()).days // 7 if r_date else 0
                pri_label = {"A":"本命Aレース","B":"練習Bレース","C":"Cレース"}.get(pri, "")
                note = f"残り{weeks_to}週 [{pri_label}]"
                if r_.get("past_time_str"):
                    note += f" 前回:{r_['past_time_str']}"
                if r_.get("rival"):
                    note += f" 目標:{r_['rival']}"
                print(f"  {prio_icons.get(pri,'🏁')} {r_['name']} ({r_date}) {note}")
        elif race:
            print(f"  🏁 {race['name']} ({race['date']}) 残り{race_info['weeks_to_race']}週 [{phase.upper()}]")

        # ゴール情報
        gt = goal_targets
        if gt.get("goal_src"):
            print(f"  🎯 目標根拠: {gt['goal_src']}")
        if gt.get("targets",{}).get("race_run_pace"):
            print(f"     ラン目標: {_fmt_pace(gt['targets']['race_run_pace'])}/km"
                  f"  バイク目標: {gt['targets'].get('race_bike_w','-')}W")

        # ── 筋肉量・体組成ステータス ───────────────────────────────
        if str_prog:
            _print_body_comp_status(athlete, str_prog, race_info, cfg=cfg)

        # 不足種目
        deficient = detect_deficient_sports(athlete.get("weekly_counts",{}), cfg)
        if deficient:
            print(f"  📊 不足種目: {' / '.join(deficient)} → 短時間セッションで補強中")
        active = [p for p in plan if p["sport"] not in ("rest","race")]
        print(f"\n  週合計: {sum(p['duration_min'] for p in active)/60:.1f}h / {len(active)}セッション\n")

    for item in plan:
        if today_mode and item["date"] != date.today().isoformat(): continue
        e = EMOJI.get(item["sport"],"🏋️")
        if item["sport"] in ("rest","race"):
            gcn = "  ".join(item.get("gcal_notes",[]))
            print(f"\n  {item['date']} {e}  {item['name']}" + (f"  [{gcn}]" if gcn else ""))
            for line in item["description"].split("\n")[:2]:
                if line.strip(): print(f"    {line.strip()}")
            nt = item.get("nutrition")
            if nt:
                label = "休養日" if item["sport"] == "rest" else "レース当日"
                print(f"    🍽 {label}: {nt['kcal']}kcal  "
                      f"P:{nt['prot']}g  C:{nt['carb']}g  F:{nt['fat']}g")
            continue

        gcn = item.get("gcal_notes",[])
        print(f"\n  {item['date']} {e}  {item['name']}  ({item['duration_min']}分)")
        for g in gcn: print(f"    📅 {g}")
        # 説明文（最大6行）
        desc_lines = item["description"].split("\n")
        for line in desc_lines[:6]:
            if line.strip(): print(f"    {line.strip()}")
        if len(desc_lines) > 6:
            remaining = [l for l in desc_lines[6:] if l.strip()]
            if remaining: print(f"    ...")
        # 栄養情報を1行で表示
        nt = item.get("nutrition")
        if nt:
            ex_str = f"  運動消費:{nt['exercise_kcal']}kcal" if nt.get("exercise_kcal") else ""
            print(f"    🍽 {nt['kcal']}kcal  "
                  f"P:{nt['prot']}g({nt['p_per_kg']}g/kg)  "
                  f"C:{nt['carb']}g  F:{nt['fat']}g{ex_str}")

    # ── GCalスケジュールサマリ（プラン末尾に表示） ──────────────
    if gcal_days is not None:
        from .summary import print_work_schedule_summary
        # プランの開始日を取得
        _plan_start = date.today()
        if plan:
            try: _plan_start = date.fromisoformat(plan[0]["date"])
            except: pass
        print_work_schedule_summary(gcal_days, _plan_start, num_days=num_days)


def _print_body_comp_status(athlete, str_prog, race_info, cfg=None):
    """
    現在の筋肉量・体重と、Aレース当日の目標値を表示する。
    10日間の筋トレ指針も併記する。
    """
    if cfg is None:
        cfg = {}
    cfg_a = {}
    weight      = float(athlete.get("weight", athlete.get("weight_kg", 68.4)))
    # 体脂肪率・除脂肪体重 (lbm) をathleteから取得（wellness実測値 → config推定 の順）
    body_fat_pct = float(athlete.get("body_fat_pct") or cfg.get("athlete",{}).get("body_fat_pct", 18.0))
    fat_kg       = weight * body_fat_pct / 100
    lean_kg      = weight - fat_kg   # 除脂肪体重（筋肉+骨+内臓等）
    # 骨格筋量: Garmin/Withings 実測値を優先、なければ除脂肪体重×0.55 で推算
    _measured_muscle = athlete.get("muscle_mass_kg")  # fetch_athlete_data で取得
    estimated_muscle = float(_measured_muscle) if _measured_muscle else lean_kg * 0.55
    _muscle_src = "実測" if _measured_muscle else "推算(除脂肪×0.55)"

    goal_muscle  = float(str_prog.get("goal_muscle_kg") or 0)
    goal_date    = str_prog.get("goal_date","")
    weeks_left   = str_prog.get("weeks_to_goal", 0)
    level        = str_prog.get("level","base")

    # Aレース日の目標体重（筋肉量を増やしながら体重は維持or軽減が理想）
    a_race = race_info.get("race")
    a_race_date = a_race["date"] if a_race else ""
    weeks_to_a  = race_info.get("weeks_to_race", 0)

    # Readiness があれば体組成の横に表示
    _readiness = athlete.get("readiness")
    _rd_str    = (f"  Readiness:{int(_readiness)}/100" if _readiness is not None else "")
    _hydration = athlete.get("hydration_ml")
    _hy_str    = (f"  水分:{_hydration/1000:.1f}L" if _hydration else "")

    print(f"\n  ─── 体組成ステータス ───")
    print(f"  現在  体重:{weight:.1f}kg  骨格筋量:{estimated_muscle:.1f}kg[{_muscle_src}]  "
          f"体脂肪:{body_fat_pct:.0f}%({fat_kg:.1f}kg)  除脂肪:{lean_kg:.1f}kg"
          f"{_rd_str}{_hy_str}")

    if goal_muscle > 0:
        gap = goal_muscle - estimated_muscle
        weekly_gain = gap / weeks_left if weeks_left > 0 else 0
        direction = "増" if gap > 0 else "減"
        print(f"  目標  骨格筋量:{goal_muscle:.1f}kg  "
              f"({goal_date} / 残り{weeks_left}週)")
        print(f"  差分  {gap:+.1f}kg  → 週{abs(weekly_gain):.2f}kg{direction}が必要")

    # Aレース当日の理想体組成
    if a_race_date and goal_muscle > 0:
        weeks_to_goal = weeks_left
        # レース当日に何kg筋肉がつくか（週0.1〜0.3kgが現実的上限）
        realistic_gain_per_week = 0.15  # トレーニング中の保守的推定
        gain_by_race = min(gap, realistic_gain_per_week * weeks_to_a) if weeks_to_a > 0 else 0
        muscle_at_race = estimated_muscle + gain_by_race
        # レース目標体重（PWR向上のため軽量化も考慮）
        target_weight_at_race = weight  # 現状維持をデフォルト
        print(f"  🏁 {a_race['name']} ({a_race_date}) 当日予測:")
        print(f"     筋肉量:{muscle_at_race:.1f}kg  "
              f"（{gain_by_race:+.1f}kg）  "
              f"体重:{target_weight_at_race:.1f}kg")

    # 10日間の筋トレ指針
    STRENGTH_GUIDANCE = {
        "base": [
            "上半身・体幹を重視。トライアスロンで酷使する肩・体幹を重点的に強化。",
            "週2回: スクワット/デッドリフト（8〜12rep × 3set）で下半身の土台を作る。",
            "セッション後30分以内にタンパク質25g（鶏むね/プロテイン）を摂取。",
        ],
        "build": [
            "筋力→筋持久力へシフト。高rep（15〜20rep）・短rest（60秒）で心肺も鍛える。",
            "プランク・サイドプランク・ヒップスラストでトライアスロン特化の体幹を強化。",
            "週2回: ラン前日に実施（筋肉痛がある状態でのランで適応を促す）。",
        ],
        "peak": [
            "筋トレは維持モード。週1〜2回・30分・高強度低ボリュームに削減。",
            "爆発系（パワークリーン・ジャンプスクワット）で速筋繊維を活性化。",
            "レース3週前から筋トレ量を20%ずつ削減してフレッシュさを確保。",
        ],
        "taper": [
            "筋トレは週1回・20分のメンテナンスのみ。疲労を残さない。",
            "動的ストレッチ・フォームローラーで筋肉の質を整える。",
        ],
        "maintenance": [
            "目標筋肉量を維持する段階。週2回・現状の負荷を維持。",
            "レース期間は過度な筋トレより回復を優先。",
        ],
    }

    guidance = STRENGTH_GUIDANCE.get(level, STRENGTH_GUIDANCE["base"])
    print(f"\n  ─── 10日間の筋トレ指針 [{level.upper()}フェーズ] ───")
    for g in guidance:
        print(f"  💪 {g}")
    print()
