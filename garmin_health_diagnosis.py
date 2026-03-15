"""
garmin_health_diagnosis.py — Garmin Connect ヘルスデータ身体診断モジュール
===========================================================================
Garmin Connect APIから取得できる全ヘルスデータを統合し、
現在の身体状態を多次元で診断してトレーニング強度推奨を返す。

■ 取得・分析するGarminデータ
  - Body Battery (エネルギー残量 0-100)
  - HRV Status (hrv5, hrv7d, hrvBaseline — Garmin Firstbeat Analytics)
  - 睡眠スコア / 睡眠ステージ / 睡眠時間 / 体動スコア
  - 安静時心拍数 (RHR) / 平均心拍数
  - ストレス (平均ストレス / 最大ストレス / ストレス持続時間)
  - 直近アクティビティ (強度・消費カロリー・TSS推定)
  - 体重変動 (グリコーゲン枯渇推定に使用)
  - SpO2 (血中酸素飽和度)
  - 水分摂取量 (hydrationMl)
  - 呼吸数 (respiration)
  - 歩数 / 活動カロリー / 座位時間

■ 診断アルゴリズムの根拠
  Body Battery:
    Garmin Firstbeat Analytics (proprietary): HRV + 活動 + 睡眠の統合スコア

  グリコーゲン枯渇推定 (体重ベース):
    Schytz et al. 2023 (Scand J Med Sci Sports): 1g glycogen ≒ 3-4g水を結合
    → 体重が通常比 -0.5kg以上の減少 ≒ グリコーゲン+水分 150-200g以上消費
    → 推定枯渇率 = max(0, (baseline_weight - current_weight) / 0.45 * 0.5)
    ただし発汗・排泄・食事による体重変動も含むため上限50%とする

  HRV guided training:
    Carrasco-Poyatos et al. 2025 (Scientific Reports): vmHRV+WB+RHR多変数ガイド
    Kubios HRV 2025: HRVが個人ベースライン比-10%以下→強度下げる
    Altini & Amft 2016 (HRV4Training): RMSSD contextual interpretation

  睡眠と回復:
    Tanaka et al. 2014 (J Phys Ther Sci): 睡眠スコア < 60 → 自律神経回復不十分
    Morgan et al. 2021 (Front Psychol): 深い睡眠(NREM stage3)が筋回復を担う

  ストレスと強度:
    Garmin Firstbeat: stress score 0-25=low, 26-50=medium, 51-75=high, 76-100=very high
    Wiese et al. 2019 (Int J Environ Res): 慢性ストレスは持久力パフォーマンスを低下

  直前セッション影響:
    Glycogen depletion meta-analysis (PMC6019055 2018):
      高強度90分以上 → グリコーゲン40-60%消費
      中強度60分 → グリコーゲン20-35%消費
      低強度 → 主として脂肪利用 グリコーゲン消費10-15%
    Robson-Ansley et al. 2011 (Eur J Appl Physiol):
      筋損傷マーカー(CK)はHIIT後24-48h、長距離後48-72hでピーク

  Garmin Training Readiness:
    Garmin proprietary score (1-100):
      1-24: Poor, 25-49: Low, 50-74: Moderate, 75-94: High, 95-100: Prime
    複数指標の統合スコアとして利用

使い方:
    from garmin_health_diagnosis import fetch_garmin_health, diagnose_body_state

    # Garminデータ取得
    health = fetch_garmin_health(email, password)

    # 診断実行
    diagnosis = diagnose_body_state(health)

    # 診断結果を表示
    print_diagnosis(diagnosis)

    # トレーニング強度修正係数を取得 (-2〜+2 の整数)
    modifier = diagnosis["intensity_modifier"]
"""

import json
from datetime import datetime, timedelta, date
from pathlib import Path

# ============================================================
# Garmin Connect APIラッパー (garminconnect ライブラリ使用)
# ============================================================

def _init_garmin(email: str, password: str):
    """Garmin Connect認証。トークンキャッシュを使用"""
    try:
        from garminconnect import Garmin
    except ImportError:
        raise RuntimeError(
            "garminconnect が未インストールです。\n"
            "  pip install garminconnect\n"
            "を実行してください。"
        )
    token_dir = Path.home() / ".garminconnect"
    token_str = str(token_dir)   # garminconnect は str を要求 (WindowsPath 不可)

    # トークンキャッシュで試みる
    if token_dir.exists():
        try:
            garmin = Garmin()
            garmin.login(token_str)
            return garmin
        except Exception:
            pass
        # キャッシュ破損 → 削除
        try:
            import shutil
            shutil.rmtree(token_dir)
        except Exception:
            pass

    # トークンなし → 対話的セットアップが必要
    raise RuntimeError(
        "Garmin Connect トークンが未設定です。\n"
        "以下を VS Code ターミナルで一度だけ実行してください:\n\n"
        "  python garmin_token_setup.py\n\n"
        "MFAコードの入力が求められたら Garmin メールに届いたコードを入力してください。\n"
        "セットアップ後は自動ログインが有効になります。"
    )


def _safe(d: dict, *keys, default=None):
    """ネストしたdictのsafe getter"""
    v = d
    for k in keys:
        if not isinstance(v, dict):
            return default
        v = v.get(k, default)
        if v is None:
            return default
    return v


