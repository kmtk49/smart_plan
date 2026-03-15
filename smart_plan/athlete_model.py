"""
athlete_model.py — HRV・目標ペース・アスリートデータモジュール
smart_plan_v10.py line 91-248, 1142-1529 から抽出
"""

import re
from datetime import datetime, timedelta, date
from pathlib import Path

from .icu_api import icu_get
from .result_parser import _find_activities_csv


# ============================================================
# フォーマットユーティリティ
# ============================================================

def _fmt_pace(sec):
    m,s = divmod(int(sec),60); return f"{m}:{s:02d}"


def _pace_to_icu(sec):
    """秒/km → intervals.icu 絶対ペース表記 (M:SS/km)"""
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}/km"


def _swim_pace(sec_per_100m, for_wdoc=False):
    """
    秒/100m → 文字列
    for_wdoc=True  → "@M:SS/100m"  (intervals.icu workout_doc用: /100mを必ず付ける)
    for_wdoc=False → "M:SS/100m"   (desc_text表示用)
    """
    m, s = divmod(int(sec_per_100m), 60)
    if for_wdoc:
        return f"@{m}:{s:02d}/100m"
    return f"{m}:{s:02d}/100m"


def _swim_pace_icu(sec_per_100m):
    """秒/100m → intervals.icu workout_doc用ペース表記
    正しい構文: "- 400mtr 2:23/100m Pace"
    m=分, mtr=メートル なので距離はmtrで指定する。
    """
    m, s = divmod(int(sec_per_100m), 60)
    return f"{m}:{s:02d}/100m Pace"


