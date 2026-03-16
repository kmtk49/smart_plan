"""
garmin_fetch_health.py
======================
Garmin Connectから体重・体内水分量・その他ヘルスデータを取得して
JSON出力するスタンドアロンスクリプト。

使い方:
    python garmin_fetch_health.py
    python garmin_fetch_health.py --days 30
    python garmin_fetch_health.py --email your@email.com

出力されたJSONをそのままClaudeに貼り付けると再解析できます。

必要なもの:
    pip install garminconnect
"""

import json
import sys
import argparse
import getpass
from datetime import date, timedelta
from pathlib import Path

TOKEN_DIR = Path.home() / ".garminconnect"


def login(email=None, password=None):
    try:
        from garminconnect import Garmin
    except ImportError:
        print("❌ garminconnect が未インストールです")
        print("   pip install garminconnect")
        sys.exit(1)

    # トークンキャッシュが存在すればパスワード不要
    token_file = TOKEN_DIR / "oauth2_token.json"
    if token_file.exists() and not email:
        try:
            api = Garmin()
            api.login(TOKEN_DIR)
            print("✅ 保存済みトークンでログイン成功")
            return api
        except Exception:
            print("⚠️  トークン期限切れ → 再ログインします")

    if not email:
        print("\nGarmin Connect の認証情報を入力してください")
        print("(~/.garminconnect/ にトークンが保存されます。次回以降は不要)\n")
        email = input("メールアドレス: ").strip()
    if not password:
        password = getpass.getpass("パスワード: ")

    api = Garmin(email=email, password=password)
    api.login()
    TOKEN_DIR.mkdir(mode=0o700, exist_ok=True)
    api.garth.dump(TOKEN_DIR)
    print("✅ ログイン成功 (トークン保存済み)")
    return api