def fetch_garmin_health(email: str, password: str, days_back: int = 7) -> dict:
    """
    Garmin Connectから全ヘルス指標を取得して辞書にまとめる。

    Returns:
        {
          "today": {...},        # 今日の各指標
          "history": [...],      # 過去 days_back 日分の履歴
          "activities": [...],   # 直近アクティビティ (7日)
          "fetch_errors": [...], # 取得失敗した項目
        }
    """
    garmin = _init_garmin(email, password)
    today_str = date.today().isoformat()
    fetch_errors = []

    # ── 今日のデータ取得 ─────────────────────────────────────────
    def _get(label, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            fetch_errors.append(f"{label}: {e}")
            return None

    stats     = _get("stats",      garmin.get_stats,             today_str)
    body_comp = _get("body_comp",  garmin.get_body_composition,  today_str)
    sleep     = _get("sleep",      garmin.get_sleep_data,        today_str)
    hrv       = _get("hrv",        garmin.get_hrv_data,          today_str)
    stress    = _get("stress",     garmin.get_stress_data,       today_str)
    hydration = _get("hydration",  garmin.get_hydration_data,    today_str)
    spo2      = _get("spo2",       garmin.get_spo2_data,         today_str)
    resp      = _get("respiration",garmin.get_respiration_data,  today_str)
    readiness = _get("readiness",  garmin.get_training_readiness,today_str)
    rhr       = _get("rhr",        garmin.get_rhr_day,           today_str)
    bb        = _get("body_battery",garmin.get_body_battery,     today_str, today_str)

    # ── 直近 days_back 日の体重履歴 ─────────────────────────────
    ago = (date.today() - timedelta(days=days_back)).isoformat()
    body_history = _get("body_history", garmin.get_body_composition, ago, today_str)

    # ── 直近アクティビティ ──────────────────────────────────────
    activities = _get("activities", garmin.get_activities, 0, 15) or []

    # ── 睡眠スコア履歴 ─────────────────────────────────────────
    sleep_history = []
    for i in range(1, days_back + 1):
        d = (date.today() - timedelta(days=i)).isoformat()
        s = _get(f"sleep_{d}", garmin.get_sleep_data, d)
        if s:
            sleep_history.append({"date": d, "data": s})

    today_data = {
        "stats":        stats,
        "body_comp":    body_comp,
        "sleep":        sleep,
        "hrv":          hrv,
        "stress":       stress,
        "hydration":    hydration,
        "spo2":         spo2,
        "respiration":  resp,
        "readiness":    readiness,
        "rhr":          rhr,
        "body_battery": bb,
    }

    return {
        "today":         today_data,
        "body_history":  body_history,
        "sleep_history": sleep_history,
        "activities":    activities,
        "fetch_errors":  fetch_errors,
        "fetched_at":    today_str,
    }


# ============================================================
# Garminデータパーサー群
# ============================================================

def _parse_body_battery(bb_data) -> dict:
    """Body Battery の現在値・最低値・最高値を抽出"""
    result = {"current": None, "min_today": None, "max_today": None}
    if not bb_data:
        return result
    # リスト形式: [{"startTimestampLocal": ..., "endTimestampLocal": ..., "bodyBatteryValuesArray": [[ms, val], ...]}]
    if isinstance(bb_data, list) and bb_data:
        all_vals = []
        for seg in bb_data:
            arr = seg.get("bodyBatteryValuesArray") or seg.get("bodyBatteryValues") or []
            for item in arr:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    v = item[1]
                else:
                    v = item.get("value") if isinstance(item, dict) else None
                if v is not None:
                    try:
                        all_vals.append(float(v))
                    except (TypeError, ValueError):
                        pass
        if all_vals:
            result["current"]   = all_vals[-1]
            result["min_today"] = min(all_vals)
            result["max_today"] = max(all_vals)
    elif isinstance(bb_data, dict):
        result["current"] = bb_data.get("bodyBatteryLevel") or bb_data.get("bodyBattery")
    return result


def _parse_sleep(sleep_data) -> dict:
    """睡眠データから主要指標を抽出"""
    result = {
        "score": None,        # 0-100
        "total_h": None,      # 総睡眠時間(時)
        "deep_pct": None,     # 深睡眠割合 (0-1)
        "rem_pct": None,      # REM割合 (0-1)
        "light_pct": None,
        "awake_min": None,    # 覚醒時間(分)
        "sleep_quality": None # "excellent"/"good"/"fair"/"poor"
    }
    if not sleep_data:
        return result
    # dailySleepDTO 形式
    dto = (sleep_data.get("dailySleepDTO") or
           sleep_data.get("sleepDTO") or sleep_data)
    if isinstance(dto, list) and dto:
        dto = dto[0]
    if not isinstance(dto, dict):
        return result

    score = dto.get("sleepScores", {})
    if isinstance(score, dict):
        result["score"] = score.get("totalDuration") or score.get("overallScore") or dto.get("sleepScore")
    else:
        result["score"] = dto.get("sleepScore") or dto.get("averageSpO2Value")

    total_s = dto.get("sleepTimeSeconds") or dto.get("sleptDuration") or 0
    result["total_h"] = total_s / 3600 if total_s else None

    deep_s  = dto.get("deepSleepSeconds") or dto.get("deepSleepDuration") or 0
    rem_s   = dto.get("remSleepSeconds")  or dto.get("remSleepDuration")  or 0
    light_s = dto.get("lightSleepSeconds")or dto.get("lightSleepDuration")or 0
    awake_s = dto.get("awakeSleepSeconds")or dto.get("awakeDuration")     or 0

    if total_s > 0:
        result["deep_pct"]  = deep_s  / total_s
        result["rem_pct"]   = rem_s   / total_s
        result["light_pct"] = light_s / total_s
        result["awake_min"] = awake_s / 60

    # スコアから品質ラベル
    sc = result["score"]
    if sc is not None:
        if   sc >= 80: result["sleep_quality"] = "excellent"
        elif sc >= 65: result["sleep_quality"] = "good"
        elif sc >= 50: result["sleep_quality"] = "fair"
        else:          result["sleep_quality"] = "poor"
    return result


def _parse_hrv(hrv_data) -> dict:
    """HRVデータを抽出 (Garmin HRV Status)"""
    result = {
        "last_night_5min": None,  # 直近夜のHRV (ms, Garmin 5min)
        "weekly_avg": None,       # 7日平均
        "baseline_low": None,     # 個人ベースライン下限
        "baseline_high": None,    # 個人ベースライン上限
        "status": None,           # "balanced"/"unbalanced"/"poor"/"low"
    }
    if not hrv_data:
        return result
    # Garmin HRV data structure
    hd = hrv_data if isinstance(hrv_data, dict) else {}
    # weeklyAvg / baseline / lastNight5MinHigh
    result["last_night_5min"] = (hd.get("lastNight5MinHigh") or
                                 hd.get("hrvValue") or
                                 hd.get("lastNight"))
    result["weekly_avg"]      = hd.get("weeklyAvg") or hd.get("hrv7dAvg")
    bl = hd.get("baseline") or {}
    if isinstance(bl, dict):
        result["baseline_low"]  = bl.get("balancedLow") or bl.get("low")
        result["baseline_high"] = bl.get("balancedHigh") or bl.get("high")
    result["status"] = hd.get("hrvStatus") or hd.get("status")
    return result


def _parse_stress(stress_data) -> dict:
    """ストレスデータから主要指標を抽出"""
    result = {
        "avg": None,          # 平均ストレス (0-100)
        "max": None,          # 最大ストレス
        "high_stress_min": None,  # 高ストレス時間(分)
        "rest_min": None,     # 安静時間(分)
        "level": None,        # "low"/"medium"/"high"/"very_high"
    }
    if not stress_data:
        return result
    sd = stress_data if isinstance(stress_data, dict) else {}
    result["avg"] = sd.get("avgStressLevel") or sd.get("averageStressLevel")
    result["max"] = sd.get("maxStressLevel")
    result["high_stress_min"] = (sd.get("highStressDuration") or
                                 sd.get("highStressSecs",0)) / 60
    result["rest_min"] = (sd.get("restStressDuration") or
                          sd.get("restStressSecs",0)) / 60
    avg = result["avg"]
    if avg is not None:
        if   avg <= 25: result["level"] = "low"
        elif avg <= 50: result["level"] = "medium"
        elif avg <= 75: result["level"] = "high"
        else:           result["level"] = "very_high"
    return result


def _parse_stats(stats_data) -> dict:
    """日次統計から主要指標を抽出"""
    result = {
        "rhr": None,              # 安静時心拍
        "calories_active": None,  # 活動カロリー
        "calories_bmr": None,     # 基礎代謝
        "steps": None,
        "intensity_min": None,    # 中高強度活動時間(分)
        "sedentary_min": None,    # 座位時間(分)
        "spo2_avg": None,
        "respiration_avg": None,
    }
    if not stats_data:
        return result
    sd = stats_data if isinstance(stats_data, dict) else {}
    result["rhr"]            = sd.get("restingHeartRate") or sd.get("minHeartRate")
    result["calories_active"]= sd.get("activeKilocalories") or sd.get("activeCalories")
    result["calories_bmr"]   = sd.get("bmrKilocalories") or sd.get("bmrCalories")
    result["steps"]          = sd.get("totalSteps") or sd.get("steps")
    result["intensity_min"]  = sd.get("intensityMinutes") or sd.get("moderateIntensityMinutes",0)
    result["sedentary_min"]  = sd.get("sedentarySeconds",0) / 60
    result["spo2_avg"]       = sd.get("averageSpO2") or sd.get("avgSpO2")
    result["respiration_avg"]= sd.get("averageBreathingDepth") or sd.get("avgRespirationRate")
    return result


def _parse_body_comp(body_comp_data) -> dict:
    """
    Garmin get_body_composition() レスポンスから体組成を抽出。
    Returns: {"body_water_pct": float|None, "muscle_mass_kg": float|None,
              "bone_mass_kg": float|None, "weight_kg": float|None,
              "body_fat_pct": float|None}
    """
    result = {"body_water_pct": None, "muscle_mass_kg": None,
              "bone_mass_kg": None, "weight_kg": None, "body_fat_pct": None}
    if not body_comp_data:
        return result
    # dateWeightList 形式 (複数日) → 最新エントリを使用
    entries = (body_comp_data.get("dateWeightList") or
               body_comp_data.get("compositionList") or [])
    if entries and isinstance(entries, list):
        entry = entries[-1] if isinstance(entries[-1], dict) else {}
    elif isinstance(body_comp_data, dict):
        entry = body_comp_data
    else:
        return result
    # 体内水分量% (Garmin API: bodyWaterPercentage)
    bw = (entry.get("bodyWaterPercentage") or
          entry.get("bodyWater") or
          entry.get("waterPercentage"))
    result["body_water_pct"] = float(bw) if bw is not None else None
    # 体重 (grams → kg)
    wt = entry.get("weight") or entry.get("totalWeightKg")
    if wt:
        result["weight_kg"] = float(wt) / 1000 if float(wt) > 500 else float(wt)
    # 体脂肪%
    bf = entry.get("bodyFatPercentage") or entry.get("bodyFat")
    result["body_fat_pct"] = float(bf) if bf is not None else None
    # 骨格筋量 (grams → kg)
    mm = entry.get("muscleMassInGrams") or entry.get("muscleMass")
    if mm:
        result["muscle_mass_kg"] = float(mm) / 1000 if float(mm) > 500 else float(mm)
    # 骨量 (grams → kg)
    bm = entry.get("boneMassInGrams") or entry.get("boneMass")
    if bm:
        result["bone_mass_kg"] = float(bm) / 1000 if float(bm) > 500 else float(bm)
    return result


def _parse_hydration(hydration_data) -> dict:
    """水分摂取データ"""
    result = {"intake_ml": None, "goal_ml": None, "pct": None}
    if not hydration_data:
        return result
    hd = hydration_data if isinstance(hydration_data, dict) else {}
    result["intake_ml"] = hd.get("totalIntakeInMl") or hd.get("valueInML")
    result["goal_ml"]   = hd.get("dailyIntakeGoalInMl") or hd.get("goalInML")
    if result["intake_ml"] and result["goal_ml"]:
        result["pct"] = min(1.5, result["intake_ml"] / result["goal_ml"])
    return result


def _parse_readiness(readiness_data) -> dict:
    """Training Readiness スコア"""
    result = {"score": None, "level": None, "feedback": None}
    if not readiness_data:
        return result
    rd = readiness_data if isinstance(readiness_data, dict) else {}
    # リスト形式で返ってくる場合
    if isinstance(readiness_data, list) and readiness_data:
        rd = readiness_data[0] if isinstance(readiness_data[0], dict) else {}
    score = rd.get("score") or rd.get("trainingReadinessScore")
    result["score"] = score
    if score is not None:
        if   score >= 95: result["level"] = "prime"
        elif score >= 75: result["level"] = "high"
        elif score >= 50: result["level"] = "moderate"
        elif score >= 25: result["level"] = "low"
        else:             result["level"] = "poor"
    result["feedback"] = rd.get("primaryFactor") or rd.get("feedback")
    return result


def _parse_recent_activities(activities) -> list:
    """直近アクティビティの強度・消費カロリー・推定TSS"""
    parsed = []
    for act in (activities or [])[:10]:
        if not isinstance(act, dict):
            continue
        start_str = act.get("startTimeLocal") or act.get("startTime") or ""
        try:
            start_dt = datetime.strptime(start_str[:16], "%Y-%m-%d %H:%M")
        except Exception:
            try:
                start_dt = datetime.strptime(start_str[:16], "%Y-%m-%dT%H:%M")
            except Exception:
                start_dt = None

        act_type  = (act.get("activityType",{}).get("typeKey","")
                     if isinstance(act.get("activityType"),dict)
                     else act.get("activityType","")).lower()
        dur_s     = float(act.get("duration") or act.get("movingDuration") or 0)
        avg_hr    = float(act.get("averageHR") or act.get("averageHeartRate") or 0)
        calories  = float(act.get("calories") or 0)
        distance  = float(act.get("distance") or 0)
        avg_power = float(act.get("avgPower") or act.get("normalizedPower") or 0)

        # 強度ゾーン推定 (HR比ベース / パワー比ベース)
        intensity_label = _estimate_intensity(act_type, avg_hr, avg_power, dur_s)

        # TSS推定 (run/bike/swim別)
        tss = _estimate_tss(act_type, avg_hr, avg_power, dur_s, distance)

        # 直前セッション影響スコア (0-10)
        hours_ago = 0
        if start_dt:
            hours_ago = max(0, (datetime.now() - start_dt).total_seconds() / 3600)

        parsed.append({
            "name":       act.get("activityName") or act.get("name",""),
            "type":       act_type,
            "start":      start_str[:10],
            "hours_ago":  round(hours_ago, 1),
            "dur_min":    round(dur_s / 60, 1),
            "avg_hr":     avg_hr,
            "calories":   calories,
            "distance_m": distance,
            "avg_power":  avg_power,
            "intensity":  intensity_label,
            "tss":        tss,
        })
    return parsed


def _estimate_intensity(act_type: str, avg_hr: float, avg_power: float,
                        dur_s: float) -> str:
    """活動の強度ラベル推定"""
    # パワーベース (バイク)
    if "cycling" in act_type or "bike" in act_type:
        if avg_power > 0:
            # FTPを220Wと仮定してIF推定
            IF = avg_power / 220
            if   IF >= 0.95: return "hard"
            elif IF >= 0.76: return "moderate"
            elif IF >= 0.56: return "easy"
            else:             return "recovery"
    # HRベース (全種目共通フォールバック)
    if avg_hr > 0:
        # 最大心拍180を仮定
        pct = avg_hr / 180
        if   pct >= 0.90: return "hard"
        elif pct >= 0.80: return "moderate"
        elif pct >= 0.70: return "easy"
        else:              return "recovery"
    # 時間だけで推定
    dur_min = dur_s / 60
    if   dur_min >= 90:  return "moderate"
    elif dur_min >= 45:  return "easy"
    else:                return "recovery"


def _estimate_tss(act_type: str, avg_hr: float, avg_power: float,
                  dur_s: float, distance_m: float) -> float:
    """Training Stress Score 推定値"""
    dur_h = dur_s / 3600
    if dur_h <= 0:
        return 0
    # バイク: パワーベース TSS = (sec * NP * IF) / (FTP * 3600) * 100
    if ("cycling" in act_type or "bike" in act_type) and avg_power > 0:
        ftp_est = 220
        IF = avg_power / ftp_est
        return round((dur_s * avg_power * IF) / (ftp_est * 3600) * 100, 1)
    # ラン: HR-based TSS (hrTSS = dur_h * 100 * IF^2, IF = avg_hr / LTHR)
    if "run" in act_type and avg_hr > 0:
        lthr_est = 165
        IF = avg_hr / lthr_est
        return round(dur_h * 100 * IF ** 2, 1)
    # スイム: 距離ベース概算
    if "swim" in act_type and distance_m > 0:
        return round(distance_m / 1000 * 20, 1)  # 1km あたり約20TSS
    # 汎用フォールバック
    return round(dur_h * 50, 1)


# ============================================================
# グリコーゲン枯渇推定
# ============================================================

def estimate_glycogen_depletion(
    activities: list,
    body_history,
    athlete_weight_kg: float = 68.4,
    hours_window: float = 36.0,
) -> dict:
    """
    グリコーゲン枯渇度を複数の指標から推定する。

    アルゴリズム:
    1. 体重変動ベース (Schytz et al. 2023):
       glycogen_delta_kg = (baseline_weight - today_weight)
       1gグリコーゲン ≈ 3-4gの水を結合するため
       glycogen_depleted_g = glycogen_delta_kg * 1000 / 4 (水4倍仮定)
       全身グリコーゲン容量 ≈ 400-600g → 枯渇率 = glycogen_depleted_g / 500

    2. 直近セッション消費ベース (Glycogen metabolism review PMC5872716):
       hard 60min → ~200g (40% of 500g)
       moderate 60min → ~120g (24%)
       easy 60min → ~60g (12%)
       強度 × 時間で比例スケール、24h後回復50%、48h後90%回復

    3. 統合スコア (0-100, 0=完全回復、100=完全枯渇):
       両手法の加重平均 + 水分不足ペナルティ

    Returns:
        {
          "depletion_pct": float,      # 0-100 枯渇率(%)
          "depletion_level": str,       # "full"/"partial"/"mild"/"minimal"
          "weight_delta_kg": float,    # 体重変動
          "session_impact": float,     # 直近セッション影響(0-100)
          "recovery_hours_needed": int, # 完全回復推定時間
          "recommendations": [str],    # 補給推奨
        }
    """
    result = {
        "depletion_pct":         0.0,
        "depletion_level":       "minimal",
        "weight_delta_kg":       0.0,
        "session_impact":        0.0,
        "session_summary":       [],
        "recovery_hours_needed": 0,
        "recommendations":       [],
    }

    # ── 1) 体重変動ベース推定 ─────────────────────────────────
    weight_delta = 0.0
    weight_depletion_pct = 0.0
    if body_history and isinstance(body_history, dict):
        weights = []
        for key in ("dateWeightList", "bodyCompositionList", "totalAverage"):
            blist = body_history.get(key)
            if isinstance(blist, list):
                for item in blist:
                    w = (item.get("weight") or item.get("weightInGrams",0)/1000
                         if isinstance(item, dict) else None)
                    if w and 40 < w < 150:
                        weights.append(w)
        if len(weights) >= 2:
            baseline = sum(weights[-3:]) / len(weights[-3:]) if len(weights) >= 3 else weights[-1]
            today_w  = weights[0]
            weight_delta = baseline - today_w
            # 1gグリコーゲン ≈ 4gの水 → delta_kg * 1000 / 4 = 枯渇グリコーゲンg
            glycogen_lost_g  = max(0, weight_delta * 1000 / 4)
            glycogen_capacity = athlete_weight_kg * 7  # 体重kg × 7g が全身グリコーゲン容量の概算
            weight_depletion_pct = min(100, glycogen_lost_g / glycogen_capacity * 100)

    result["weight_delta_kg"] = round(weight_delta, 2)

    # ── 2) 直近セッション影響ベース推定 ─────────────────────────
    # 強度別グリコーゲン消費率 (per 60min, % of total stores)
    GLYCOGEN_RATE = {
        "hard":     40.0,  # 高強度60分 ≒ 40%消費 (PMC5872716)
        "moderate": 22.0,  # 中強度60分 ≒ 22%消費
        "easy":     10.0,  # 低強度60分 ≒ 10%消費
        "recovery":  5.0,  # リカバリー60分 ≒ 5%消費
    }
    # 回復率 (時間ごと): 最初24hで50%, 24-48hでさらに40%
    def _recovery_factor(hours_ago: float) -> float:
        if hours_ago >= 48:  return 0.05
        elif hours_ago >= 24: return 0.50 - (hours_ago - 24) / 24 * 0.45
        else:                 return 1.0  - hours_ago / 24 * 0.50

    session_total = 0.0
    session_summaries = []
    for act in activities:
        hours = act.get("hours_ago", 999)
        if hours > hours_window:
            continue
        intensity = act.get("intensity", "easy")
        dur_min   = act.get("dur_min", 0)
        rate      = GLYCOGEN_RATE.get(intensity, 10.0)
        consumed  = rate * (dur_min / 60)  # 60分換算
        recov     = _recovery_factor(hours)
        remaining = consumed * recov
        session_total += remaining
        session_summaries.append({
            "name":       act["name"][:20],
            "hours_ago":  hours,
            "intensity":  intensity,
            "dur_min":    dur_min,
            "consumed_pct": round(consumed, 1),
            "remaining_impact_pct": round(remaining, 1),
        })

    result["session_impact"]  = round(min(100, session_total), 1)
    result["session_summary"] = session_summaries

    # ── 3) 統合スコア ──────────────────────────────────────────
    # 体重ベース(信頼度低め: 発汗等のノイズあり) × 0.3
    # セッションベース × 0.7
    combined = weight_depletion_pct * 0.3 + session_total * 0.7
    combined = min(100, combined)
    result["depletion_pct"] = round(combined, 1)

    if   combined >= 60: result["depletion_level"] = "full"
    elif combined >= 35: result["depletion_level"] = "partial"
    elif combined >= 15: result["depletion_level"] = "mild"
    else:                result["depletion_level"] = "minimal"

    # ── 4) 回復推定時間 ─────────────────────────────────────────
    # 補給なし場合の完全回復: 非線形 (最初12hで50%回復, 12-24hでさらに35%)
    # → combined が大きいほど長い
    if   combined >= 60: result["recovery_hours_needed"] = 36
    elif combined >= 35: result["recovery_hours_needed"] = 24
    elif combined >= 15: result["recovery_hours_needed"] = 12
    else:                result["recovery_hours_needed"] = 0

    # ── 5) 補給推奨 ─────────────────────────────────────────────
    recs = []
    if combined >= 35:
        recs.append(
            f"🍚 グリコーゲン枯渇{combined:.0f}% → 今日の食事で炭水化物を増やす"
            f" (目安: 体重×6-8g/kg = {int(athlete_weight_kg*7)}g)"
        )
        recs.append("⏱ 30分以内に高GI炭水化物+タンパク質 (CHO:PRO=3:1) を摂取推奨")
    elif combined >= 15:
        recs.append(
            f"🍙 グリコーゲン軽度消費 → 次の食事で炭水化物を意識"
            f" (目安: {int(athlete_weight_kg*5)}g)"
        )
    if weight_delta > 0.5:
        water_deficit_ml = int(weight_delta * 1000)
        recs.append(f"💧 体重-{weight_delta:.1f}kg → 水分不足推定 {water_deficit_ml}mL の補給を")
    if not recs:
        recs.append("✅ グリコーゲン残量十分。通常の食事で問題なし。")
    result["recommendations"] = recs

    return result


# ============================================================
# 身体状態の多次元診断
# ============================================================

def diagnose_body_state(health_data: dict, athlete_weight_kg: float = 68.4) -> dict:
    """
    Garminヘルスデータ全体を統合して現在の身体状態を診断する。

    Returns:
        {
          "overall_score": float,      # 0-100 総合コンディションスコア
          "intensity_modifier": int,   # -2〜+2 (次のトレーニング強度調整)
          "readiness_level": str,      # "prime"/"high"/"moderate"/"low"/"poor"
          "body_battery": dict,        # Body Battery情報
          "sleep": dict,               # 睡眠情報
          "hrv": dict,                 # HRV情報
          "stress": dict,              # ストレス情報
          "glycogen": dict,            # グリコーゲン推定
          "hydration": dict,           # 水分情報
          "readiness": dict,           # Training Readiness
          "warnings": [str],           # 警告メッセージ
          "positives": [str],          # 良好サイン
          "training_recommendation": str, # 次のセッション推奨
          "nutrition_alert": [str],    # 栄養アドバイス
          "evidence_notes": [str],     # 根拠論文メモ
        }
    """
    today     = health_data.get("today", {})
    bb        = _parse_body_battery(today.get("body_battery"))
    sleep     = _parse_sleep(today.get("sleep"))
    hrv_info  = _parse_hrv(today.get("hrv"))
    stress    = _parse_stress(today.get("stress"))
    stats     = _parse_stats(today.get("stats") or today.get("rhr"))
    hydration = _parse_hydration(today.get("hydration"))
    readiness = _parse_readiness(today.get("readiness"))
    activities= _parse_recent_activities(health_data.get("activities", []))
    glycogen  = estimate_glycogen_depletion(
        activities, health_data.get("body_history"),
        athlete_weight_kg
    )

    warnings   = []
    positives  = []
    ev_notes   = []

    # ────────────────────────────────────────────────────────
    # 各指標のスコア化 (0-100)
    # ────────────────────────────────────────────────────────

    # 1) Body Battery スコア (0-100、直接値)
    bb_score = bb["current"] or 50
    if bb_score <= 20:
        warnings.append(f"⚡ Body Battery {bb_score}/100 — 非常に低い。高強度セッション非推奨")
        ev_notes.append("Garmin Firstbeat: Body Battery < 25 → 高強度はBody Battery回復を妨げる")
    elif bb_score <= 40:
        warnings.append(f"⚡ Body Battery {bb_score}/100 — 低め。中強度以下が推奨")
    elif bb_score >= 75:
        positives.append(f"⚡ Body Battery {bb_score}/100 — 高いエネルギー残量")

    # 2) 睡眠スコア (0-100 → 0-100)
    sleep_score = 50
    if sleep["score"] is not None:
        sleep_score = float(sleep["score"])
        if sleep_score < 50:
            warnings.append(f"😴 睡眠スコア {sleep_score:.0f}/100 — 回復不十分")
            ev_notes.append("Tanaka 2014: 睡眠スコア<60 → 副交感神経回復不十分 → 強度を下げる")
        elif sleep_score >= 75:
            positives.append(f"😴 睡眠スコア {sleep_score:.0f}/100 — 良質な回復")

    sleep_h = sleep.get("total_h") or 0
    if sleep_h > 0:
        if sleep_h < 6:
            warnings.append(f"😴 睡眠時間 {sleep_h:.1f}h — 不足 (推奨: 7-9h)")
        elif sleep_h >= 7.5:
            positives.append(f"😴 睡眠 {sleep_h:.1f}h — 十分な睡眠時間")

    # 3) HRV スコア
    hrv_score = 50
    hrv_val   = hrv_info.get("last_night_5min") or hrv_info.get("weekly_avg")
    hrv_base  = hrv_info.get("baseline_low")
    hrv_high  = hrv_info.get("baseline_high")
    if hrv_val and hrv_base:
        drop_pct = (hrv_base - hrv_val) / hrv_base * 100 if hrv_base > 0 else 0
        if drop_pct >= 20:
            hrv_score = 20
            warnings.append(f"💓 HRV急低下 -{drop_pct:.0f}% ({hrv_base:.0f}→{hrv_val:.0f}ms) — 強度を大幅低下")
            ev_notes.append("Kubios HRV 2025: HRV>ベースライン比-10%で強度低下推奨 (-20%以上は特に注意)")
        elif drop_pct >= 10:
            hrv_score = 35
            warnings.append(f"💓 HRV低下 -{drop_pct:.0f}% ({hrv_val:.0f}ms) — 中強度以下推奨")
        elif hrv_high and hrv_val >= hrv_high * 0.95:
            hrv_score = 90
            positives.append(f"💓 HRV高値 {hrv_val:.0f}ms — ベースライン上限付近。高強度OK")
        else:
            hrv_score = 70
    elif hrv_val:
        # ベースラインなし → 絶対値で判定
        if   hrv_val >= 80: hrv_score = 80
        elif hrv_val >= 60: hrv_score = 60
        elif hrv_val >= 40: hrv_score = 40
        else:                hrv_score = 25

    hrv_status = hrv_info.get("status")
    if hrv_status and hrv_status.lower() in ("unbalanced", "poor", "low"):
        warnings.append(f"💓 Garmin HRV Status: {hrv_status} — 回復セッション推奨")
        ev_notes.append("Carrasco-Poyatos 2025 (Sci Rep): HRV+WB+RHR多変数ガイドで最大パフォーマンス向上")

    # 4) ストレス スコア
    stress_score = 70
    stress_avg   = stress.get("avg")
    if stress_avg is not None:
        stress_score = max(0, 100 - stress_avg)
        if stress_avg > 60:
            warnings.append(f"🔥 平均ストレス {stress_avg:.0f}/100 — 高ストレス状態。自律神経に注意")
            ev_notes.append("Wiese 2019: 慢性高ストレスは持久力パフォーマンスを低下させる")
        elif stress_avg < 25:
            positives.append(f"🧘 平均ストレス {stress_avg:.0f}/100 — 低ストレス良好状態")

    # 5) グリコーゲン スコア
    glyco_pct   = glycogen["depletion_pct"]
    glyco_score = max(0, 100 - glyco_pct * 1.2)
    if glyco_pct >= 40:
        warnings.append(f"🍞 グリコーゲン枯渇推定 {glyco_pct:.0f}% — 高強度セッション前に補給必要")
        ev_notes.append("Glycogen metabolism PMC5872716: 高強度インターバル前のグリコーゲン不足はVO2maxを低下させる")
    elif glyco_pct >= 20:
        warnings.append(f"🍙 グリコーゲン軽度消費 {glyco_pct:.0f}% — 次食で炭水化物を意識")

    # 6) Hydration スコア
    hydration_score = 75
    h_pct = hydration.get("pct")
    if h_pct is not None:
        hydration_score = min(100, h_pct * 100)
        if h_pct < 0.6:
            warnings.append(f"💧 水分摂取 目標の{h_pct*100:.0f}% — 脱水注意。スポーツドリンクを")
        elif h_pct >= 0.9:
            positives.append(f"💧 水分摂取 {h_pct*100:.0f}% — 水分補給良好")

    # 7) Garmin Training Readiness
    readiness_score = 50
    rd_score = readiness.get("score")
    if rd_score is not None:
        readiness_score = float(rd_score)
        if readiness_score >= 75:
            positives.append(f"🎯 Garmin Training Readiness: {rd_score}/100 ({readiness['level']})")
        elif readiness_score <= 30:
            warnings.append(f"🎯 Garmin Training Readiness: {rd_score}/100 ({readiness['level']}) — 高強度非推奨")

    # ────────────────────────────────────────────────────────
    # 総合スコア (加重平均)
    # 根拠: 各指標の相対的重要度を反映した重み付け
    #   - Body Battery: Garmin推奨指標 (最重要)
    #   - HRV: 自律神経状態の直接指標
    #   - 睡眠: 回復の根幹
    #   - ストレス: HRVとの相関が高いが独立した情報も含む
    #   - グリコーゲン: パフォーマンスの燃料状態
    #   - 水分: 短期的に補正可能
    #   - Readiness: 複合スコアとして参考
    # ────────────────────────────────────────────────────────
    WEIGHTS = {
        "bb":         0.25,
        "sleep":      0.20,
        "hrv":        0.20,
        "stress":     0.15,
        "glycogen":   0.10,
        "hydration":  0.05,
        "readiness":  0.05,
    }
    overall = (
        bb_score        * WEIGHTS["bb"] +
        sleep_score     * WEIGHTS["sleep"] +
        hrv_score       * WEIGHTS["hrv"] +
        stress_score    * WEIGHTS["stress"] +
        glyco_score     * WEIGHTS["glycogen"] +
        hydration_score * WEIGHTS["hydration"] +
        readiness_score * WEIGHTS["readiness"]
    )
    overall = round(min(100, max(0, overall)), 1)

    # ────────────────────────────────────────────────────────
    # readiness_level と intensity_modifier の決定
    # ────────────────────────────────────────────────────────
    if   overall >= 82: readiness_level = "prime";    modifier = +2
    elif overall >= 68: readiness_level = "high";     modifier = +1
    elif overall >= 50: readiness_level = "moderate"; modifier =  0
    elif overall >= 35: readiness_level = "low";      modifier = -1
    else:               readiness_level = "poor";     modifier = -2

    # ウォーニング数によるmodifier追加調整
    critical_warnings = [w for w in warnings if any(
        k in w for k in ["急低下", "非常に低い", "非推奨", "枯渇推定 "]
    )]
    if len(critical_warnings) >= 2:
        modifier = max(-2, modifier - 1)

    # ────────────────────────────────────────────────────────
    # トレーニング推奨メッセージ
    # ────────────────────────────────────────────────────────
    RECOMMEND_MAP = {
        "prime":    ("🟢 PRIME — 高強度・長時間セッション最適。最高パフォーマンスが期待できます",
                     "+2 → 計画より1ランク強度UP可"),
        "high":     ("🟡 HIGH — 計画通りの強度で問題なし。良い適応が期待できます",
                     "+1 → 計画通りか1段階UP"),
        "moderate": ("🟠 MODERATE — 計画通りの強度で実施。回復を優先しつつ刺激を維持",
                     " 0 → 計画通りの強度"),
        "low":      ("🔴 LOW — 強度を1段階下げる推奨。疲労が蓄積しています",
                     "-1 → 計画より1段階DOWN"),
        "poor":     ("⛔ POOR — 高強度禁忌。回復 or 軽いアクティブリカバリーのみ",
                     "-2 → リカバリーセッションのみ"),
    }
    rec_msg, mod_msg = RECOMMEND_MAP.get(readiness_level, ("普通のセッション", "変更なし"))

    # 直前セッション情報の追加
    recent_hard = [a for a in activities if a["intensity"] in ("hard","moderate") and a["hours_ago"] < 24]
    if recent_hard:
        latest = recent_hard[0]
        rec_msg += (f"\n  📊 直前セッション: {latest['name'][:20]} "
                    f"({latest['intensity'].upper()} {latest['dur_min']:.0f}分, "
                    f"{latest['hours_ago']:.1f}h前, TSS推定{latest['tss']:.0f})")

    return {
        "overall_score":          overall,
        "intensity_modifier":     modifier,
        "readiness_level":        readiness_level,
        "body_battery":           {**bb, "score": bb_score},
        "sleep":                  {**sleep, "score": sleep_score},
        "hrv":                    {**hrv_info, "score": hrv_score},
        "stress":                 {**stress, "score": stress_score},
        "glycogen":               glycogen,
        "hydration":              {**hydration, "score": hydration_score},
        "readiness":              {**readiness, "score": readiness_score},
        "activities":             activities,
        "warnings":               warnings,
        "positives":              positives,
        "training_recommendation":f"{rec_msg}\n  {mod_msg}",
        "nutrition_alert":        glycogen["recommendations"],
        "evidence_notes":         ev_notes,
        "debug": {
            "bb_score":        bb_score,
            "sleep_score":     sleep_score,
            "hrv_score":       hrv_score,
            "stress_score":    stress_score,
            "glyco_score":     glyco_score,
            "hydration_score": hydration_score,
            "readiness_score": readiness_score,
        }
    }


# ============================================================
# 診断結果の表示
# ============================================================

def print_diagnosis(diag: dict) -> None:
    """診断結果をコンソールに表示"""
    overall  = diag["overall_score"]
    level    = diag["readiness_level"]
    modifier = diag["intensity_modifier"]

    LEVEL_ICONS = {
        "prime":    "🟢", "high": "🟡", "moderate": "🟠",
        "low": "🔴", "poor": "⛔"
    }
    icon = LEVEL_ICONS.get(level, "⚪")

    print(f"\n{'═'*64}")
    print(f"  🏥 Garmin ヘルス診断レポート")
    print(f"{'═'*64}")
    print(f"  総合コンディションスコア: {overall:.1f}/100  {icon} {level.upper()}")
    mod_str = f"+{modifier}" if modifier > 0 else str(modifier)
    print(f"  強度調整係数: {mod_str}  (計画強度に加算)")
    print(f"{'─'*64}")

    # Body Battery
    bb = diag["body_battery"]
    bb_curr = bb.get("current", "N/A")
    bb_max  = bb.get("max_today", "N/A")
    print(f"  ⚡ Body Battery   現在:{bb_curr} / 本日最高:{bb_max} / スコア:{bb['score']:.0f}")

    # 睡眠
    sl = diag["sleep"]
    sl_h   = f"{sl.get('total_h',0):.1f}h" if sl.get("total_h") else "N/A"
    sl_sc  = f"{sl.get('score',50):.0f}/100"
    sl_deep= f"深{sl.get('deep_pct',0)*100:.0f}%" if sl.get("deep_pct") else ""
    sl_rem = f"REM{sl.get('rem_pct',0)*100:.0f}%" if sl.get("rem_pct") else ""
    print(f"  😴 睡眠           {sl_h}  スコア:{sl_sc}  {sl_deep} {sl_rem}")

    # HRV
    hv = diag["hrv"]
    hv_val  = hv.get("last_night_5min") or hv.get("weekly_avg")
    hv_base = hv.get("baseline_low")
    hv_stat = hv.get("status") or "N/A"
    hv_str  = f"{hv_val:.0f}ms" if hv_val else "N/A"
    bl_str  = f"(base {hv_base:.0f})" if hv_base else ""
    print(f"  💓 HRV            {hv_str} {bl_str}  Status:{hv_stat}  スコア:{hv['score']:.0f}")

    # ストレス
    st = diag["stress"]
    st_avg = f"{st.get('avg',0):.0f}" if st.get("avg") is not None else "N/A"
    st_lvl = st.get("level") or "N/A"
    print(f"  🔥 ストレス        平均:{st_avg}/100  レベル:{st_lvl}  スコア:{st['score']:.0f}")

    # グリコーゲン
    gl = diag["glycogen"]
    gl_pct   = gl["depletion_pct"]
    gl_level = gl["depletion_level"]
    gl_w     = gl["weight_delta_kg"]
    gl_si    = gl["session_impact"]
    print(f"  🍞 グリコーゲン    枯渇推定:{gl_pct:.0f}%  レベル:{gl_level}")
    print(f"     体重変動:{gl_w:+.2f}kg  直近セッション影響:{gl_si:.0f}%")

    # 水分
    hyd = diag["hydration"]
    hyd_ml = f"{hyd.get('intake_ml',0):.0f}mL" if hyd.get("intake_ml") else "N/A"
    hyd_pct= f"{hyd.get('pct',0)*100:.0f}%" if hyd.get("pct") else "N/A"
    print(f"  💧 水分            摂取:{hyd_ml}  目標達成:{hyd_pct}")

    # Training Readiness
    rd = diag["readiness"]
    rd_sc = rd.get("score")
    rd_lv = rd.get("level") or "N/A"
    rd_str = f"{rd_sc:.0f}/100 ({rd_lv})" if rd_sc is not None else "N/A"
    print(f"  🎯 Training Readiness: {rd_str}")

    print(f"\n{'─'*64}")

    # 警告
    if diag["warnings"]:
        print(f"  ⚠️  警告:")
        for w in diag["warnings"]:
            print(f"    {w}")

    # 良好サイン
    if diag["positives"]:
        print(f"  ✅ 良好サイン:")
        for p in diag["positives"]:
            print(f"    {p}")

    print(f"\n  💡 次のトレーニング推奨:")
    for line in diag["training_recommendation"].split("\n"):
        print(f"    {line}")

    if diag["nutrition_alert"]:
        print(f"\n  🍽 栄養アドバイス:")
        for n in diag["nutrition_alert"]:
            print(f"    {n}")

    if diag["evidence_notes"]:
        print(f"\n  📚 科学的根拠:")
        for e in diag["evidence_notes"]:
            print(f"    {e}")

    # 直近セッション
    if diag["activities"]:
        print(f"\n  🏃 直近セッション:")
        for act in diag["activities"][:5]:
            hrs = f"{act['hours_ago']:.0f}h前"
            print(f"    {act['start']} {act['name'][:25]:25s} "
                  f"{act['intensity'].upper():10s} "
                  f"{act['dur_min']:.0f}min  TSS≈{act['tss']:.0f}  ({hrs})")

    print(f"{'═'*64}\n")


# ============================================================
# トレーニング計画への組み込み: intensity modifier 適用
# ============================================================

INTENSITY_ORDER = ["recovery", "easy", "moderate", "hard"]

def apply_garmin_modifier(plan: list, modifier: int, diagnosis: dict) -> list:
    """
    生成済みプランの強度をGarmin診断に基づいて調整する。

    Args:
        plan:      generate_days()の戻り値
        modifier:  diagnose_body_state()から返るintensity_modifier (-2〜+2)
        diagnosis: 診断結果辞書

    Returns:
        調整済みプラン (inplace変更)
    """
    if modifier == 0:
        return plan

    glyco_level = diagnosis["glycogen"]["depletion_level"]
    bb_current  = diagnosis["body_battery"].get("current", 50)

    for day in plan:
        sport     = day.get("sport", "rest")
        intensity = day.get("intensity", "easy")
        if sport in ("rest", "race", "yoga"):
            continue

        idx = INTENSITY_ORDER.index(intensity) if intensity in INTENSITY_ORDER else 1
        new_idx = max(0, min(len(INTENSITY_ORDER) - 1, idx + modifier))

        # 強制リカバリー条件
        if bb_current <= 20:
            new_idx = 0  # recovery強制
        elif glyco_level in ("full",) and intensity == "hard":
            new_idx = max(0, idx - 1)  # グリコーゲン完全枯渇時は hard 禁止

        day["intensity"] = INTENSITY_ORDER[new_idx]

        # 説明文への追記
        if new_idx != idx:
            direction = "↓DOWN" if new_idx < idx else "↑UP"
            orig = INTENSITY_ORDER[idx]
            new  = INTENSITY_ORDER[new_idx]
            day["garmin_adjustment"] = (
                f"Garmin診断による強度調整: {orig.upper()}→{new.upper()} ({direction})"
                f"\n  modifier={modifier:+d} / 総合スコア={diagnosis['overall_score']:.0f}/100"
            )

    return plan


def garmin_adjustment_summary(diagnosis: dict) -> str:
    """診断サマリーを1行テキストで返す（プラン表示用）"""
    overall  = diagnosis["overall_score"]
    level    = diagnosis["readiness_level"]
    modifier = diagnosis["intensity_modifier"]
    mod_str  = f"{modifier:+d}" if modifier != 0 else "±0"
    ICONS = {"prime":"🟢","high":"🟡","moderate":"🟠","low":"🔴","poor":"⛔"}
    icon = ICONS.get(level, "⚪")
    warns = len(diagnosis["warnings"])
    gl_pct = diagnosis["glycogen"]["depletion_pct"]
    bb_val = diagnosis["body_battery"].get("current", "?")
    return (
        f"{icon} Garmin診断: {overall:.0f}/100 ({level.upper()})  "
        f"強度{mod_str}  BB:{bb_val}  GL枯渇:{gl_pct:.0f}%  ⚠️×{warns}"
    )


# ============================================================
# スタンドアロン実行
# ============================================================

if __name__ == "__main__":
    import sys
    import getpass

    print("=" * 64)
    print("  🏥 Garmin ヘルス診断システム")
    print("=" * 64)
    print("  Garmin Connect の認証情報を入力してください")
    print("  ※ ~/.garminconnect にトークンが保存されていれば入力不要\n")

    email = input("  Email (Enterでスキップ): ").strip()
    if email:
        password = getpass.getpass("  Password: ")
    else:
        email = ""
        password = ""

    print("\n  📡 Garmin Connect からデータを取得中...")
    try:
        health = fetch_garmin_health(email or None, password or None)
        print(f"  ✅ 取得完了 (エラー: {len(health['fetch_errors'])}件)")
        if health["fetch_errors"]:
            for e in health["fetch_errors"][:5]:
                print(f"    ⚠️  {e}")
    except Exception as e:
        print(f"  ❌ 取得失敗: {e}")
        sys.exit(1)

    print("\n  🔬 診断中...")
    diag = diagnose_body_state(health)
    print_diagnosis(diag)

    # JSON保存オプション
    save = input("  診断結果をJSONで保存しますか? (y/N): ").strip().lower()
    if save == "y":
        fname = f"garmin_diagnosis_{date.today().isoformat()}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(diag, f, ensure_ascii=False, indent=2, default=str)
        print(f"  📄 保存完了: {fname}")