def _fmt_time(sec):
    h,r = divmod(sec,3600); m,s = divmod(r,60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_time_s(sec):
    """秒 → H:MM:SS / M:SS 文字列"""
    m, s = divmod(int(sec), 60)
    h, m2 = divmod(m, 60)
    return f"{h}:{m2:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ============================================================
# アスリートデータ取得
# ============================================================

def _extract_results(race_acts):
    """レース活動から過去リザルトを抽出"""
    results = []
    for a in race_acts:
        sport = a.get("type","")
        dist  = float(a.get("distance") or 0)
        move  = float(a.get("moving_time") or 0)
        if move < 600: continue
        pace  = move/(dist/1000) if dist>0 else 0
        results.append({
            "date":     a.get("start_date_local","")[:10],
            "name":     a.get("name",""),
            "sport":    sport,
            "dist_m":   dist,
            "time_s":   move,
            "pace_s":   pace,
            "pace_str": _fmt_pace(int(pace)) if pace>0 else "",
            "time_str": _fmt_time(int(move)),
            "source":   "intervals_icu",
        })
    return sorted(results, key=lambda x: x["date"], reverse=True)[:30]


def _count_sports(acts):
    counts = {"run":0,"bike":0,"swim":0,"strength":0}
    for a in acts:
        t = (a.get("type") or "").lower()
        if "run" in t: counts["run"] += 1
        elif "ride" in t or "cycling" in t: counts["bike"] += 1
        elif "swim" in t: counts["swim"] += 1
        elif "weight" in t or "strength" in t: counts["strength"] += 1
    return counts


def _default_athlete(weight=68.4, ftp=223, tp_sec=288, css=125,
                     past_results=None, weekly_counts=None, cfg=None):
    """
    フォールバック用アスリート辞書。
    cfg が渡された場合は config.yaml の fallback 値を使用する。
    """
    a_cfg = cfg.get("athlete", {}) if cfg else {}
    ctl   = float(a_cfg.get("ctl_fallback",   70.5))
    atl   = float(a_cfg.get("atl_fallback",   70.0))
    hrv   = float(a_cfg.get("hrv_fallback",   60.0))
    rhr   = float(a_cfg.get("rhr_fallback",   50.0))
    slp   = float(a_cfg.get("sleep_fallback",  7.0))
    return {"weight": weight, "ftp": ftp, "tp_sec": tp_sec, "css": css,
            "ctl": ctl, "atl": atl, "form": ctl - atl,
            "hrv": hrv, "hrv_7d_avg": hrv, "sleep_h": slp, "rhr": rhr, "rhr_avg": rhr,
            "wellness_history": [], "past_results": past_results or [],
            "weekly_counts": weekly_counts or {"run":0,"bike":0,"swim":0,"strength":0}}


def fetch_athlete_data(cfg):
    aid     = cfg["athlete"]["intervals_icu_athlete_id"]
    api_key = cfg["athlete"]["intervals_icu_api_key"]
    base    = f"https://intervals.icu/api/v1/athlete/{aid}"
    print("  📡 Intervals.icu から最新データを取得中...")
    profile = icu_get(f"{base}", api_key) or {}
    weight  = float(profile.get("icu_weight") or 68.4)
    today   = datetime.now().strftime("%Y-%m-%d")
    ago30   = (datetime.now()-timedelta(days=30)).strftime("%Y-%m-%d")
    ago7    = (datetime.now()-timedelta(days=7)).strftime("%Y-%m-%d")
    ago90   = (datetime.now()-timedelta(days=90)).strftime("%Y-%m-%d")
    wellness= icu_get(f"{base}/wellness", api_key, {"oldest":ago30,"newest":today}) or []
    wellness= sorted(wellness, key=lambda x: x.get("id",""))

    # ── FTP 取得（優先順位付き） ─────────────────────────────────
    ftp_profile  = float(profile.get("icu_ftp") or 0)
    ftp_wellness = 0.0
    if wellness:
        ftp_wellness = float(wellness[-1].get("icu_pm_ftp") or 0)
    acts_90 = icu_get(f"{base}/activities", api_key, {"oldest":ago90,"newest":today}) or []
    ftps_90 = [float(a.get("icu_rolling_ftp") or 0) for a in acts_90 if a.get("icu_rolling_ftp")]
    ftp_rolling = max(ftps_90) if ftps_90 else 0.0
    ftp_fallback = float(cfg.get("athlete",{}).get("ftp_fallback", 223))
    ftp = ftp_profile or ftp_wellness or ftp_rolling or ftp_fallback
    ftp_src = ("プロフィール設定" if ftp_profile else
               "wellness(PM)" if ftp_wellness else
               "rolling(90日)" if ftp_rolling else "デフォルト")

    # ── TP（ランニング閾値ペース）取得（優先順位付き） ───────────────
    tp_profile = float(profile.get("icu_run_threshold_pace") or 0)   # 秒/m → 秒/km換算が必要
    if tp_profile > 0:
        tp_profile = tp_profile * 1000  # intervals.icu は 秒/m で保持している場合がある
        if tp_profile > 600 or tp_profile < 150:  # 異常値は無視
            tp_profile = 0
    tp_wellness = 0.0
    if wellness:
        v = float(wellness[-1].get("run_threshold_pace") or 0)
        if 150 < v < 600:  # 2:30〜10:00/km の範囲チェック
            tp_wellness = v
    pace_secs = []
    for a in acts_90:
        if a.get("type") in ("Run","VirtualRun") and float(a.get("distance",0)) > 12000:
            dist = float(a.get("distance",1)); move = float(a.get("moving_time",1))
            if dist > 0 and move > 0:
                pace_secs.append(move / (dist / 1000))
    tp_recent = min(pace_secs) * 1.05 if pace_secs else 0.0
    tp_fallback = float(cfg.get("athlete",{}).get("tp_fallback", 288))
    tp_sec = tp_profile or tp_wellness or tp_recent or tp_fallback
    tp_src = ("プロフィール設定" if tp_profile else
              "wellness" if tp_wellness else
              "直近ラン推算(90日)" if tp_recent else "デフォルト")

    # ── CSS（スイム閾値ペース）取得（優先順位付き） ──────────────────
    css_profile = float(profile.get("icu_swim_threshold_pace") or 0)   # 秒/m
    if css_profile > 0:
        css_profile = css_profile * 100  # → 秒/100m
        if css_profile > 200 or css_profile < 55:  # 0:55〜3:20/100m の範囲チェック
            css_profile = 0
    css_wellness = 0.0
    if wellness:
        v = float(wellness[-1].get("swim_threshold_pace") or 0)
        if 55 < v < 200:
            css_wellness = v
    swim_paces = []
    for a in acts_90:
        if a.get("type") in ("Swim","VirtualSwim","OpenWaterSwim") and float(a.get("distance",0)) > 500:
            dist = float(a.get("distance",1)); move = float(a.get("moving_time",1))
            if dist > 0 and move > 0:
                swim_paces.append(move / (dist / 100))  # 秒/100m
    css_recent = min(swim_paces) * 1.05 if swim_paces else 0.0
    if css_recent > 0 and (css_recent > 200 or css_recent < 55):
        css_recent = 0
    css_fallback = float(cfg.get("athlete",{}).get("css_fallback", 125))
    css = css_profile or css_wellness or css_recent or css_fallback
    css_src = ("プロフィール設定" if css_profile else
               "wellness" if css_wellness else
               "直近スイム推算(90日)" if css_recent else "デフォルト")

    acts = acts_90  # 互換性のため

    # 過去リザルト取得（レース結果）
    ago730 = (datetime.now()-timedelta(days=730)).strftime("%Y-%m-%d")
    race_acts = icu_get(f"{base}/activities", api_key,
                        {"oldest":ago730,"newest":today,"category":"RACE"}) or []
    if len(race_acts) < 3:
        all_acts_yr = icu_get(f"{base}/activities", api_key,
                              {"oldest":ago730,"newest":today}) or []
        race_kws = ["race","レース","marathon","マラソン","triathlon","トライアスロン",
                    "大会","競走","ラン大会"]
        extra = [a for a in all_acts_yr
                 if any(k in (a.get("name") or "").lower() for k in race_kws)
                 and a not in race_acts]
        race_acts = race_acts + extra
    past_results = _extract_results(race_acts)

    # 週別練習量（種目バランス確認）
    ago14 = (datetime.now()-timedelta(days=14)).strftime("%Y-%m-%d")
    recent = icu_get(f"{base}/activities", api_key, {"oldest":ago14,"newest":today}) or []
    weekly_counts = _count_sports(recent)

    if not wellness:
        print("  ⚠️  ウェルネスデータなし、デフォルト値を使用")
        return _default_athlete(weight, ftp, tp_sec, css, past_results, weekly_counts, cfg=cfg)
    latest   = wellness[-1]
    _a_cfg   = cfg.get("athlete", {})
    ctl      = float(latest.get("ctl")        or _a_cfg.get("ctl_fallback",   70.5))
    atl      = float(latest.get("atl")        or _a_cfg.get("atl_fallback",   70.0))
    hrv      = float(latest.get("hrv")        or _a_cfg.get("hrv_fallback",   60.0))
    sleep_h  = float(latest.get("sleepSecs")  or 0) / 3600
    if sleep_h == 0:
        sleep_h = float(_a_cfg.get("sleep_fallback", 7.0))
    rhr      = float(latest.get("restingHR")  or _a_cfg.get("rhr_fallback",   50.0))
    hrv_vals = [float(w.get("hrv") or 0) for w in wellness[-7:] if w.get("hrv")]
    hrv_7d   = sum(hrv_vals)/len(hrv_vals) if hrv_vals else hrv
    rhr_vals = [float(w.get("restingHR") or 0) for w in wellness if w.get("restingHR")]
    rhr_avg  = sum(rhr_vals)/len(rhr_vals) if rhr_vals else rhr

    # ── 体組成データ (Garmin Body Composition / Withings / InBody 同期時) ──
    # bodyFat: Intervals.icu wellness の "bodyFat" フィールド (%)
    body_fat_pct_well = float(latest.get("bodyFat") or 0)
    body_fat_pct = (body_fat_pct_well if body_fat_pct_well > 0
                    else float(_a_cfg.get("body_fat_pct", 18.0)))
    # muscleMassKg: Garmin/Withings から同期される骨格筋量 (kg)
    muscle_mass_kg_well = float(latest.get("muscleMassKg") or latest.get("muscle_mass") or 0)

    # ── Readiness (Intervals.icu wellness の "readiness" フィールド) ──
    readiness_well = (latest.get("readiness") or
                      latest.get("trainingReadiness") or
                      latest.get("icu_training_readiness"))
    readiness = float(readiness_well) if readiness_well is not None else None

    # ── 水分量 (Intervals.icu wellness では基本 None のため初期値 None) ──
    hydration_ml = float(latest.get("hydrationVolume") or
                         latest.get("hydration") or
                         latest.get("hydrationMilliliters") or
                         latest.get("hydrationIntakeInMilliliters") or 0) or None
    # 体内水分量% (Garmin Index スケール同期 → Intervals.icu では非対応のため None)
    body_water_pct = None

    # ── Garmin Connect 直接取得 (Readiness / 体内水分% / Body Battery) ─────
    # garminconnect ライブラリがインストールされており、config.yaml に
    # garmin.email が設定されている場合のみ実行。
    # 未インストール・API失敗時は警告を出して Intervals.icu 値にフォールバック。
    _g_cfg = cfg.get("garmin", {})
    if _g_cfg.get("email"):
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent.parent))
            from garmin_health_diagnosis import (fetch_garmin_health,
                                                  _parse_readiness,
                                                  _parse_body_battery,
                                                  _parse_body_comp)
            print("  📡 Garmin Connect から Readiness / 体内水分% を取得中...")
            _gh = fetch_garmin_health(
                _g_cfg["email"],
                _g_cfg.get("password", ""),
                days_back=1,
            )
            # Training Readiness (Garmin Firstbeat Analytics スコア)
            _rd = _parse_readiness(_gh["today"].get("readiness"))
            if _rd.get("score") is not None:
                readiness = float(_rd["score"])
                _level_str = _rd.get("level", "")
                print(f"     ✅ Readiness={readiness:.0f}/100  [{_level_str}]"
                      + (f"  ({_rd['feedback']})" if _rd.get("feedback") else ""))
            # 体内水分% (Garmin Index スケール → body_comp)
            _bc = _parse_body_comp(_gh["today"].get("body_comp"))
            if _bc.get("body_water_pct") is not None:
                body_water_pct = _bc["body_water_pct"]
                print(f"     💧 体内水分%={body_water_pct:.1f}%  (Garmin Connect 直接)")
            # 体組成の補完（骨格筋量・体脂肪がwellnessで取れていない場合）
            if muscle_mass_kg_well == 0 and _bc.get("muscle_mass_kg"):
                muscle_mass_kg_well = _bc["muscle_mass_kg"]
                print(f"     💪 骨格筋量={muscle_mass_kg_well:.1f}kg  (Garmin 体組成)")
            if body_fat_pct_well == 0 and _bc.get("body_fat_pct"):
                body_fat_pct = _bc["body_fat_pct"]
                body_fat_pct_well = body_fat_pct
            # Body Battery (情報表示のみ)
            _bb = _parse_body_battery(_gh["today"].get("body_battery"))
            if _bb.get("current") is not None:
                print(f"     🔋 Body Battery={_bb['current']:.0f}")
            # 取得エラーがあれば出力
            for _err in (_gh.get("fetch_errors") or []):
                print(f"     ⚠️  Garmin: {_err}")
        except RuntimeError as _ge:
            print(f"  ⚠️  Garmin Connect スキップ (garminconnect 未インストール): {_ge}")
        except Exception as _ge:
            print(f"  ⚠️  Garmin Connect 取得スキップ: {type(_ge).__name__}: {_ge}")

    # ── 総カロリー (totalKilocalories) の取得 ─────────────────────────────
    # 優先順位:
    #   1) Garmin 同期 "totalKilocalories" (BMR + アクティブカロリー合算)
    #   2) "bmrKilocalories" + "kcal"(アクティビティ)
    #   3) wellness["kcal"] + BMR 推算値
    total_kcal_well = float(latest.get("totalKilocalories") or
                            latest.get("totalCalories") or 0)
    bmr_kcal_well   = float(latest.get("bmrKilocalories") or
                            latest.get("bmrCalories") or 0)
    act_kcal_well   = float(latest.get("kcal") or
                            latest.get("activityCalories") or 0)

    if total_kcal_well > 0:
        total_kcal_latest = total_kcal_well
        total_kcal_src    = "Garmin総消費(wellness同期)"
    elif bmr_kcal_well > 0 and act_kcal_well > 0:
        total_kcal_latest = bmr_kcal_well + act_kcal_well
        total_kcal_src    = "BMR+アクティビティ(wellness合算)"
    elif act_kcal_well > 0:
        # BMRを体重・年齢から推算して加算
        _h   = float(_a_cfg.get("height_cm", 170))
        _age = int(_a_cfg.get("age", 35))
        _sex = 5 if _a_cfg.get("gender","male") == "male" else -161
        _bmr = round(10 * weight + 6.25 * _h - 5 * _age + _sex)
        total_kcal_latest = act_kcal_well + _bmr
        total_kcal_src    = "アクティビティ+BMR推算"
    else:
        total_kcal_latest = None
        total_kcal_src    = "データなし"

    print(f"  ✅ 体重={weight}kg  FTP={ftp:.0f}W [{ftp_src}]  TP={_fmt_pace(int(tp_sec))}/km [{tp_src}]")
    print(f"     CSS={_swim_pace(css)}/100m [{css_src}]")
    if total_kcal_latest:
        print(f"     総消費kcal={total_kcal_latest:.0f}  [{total_kcal_src}]")
    if readiness is not None:
        print(f"     Readiness={readiness:.0f}/100")
    if hydration_ml:
        print(f"     水分={hydration_ml/1000:.1f}L")
    if muscle_mass_kg_well > 0:
        print(f"     骨格筋量={muscle_mass_kg_well:.1f}kg  体脂肪={body_fat_pct:.1f}%  [実測値]")
    else:
        print(f"     体脂肪={body_fat_pct:.1f}%  [{'wellness' if body_fat_pct_well > 0 else 'config推定'}]")
    # activities CSV の存在を確認して表示（ファイル名は複数パターン対応）
    _csv_found = _find_activities_csv()
    if _csv_found:
        try:
            import csv as _csv_mod
            with open(_csv_found, encoding="utf-8-sig", errors="replace") as _f:
                _rows = sum(1 for _ in _csv_mod.reader(_f)) - 1
            print(f"  📊 activities_detail.csv: {_rows}件のアクティビティ [{_csv_found.name}]")
            print(f"     ↑ intervals.icuからエクスポートした全アクティビティ詳細データ")
            print(f"        (レース当日のスプリット自動取得・FTP/TP/CTL算出に使用)")
        except: pass
    else:
        print(f"  ℹ️  activitiesCSV 未配置 (i275804_activities.csv または activities_detail.csv をスクリプト隣に置くと過去レーススプリットを自動取得できます)")
    print(f"     CTL={ctl:.1f} ATL={atl:.1f} Form={ctl-atl:.1f}")
    print(f"     HRV={hrv:.0f}(7d:{hrv_7d:.0f}) 睡眠={sleep_h:.1f}h RHR={rhr:.0f}bpm")
    return {"weight":weight,"ftp":ftp,"tp_sec":tp_sec,"css":css,
            "ctl":ctl,"atl":atl,"form":ctl-atl,
            "hrv":hrv,"hrv_7d_avg":hrv_7d,
            "sleep_h":sleep_h,"rhr":rhr,"rhr_avg":rhr_avg,
            "body_fat_pct":    body_fat_pct,
            "body_water_pct":  body_water_pct,
            "muscle_mass_kg":  muscle_mass_kg_well if muscle_mass_kg_well > 0 else None,
            "readiness":       readiness,
            "hydration_ml":    hydration_ml,
            "total_kcal_src":  total_kcal_src,
            "wellness_history":wellness,
            "past_results":past_results,
            "weekly_counts":weekly_counts,
            "_acts_90":acts_90,
            "_icu_base":base, "_api_key":api_key,
            "_ftp_src":ftp_src, "_tp_src":tp_src, "_css_src":css_src}