def safe_get(fn, *args, label="", **kwargs):
    """エラーをキャッチして None を返す"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"  ⚠️  {label}: {str(e)[:60]}")
        return None


def fetch_weigh_ins_parsed(api, days: int = 14) -> dict:
    """
    get_weigh_ins() で全計測データを取得し、タイムスタンプ付きで返す。

    戻り値:
        {
          "YYYY-MM-DD": {
            "morning": {"weight_kg": float, "body_water_pct": float,
                        "body_fat_pct": float, "time_jst": "HH:MM",
                        "ts_sec": float},
            "all": [ ...morning と同構造... ]   # 古い順ソート済み
          },
          ...
        }
    """
    from datetime import date as _date, timedelta as _td, datetime as _dt, timezone as _tz
    JST = _tz(_td(hours=9))
    start_str = (_date.today() - _td(days=days)).isoformat()
    end_str   = _date.today().isoformat()

    raw = safe_get(api.get_weigh_ins, start_str, end_str, label="全計測(weigh_ins)")
    if not raw:
        return {}

    result = {}
    for day_summary in (raw.get("dailyWeightSummaries") or []):
        date_str    = day_summary.get("summaryDate", "")
        all_metrics = day_summary.get("allWeightMetrics") or []
        if not date_str or not all_metrics:
            continue

        parsed = []
        for m in all_metrics:
            ts_ms = m.get("timestampGMT") or m.get("date") or 0
            if not ts_ms:
                continue
            ts_sec  = ts_ms / 1000
            dt_jst  = _dt.fromtimestamp(ts_sec, tz=JST)
            w_g     = m.get("weight") or 0
            mu_g    = m.get("muscleMass") or 0
            parsed.append({
                "weight_kg":      round(w_g  / 1000, 2) if w_g  else None,
                "body_water_pct": m.get("bodyWater"),
                "body_fat_pct":   m.get("bodyFat"),
                "muscle_mass_kg": round(mu_g / 1000, 2) if mu_g else None,
                "time_jst":       dt_jst.strftime("%H:%M"),
                "ts_sec":         ts_sec,
            })

        if not parsed:
            continue
        parsed.sort(key=lambda x: x["ts_sec"])   # 古い順
        result[date_str] = {
            "morning": parsed[0],   # 最も早い計測 = 朝一番
            "all":     parsed,
        }
    return result


def fetch_body_composition_range(api, start_date: str, end_date: str):
    """
    日付範囲の体組成データを取得。
    返り値: [{"date": "YYYY-MM-DD", "weight": float, "bodyFatPercent": float,
               "bodyWaterPercent": float, "muscleMass": float, "boneMass": float}, ...]
    """
    raw = safe_get(api.get_body_composition, start_date, end_date,
                   label="体組成(range)")
    if not raw:
        return []

    results = []
    # dateWeightList 形式
    items = (raw.get("dateWeightList") or
             raw.get("bodyCompositionList") or
             raw.get("totalAverage") or [])
    if isinstance(items, dict):
        items = [items]

    for item in items:
        if not isinstance(item, dict):
            continue
        d = item.get("calendarDate") or item.get("date") or ""
        w = (item.get("weight") or
             (item.get("weightInGrams", 0) / 1000 if item.get("weightInGrams") else None))
        results.append({
            "date":             d,
            "weight_kg":        round(float(w), 2) if w else None,
            "body_fat_pct":     item.get("bodyFatPercent"),
            "body_water_pct":   item.get("bodyWaterPercent"),
            "muscle_mass_kg":   item.get("muscleMassInGrams", 0) / 1000 if item.get("muscleMassInGrams") else item.get("muscleMass"),
            "bone_mass_kg":     item.get("boneMassInGrams", 0) / 1000 if item.get("boneMassInGrams") else item.get("boneMass"),
            "bmi":              item.get("bmi"),
        })
    return sorted(results, key=lambda x: x["date"])


def fetch_daily_summary(api, day: date):
    """1日分の活動・心拍・ストレス・水分・睡眠を取得"""
    ds = day.isoformat()
    result = {"date": ds}

    # 活動サマリー (steps, calories, etc.)
    stats = safe_get(api.get_stats, ds, label="stats")
    if stats:
        result["steps"]           = stats.get("totalSteps")
        result["calories_active"] = stats.get("activeKilocalories")
        result["calories_bmr"]    = stats.get("bmrKilocalories")
        result["intensity_min"]   = stats.get("intensityMinutesGoal")
        result["rhr"]             = stats.get("restingHeartRate") or stats.get("minHeartRate")

    # 体組成（当日）
    body = safe_get(api.get_body_composition, ds, label="body_comp")
    if body:
        items = (body.get("dateWeightList") or
                 body.get("bodyCompositionList") or [])
        if items and isinstance(items[0], dict):
            it = items[0]
            w = it.get("weight") or (it.get("weightInGrams", 0) / 1000 if it.get("weightInGrams") else None)
            result["weight_kg"]      = round(float(w), 2) if w else None
            result["body_fat_pct"]   = it.get("bodyFatPercent")
            result["body_water_pct"] = it.get("bodyWaterPercent")
            result["muscle_mass_kg"] = (it.get("muscleMassInGrams", 0) / 1000
                                        if it.get("muscleMassInGrams") else it.get("muscleMass"))

    # 水分摂取
    hyd = safe_get(api.get_hydration_data, ds, label="hydration")
    if hyd:
        result["hydration_intake_ml"] = (hyd.get("totalIntakeInMl") or
                                          hyd.get("valueInML"))
        result["hydration_goal_ml"]   = (hyd.get("dailyIntakeGoalInMl") or
                                          hyd.get("goalInML"))

    # 睡眠
    sleep = safe_get(api.get_sleep_data, ds, label="sleep")
    if sleep:
        dto = (sleep.get("dailySleepDTO") or
               sleep.get("sleepDTO") or sleep)
        if isinstance(dto, list) and dto:
            dto = dto[0]
        if isinstance(dto, dict):
            result["sleep_total_s"]  = dto.get("sleepTimeSeconds") or dto.get("sleptDuration")
            result["sleep_score"]    = (dto.get("sleepScores", {}).get("overallScore")
                                        if isinstance(dto.get("sleepScores"), dict)
                                        else dto.get("sleepScore"))
            result["deep_sleep_s"]   = dto.get("deepSleepSeconds")
            result["rem_sleep_s"]    = dto.get("remSleepSeconds")

    # ストレス
    stress = safe_get(api.get_stress_data, ds, label="stress")
    if stress:
        result["stress_avg"] = (stress.get("avgStressLevel") or
                                 stress.get("averageStressLevel"))
        result["stress_max"] = stress.get("maxStressLevel")

    # HRV
    hrv = safe_get(api.get_hrv_data, ds, label="hrv")
    if hrv and isinstance(hrv, dict):
        result["hrv_last_night"]  = hrv.get("lastNight5MinHigh") or hrv.get("hrvValue")
        result["hrv_weekly_avg"]  = hrv.get("weeklyAvg")
        bl = hrv.get("baseline") or {}
        if isinstance(bl, dict):
            result["hrv_baseline_low"]  = bl.get("balancedLow")
            result["hrv_baseline_high"] = bl.get("balancedHigh")
        result["hrv_status"] = hrv.get("hrvStatus")

    # Training Readiness
    rd = safe_get(api.get_training_readiness, ds, label="readiness")
    if rd:
        if isinstance(rd, list) and rd:
            rd = rd[0]
        if isinstance(rd, dict):
            result["training_readiness"] = rd.get("score") or rd.get("trainingReadinessScore")

    return result


def main():
    parser = argparse.ArgumentParser(description="Garminヘルスデータ取得スクリプト")
    parser.add_argument("--email",    default="", help="Garmin Connectメールアドレス")
    parser.add_argument("--password", default="", help="パスワード（省略推奨 → 対話入力）")
    parser.add_argument("--days",     type=int, default=14, help="取得日数（デフォルト14日）")
    parser.add_argument("--output",   default="garmin_health_data.json",
                        help="出力JSONファイル名（デフォルト: garmin_health_data.json）")
    args = parser.parse_args()

    print("=" * 56)
    print("  Garmin Connect ヘルスデータ取得スクリプト")
    print("=" * 56)

    api = login(args.email or None, args.password or None)

    today  = date.today()
    start  = today - timedelta(days=args.days - 1)
    start_str = start.isoformat()
    end_str   = today.isoformat()

    print(f"\n📡 {start_str} 〜 {end_str} ({args.days}日分) を取得中...")

    # 体組成（範囲取得）
    print("  ⚖️  体組成データ (体重・体内水分%・筋肉量)...")
    body_series = fetch_body_composition_range(api, start_str, end_str)
    body_map = {b["date"]: b for b in body_series}

    # 日次データ
    daily = []
    for i in range(args.days):
        d = start + timedelta(days=i)
        print(f"  📅 {d.isoformat()} ...", end="\r")
        row = fetch_daily_summary(api, d)
        # 体組成データをマージ
        if d.isoformat() in body_map:
            bdata = body_map[d.isoformat()]
            for k in ["weight_kg","body_fat_pct","body_water_pct","muscle_mass_kg","bone_mass_kg","bmi"]:
                if bdata.get(k) is not None and row.get(k) is None:
                    row[k] = bdata[k]
        daily.append(row)

    print(f"\n  ✅ {len(daily)}日分取得完了")

    # サマリー集計
    has_water = sum(1 for d in daily if d.get("body_water_pct"))
    has_weight = sum(1 for d in daily if d.get("weight_kg"))
    weights = [d["weight_kg"] for d in daily if d.get("weight_kg")]
    waters  = [d["body_water_pct"] for d in daily if d.get("body_water_pct")]

    summary = {
        "fetch_date":       today.isoformat(),
        "days_fetched":     len(daily),
        "weight_available_days":    has_weight,
        "body_water_available_days": has_water,
        "weight_avg_kg":    round(sum(weights)/len(weights), 2) if weights else None,
        "weight_min_kg":    min(weights) if weights else None,
        "weight_max_kg":    max(weights) if weights else None,
        "body_water_avg_pct": round(sum(waters)/len(waters), 1) if waters else None,
        "note": ("体内水分%が取得できませんでした。"
                 "Garmin体組成計（Index S2など）が必要です。"
                 if not has_water else
                 f"体内水分%を{has_water}日分取得しました。"),
    }

    output = {
        "summary": summary,
        "daily":   daily,
    }

    # ファイル保存
    out_path = Path(args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"\n{'=' * 56}")
    print(f"  📊 取得サマリー")
    print(f"{'─' * 56}")
    print(f"  体重データ:     {has_weight}/{args.days}日")
    print(f"  体内水分%:      {has_water}/{args.days}日")
    if weights:
        print(f"  体重範囲:       {min(weights):.1f} 〜 {max(weights):.1f} kg")
    if waters:
        print(f"  体内水分%平均:  {sum(waters)/len(waters):.1f}%")

    print(f"\n  💾 保存先: {out_path.absolute()}")
    print(f"\n  次のステップ:")
    print(f"  1. {out_path} の内容をコピー")
    print(f"  2. Claudeのチャットに貼り付けて「再解析して」と送信")
    print(f"{'=' * 56}")

    # 標準出力にもJSONを出力（パイプ処理用）
    if "--stdout" in sys.argv:
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