# ============================================================
# HRVスコアリング
# ============================================================
def calc_hrv_score(athlete, hrv_cfg):
    score=5.0
    hrv=athlete["hrv"]; hrv_base=athlete["hrv_7d_avg"]
    sleep=athlete["sleep_h"]; rhr=athlete["rhr"]; rhr_avg=athlete["rhr_avg"]; form=athlete["form"]
    drop_pct=0.0
    if hrv_base>0:
        drop_pct=(hrv_base-hrv)/hrv_base*100
        if drop_pct>=hrv_cfg.get("hrv_drop_severe_pct",20): score-=3.0
        elif drop_pct>=hrv_cfg.get("hrv_drop_moderate_pct",10): score-=1.5
        elif drop_pct<=-hrv_cfg.get("hrv_drop_moderate_pct",10): score+=1.0
    if hrv<hrv_cfg.get("hrv_crash_threshold",60): score-=2.0
    elif hrv<hrv_cfg.get("hrv_alert_threshold",75): score-=1.0
    elif hrv>hrv_base*1.05: score+=0.5
    if sleep>=hrv_cfg.get("sleep_good_hours",7.0): score+=1.0
    elif sleep<hrv_cfg.get("sleep_terrible_hours",5.0): score-=2.0
    elif sleep<hrv_cfg.get("sleep_poor_hours",6.0): score-=1.0
    rhr_diff=rhr-rhr_avg
    if rhr_diff>=hrv_cfg.get("rhr_elevated_bpm",5)*2: score-=1.5
    elif rhr_diff>=hrv_cfg.get("rhr_elevated_bpm",5): score-=0.8
    if form>=hrv_cfg.get("form_fresh_threshold",5): score+=1.5
    elif form<hrv_cfg.get("form_overreach_threshold",-20): score-=2.0
    elif form<-10: score-=1.0
    score=max(0,min(10,score))
    if   score>=7.5: cond="peak"
    elif score>=6.0: cond="good"
    elif score>=4.0: cond="normal"
    elif score>=2.5: cond="fatigued"
    else:            cond="depleted"
    reasons=[]
    if drop_pct>=hrv_cfg.get("hrv_drop_severe_pct",20):
        reasons.append(f"HRV急低下 {drop_pct:.0f}% ({hrv_base:.0f}→{hrv:.0f})")
    elif drop_pct>=hrv_cfg.get("hrv_drop_moderate_pct",10):
        reasons.append(f"HRVやや低下 {drop_pct:.0f}%")
    if sleep<hrv_cfg.get("sleep_poor_hours",6.0): reasons.append(f"睡眠少 {sleep:.1f}h")
    if rhr_diff>=hrv_cfg.get("rhr_elevated_bpm",5): reasons.append(f"RHR上昇 +{rhr_diff:.0f}bpm")
    if form<hrv_cfg.get("form_overreach_threshold",-20): reasons.append(f"オーバーリーチ Form={form:.1f}")
    if form>=hrv_cfg.get("form_fresh_threshold",5): reasons.append(f"フレッシュ Form=+{form:.1f}")
    return {"score":round(score,1),"condition":cond,"reasons":reasons}


# ============================================================
# 過去リザルト & ライバル → ゴールペース計算
# ============================================================

# RACE_DISTANCE_DEFS は calendar_parser.py に定義される
# ここでは遅延インポートで対応
def _get_race_distance_defs():
    from .calendar_parser import RACE_DISTANCE_DEFS
    return RACE_DISTANCE_DEFS


def run_pace_zones(tp_sec):
    """
    intervals.icu のRunペースゾーンをTPから動的計算して返す。
    Friel式ランペースゾーン（intervals.icuのデフォルト設定準拠）:
      Z1 Recovery :  > TP×1.29
      Z2 Aerobic  : TP×1.14 〜 TP×1.29
      Z3 Tempo    : TP×1.06 〜 TP×1.13
      Z4 Threshold: TP×0.99 〜 TP×1.05
      Z5 VO2Max   : TP×0.90 〜 TP×0.98
      Z6 Anaerobic:   < TP×0.89
    Returns: dict {zone_num: {"lo_sec", "hi_sec", "lo", "hi", "label"}}
    """
    tp = float(tp_sec)
    raw = {
        1: (tp * 1.29,  9999),
        2: (tp * 1.14,  tp * 1.29),
        3: (tp * 1.06,  tp * 1.13),
        4: (tp * 0.99,  tp * 1.05),
        5: (tp * 0.90,  tp * 0.98),
        6: (0,          tp * 0.89),
    }
    result = {}
    for z, (lo, hi) in raw.items():
        lo_s = int(lo)
        hi_s = int(hi) if hi < 9000 else None
        lo_str = _fmt_pace(lo_s)
        hi_str = _fmt_pace(hi_s) if hi_s else "none"
        result[z] = {
            "lo_sec": lo_s,
            "hi_sec": hi_s,
            "lo":     lo_str,
            "hi":     hi_str,
            "label":  f"{hi_str}〜{lo_str}/km" if hi_s else f">{lo_str}/km",
        }
    return result


def merge_manual_results(past_results, cfg):
    """config.yaml の manual_results を past_results にマージ"""
    manual = cfg.get("manual_results") or []
    existing_keys = {(r["date"], r["name"]) for r in past_results}
    for m in manual:
        key = (m.get("date",""), m.get("name",""))
        if key in existing_keys:
            continue
        t_sec = m.get("finish_time_sec")
        t_str = m.get("finish_time","")
        if not t_sec and t_str:
            # "H:MM:SS" or "MM:SS" → 秒
            parts = t_str.split(":")
            try:
                if len(parts)==3: t_sec=int(parts[0])*3600+int(parts[1])*60+int(parts[2])
                elif len(parts)==2: t_sec=int(parts[0])*60+int(parts[1])
            except: pass
        if not t_sec: continue
        past_results.append({
            "date":     m.get("date",""),
            "name":     m.get("name",""),
            "sport":    m.get("type","race"),
            "dist_m":   0,
            "time_s":   t_sec,
            "pace_s":   0,
            "pace_str": "",
            "time_str": _fmt_time(t_sec),
            "source":   "manual",
            "distance": m.get("distance",""),
            "notes":    m.get("notes",""),
        })
    return sorted(past_results, key=lambda x: x["date"], reverse=True)


def search_icu_race_by_name(athlete, race_name, race_date_str):
    """
    カレンダーの説明欄に「レース結果から自動取得」と書いてある場合、
    レース名でIntervals.icuを検索してリザルトを取得する
    """
    base    = athlete.get("_icu_base","")
    api_key = athlete.get("_api_key","")
    if not base or not api_key:
        return None
    # レース日前後2週間を検索
    try:
        rd   = datetime.fromisoformat(race_date_str)
        old  = (rd - timedelta(days=14)).strftime("%Y-%m-%d")
        new  = (rd + timedelta(days=1)).strftime("%Y-%m-%d")
    except:
        return None
    acts = icu_get(f"{base}/activities", api_key,
                   {"oldest":old,"newest":new}) or []
    # 名前の類似度で一致を探す
    race_lower = race_name.lower()
    best = None
    for a in acts:
        name_l = (a.get("name") or "").lower()
        # 部分一致 or 共通語チェック
        tokens = [t for t in re.split(r'[\s　\-_/]', race_lower) if len(t)>=2]
        if any(t in name_l for t in tokens) or race_lower in name_l:
            move = float(a.get("moving_time") or 0)
            if move > 600:
                if best is None or move > float(best.get("moving_time",0)):
                    best = a
    if best:
        move  = float(best.get("moving_time",0))
        dist  = float(best.get("distance") or 0)
        pace  = move/(dist/1000) if dist>0 else 0
        return {
            "date":     best.get("start_date_local","")[:10],
            "name":     best.get("name",""),
            "sport":    best.get("type",""),
            "dist_m":   dist,
            "time_s":   move,
            "pace_s":   pace,
            "pace_str": _fmt_pace(int(pace)) if pace>0 else "",
            "time_str": _fmt_time(int(move)),
            "source":   "intervals_icu_search",
        }
    return None


def _match_race_type(sport_str, rtype):
    s = (sport_str or "").lower()
    if rtype == "triathlon": return "tri" in s
    if rtype == "marathon":  return "run" in s or "marathon" in s
    if rtype == "cycling":   return "ride" in s or "cycling" in s
    return False


def _match_race_dist(dist_str, rdist):
    d = (dist_str or "").lower()
    return rdist.lower() in d or d in rdist.lower()


def _calc_targets_from_goal(rtype, rdist, goal_sec, ftp, tp_sec):
    """ゴール秒数からトレーニング用ターゲットを計算"""
    if rtype == "triathlon":
        dist_map = {"sprint":{"swim":750,"bike":20,"run":5},
                    "olympic":{"swim":1500,"bike":40,"run":10},
                    "middle":{"swim":1900,"bike":90,"run":21.1},
                    "iron":{"swim":3800,"bike":180,"run":42.2},
                    "half":{"swim":1900,"bike":90,"run":21.1}}  # halfはmiddleの別名
        # RACE_DISTANCE_DEFS も参照（km→m変換）
        RACE_DISTANCE_DEFS = _get_race_distance_defs()
        _rdd = RACE_DISTANCE_DEFS.get(rdist)
        if _rdd and _rdd.get("swim",0)+_rdd.get("bike",0)+_rdd.get("run",0)>0:
            d = {"swim": int(_rdd["swim"]*1000), "bike": _rdd["bike"], "run": _rdd["run"]}
        else:
            d = dist_map.get(rdist, dist_map["olympic"])
        bp_map = {"sprint":0.88,"olympic":0.82,"half":0.76,"iron":0.70}
        rp_map = {"sprint":0.93,"olympic":0.88,"half":0.83,"iron":0.75}
        bp = bp_map.get(rdist, 0.82)
        rp = rp_map.get(rdist, 0.88)
        # ゴールタイムがあれば逆算でパワー・ペースを調整
        if goal_sec:
            # 簡易分配: swim20%, bike50%, run30%（トライアスロン目安）
            run_sec   = goal_sec * 0.30
            run_pace  = int(run_sec / d["run"] * 1000 / 1000) if d["run"] else int(tp_sec/rp)
            bike_w    = int(ftp * bp)
        else:
            run_pace  = int(tp_sec / rp)
            bike_w    = int(ftp * bp)
        return {
            "race_run_pace": run_pace,
            "race_bike_w":   bike_w,
            "train_run_tp":  int(run_pace * 0.95),   # TP練習はレースペース×0.95
            "train_bike_ftp": int(bike_w * 1.03),    # バイク練習はレースパワー×103%
            "bike_pct": bp, "run_pct": rp,
        }
    elif rtype == "marathon":
        rp = {"full":0.92,"half":0.96}.get(rdist,0.92)
        if goal_sec:
            dist_m = {"full":42195,"half":21098}.get(rdist,42195)
            pace = goal_sec / (dist_m/1000)
        else:
            pace = int(tp_sec/rp)
        return {"race_run_pace": int(pace), "train_run_tp": int(pace*0.95)}
    return {}


def calc_goal_targets(race_info, athlete, cfg):
    """
    レース情報に対して目標ペース/パワーを計算
    優先順位:
      1) 本人の過去タイム（Intervals.icu + manual_results） × 0.97
      2) カレンダー説明欄の「目標:○○」で指定したライバルのタイム
      3) config.yaml の rivals セクション
      4) フィジカルから推定
    """
    race     = race_info.get("race") or {}
    rtype    = race.get("type","triathlon")
    rdist    = race.get("distance","olympic")
    ftp      = athlete["ftp"]
    tp_sec   = athlete["tp_sec"]
    race_name= race.get("name","")

    # manual_results をマージ（まだマージされていなければ）
    all_results = merge_manual_results(
        list(athlete.get("past_results",[])), cfg)

    # 本人の過去タイム — 種目・距離で絞り込み
    best_result = None
    for r in all_results:
        # 種目マッチ
        if not _match_race_type(r["sport"], rtype):
            continue
        # 距離マッチ（manual_resultsのdistanceフィールドも見る）
        rdist_match = (rdist == "unknown" or
                       _match_race_dist(r.get("distance",""), rdist) or
                       _match_race_dist(r.get("name",""), rdist))
        if not rdist_match:
            continue
        if best_result is None or r["time_s"] < best_result["time_s"]:
            best_result = r

    # ライバル: カレンダー説明欄からの override を優先
    cal_rival_name = race.get("rival")   # parse_gcal_day で抽出済み
    rival_time = None
    rival_name = None
    rival_notes= ""

    rivals_cfg = cfg.get("rivals") or {}

    if cal_rival_name:
        # カレンダー説明欄の「目標:○○」→ config.yaml の rivals から名前検索
        for rv_key, rv_data in rivals_cfg.items():
            rv_n = rv_data.get("name","")
            if cal_rival_name in rv_n or rv_n in cal_rival_name or cal_rival_name == rv_key:
                rival_time  = rv_data.get("finish_time_sec")
                rival_name  = rv_n or cal_rival_name
                rival_notes = rv_data.get("notes","")
                break
        if not rival_time:
            # 名前だけ記録（タイム未登録の場合）
            rival_name = cal_rival_name

    if not rival_time:
        # config.yaml の rivals から種目・距離マッチで最初のものを使用
        for rv_key, rv_data in rivals_cfg.items():
            rt = rv_data.get("race_type","")
            rd = rv_data.get("distance","")
            if (_match_race_type(rt or "triathlon", rtype) and
                    _match_race_dist(rd, rdist)):
                rival_time  = rv_data.get("finish_time_sec")
                rival_name  = rv_data.get("name", rv_key)
                rival_notes = rv_data.get("notes","")
                break

    # ゴールタイム決定
    if best_result:
        past_goal = int(best_result["time_s"] * 0.97)
        if rival_time and rival_time < past_goal:
            # ライバルが前回-3%より速い → ライバルを目標に
            goal_sec = rival_time
            goal_src = (f"ライバル目標: {rival_name} {_fmt_time(rival_time)}"
                        f"  (前回{best_result['time_str']}の-3%={_fmt_time(past_goal)}より速い)")
        else:
            goal_sec = past_goal
            goal_src = (f"本人ベスト({best_result['date']}) {best_result['time_str']} → -3%目標"
                        f"  [出典: {best_result.get('source','icu')}]")
            if rival_name:
                goal_src += f"  / 参考ライバル: {rival_name}"
                if rival_notes: goal_src += f"({rival_notes})"
    elif rival_time:
        # 自分の出場実績なし → ライバルタイムをそのまま目標（0%減）
        goal_sec = rival_time
        goal_src = (f"ライバル目標: {rival_name} {_fmt_time(rival_time)}"
                    f"  (自分の出場実績なし → 同タイムを目標)")
        if rival_notes: goal_src += f"  ({rival_notes})"
    else:
        goal_sec = None
        goal_src = "フィジカルから推定（過去リザルト・ライバル未設定）"

    targets = _calc_targets_from_goal(rtype, rdist, goal_sec, ftp, tp_sec)

    return {
        "best_result":    best_result,
        "rival_name":     rival_name,
        "rival_time_sec": rival_time,
        "rival_notes":    rival_notes,
        "goal_time_sec":  goal_sec,
        "goal_src":       goal_src,
        "targets":        targets,
    }
