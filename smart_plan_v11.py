"""
smart_plan_v11.py — トレーニングプラン生成 v11
==============================================
【v11 追加機能】
  - Garmin Connect ヘルスデータ統合による身体状態診断
    ▸ Body Battery / HRV Status / 睡眠スコア / ストレス / Training Readiness
    ▸ 体重変動からのグリコーゲン枯渇推定 (Schytz et al. 2023)
    ▸ 直前セッション影響度スコア (強度×時間×経過時間)
    ▸ 水分摂取量・SpO2・呼吸数
    ▸ 総合コンディションスコア(0-100)と強度調整係数(-2〜+2)
    ▸ 自動メニュー強度アップダウン (Body Battery ≤20で recovery 強制 等)
  - Garmin診断サマリーをプランのヘッダーに追加表示
  - 栄養補給アドバイス(グリコーゲン補充推奨量)の自動計算
  - pip install garminconnect で有効化 / 未インストール時は既存の
    Intervals.icu/HRV-onlyモードにフォールバック

【v10 追加機能】
  - ランメニュー 4→7タイプ (hard), 3→5(moderate), 3→5(easy)
  - バイクメニュー 5→7タイプ (hard), 3→5(moderate), 2→4(easy)
  - ヨガ 6タイプ完全リニューアル (スイム系/バイク&ラン系/全身/ブレス/コア/リストラティブ)
  - print_calorie_summary 関数追加
  - 新規論文16本追加

【v5〜v9からの継続機能】
  - Googleカレンダー解析, HRVスコアリング, Intervals.icuアップロード
  - Garmin Connect ワークアウト JSON 生成・エクスポート
  - レース天気取得 (Open-Meteo), PDF リザルト解析

使い方:
  python smart_plan_v11.py --preview          # 10日プレビュー
  python smart_plan_v11.py --days 14          # 14日分
  python smart_plan_v11.py --today            # 当日のみ (Garmin診断付き)
  python smart_plan_v11.py --diagnose         # Garmin診断のみ実行
  python smart_plan_v11.py --no-upload        # アップロードなし
  python smart_plan_v11.py --server           # HTMLチャットUIサーバー
"""

import yaml, json, base64, argparse, re, math
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, date
from pathlib import Path

# PyPDF2はオプション（なくても動作）
try:
    import PyPDF2
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False

# ============================================================
# Garmin ヘルスデータ診断モジュール (v11 新機能)
# garmin_health_diagnosis.py を同じフォルダに置いてください
# pip install garminconnect が必要です
# ============================================================
try:
    from garmin_health_diagnosis import (
        fetch_garmin_health,
        diagnose_body_state,
        print_diagnosis,
        apply_garmin_modifier,
        garmin_adjustment_summary,
    )
    HAS_GARMIN_DIAG = True
except ImportError:
    HAS_GARMIN_DIAG = False


def _run_garmin_diagnosis(cfg, athlete_weight_kg=68.4):
    """
    Garmin Connectからヘルスデータを取得して身体状態を診断する。
    garminconnect未インストール / 認証失敗時はNoneを返す (フォールバック)。
    Returns: diagnosis dict  or  None
    """
    if not HAS_GARMIN_DIAG:
        return None
    g_cfg    = cfg.get("garmin", {})
    email    = g_cfg.get("email", "")
    password = g_cfg.get("password", "")
    if not email:
        print("  💡 Garmin Connect認証情報が config.yaml に未設定")
        print("     garmin.email / garmin.password を設定するか以下に入力:")
        try:
            import getpass
            email = input("  Garmin Email (Enterでスキップ): ").strip()
            if not email:
                print("  ℹ️  Garmin診断をスキップします (--diagnose で単独実行可能)")
                return None
            password = getpass.getpass("  Garmin Password: ")
        except (EOFError, KeyboardInterrupt):
            return None
    print("  📡 Garmin Connect からヘルスデータを取得中...")
    try:
        health = fetch_garmin_health(email, password, days_back=7)
        if health.get("fetch_errors"):
            print(f"  ⚠️  一部取得失敗: {len(health['fetch_errors'])}項目")
        diag = diagnose_body_state(health, athlete_weight_kg)
        return diag
    except Exception as e:
        print(f"  ⚠️  Garmin診断エラー: {e}")
        print("     → Intervals.icu / HRV データのみでコンディション判定を続行")
        return None


def _merge_garmin_into_cond(cond, garmin_diag):
    """
    Garmin診断結果を既存のcond辞書にマージする。
    既存のHRV scoreを保ちつつ、Garminの多次元スコアで補正する。
    """
    if garmin_diag is None:
        return cond
    g_score    = garmin_diag["overall_score"]       # 0-100
    g_modifier = garmin_diag["intensity_modifier"]  # -2〜+2
    g_score_10 = g_score / 10.0
    # 加重平均: Garmin診断 60% / 既存HRV-only 40%
    merged_score = g_score_10 * 0.6 + cond["score"] * 0.4
    merged_score = round(max(0, min(10, merged_score)), 1)
    if   merged_score >= 7.5: new_condition = "peak"
    elif merged_score >= 6.0: new_condition = "good"
    elif merged_score >= 4.0: new_condition = "normal"
    elif merged_score >= 2.5: new_condition = "fatigued"
    else:                     new_condition = "depleted"
    updated = dict(cond)
    updated["score"]           = merged_score
    updated["condition"]       = new_condition
    updated["garmin_diag"]     = garmin_diag
    updated["garmin_modifier"] = g_modifier
    updated["reasons"]         = list(cond.get("reasons", [])) + garmin_diag.get("warnings", [])[:3]
    return updated


def _print_garmin_summary_block(cond):
    """Garmin診断サマリーブロックを表示 (プラン冒頭で使用)"""
    diag = cond.get("garmin_diag")
    if not diag:
        return
    print(f"\n  {'─'*60}")
    print(f"  🏥 Garmin ヘルス診断")
    print(f"  {'─'*60}")
    print(f"  {garmin_adjustment_summary(diag)}")

    bb  = diag["body_battery"]
    sl  = diag["sleep"]
    hv  = diag["hrv"]
    gl  = diag["glycogen"]
    rd  = diag["readiness"]

    bb_cur  = bb.get("current","?")
    sl_h    = f"{sl.get('total_h',0):.1f}h" if sl.get("total_h") else "N/A"
    sl_sc   = f"{sl.get('score',0):.0f}" if sl.get("score") else "?"
    hv_val  = hv.get("last_night_5min") or hv.get("weekly_avg")
    hv_str  = f"{hv_val:.0f}ms" if hv_val else "N/A"
    gl_pct  = gl["depletion_pct"]
    gl_lv   = gl["depletion_level"]
    rd_sc   = rd.get("score")
    rd_str  = f"{rd_sc:.0f}/100" if rd_sc is not None else "N/A"

    print(f"  ⚡ Body Battery: {bb_cur}/100  "
          f"😴 睡眠: {sl_h}(スコア{sl_sc})  "
          f"💓 HRV: {hv_str}")
    print(f"  🍞 グリコーゲン枯渇: {gl_pct:.0f}%({gl_lv})  "
          f"🎯 Training Readiness: {rd_str}")

    if diag["warnings"]:
        for w in diag["warnings"][:3]:
            print(f"  ⚠️  {w}")
    if diag["positives"]:
        for p in diag["positives"][:2]:
            print(f"  ✅ {p}")
    if diag["nutrition_alert"]:
        for n in diag["nutrition_alert"][:2]:
            print(f"  🍽  {n}")
    print(f"  💡 {diag['training_recommendation'].split(chr(10))[0]}")
    print(f"  {'─'*60}")


CONFIG_FILE = Path(__file__).parent / "config.yaml"

# ============================================================
# 設定
# ============================================================
def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f)

# ============================================================
# Intervals.icu
# ============================================================
def icu_headers(api_key):
    auth = base64.b64encode(f"API_KEY:{api_key}".encode()).decode()
    return {"Authorization": f"Basic {auth}", "Content-Type": "application/json"}

def icu_get(url, api_key, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=icu_headers(api_key))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception:
        return None

def icu_post(url, api_key, body):
    data = json.dumps(body, ensure_ascii=False).encode()
    req  = urllib.request.Request(url, data=data, headers=icu_headers(api_key), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  [HTTP {e.code}] {e.read().decode()[:150]}")
        return None
    except Exception as e:
        print(f"  [エラー] {e}")
        return None

def icu_delete(url, api_key):
    """Intervals.icu のイベントを DELETE する"""
    req = urllib.request.Request(url, headers=icu_headers(api_key), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True   # すでに存在しない = 削除済み扱い
        print(f"  [DELETE HTTP {e.code}] {e.read().decode()[:100]}")
        return False
    except Exception as e:
        print(f"  [DELETE エラー] {e}")
        return False


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
    # 1) アスリートプロフィールの icu_ftp（intervals.icu 設定値）
    # 2) wellness の icu_pm_ftp（パワーメーター由来の最新値）
    # 3) 直近90日アクティビティの icu_rolling_ftp の最大値
    # 4) configのフォールバック値（デフォルト223W）
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
    # 1) アスリートプロフィールの icu_run_threshold_pace（設定値）
    # 2) wellness の run_threshold_pace
    # 3) 直近90日の15km以上ランの最速ペース × 1.05（概算）
    # 4) configのフォールバック値（デフォルト288秒/km）
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
    # 1) アスリートプロフィールの icu_swim_threshold_pace
    # 2) wellness の swim_threshold_pace
    # 3) 直近90日のスイムアクティビティの最速ペース（/100m）× 1.05
    # 4) configのフォールバック値（デフォルト125秒/100m = 2:05/100m）
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
    # 1) Intervals.icuのRACEカテゴリから過去2年分
    ago730 = (datetime.now()-timedelta(days=730)).strftime("%Y-%m-%d")
    race_acts = icu_get(f"{base}/activities", api_key,
                        {"oldest":ago730,"newest":today,"category":"RACE"}) or []
    # 2) RACE以外でもレース名が含まれるアクティビティも取得
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
    print(f"  ✅ 体重={weight}kg  FTP={ftp:.0f}W [{ftp_src}]  TP={_fmt_pace(int(tp_sec))}/km [{tp_src}]")
    print(f"     CSS={_swim_pace(css)}/100m [{css_src}]")
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
            "wellness_history":wellness,
            "past_results":past_results,
            "weekly_counts":weekly_counts,
            "_icu_base":base, "_api_key":api_key,
            "_ftp_src":ftp_src, "_tp_src":tp_src, "_css_src":css_src}

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

# ============================================================
# PDFリザルト解析 (スイム/T1/バイク/T2/ラン)
# ============================================================

# 会場名→(緯度, 経度, 表示名) マッピング
VENUE_COORDS = {
    # 関東
    "山下公園":       (35.4437, 139.6380, "横浜"),
    "横浜":           (35.4437, 139.6380, "横浜"),
    "渡良瀬":         (36.1833, 139.6833, "栃木/渡良瀬"),
    "渡良瀬遊水池":   (36.1833, 139.6833, "栃木/渡良瀬"),
    "戸田":           (35.8333, 139.6833, "埼玉/戸田"),
    "江の島":         (35.2994, 139.4789, "神奈川/江の島"),
    "幕張":           (35.6489, 140.0417, "千葉/幕張"),
    # 東北・北海道
    "洞爺":           (42.5667, 140.7667, "北海道/洞爺"),
    "函館":           (41.7686, 140.7290, "北海道/函館"),
    # 中部・関西
    "琵琶湖":         (35.3089, 136.0692, "滋賀/琵琶湖"),
    "淡路島":         (34.5955, 134.8944, "兵庫/淡路島"),
    # 四国・中国
    "宮古島":         (24.8056, 125.2811, "沖縄/宮古島"),
    "石垣":           (24.3448, 124.1572, "沖縄/石垣島"),
    # ソウル（海外）
    "ソウル":         (37.5665, 126.9780, "韓国/ソウル"),
    "金浦":           (37.5665, 126.9780, "韓国/ソウル"),
    # 奄美・徳之島
    "奄美":           (28.3667, 129.5000, "鹿児島/奄美"),
    "徳之島":         (27.7083, 128.9833, "鹿児島/徳之島"),
}

WMO_CODES = {
    0:"快晴",1:"晴れ",2:"一部曇り",3:"曇り",
    45:"霧",48:"霧氷",51:"小雨",53:"雨",55:"大雨",
    61:"小雨",63:"雨",65:"大雨",71:"小雪",73:"雪",75:"大雪",
    80:"にわか雨",81:"にわか雨(強)",82:"激しいにわか雨",
    95:"雷雨",96:"雷雨+ひょう",99:"激しい雷雨",
}

def extract_venue_coords(location_str):
    """場所文字列から緯度経度を推定する"""
    if not location_str:
        return None
    for kw, (lat, lon, name) in VENUE_COORDS.items():
        if kw in location_str:
            return lat, lon, name
    return None

def fetch_weather_for_race(location_str, race_date_str):
    """
    レース会場の気象情報を取得:
      - レース日が16日以内 → 予報API
      - それ以外 → 1年前同日の過去実績API
    戻り値: dict or None
    """
    coords = extract_venue_coords(location_str)
    if not coords:
        return None
    lat, lon, venue_name = coords

    try:
        rd = date.fromisoformat(race_date_str)
    except:
        return None

    today = date.today()
    days_until = (rd - today).days

    # 予報か過去データかを選択
    if 0 <= days_until <= 16:
        url    = "https://api.open-meteo.com/v1/forecast"
        target = race_date_str
        source = "予報"
    else:
        # 1年前の同日
        past_date = rd.replace(year=rd.year - 1)
        url    = "https://archive-api.open-meteo.com/v1/archive"
        target = past_date.isoformat()
        source = f"{past_date.year}年実績"

    params = {
        "latitude":  lat,
        "longitude": lon,
        "daily":     "temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode,windspeed_10m_max",
        "timezone":  "Asia/Tokyo",
        "start_date": target,
        "end_date":   target,
    }
    full_url = url + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(full_url, headers={"User-Agent": "SmartPlan/6"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
        d = data.get("daily", {})
        if not d or not d.get("time"):
            return None
        wcode = d.get("weathercode",[0])[0] or 0
        return {
            "venue":    venue_name,
            "source":   source,
            "date":     target,
            "temp_max": d.get("temperature_2m_max",[None])[0],
            "temp_min": d.get("temperature_2m_min",[None])[0],
            "precip":   d.get("precipitation_sum",[0])[0],
            "wind":     d.get("windspeed_10m_max",[None])[0],
            "weather":  WMO_CODES.get(int(wcode), f"コード{wcode}"),
        }
    except Exception as e:
        return {"venue": venue_name, "source": "取得失敗", "error": str(e)}

def parse_split_times_from_text(text):
    """
    PDF/テキストからトライアスロンのスプリットタイムを抽出する。
    H:MM:SS または MM:SS 形式に対応。
    """
    TIME_PAT = r"(\d{1,2}:\d{2}(?::\d{2})?)"
    SECTIONS = {
        "swim":  [r"swim[\s:：]+"+TIME_PAT,
                  r"スイム[\s:：]+"+TIME_PAT,
                  r"S[\s:：]+"+TIME_PAT,
                  r"(?:swim|スイム)[^\n]*?"+TIME_PAT],
        "t1":    [r"T1[\s:：]+"+TIME_PAT,
                  r"transition[\s1:：]+"+TIME_PAT],
        "bike":  [r"bike[\s:：]+"+TIME_PAT,
                  r"バイク[\s:：]+"+TIME_PAT,
                  r"cycle[\s:：]+"+TIME_PAT,
                  r"B[\s:：]+"+TIME_PAT,
                  r"(?:bike|バイク|cycle)[^\n]*?"+TIME_PAT],
        "t2":    [r"T2[\s:：]+"+TIME_PAT],
        "run":   [r"run[\s:：]+"+TIME_PAT,
                  r"ラン[\s:：]+"+TIME_PAT,
                  r"R[\s:：]+"+TIME_PAT,
                  r"(?:run|ラン)[^\n]*?"+TIME_PAT],
        "total": [r"finish[\w\s]*[\s:：]+"+TIME_PAT,
                  r"フィニッシュ[\s:：]+"+TIME_PAT,
                  r"total[\s:：]+"+TIME_PAT,
                  r"合計[\s:：]+"+TIME_PAT,
                  r"total[\s:：]?\s*"+TIME_PAT],
    }

    splits = {}
    for key, patterns in SECTIONS.items():
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE | re.MULTILINE)
            if m:
                t_str = m.group(1)
                splits[key] = {"str": t_str, "sec": _time_str_to_sec(t_str)}
                break
    return splits

def _time_str_to_sec(t_str):
    """MM:SS または H:MM:SS → 秒数"""
    parts = t_str.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0])*3600 + int(parts[1])*60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0])*60 + int(parts[1])
    except:
        pass
    return 0

def _get_gdrive_token(cfg=None):
    """
    Google Drive アクセストークンを以下の優先順で取得:
    1. 環境変数 GOOGLE_DRIVE_TOKEN
    2. スクリプト隣の token.json (google-auth-oauthlib が生成するファイル)
    3. スクリプト隣の gtoken.json (カスタム設定)
    4. config.yaml の google_drive.token
    Returns: token_str or ""
    """
    import os, json, pathlib
    # 1) 環境変数
    t = os.environ.get("GOOGLE_DRIVE_TOKEN", "")
    if t: return t
    # 2-3) token.json / gtoken.json
    script_dir = pathlib.Path(__file__).parent
    for fname in ["token.json", "gtoken.json", "gdrive_token.json"]:
        tp = script_dir / fname
        if tp.exists():
            try:
                data = json.loads(tp.read_text(encoding="utf-8"))
                # google-auth token.json 形式: {"token": "ya29.xxx", ...}
                tok = data.get("token") or data.get("access_token") or data.get("gdrive_token","")
                if tok:
                    return tok
            except: pass
    # 4) config.yaml
    tok = ((cfg or {}).get("google_drive") or {}).get("token","")
    return tok


def fetch_gdrive_pdf_via_api(file_id, cfg=None):
    """
    Google Drive API経由でPDFをダウンロードしてテキスト抽出を試みる。

    トークン取得優先順:
      1. 環境変数 GOOGLE_DRIVE_TOKEN
      2. スクリプト隣の token.json / gtoken.json
      3. config.yaml の google_drive.token
      4. (パブリックファイルのみ) export URL で認証なし

    Returns: (text: str, error: str|None)
    """
    import urllib.request, urllib.error, io

    token      = _get_gdrive_token(cfg)
    api_url    = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    export_url = f"https://drive.google.com/uc?export=download&id={file_id}"

    attempts = []
    if token:
        attempts.append((api_url, token))
    attempts.append((export_url, ""))  # 認証なし（パブリックファイル用）

    for url, tok in attempts:
        try:
            req = urllib.request.Request(url)
            if tok:
                req.add_header("Authorization", f"Bearer {tok}")
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=20) as resp:
                if resp.status == 200:
                    pdf_bytes = resp.read()
                    if pdf_bytes[:4] == b"%PDF":
                        txt = _extract_pdf_text_zlib(pdf_bytes)
                        if txt.strip():
                            return txt, None
                        if HAS_PYPDF2:
                            reader = PyPDF2.PdfReader(io.BytesIO(pdf_bytes))
                            txt = "\n".join(p.extract_text() or "" for p in reader.pages)
                            if txt.strip(): return txt, None
                        return "", "テキスト抽出失敗(画像PDF: OCRが必要)"
                    # PDF以外のレスポンス（HTMLリダイレクト等）
                    if b"<html" in pdf_bytes[:200].lower():
                        continue  # 認証リダイレクトページ → 次のURLを試す
        except urllib.error.HTTPError as e:
            if e.code == 403:
                if token:
                    return "", (f"GDrive 403: トークン期限切れの可能性。"
                                f"token.jsonを更新するか GOOGLE_DRIVE_TOKEN を再設定してください")
                continue
            if e.code == 404:
                return "", f"GDrive 404: ファイルが見つかりません (fileId={file_id})"
        except Exception:
            continue

    if not token:
        return "", ("GDriveアクセス不可 (プライベートファイル)。"
                    "GOOGLE_DRIVE_TOKEN環境変数 または token.json をスクリプトと同じフォルダに置いてください。\n"
                    "取得方法: https://developers.google.com/drive/api/quickstart/python")
    return "", "GDriveからのダウンロード失敗"


def _extract_pdf_text_zlib(pdf_bytes):
    """
    PyPDF2なしでPDFバイナリからテキストを抽出する。
    FlateDecode圧縮ストリームをzlibで展開し、PDF Tj/TJ演算子から文字列を収集する。
    """
    import zlib as _zlib
    parts = []
    # stream〜endstream ブロックを展開
    _RE = re.compile(rb'stream\r?\n(.*?)\r?\nendstream', re.DOTALL)
    for m in _RE.finditer(pdf_bytes):
        data = m.group(1)
        for wbits in (15, 47, -15):
            try:
                parts.append(_zlib.decompress(data, wbits))
                break
            except Exception:
                pass
    parts.append(pdf_bytes)  # 非圧縮部分も対象

    pieces = []
    for raw in parts:
        try:
            content = raw.decode("latin-1", errors="replace")
        except Exception:
            continue
        # (text) Tj  /  (text) '
        for m in re.finditer(r'\(([^)]*)\)\s*[Tj\']', content):
            pieces.append(m.group(1))
        # [(text) num ...] TJ
        for m in re.finditer(r'\[([^\]]+)\]\s*TJ', content):
            for sm in re.finditer(r'\(([^)]*)\)', m.group(1)):
                pieces.append(sm.group(1))

    def _unescape(s):
        return (s.replace(r'\n', '\n').replace(r'\r', '\r')
                 .replace(r'\t', '\t').replace(r'\(', '(')
                 .replace(r'\)', ')').replace('\\\\', '\\'))

    return "\n".join(_unescape(p) for p in pieces if p.strip())


def parse_pdf_result(pdf_path):
    """
    PDFファイルからリザルトテキストを抽出してスプリットタイムを返す。

    優先順位:
      1. PyPDF2 が使える → PyPDF2 で高精度抽出
      2. PyPDF2 なし    → zlib独自パーサーでフォールバック抽出
      3. 文字列が渡された → そのままテキストパース（コマンドラインから直接入力など）

    Returns: {"splits": dict, "raw_text": str, "method": str, "error": str|None}
    """
    raw_text = ""
    method   = "none"
    p = Path(str(pdf_path))

    if p.exists():
        # ── 方法1: PyPDF2 ─────────────────────────────────────────
        if HAS_PYPDF2:
            try:
                with open(p, "rb") as f:
                    reader = PyPDF2.PdfReader(f)
                    for page in reader.pages:
                        t = page.extract_text()
                        if t:
                            raw_text += t + "\n"
                method = "PyPDF2"
            except Exception:
                raw_text = ""

        # ── 方法2: zlib独自パーサー ───────────────────────────────
        if not raw_text.strip():
            try:
                raw_text = _extract_pdf_text_zlib(p.read_bytes())
                method   = "zlib"
            except Exception as e:
                return {"splits": {}, "raw_text": "", "method": "error",
                        "error": str(e)}

        if not raw_text.strip():
            return {
                "splits": {}, "raw_text": "", "method": method,
                "error": (f"テキスト抽出できませんでした: {p.name}\n"
                          "  PDFが画像スキャン形式の可能性があります。\n"
                          "  → pip install PyPDF2 で改善することがあります。"),
            }

    elif isinstance(pdf_path, str):
        # テキスト文字列として直接パース
        raw_text = pdf_path
        method   = "text_direct"
    else:
        return {"splits": {}, "raw_text": "", "method": "not_found",
                "error": f"ファイルが見つかりません: {pdf_path}"}

    splits = parse_split_times_from_text(raw_text)
    return {"splits": splits, "raw_text": raw_text,
            "raw_len": len(raw_text), "method": method, "error": None}


# ============================================================
# Excelリザルト解析
# ============================================================

# openpyxl / pandas はオプション（なくても動作）
try:
    import openpyxl, pandas as pd
    HAS_EXCEL = True
except ImportError:
    HAS_EXCEL = False

# スプリットフィールドのキーワードマッピング（日英・略語・スペース違いを網羅）
EXCEL_KEY_MAP = {
    "swim":  ["swim","スイム","swimming","sw","s","水泳"],
    "t1":    ["t1","transition1","tr1","トランジション1","trans1"],
    "bike":  ["bike","バイク","cycle","cycling","サイクル","b","ride"],
    "t2":    ["t2","transition2","tr2","トランジション2","trans2"],
    "run":   ["run","ラン","running","r","walk"],
    "total": ["total","finish","フィニッシュ","合計","finish time",
              "total time","finishtime","ゴール","goal","完走タイム"],
}

def _match_split_key(cell_str):
    """セル文字列がどのスプリットフィールドか判定する"""
    s = str(cell_str).lower().strip()
    for field, kws in EXCEL_KEY_MAP.items():
        # 完全一致 or 先頭マッチ（例: "スイム(1.5km)" → swim）
        if any(s == kw or s.startswith(kw) for kw in kws):
            return field
    return None

def _is_time_value(val):
    """セルの値がタイム文字列かどうかを判定する"""
    if val is None or (isinstance(val, float) and str(val) == 'nan'):
        return False
    s = str(val).strip()
    # H:MM:SS / MM:SS / timedelta形式
    return bool(re.match(r'^\d{1,2}:\d{2}(:\d{2})?$', s))

def _normalize_time_str(val):
    """
    様々な形式のタイム値を H:MM:SS 文字列に正規化する。
    - "31:15"       → "0:31:15"
    - "1:09:42"     → "1:09:42"
    - timedelta     → "1:09:42"
    - float (Excel serial time) → "H:MM:SS"
    """
    if val is None:
        return None
    # timedelta（pandasがExcel time型を変換する場合）
    try:
        from datetime import timedelta as td, datetime as dt
        if hasattr(val, 'seconds'):  # timedelta
            total = int(val.total_seconds())
            h, rem = divmod(total, 3600)
            m, s   = divmod(rem, 60)
            return f"{h}:{m:02d}:{s:02d}"
        if hasattr(val, 'hour'):  # datetime.time
            return f"{val.hour}:{val.minute:02d}:{val.second:02d}"
    except: pass

    s = str(val).strip()
    # "0 days HH:MM:SS" 形式 (pandas timedelta string)
    m = re.match(r'(\d+) days?[\s,]*(\d+):(\d+):(\d+)', s)
    if m:
        days = int(m.group(1))
        h = days*24 + int(m.group(2))
        return f"{h}:{m.group(3)}:{m.group(4)}"
    # "MM:SS" → "0:MM:SS"
    m = re.match(r'^(\d{1,2}):(\d{2})$', s)
    if m:
        return f"0:{m.group(1)}:{m.group(2)}"
    # "H:MM:SS" そのまま
    m = re.match(r'^(\d{1,2}):(\d{2}):(\d{2})$', s)
    if m:
        return s
    # Excelの数値時刻 (0.0〜1.0: 1日=1.0)
    try:
        f = float(s)
        if 0 < f < 1:
            total_sec = int(f * 86400)
            h, rem = divmod(total_sec, 3600)
            mi, sec = divmod(rem, 60)
            return f"{h}:{mi:02d}:{sec:02d}"
    except: pass
    return None

def parse_excel_result(xlsx_path, athlete_name=None, bib_number=None):
    """
    Excel(.xlsx/.xls/.csv)からトライアスロンのスプリットタイムを抽出する。

    対応フォーマット:
      A) 横型（公式リザルト）: ヘッダー行 + データ行（氏名・ゼッケン等でフィルタ可能）
      B) 縦型（自己管理シート）: A列=項目名, B列=値

    引数:
      xlsx_path    : ファイルパス（str/Path）
      athlete_name : 抽出対象の選手名（横型で自分のタイムだけ取る場合）
      bib_number   : ゼッケン番号（横型でフィルタする場合）

    戻り値:
      {
        "splits": {"swim": {"str":"0:31:15","sec":1875}, "t1":..., ...},
        "format": "horizontal" | "vertical" | "text",
        "matched_row": {...},  # 横型の場合のマッチした行
        "error": None or str
      }
    """
    if not HAS_EXCEL:
        # pandasなし → テキストとして読めるCSVのみ対応
        path = Path(str(xlsx_path))
        if path.suffix.lower() == '.csv' and path.exists():
            try:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
                splits = parse_split_times_from_text(text)
                return {"splits": splits, "format": "text_csv", "error": None}
            except Exception as e:
                return {"splits": {}, "format": "unknown", "error": str(e)}
        return {"splits": {}, "format": "unknown",
                "error": "openpyxl/pandasが未インストール (pip install openpyxl pandas)"}

    path = Path(str(xlsx_path))
    if not path.exists():
        return {"splits": {}, "format": "unknown",
                "error": f"ファイル未存在: {xlsx_path}"}

    try:
        # CSV対応
        if path.suffix.lower() == '.csv':
            df = pd.read_csv(path, encoding="utf-8-sig", header=None)
        else:
            # 全シートを試す
            xl = pd.ExcelFile(path)
            df = None
            # レース結果っぽいシートを優先
            preferred = ['result','results','リザルト','結果','record','記録']
            sheets = xl.sheet_names
            target_sheet = next(
                (s for s in sheets if any(p in s.lower() for p in preferred)),
                sheets[0]
            )
            df = pd.read_excel(xl, sheet_name=target_sheet, header=None)
    except Exception as e:
        return {"splits": {}, "format": "unknown", "error": f"読み込みエラー: {e}"}

    # A/B フォーマット判定
    # 縦型の条件: 列数が少ない(<=3) かつ A列に既知スプリットキーワードが含まれる
    SPLIT_LABEL_KWS = ["swim","スイム","bike","バイク","run","ラン","t1","t2",
                       "finish","フィニッシュ","合計"]

    def _has_split_labels(col):
        """列の値にスプリットラベルが含まれているか確認"""
        vals = [str(v).lower().strip() for v in col if not pd.isna(v)]
        return sum(1 for v in vals if any(kw in v for kw in SPLIT_LABEL_KWS)) >= 2

    is_vertical = (
        df.shape[1] <= 3 and
        len(df) >= 2 and
        len(df) <= 25 and
        _has_split_labels(df.iloc[:, 0])
    )

    if is_vertical:
        return _parse_vertical_excel(df)

    # B) 横型: ヘッダー行を探してカラムマッピング
    return _parse_horizontal_excel(df, athlete_name, bib_number)


def _parse_vertical_excel(df):
    """縦型（自己管理シート）のパース"""
    splits = {}
    for _, row in df.iterrows():
        label = str(row.iloc[0]).strip() if not pd.isna(row.iloc[0]) else ""
        value = row.iloc[1] if len(row) > 1 else None
        if pd.isna(value) if hasattr(value, '__class__') else value is None:
            continue
        field = _match_split_key(label)
        if field:
            t_str = _normalize_time_str(value)
            if t_str:
                splits[field] = {"str": t_str, "sec": _time_str_to_sec(t_str)}
    return {"splits": splits, "format": "vertical", "matched_row": None, "error": None}


def _parse_horizontal_excel(df, athlete_name=None, bib_number=None):
    """
    横型（公式リザルト）のパース。
    ヘッダー行を自動検出し、選手名またはゼッケンで絞り込む。
    絞り込めない場合は最初のデータ行を使用。
    """
    # ヘッダー行を探す（swim/ラン等のキーワードが含まれる行）
    header_row_idx = None
    HEADER_KWS = ["swim","スイム","bike","バイク","run","ラン","finish","フィニッシュ","t1","t2"]
    for i, row in df.iterrows():
        row_str = " ".join(str(v).lower() for v in row if not pd.isna(v))
        if sum(1 for kw in HEADER_KWS if kw in row_str) >= 2:
            header_row_idx = i
            break

    if header_row_idx is None:
        # ヘッダーが見つからない → テキストとしてパース
        text = df.to_csv(index=False)
        splits = parse_split_times_from_text(text)
        return {"splits": splits, "format": "horizontal_fallback",
                "matched_row": None, "error": None}

    # ヘッダー行でDataFrameを再構成
    df.columns = [str(v).strip() for v in df.iloc[header_row_idx]]
    df = df.iloc[header_row_idx + 1:].reset_index(drop=True)

    # カラム名 → スプリットフィールドのマッピング
    col_field_map = {}
    for col in df.columns:
        field = _match_split_key(col)
        if field:
            col_field_map[col] = field

    # 選手名またはゼッケンで行を絞り込む
    target_row = None
    if athlete_name or bib_number:
        for _, row in df.iterrows():
            row_str = " ".join(str(v) for v in row if not pd.isna(v))
            if athlete_name and athlete_name in row_str:
                target_row = row
                break
            if bib_number and str(bib_number) in row_str:
                target_row = row
                break

    if target_row is None and len(df) > 0:
        target_row = df.iloc[0]  # 見つからなければ最初の行

    if target_row is None:
        return {"splits": {}, "format": "horizontal", "matched_row": None,
                "error": "対象行が見つかりません"}

    # スプリット抽出
    splits = {}
    matched = {}
    for col, field in col_field_map.items():
        if col in target_row.index:
            val = target_row[col]
            t_str = _normalize_time_str(val)
            if t_str:
                splits[field] = {"str": t_str, "sec": _time_str_to_sec(t_str)}
                matched[col] = t_str

    return {"splits": splits, "format": "horizontal",
            "matched_row": matched, "error": None}


def _find_activities_csv(base_dir=None):
    """
    activities CSVファイルを以下の優先順で検索する:
      1. i275804_activities.csv  (intervals.icu の標準エクスポート名)
      2. activities_detail.csv   (旧デフォルト名)
      3. *_activities.csv        (アスリートID_activities.csv の任意名)
      4. activities*.csv         (その他パターン)
    base_dir が None の場合はスクリプト隣・カレントディレクトリを検索。
    Returns: Path or None
    """
    import pathlib, glob as _glob
    search_dirs = []
    if base_dir:
        search_dirs.append(pathlib.Path(base_dir))
    search_dirs += [
        pathlib.Path(__file__).parent,
        pathlib.Path(__file__).parent.parent,
        pathlib.Path.cwd(),
    ]
    patterns = [
        "i275804_activities.csv",   # intervals.icu エクスポートの標準名
        "activities_detail.csv",    # 旧デフォルト
        "*_activities.csv",         # アスリートID_activities.csv
        "activities*.csv",          # その他パターン
    ]
    for d in search_dirs:
        for pat in patterns:
            if '*' in pat:
                matches = sorted(d.glob(pat))
                if matches:
                    return matches[0]
            else:
                p = d / pat
                if p.exists():
                    return p
    return None


def resolve_result_path(filename, cfg):
    """
    リザルトファイルのパスを解決する。
    検索優先順位:
      1. スクリプト隣の Results/ フォルダ（大文字小文字全パターン）
      2. config.yaml の results.folder 設定値
      3. スクリプトディレクトリ直下
      4. カレントディレクトリ直下

    Returns: (resolved_path: str, search_log: list[str])
    """
    import pathlib, os
    script_dir  = pathlib.Path(__file__).parent.resolve()
    cwd         = pathlib.Path.cwd().resolve()
    results_cfg = (cfg or {}).get("results", {})
    cfg_folder  = results_cfg.get("folder", "./Results")

    search_log = []  # どこを探したか記録

    # 検索フォルダリスト（優先順）
    search_dirs = []
    for base in [script_dir, cwd]:
        for dname in ["Results", "results", "RESULTS"]:
            d = base / dname
            search_dirs.append(d)
    # config.yaml の設定フォルダ
    cfg_path = pathlib.Path(cfg_folder).expanduser()
    if not cfg_path.is_absolute():
        cfg_path = script_dir / cfg_path
    if cfg_path not in search_dirs:
        search_dirs.append(cfg_path)
    # スクリプト直下・cwd直下
    for d in [script_dir, cwd]:
        if d not in search_dirs:
            search_dirs.append(d)

    # ファイル名の正規化（パス区切り文字対応）
    fname = pathlib.Path(filename).name  # ベース名のみ使う

    for d in search_dirs:
        candidate = d / fname
        exists = candidate.exists()
        search_log.append(f"{'✅' if exists else '❌'} {candidate}")
        if exists:
            return str(candidate), search_log

    # フルパス指定の場合はそのまま試す
    full = pathlib.Path(filename).expanduser()
    if full.exists():
        search_log.append(f"✅ {full} (フルパス)")
        return str(full), search_log

    search_log.append(f"→ 未発見: '{fname}'")
    return None, search_log

def parse_result_file(file_path, athlete_name=None, bib_number=None, cfg=None):
    """
    ファイル拡張子に応じてPDF/Excel/CSVを自動振り分けして解析する。

    cfg が渡された場合、resolve_result_path() でフォルダ検索を行う。
    ファイル名だけ（例: "横浜2025.xlsx"）でも動作する。

    対応拡張子:
      .pdf          → parse_pdf_result()
      .xlsx/.xls    → parse_excel_result()
      .csv          → parse_excel_result()
      その他テキスト → parse_split_times_from_text()

    戻り値: {"splits": {...}, "format": str, "resolved_path": str, "error": str|None}
    """
    # パス解決 (resolve_result_path は (path_str|None, log_list) を返す)
    if cfg is not None:
        resolved_str, _rlog = resolve_result_path(file_path, cfg)
        resolved = Path(resolved_str) if resolved_str else None
    else:
        resolved = Path(str(file_path)).expanduser()
        if not resolved.exists():
            resolved = None

    if resolved is None:
        # resolve_result_path が返したログを使って詳細なエラーメッセージを生成
        _log_lines = _rlog if cfg is not None else []
        _log_summary = "\n    ".join(_log_lines[:10]) if _log_lines else "(ログなし)"
        return {
            "splits": {}, "format": "not_found", "resolved_path": None,
            "error": (
                f"ファイルが見つかりません: '{file_path}'\n"
                f"  検索ログ:\n    {_log_summary}\n"
                f"  対処: スクリプトと同じ階層の 'Results/' フォルダにPDFを入れてください"
            )
        }

    ext = resolved.suffix.lower()

    if ext == ".pdf":
        result = parse_pdf_result(resolved)
        return {**result, "format": "pdf", "resolved_path": str(resolved)}

    elif ext in (".xlsx", ".xls", ".xlsm", ".csv"):
        # config.yaml から氏名・ゼッケンを補完
        if cfg and not athlete_name:
            athlete_name = cfg.get("results", {}).get("athlete_name")
        if cfg and not bib_number:
            bib_number = cfg.get("results", {}).get("bib_number")
        result = parse_excel_result(resolved, athlete_name, bib_number)
        return {**result, "resolved_path": str(resolved)}

    elif resolved.exists():
        try:
            text   = resolved.read_text(encoding="utf-8-sig", errors="replace")
            splits = parse_split_times_from_text(text)
            return {"splits": splits, "format": "text",
                    "resolved_path": str(resolved), "error": None}
        except Exception as e:
            return {"splits": {}, "format": "unknown",
                    "resolved_path": str(resolved), "error": str(e)}

    else:
        return {"splits": {}, "format": "unknown", "resolved_path": None,
                "error": f"ファイルが見つかりません: {resolved}"}




def load_race_splits_from_csv(csv_path, race_date_str=None, race_name_kws=None):
    """
    activities_detail.csv からレース当日（race=True）のスプリットを取得する。

    Args:
        csv_path:       activities_detail.csv のパス
        race_date_str:  "YYYY-MM-DD" 形式の日付（指定時その日のみ検索）
        race_name_kws:  レース名キーワードリスト（["YOKOHAMA","横浜"] など）

    Returns:
        {
          "swim":  {"time_s": 1608, "dist_m": 1451, "hr_avg": 143, ...},
          "t1":    {"time_s": 238},
          "bike":  {"time_s": 4184, "dist_m": 39952, "speed_kmh": 34.3, ...},
          "t2":    {"time_s": 18},
          "run":   {"time_s": 2562, "dist_m": 9330, "pace_per_km": 274, ...},
          "total_s": 8612,
          "source": "csv",
        }
        スプリットが見つからない場合は {}
    """
    import csv as _csv
    p = Path(str(csv_path)).expanduser()
    if not p.exists():
        return {}

    type_map = {
        "openwaterswim": "swim", "swim": "swim",
        "ride": "bike", "virtualride": "bike",
        "run": "run", "trailrun": "run",
        "transition": "transition",
    }

    rows = []
    try:
        with open(p, encoding="utf-8-sig", errors="replace") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                rtype_row = row.get("type","").lower()
                is_race = str(row.get("race","")).lower() in ("true","1")
                is_transition = rtype_row == "transition"
                if not is_race and not is_transition:
                    continue
                d = row.get("start_date_local","")[:10]
                name = row.get("name","")
                # 日付フィルタ
                if race_date_str and d != race_date_str:
                    continue
                # 名前フィルタ（Transitionには適用しない）
                if race_name_kws and rtype_row != "transition":
                    if not any(k.lower() in name.lower() for k in race_name_kws):
                        continue
                rows.append(row)
    except Exception:
        return {}

    if not rows:
        return {}

    splits = {}
    for row in sorted(rows, key=lambda x: x.get("start_date_local","")):
        rtype = row.get("type","").lower()
        key   = type_map.get(rtype)
        if not key or key == "transition":
            # T1/T2はTransitionで判定
            prev = list(splits.keys())
            if "swim" in prev and "bike" not in prev:
                key = "t1"
            elif "bike" in prev and "run" not in prev:
                key = "t2"
            else:
                continue

        time_s = int(float(row.get("moving_time") or 0))
        dist_m = float(row.get("distance") or 0)
        if not time_s and key not in ("t1","t2"):
            continue

        entry = {"time_s": time_s, "dist_m": dist_m,
                 "str": _fmt_time_s(time_s)}
        if row.get("average_heartrate"):
            entry["hr_avg"] = int(float(row["average_heartrate"]))
        if row.get("max_heartrate"):
            entry["hr_max"] = int(float(row["max_heartrate"]))
        if rtype in ("ride","virtualride") and row.get("average_speed"):
            entry["speed_kmh"] = round(float(row["average_speed"]) * 3.6, 1)
        if rtype in ("run","trailrun") and dist_m > 100:
            entry["pace_per_km"] = int(time_s / (dist_m / 1000))
        if rtype in ("openWaterSwim","swim") and dist_m > 100:
            entry["pace_per_100m"] = int(time_s / (dist_m / 100))

        splits[key] = entry

    if not splits:
        return {}

    total_s = sum(s.get("time_s",0) for s in splits.values())
    splits["total_s"] = total_s
    splits["total_str"] = _fmt_time_s(total_s)
    splits["source"] = "activities_detail_csv"
    return splits


def _fmt_time_s(sec):
    """秒 → H:MM:SS / M:SS 文字列"""
    m, s = divmod(int(sec), 60)
    h, m2 = divmod(m, 60)
    return f"{h}:{m2:02d}:{s:02d}" if h else f"{m}:{s:02d}"


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

def _fmt_pace(sec):
    m,s = divmod(int(sec),60); return f"{m}:{s:02d}"

def _pace_to_icu(sec):
    """秒/km → intervals.icu 絶対ペース表記 (M:SS/km)"""
    m, s = divmod(int(sec), 60)
    return f"{m}:{s:02d}/km"

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
                    "sprint":{"swim":750, "bike":20, "run":5},
                    "half":{"swim":1900,"bike":90,"run":21.1}}  # halfはmiddleの別名
        # RACE_DISTANCE_DEFS も参照（km→m変換）
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

# ============================================================
# 不足種目検出（設定で無効化可能）
# ============================================================
# IDEAL_WEEKLY は config.yaml の ideal_weekly_sessions から読み込む（load_config()後に設定）
IDEAL_WEEKLY = {"run":3,"bike":2,"swim":3,"strength":2}  # デフォルト（configなし時）

def detect_deficient_sports(weekly_counts, cfg=None):
    """
    14日間の実績から不足種目を検出（2週換算）
    cfg の training.balance_check: false で無効化
    """
    # 設定でオフにされている場合は空リストを返す
    if cfg:
        training_cfg = cfg.get("training") or {}
        if not training_cfg.get("balance_check", True):
            return []

    # ideal_weekly_sessions を config から読み込む（なければ定数 IDEAL_WEEKLY を使用）
    ideal_map = IDEAL_WEEKLY.copy()
    if cfg:
        cfg_ideal = cfg.get("ideal_weekly_sessions")
        if cfg_ideal:
            ideal_map.update({k: int(v) for k, v in cfg_ideal.items()})

    deficient = []
    for sport, ideal in ideal_map.items():
        actual = weekly_counts.get(sport, 0) / 2
        if actual < ideal * 0.7:
            deficient.append(sport)
    return deficient

# ============================================================
# 短時間セッション — 3軸自動選択
# ============================================================

# 短時間セッションDB: key=(mode, focus_sport) → list of options per duration
# mode: "load" (負荷必要) / "recovery" (回復優先) / "neutral"
# focus_sport: "swim" / "bike" / "run" / None

SHORT_SESSIONS = {
    # ─── 負荷が必要な時（peak/build + fresh/good）────────────────────────
    ("load", "swim"): {
        5:  ("strength", "肩・体幹アクティベーション（5分）",
             "スイムフォームの土台となる肩甲骨まわりと体幹を素早く目覚めさせます。\n"
             "腕振り10回→肩回し前後10回→バンドプル（またはタオル）×15回×2セット\n"
             "【目的】プル動作の効率化・肩の怪我予防"),
        15: ("strength", "スイム補強 ショート（15分）",
             "水中での推進力の源、肩・広背筋・体幹を集中強化。\n"
             "プッシュアップ3×12 / パイクPU3×10 / プランク3×40秒 / バードドッグ2×10\n"
             "【目的】ストローク力向上・泳力底上げ\n"
             "【モチベ】スイムが1番の弱点種目——ここで積み上げた筋力が水の中で活きる"),
        25: ("hiit", "スイム系HIIT（25分）",
             "泳げない日でも心肺とスイム筋を同時に刺激する陸上HIITです。\n"
             "W-up 3分 → バーピー×10/マウンテンクライマー×20/腕立て×15 × 4ラウンド 各ラウンド90秒rest\n"
             "【目的】スイムの有酸素基盤＋爆発力\n"
             "【モチベ】VO2maxを上げることがトライアスロン全3種目の底上げになる"),
        30: ("hiit", "スイム補強HIIT（30分）",
             "スイム特化の複合メニュー。陸上で泳ぎをシミュレートします。\n"
             "W-up 5分 → ①バードドッグ3×12 ②アーチャーPU3×8 ③コアローテーション3×15\n"
             "            ④プランク→サイドプランク×左右 → C-down 3分\n"
             "【目的】プル効率・体軸安定・入水角度改善\n"
             "【モチベ】1分あたりの練習密度を最大化する日——短くても確実に前進できる"),
    },
    ("load", "bike"): {
        5:  ("strength", "バイク用ケイデンス活性化（5分）",
             "バイク効率の核心、股関節まわりと臀筋を起動させます。\n"
             "ヒップサークル×20回 / グルートブリッジ×20回 / スクワット×15回\n"
             "【目的】死点なしの滑らかなペダリング準備"),
        15: ("strength", "バイク補強（15分）",
             "ペダリングの推進力となる臀筋・大腿四頭筋・股関節を強化します。\n"
             "スクワット3×15 / ランジ3×12(左右) / グルートブリッジ3×15 / カーフレイズ2×20\n"
             "【目的】ペダリング出力向上・長距離での失速防止\n"
             "【モチベ】バイクは3種目の中で最も時間を占める——ここの強化が総合タイムを変える"),
        25: ("hiit", "バイク系HIIT（25分）",
             "バイクの強度域を陸上で再現。VO2maxと乳酸閾値を同時に刺激します。\n"
             "W-up 3分 → ジャンプスクワット×10/ブルガリアSS×8/マウンテンクライマー×20 × 4ラウンド\n"
             "【目的】FTP向上の補助・バイクパワー基盤づくり\n"
             "【モチベ】今日のHIITが来週のFTPテストの数字を変える"),
        30: ("hiit", "バイク補強HIIT（30分）",
             "股関節の爆発力とケイデンス筋群を集中攻略する30分です。\n"
             "W-up 5分 → SSB(椅子)3×45秒/ジャンプスクワット3×10/シングルレッグRDL3×8 → C-down\n"
             "【目的】ペダリング効率・登坂力・TTパワー向上"),
    },
    ("load", "run"): {
        5:  ("strength", "ランアクティベーション（5分）",
             "ランニングエコノミーの鍵、臀筋と足首を起動させます。\n"
             "クラムシェル×15/グルートブリッジ×15/足首回し×10\n"
             "【目的】着地効率改善・ランニング障害予防"),
        15: ("strength", "ラン補強（15分）",
             "ランニングの推進力と衝撃吸収の土台を作る15分です。\n"
             "シングルレッグスクワット3×8 / ランジ3×12 / カーフレイズ3×20 / デッドバグ3×10\n"
             "【目的】ストライド効率・膝・足首の安定性向上\n"
             "【モチベ】ランの弱さはたいてい筋力不足——ここで積み上げることがPBへの近道"),
        25: ("hiit", "ラン系HIIT（25分）",
             "走れない日でもランの心肺と脚筋を同時に刺激するメニューです。\n"
             "W-up 3分 → ジャンプランジ×10/ボックスステップ×15/バーピー×8 × 4ラウンド\n"
             "【目的】VO2max・ランニングエコノミー向上\n"
             "【モチベ】陸上ドリルで走りが変わる——「速く走る」ための神経系刺激"),
        30: ("hiit", "ラン補強HIIT（30分）",
             "スピードに必要な爆発力と体幹安定性を同時に高めます。\n"
             "W-up 5分 → ①ジャンプスクワット3×12 ②シングルレッグRDL3×8 ③スプリントドリル(その場)\n"
             "          3×20秒 ④プランク×60秒 → C-down\n"
             "【目的】キックパワー・ピッチ改善・後半失速防止"),
    },
    ("load", None): {
        5:  ("strength", "全身アクティベーション（5分）",
             "全身の関節と神経系を短時間で起動させます。\n"
             "関節回し(首→肩→腰→膝→足首)各10回 / ジャンピングジャック30秒\n"
             "【目的】次のトレーニングの準備・怪我予防"),
        15: ("hiit", "全身HIIT（15分）",
             "15分で心拍数を上げ、全身の筋持久力を刺激するショートメニューです。\n"
             "W-up 2分 → バーピー×8/スクワット×15/プッシュアップ×12 × 3ラウンド(rest 60秒)\n"
             "【目的】代謝向上・心肺基盤づくり\n"
             "【モチベ】15分でも継続することが最強の習慣"),
        25: ("hiit", "全身HIIT（25分）",
             "トライアスロン全3種目に必要な心肺と筋持久力を凝縮した25分です。\n"
             "W-up 3分 → 4ラウンド(バーピー×10/マウンテンクライマー×20/ジャンプスクワット×10) → C-down\n"
             "【目的】有酸素能力・全身筋持久力の底上げ"),
        30: ("strength", "全身筋トレ（30分）",
             None),  # gen_strength_menu を使用
    },

    # ─── 回復優先（depleted/fatigued or taper/race_week）───────────────────
    ("recovery", "swim"): {
        5:  ("stretch", "スイム後ストレッチ（5分）",
             "泳いだ後に縮んだ肩・胸・脇腹をほぐします。水泳では珍しく、陸でのケアが重要です。\n"
             "胸ストレッチ(壁使用)30秒 / 肩甲骨寄せ10回 / 脇腹伸ばし左右30秒\n"
             "【目的】肩・首のコリ解消・次回練習の質を上げる"),
        15: ("yoga", "スイマーズヨガ（15分）",
             "水泳特有の前傾姿勢で固まった胸・肩・股関節を徹底的にほぐします。\n"
             "キャット&カウ8回 → ダウンドッグ30秒 → コブラポーズ30秒 → 鳩のポーズ左右30秒\n"
             "【目的】肩可動域回復・体軸改善\n"
             "【モチベ】柔軟性は地味だが確実に水中での抵抗を減らす"),
        20: ("yoga", "スイム・リカバリーヨガ（20分）",
             "泳ぎすぎた体を丁寧にリセットするフローです。\n"
             "太陽礼拝×3 → ねじりのポーズ左右 → 仰向けのストレッチ各1分 → シャバアーサナ3分\n"
             "【目的】肩・背中の疲労回復・睡眠の質向上"),
        30: ("yoga", "ディープリカバリーヨガ（30分）",
             "翌日のスイム練習のためのフル回復セッションです。\n"
             "全身モビリティフロー20分 + 呼吸法・シャバアーサナ10分\n"
             "【目的】副交感神経優位・深部疲労の解消"),
    },
    ("recovery", "bike"): {
        5:  ("stretch", "バイク後ハムスト・臀筋ストレッチ（5分）",
             "長時間のライディングで縮んだ股関節屈筋とハムストリングスをほぐします。\n"
             "ヒップフレクサーストレッチ左右各30秒 / ハムストレッチ各30秒 / ピジョンポーズ各30秒\n"
             "【目的】腰痛予防・次回ライドの出力回復"),
        15: ("yoga", "バイカーズヨガ（15分）",
             "前傾姿勢で縮んだ体を解放し、腸腰筋・股関節・背中をケアします。\n"
             "猫のポーズ8回 → 糸を通すポーズ → 低い弓のポーズ → 鳩のポーズ左右各45秒\n"
             "【目的】TT姿勢の維持・鼠径部の柔軟性向上\n"
             "【モチベ】バイクポジションの改善がそのままタイム短縮につながる"),
        20: ("stretch", "バイク・アクティブリカバリー（20分）",
             "フォームローラーで腸脛靭帯と大腿四頭筋をほぐし、股関節を解放します。\n"
             "フォームローラー:腸脛靭帯2分/大腿四頭筋2分/ふくらはぎ1分 → 動的ストレッチ15分\n"
             "【目的】脚の疲労回復・血流改善"),
        30: ("yoga", "バイク・ディープリカバリー（30分）",
             "バイクトレーニングの翌日に最適なヨガフローです。\n"
             "動的W-up5分 → ヨガフロー20分 → シャバアーサナ5分\n"
             "【目的】全身のリセット・FTP向上のための回復投資"),
    },
    ("recovery", "run"): {
        5:  ("stretch", "ラン後ふくらはぎ・ハムストレッチ（5分）",
             "ランニング後の下半身ケアの最低限。継続することで故障率が大幅に下がります。\n"
             "ふくらはぎ左右各30秒 / ハムスト各30秒 / 大腿四頭筋各30秒 / 腸腰筋各20秒\n"
             "【目的】遅発性筋肉痛軽減・膝・アキレス腱の保護"),
        15: ("yoga", "ランナーズヨガ（15分）",
             "ランニングで酷使した下半身と股関節を丁寧にほぐす15分です。\n"
             "座位の前屈 → ハーフピジョン左右 → 仰向けのハムストレッチ → 死者のポーズ3分\n"
             "【目的】腸腰筋・大腿筋膜張筋の柔軟性回復\n"
             "【モチベ】ケアをしっかりすることが一番のパフォーマンスアップ"),
        20: ("stretch", "ラン・アクティブリカバリー（20分）",
             "フォームローラーと動的ストレッチで下半身を徹底的にケアします。\n"
             "フォームローラー:腸脛靭帯/ハムスト/ふくらはぎ各2分 → 動的ストレッチ12分\n"
             "【目的】血流改善・筋繊維の修復促進"),
        30: ("yoga", "ランナーズ・ディープリカバリー（30分）",
             "長距離走の翌日に最適。股関節から足首まで全て解放するフローです。\n"
             "太陽礼拝×2 → ランナー向けポーズシーケンス20分 → 呼吸法5分\n"
             "【目的】疲労回復・精神的リセット・次回練習への準備"),
    },
    ("recovery", None): {
        5:  ("stretch", "全身ストレッチ（5分）",
             "全身の主要筋を5分でケアします。毎日続けることが一番の投資です。\n"
             "首→肩→背中→腰→大腿→ふくらはぎ 各30秒\n"
             "【目的】疲労回復・怪我予防"),
        10: ("stretch", "モビリティ（10分）",
             "トレーニング後の関節可動域を維持するためのルーティンです。\n"
             "股関節サークル×10/ワールドグレーテストストレッチ×6/胸椎回旋×10\n"
             "【目的】関節健康維持・次のセッションへの準備"),
        15: ("yoga", "ショートリカバリーヨガ（15分）",
             "疲れた心と体を15分でリセットするヨガです。\n"
             "猫のポーズ→ダウンドッグ→戦士のポーズ×左右→シャバアーサナ4分\n"
             "【目的】副交感神経優位・睡眠の質向上\n"
             "【モチベ】回復もトレーニングの一部——休むことで強くなる"),
        20: ("stretch", "アクティブリカバリー（20分）",
             "フォームローラーと動的ストレッチで体のリセットをかけます。\n"
             "フォームローラー全身10分 → 全身ストレッチ10分\n"
             "【目的】血流改善・筋繊維修復促進"),
        25: ("yoga", "リカバリーヨガ（25分）",
             "疲労が蓄積したときこそ、このヨガで体と向き合いましょう。\n"
             "全身モビリティフロー15分 → 呼吸法・シャバアーサナ10分\n"
             "【目的】自律神経調整・深部疲労の解消"),
        30: ("yoga", "ディープリカバリー（30分）",
             "過負荷の状態で最も効果的な回復手段がこれです。\n"
             "リストラティブヨガ25分 + シャバアーサナ5分\n"
             "【目的】HRV回復・全身のリセット"),
    },
    # ─── ニュートラル ────────────────────────────────────────────────────────
    ("neutral", None): {
        5:  ("stretch", "アクティベーション（5分）",
             "今日の練習を最大限に活かすための準備運動です。\n"
             "全身関節回し + ジャンピングジャック30秒\n"
             "【目的】怪我予防・パフォーマンス向上"),
        10: ("stretch", "モビリティ（10分）",
             "股関節・胸椎・足首を動かして可動域を維持します。\n"
             "ヒップサークル/ワールドグレーテストストレッチ/胸椎回旋 各10回\n"
             "【目的】動作効率改善"),
        15: ("yoga",    "ヨガ（15分）",
             "軽く体を動かしながら可動域と回復を両立させます。\n"
             "フローヨガ12分 + シャバアーサナ3分\n"
             "【目的】柔軟性維持・メンタルリセット"),
        20: ("stretch", "アクティブリカバリー（20分）",
             "フォームローラー + 全身ストレッチで体をケアします。\n"
             "【目的】疲労管理・次回練習への準備"),
        25: ("yoga",    "ヨガ（25分）",
             "体の状態に耳を傾けながら丁寧に動くセッションです。\n"
             "全身モビリティ + 呼吸法\n"
             "【目的】柔軟性・回復・メンタル強化"),
        30: ("strength", "筋トレ（30分）",
             None),  # gen_strength_menu を使用
    },
}

def pick_short_session(avail_min, cond_info, phase, deficient_sports,
                       strength_cfg, str_prog):
    """
    3軸（疲労度・フェーズ・不足種目）から最適な短時間セッションを選ぶ
    """
    cond  = cond_info["condition"]
    score = cond_info["score"]

    # モード決定
    if cond in ("depleted","fatigued") or phase in ("taper","race_week","recovery"):
        mode = "recovery"
    elif score >= 6.0 and phase in ("build","peak"):
        mode = "load"
    else:
        mode = "neutral"

    # フォーカス種目決定（不足種目を優先）
    focus = None
    if deficient_sports:
        # load時は最も不足している種目をフォーカス
        # recovery時はその種目向けのストレッチを選ぶ
        priority = ["swim","run","bike"]  # スイムを最優先
        for s in priority:
            if s in deficient_sports:
                focus = s
                break

    # セッションDB検索
    db = SHORT_SESSIONS.get((mode, focus)) or SHORT_SESSIONS.get((mode, None)) or {}
    if not db:
        db = SHORT_SESSIONS.get(("neutral", None), {})

    # 利用可能時間に合う最大のものを選ぶ
    thresholds = sorted(db.keys(), reverse=True)
    chosen_dur = 5
    for th in thresholds:
        if avail_min >= th:
            chosen_dur = th
            break

    sport, name, desc = db.get(chosen_dur, ("stretch","ストレッチ","全身ストレッチ"))
    actual_dur = min(avail_min, chosen_dur)

    # 30分筋トレはメニュー生成
    if desc is None:
        desc = gen_strength_menu(strength_cfg, phase, cond_info, str_prog, actual_dur)

    return {
        "sport": sport, "name": name, "description": desc,
        "duration_min": actual_dur,
        "mode": mode, "focus": focus,
    }

# ============================================================
# カレンダー解析（v4: ライバル・リザルト記載対応）
# ============================================================
def parse_gcal_day(events, cfg_cal, target_date, athlete=None):
    """
    1日分のGoogleカレンダーイベントを解析。
    レースイベントの説明欄から:
      - 過去リザルト（テキスト: 「過去: 2:15:30」「前回: 2:15:30」等）
      - 「レース結果から自動取得」→ Intervals.icuをレース名で検索
      - ライバル指定（「目標: ○○」）
    """
    is_weekend  = target_date.weekday() >= 5
    default_min = (cfg_cal["default_availability"]["weekend_max_min"] if is_weekend else 60)

    result = {
        "available_min":      default_min,
        "morning_ok":         True,
        "races":              [],
        "reduce_next_morning":False,
        "is_trip":            False,
        "notes":              [],
        "event_urls":         [],
        "rival_override":     None,
        "result_note":        None,
    }

    for ev in events:
        title    = (ev.get("summary") or "").strip()
        desc     = (ev.get("description") or "")
        tl       = title.lower()
        all_text = title + " " + desc

        # ── レースイベント ───────────────────────────────────
        is_race = any(k.lower() in tl for k in cfg_cal.get("race_keywords",[]))
        if is_race:
            pri  = ("A" if any(k in title for k in cfg_cal.get("race_priority_a_keywords",[]))
                    else "B")
            urls = re.findall(r'https?://[^\s\)\"\']+', desc)

            rival_m  = re.search(r'目標[:：\s]\s*(.{2,25})', desc)
            result_m = re.search(
                r'(?:過去|前回|リザルト|result|タイム|time)[:：\s]\s*'
                r'(\d{1,2}:\d{2}(?::\d{2})?)', desc, re.IGNORECASE)
            auto_fetch = bool(re.search(
                r'レース結果から自動取得|auto.?fetch|icu.?result', desc, re.IGNORECASE))

            past_result_data = None
            if result_m:
                t_str = result_m.group(1)
                parts = t_str.split(":")
                try:
                    t_sec = (int(parts[0])*3600+int(parts[1])*60+int(parts[2])
                             if len(parts)==3 else int(parts[0])*60+int(parts[1]))
                    past_result_data = {
                        "date": target_date.isoformat(), "name": title,
                        "sport": _detect_race_type(tl),
                        "time_s": t_sec, "time_str": t_str,
                        "dist_m": 0, "pace_s": 0, "pace_str": "",
                        "source": "calendar_text",
                        "distance": _detect_race_distance(tl),
                    }
                except: pass
            elif auto_fetch and athlete:
                found = search_icu_race_by_name(athlete, title, target_date.isoformat())
                if found:
                    past_result_data = found
                    result["notes"].append(f"📊 ICUリザルト自動取得: {found['time_str']}")

            result["races"].append({
                "name":        title,
                "date":        target_date.isoformat(),
                "type":        _detect_race_type(tl),
                "distance":    _detect_race_distance(tl),
                "priority":    pri,
                "urls":        urls,
                "rival":       rival_m.group(1).strip() if rival_m else None,
                "past_result": past_result_data,
            })
            result["event_urls"].extend(urls)
            result["available_min"] = 0
            result["notes"].append(f"🏁 レース: {title}")
            if rival_m:
                result["rival_override"] = rival_m.group(1).strip()
                result["notes"].append(f"🎯 目標ライバル: {rival_m.group(1).strip()}")
            if past_result_data:
                result["result_note"] = f"前回: {past_result_data['time_str']}"
                result["notes"].append(f"📈 {result['result_note']}")
            continue

        # ── 直接分数指定 ──────────────────────────────
        direct_min = None
        for pat in [r"(\d{1,3})\s*分\s*(?:練習|トレーニング|ラン|バイク|スイム)可能",
                    r"(?:練習|トレーニング)\s*(\d{1,3})\s*分",
                    r"(\d{1,3})\s*min\s*available"]:
            m = re.search(pat, all_text, re.IGNORECASE)
            if m: direct_min = int(m.group(1)); break
        if direct_min is not None:
            result["available_min"] = direct_min
            result["notes"].append(f"⏱ {direct_min}分練習可能（直接指定）")
            continue

        if any(k in title for k in cfg_cal.get("unavailable_keywords",[])):
            result["available_min"] = 0
            if any(k in title for k in cfg_cal.get("next_morning_reduce_keywords",[])):
                result["reduce_next_morning"] = True
            result["notes"].append(f"❌ 練習不可: {title}")
            continue

        trip_kws = ["出張","travel","trip"]
        if any(k in title for k in trip_kws):
            result["available_min"] = min(result["available_min"], 30)
            result["is_trip"] = True
            result["notes"].append(f"✈️ 出張 → 30分・前後日も軽減")
            continue

        if any(k in title for k in cfg_cal.get("wfh_keywords",[])):
            result["morning_ok"] = True
            result["available_min"] = min(result["available_min"]+30,
                                          cfg_cal["default_availability"]["weekend_max_min"])
            result["notes"].append(f"🏠 在宅 → 朝練可・+30分")
            continue

        if any(k in title for k in cfg_cal.get("no_morning_keywords",[])):
            result["morning_ok"] = False
            result["notes"].append(f"🏢 出社 → 朝練不可、夜練に変更")
            continue

        homecoming_hour = None
        for pattern in cfg_cal.get("homecoming_patterns",[]):
            m = re.search(pattern, all_text)
            if m: homecoming_hour = int(m.group(1)); break
        if homecoming_hour is not None:
            ev_end_h = int(cfg_cal["default_availability"]["evening_end"].split(":")[0])
            avail = max(0, int((ev_end_h-homecoming_hour-0.5)*60))
            avail = min(avail, 120)
            result["available_min"] = avail
            result["notes"].append(f"🏠 {homecoming_hour}時帰宅 → 練習可能 {avail}分")
            continue

        if any(k in title for k in cfg_cal.get("next_morning_reduce_keywords",[])):
            result["available_min"] = 0
            result["reduce_next_morning"] = True
            result["notes"].append(f"🍺 {title} → 夜練なし・翌朝強度↓")
            continue

        if any(k in title for k in cfg_cal.get("afternoon_available_keywords",[])):
            result["available_min"] = min(result["available_min"]+60,
                                          cfg_cal["default_availability"]["weekend_max_min"])
            result["notes"].append(f"🌤 {title} → 午後練習可 +60分")
            continue

    return result

def _detect_race_type(text):
    """テキストから競技種別を検出する"""
    t = text.lower()
    if any(k in t for k in ["triathlon","トライアスロン","トライ","tri "]):    return "triathlon"
    if any(k in t for k in ["sprint","スプリント"]) and "triathlon" not in t:   return "triathlon"
    if any(k in t for k in ["duathlon","デュアスロン"]):                        return "duathlon"
    if any(k in t for k in ["フルマラソン","full marathon","ハーフマラソン","half marathon"]): return "marathon"
    if any(k in t for k in ["marathon","マラソン","ランニング大会","road race"]): return "marathon"
    if re.search(r'\b(10|5)\s*km?\b|10キロ|5キロ', t):                       return "run"
    if any(k in t for k in ["swim","スイム","水泳","ows","open water"]):         return "swim"
    if any(k in t for k in ["cycling","ライド","サイクル","bike race"]):         return "cycling"
    if any(k in t for k in ["strength","筋トレ","weightlifting"]):              return "strength"
    if any(k in t for k in ["yoga","ヨガ","mobility","モビリティ","stretch","ストレッチ"]): return "yoga"
    if any(k in t for k in ["yoga","ヨガ"]):                                    return "yoga"
    return "race"

# ── 競技距離マスター ──────────────────────────────────────────────────────
# ITU / World Triathlon 競技規格準拠。各距離の swim/bike/run は km 単位。
RACE_DISTANCE_DEFS = {
    # ── トライアスロン ──────────────────────────────────────────────────
    "sprint":  {"rtype":"triathlon","swim":0.75, "bike":20,   "run":5,      "label":"スプリント(25.75km)"},
    "olympic": {"rtype":"triathlon","swim":1.5,  "bike":40,   "run":10,     "label":"オリンピック/OD(51.5km)"},
    "middle":  {"rtype":"triathlon","swim":1.9,  "bike":90,   "run":21.1,   "label":"ミドル/ハーフアイアン(113km)"},
    "iron":    {"rtype":"triathlon","swim":3.8,  "bike":180,  "run":42.2,   "label":"アイアンマン/フル(226km)"},
    # ── マラソン ────────────────────────────────────────────────────────
    "marathon":{"rtype":"marathon", "swim":0,    "bike":0,    "run":42.195, "label":"フルマラソン(42.195km)"},
    "half_run":{"rtype":"marathon", "swim":0,    "bike":0,    "run":21.0975,"label":"ハーフマラソン(21.097km)"},
    "10k":     {"rtype":"run",      "swim":0,    "bike":0,    "run":10,     "label":"10kmレース"},
    "5k":      {"rtype":"run",      "swim":0,    "bike":0,    "run":5,      "label":"5kmレース"},
    # ── デュアスロン ────────────────────────────────────────────────────
    "duathlon":{"rtype":"duathlon", "swim":0,    "bike":40,   "run":10,     "label":"デュアスロン・スタンダード"},
    # ── OWS ─────────────────────────────────────────────────────────────
    "ows":     {"rtype":"swim",     "swim":1.5,  "bike":0,    "run":0,      "label":"OWS"},
    # ── 筋トレ・ヨガ（距離なし）────────────────────────────────────────
    "strength":{"rtype":"strength", "swim":0,    "bike":0,    "run":0,      "label":"筋トレ"},
    "yoga":    {"rtype":"yoga",     "swim":0,    "bike":0,    "run":0,      "label":"ヨガ/モビリティ"},
    "yoga":    {"rtype":"yoga",     "swim":0,    "bike":0,    "run":0,      "label":"ヨガ"},
}

def _detect_race_distance(text):
    """テキストから競技距離コードを検出する（RACE_DISTANCE_DEFS のキーを返す）"""
    t = text.lower()
    # ── トライアスロン（長い距離から先に判定して誤検知を防ぐ）──────────
    if any(k in t for k in ["iron","アイアン","226","140.6"]):
        return "iron"
    if any(k in t for k in ["middle","ミドル","md ","113","70.3","half iron","half-iron","113km",
                             "ハーフアイアン","halfiro"]):
        return "middle"
    if any(k in t for k in ["olympic","オリンピック","51.5","standard","スタンダード"]):
        return "olympic"
    # タイトルに「OD」が含まれる（「横浜トライアスロン　OD」など）
    if re.search(r'\bOD\b|[ 　]OD$|[ 　]OD[ 　]', text):
        return "olympic"
    if any(k in t for k in ["sprint","スプリント","25.75"]):
        return "sprint"
    # ── マラソン（フルを先に）──────────────────────────────────────────
    if any(k in t for k in ["フルマラソン","full marathon","42.195","42km"]):
        return "marathon"
    if any(k in t for k in ["ハーフマラソン","half marathon","21.097","21.1km","21km"]):
        return "half_run"
    if re.search(r'\b10\s*km?\b|10キロ', t):  return "10k"
    if re.search(r'\b5\s*km?\b|5キロ',  t):   return "5k"
    # 「マラソン」単体 → フルマラソンとみなす
    if "マラソン" in text or "marathon" in t:
        return "marathon"
    # ── その他 ──────────────────────────────────────────────────────────
    if any(k in t for k in ["duathlon","デュアスロン"]):  return "duathlon"
    if any(k in t for k in ["ows","open water","オープンウォーター"]): return "ows"
    return "unknown"


def _parse_race_priority(desc, title=""):
    """
    カレンダー説明欄・タイトルからレース優先度を解析する。

    対応パターン:
      「優先度A」「優先度：A」「Priority A」
      「本命」「メインレース」「Aレース」
      「練習レース」「お試し」「Bレース」「Cレース」
      「ミドル」「アイアン」などの距離キーワード（デフォルトA扱い）

    戻り値: "A" / "B" / "C"
    """
    text = (desc + " " + title).lower()

    # 明示的なA判定
    if re.search(r'(?:優先度|priority|pr)[:\s：]*a\b', text, re.IGNORECASE):
        return "A"
    if re.search(r'(?:本命|メインレース|aレース|a.?race)', text, re.IGNORECASE):
        return "A"

    # 明示的なB判定
    if re.search(r'(?:優先度|priority|pr)[:\s：]*b\b', text, re.IGNORECASE):
        return "B"
    if re.search(r'(?:bレース|b.?race)', text, re.IGNORECASE):
        return "B"

    # 明示的なC判定
    if re.search(r'(?:優先度|priority|pr)[:\s：]*c\b', text, re.IGNORECASE):
        return "C"
    if re.search(r'(?:練習レース|お試し|cレース|c.?race|練習大会)', text, re.IGNORECASE):
        return "C"

    # キーワードなし → タイトルから推定
    # ミドル・アイアンは距離の長さからAとみなす
    title_lower = title.lower()
    if any(k in title_lower for k in ["アイアン","iron","ハーフ","half","ミドル","middle"]):
        return "A"

    return "B"  # デフォルト


def apply_trip_adjacency(gcal_days_map):
    trip_dates = {d for d,v in gcal_days_map.items() if v.get("is_trip")}
    for td_str in trip_dates:
        td = date.fromisoformat(td_str)
        for delta in [-1,1]:
            neighbor = (td+timedelta(days=delta)).isoformat()
            if neighbor in gcal_days_map:
                gcal_days_map[neighbor]["available_min"] = min(
                    gcal_days_map[neighbor]["available_min"], 60)
                gcal_days_map[neighbor]["notes"].append("✈️ 出張前後日 → 負荷軽減")
    return gcal_days_map


# ============================================================
# カレンダーコメントからのトレーニング指示（ディレクティブ）解析
# ============================================================

def parse_training_directive(title, description):
    """
    イベントのタイトル・説明欄から「どんなトレーニング目標に向けて準備するか」を解析し、
    generate_days() が参照できる directive dict を返す。

    対応パターン例:
      「100㎞バイク20㎞ランをするSTU練習会まで週末はできるだけ本番に対応できるメニューにして」
      「フルマラソンのペース走中心で」
      「スイム強化期間にして」
      「スイム 1.5km バイク 40km ラン 10km の本番想定で」

    戻り値:
    {
      "raw":          元文字列,
      "target_event": イベント名（タイトル）,
      "target_date":  対象日（str），
      "priority_sports": ["bike","run"],     # 重点種目
      "target_distances": {"bike":100,"run":20}, # 目標距離(km)
      "target_intensity": "race_sim",        # race_sim/threshold/endurance/speedwork
      "weekend_focus":    True,              # 週末に本番想定を集中させるか
      "description":      人間向け要約,
    }
    """
    text = (title + " " + description).lower()

    # 距離抽出パターン（優先度順）
    # パターン1: "100㎞バイク" (数字→単位→種目)
    # パターン2: "バイク100㎞" (種目→数字→単位)
    DIST_PATTERNS = {
        "swim":  [r'(\d+\.?\d*)\s*(?:km|㎞|キロ)\s*(?:swim|スイム|水泳)',
                  r'(?:swim|スイム|水泳)\s*(\d+\.?\d*)\s*(?:km|㎞|キロ)'],
        "bike":  [r'(\d+\.?\d*)\s*(?:km|㎞|キロ)\s*(?:バイク|bike|cycle|サイクル)',
                  r'(?:バイク|bike|cycle|サイクル)\s*(\d+\.?\d*)\s*(?:km|㎞|キロ)'],
        "run":   [r'(\d+\.?\d*)\s*(?:km|㎞|キロ)\s*(?:ラン|run|マラソン)',
                  r'(?:ラン|run|マラソン)\s*(\d+\.?\d*)\s*(?:km|㎞|キロ)'],
    }

    target_distances = {}
    for sport, pats in DIST_PATTERNS.items():
        for pat in pats:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                target_distances[sport] = float(m.group(1))
                break

    # 重点種目を距離情報から推定
    priority_sports = list(target_distances.keys()) if target_distances else []

    # 「本番に対応」「レースシム」「レースペース」キーワード
    is_race_sim = bool(re.search(
        r'本番|race.sim|レースシム|レースペース|本番想定|race.pace|全力|レース強度',
        text, re.IGNORECASE))

    # 「スピード」「インターバル」指定
    is_speedwork = bool(re.search(
        r'インターバル|スピード|speed|interval|VO2|閾値|threshold', text, re.IGNORECASE))

    # 「持久力」「エンデュランス」指定
    is_endurance = bool(re.search(
        r'持久|エンデュランス|endurance|長距離|ロング', text, re.IGNORECASE))

    # 「週末に」「weekend」指定
    weekend_focus = bool(re.search(
        r'週末|土日|weekend|saturday|sunday|土曜|日曜', text, re.IGNORECASE))

    # 強度分類
    if is_race_sim:
        target_intensity = "race_sim"
    elif is_speedwork:
        target_intensity = "threshold"
    elif is_endurance:
        target_intensity = "endurance"
    else:
        target_intensity = "race_sim" if target_distances else "build"

    # 人間向け要約生成
    parts = []
    if target_distances:
        dist_str = " + ".join(
            f"{s.upper()}{d:.0f}km" for s, d in target_distances.items())
        parts.append(dist_str)
    if is_race_sim:
        parts.append("本番想定強度")
    if weekend_focus:
        parts.append("週末に集中")
    desc_summary = " / ".join(parts) if parts else "トレーニング強化"

    return {
        "raw":              description,
        "target_event":     title,
        "priority_sports":  priority_sports,
        "target_distances": target_distances,
        "target_intensity": target_intensity,
        "weekend_focus":    weekend_focus,
        "is_race_sim":      is_race_sim,
        "description":      desc_summary,
    }


def build_directive_template(directive, base_phase_template, num_days, start_date=None):
    """
    ディレクティブの内容から、generate_days() 用のテンプレートを動的生成する。
    start_date を渡すと実際の曜日に基づいて週末/平日を正確に判定する。
    """
    td    = directive.get("target_distances", {})
    prio  = directive.get("priority_sports", [])
    inten = directive.get("target_intensity", "race_sim")
    weekend = directive.get("weekend_focus", True)

    if start_date is None:
        start_date = date.today() + timedelta(days=1)

    # 種目ごとの推奨セッション時間（距離から逆算）
    def est_duration(sport, dist_km):
        speeds = {"swim": 2.5, "bike": 30.0, "run": 10.0}  # km/h
        return int(dist_km / speeds.get(sport, 10) * 60)

    # 週末用のロングセッション（本番距離の70〜90%）
    weekend_sessions = []
    if "bike" in td and td["bike"] >= 60:
        long_bike_min = min(300, est_duration("bike", td["bike"] * 0.85))
        weekend_sessions.append(("bike", "race_sim", long_bike_min))
    if "run" in td and td["run"] >= 10:
        long_run_min  = min(180, est_duration("run", td["run"] * 0.85))
        weekend_sessions.append(("run", "race_sim", long_run_min))
    # バイク+ランの両方ある場合は日曜にブリック
    if "bike" in td and "run" in td:
        brick_bike_min = min(180, est_duration("bike", td["bike"] * 0.5))
        brick_run_min  = min(90,  est_duration("run",  td["run"] * 0.6))
        weekend_sessions = [
            ("bike", "race_sim", min(300, est_duration("bike", td["bike"] * 0.85))),
            ("brick", f"bike{brick_bike_min}+run{brick_run_min}",
             brick_bike_min + brick_run_min),
        ]

    # 平日用の種目別強化セッション
    weekday_pool = []
    if "bike" in prio or "bike" in td:
        dur = min(120, est_duration("bike", td.get("bike", 40) * 0.4))
        weekday_pool.append(("bike", "threshold", max(60, dur)))
        weekday_pool.append(("bike", "sweetspot", max(75, dur)))
    if "run" in prio or "run" in td:
        dur = min(90, est_duration("run", td.get("run", 10) * 0.5))
        weekday_pool.append(("run", "tempo",    max(50, dur)))
        weekday_pool.append(("run", "long",     max(60, dur)))
    if "swim" in prio or "swim" in td:
        dur = min(60, est_duration("swim", td.get("swim", 1.5) * 0.8))
        weekday_pool.append(("swim", "threshold", max(45, dur)))
    if not weekday_pool:
        weekday_pool = base_phase_template

    # num_days 分のテンプレートを実際の曜日に基づいて組み立てる
    template = []
    wd_pool_idx = 0
    ws_pool_idx = 0
    for i in range(num_days):
        target_day = start_date + timedelta(days=i)
        dow = target_day.weekday()  # 0=月 6=日
        is_weekend = (dow >= 5)

        if is_weekend and weekend and weekend_sessions:
            ws_idx = ws_pool_idx % len(weekend_sessions)
            template.append(weekend_sessions[ws_idx])
            ws_pool_idx += 1
        else:
            if dow == 0:  # 月曜はリカバリー
                template.append(("run", "easy", 40))
            elif dow == 3:  # 水曜に筋トレ
                template.append(("strength", "full", 40))
            else:
                wd = weekday_pool[wd_pool_idx % len(weekday_pool)]
                template.append(wd)
                wd_pool_idx += 1

    return template




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
    "build":     "ビルド期：体が変わってくる時期。ここを乗り越えた先に大きな伸びがあります。",
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


def build_workout(sport, intensity, dur, phase, tp, ftp, goal_targets=None, css=None):
    """
    intervals.icu のワークアウトビルダー形式テキスト (workout_doc) と
    CLI 表示用の日本語説明文 (desc_text) を返す。

    workout_doc は intervals.icu の description フィールドに渡すと
    構造化されたステップグラフが生成される。

    Returns: (workout_doc: str, desc_text: str)
    """
    label, purpose = INTENSITY_LABELS.get(intensity, ("", ""))
    motivation = PHASE_MOTIVATIONS.get(phase, "")

    # ─── RUN ──────────────────────────────────────────────────────
    # エビデンス参考文献:
    #   Seiler & Kjerland 2006 (Scand J Med Sci Sports): 偏極化80/20の実証
    #   Muñoz et al. 2014 (Int J Sports Physiol Perf): ランで偏極化>閾値
    #   Neal et al. 2013 (J Appl Physiol): 偏極化でVO2max・閾値とも向上
    #   Stöggl & Sperlich 2014 (Front Physiol): 偏極化>閾値>HIIT>HVT
    #   Billat et al. 2001 (Med Sci Sports Exerc): 30/30インターバルでvVO2maxを維持
    #   Daniels 2014 "Daniels' Running Formula": クルーズインターバル理論
    #   Esteve-Lanao et al. 2007 (J Strength Cond Res): 偏極化vs閾値5か月比較
    #   Millet et al. 2011 (Eur J Appl Physiol): トライアスロン特化ラン強度配分
    #   Laursen & Jenkins 2002 (Sports Med): HIIT効果の包括的レビュー
    #   Baechle & Earle 2008 "Essentials of Strength and Conditioning": ランドリル
    #   Barnes & Kilding 2015 (Sports Med): ランニングエコノミー向上因子
    # Garmin FIT互換メモ:
    #   intervals.icu からGarmin Connectへ自動sync可能 (Settings > Garmin)
    #   "Warmup"/"Cooldown"ヘッダー→GarminのStep Type warmup/cooldown に自動変換
    #   "Z2 Pace"等のゾーン指定→アスリートのTP設定に基づきGarminに展開される
    #   距離指定("400mtr")はGarmin対応 / 時間指定("4m")もGarmin対応
    #   Garmin Connectワークアウト形式: stepType(warmup/active/rest/cooldown)
    #     endConditionType(time/distance/lapButton)
    #     targetType(pace/heartRate/power/cadence/speed/no.target)
    #   intervals.icuのワークアウトは Settings > Connected Apps > Garmin で
    #   自動的にGarmin Connectに同期される (Garmin Connect API v2 互換)
    #   ペースゾーン(Z1-Z5)はGarmin上ではアスリートのテンポ走ペース(TP)から自動計算
    #   Garmin CoachのRun Easy/Tempo/Long/Interval分類と対応:
    #     recovery/easy → Garmin "Easy Run"  moderate → Garmin "Tempo Run"
    #     hard → Garmin "Interval Run"  long easy → Garmin "Long Run"
    if sport == "run":
        pace_base = {
            "recovery": tp * 1.40,
            "easy":     tp * 1.20,
            "moderate": tp * 1.06,
            "tempo":    tp * 1.09,
            "hard":     tp * 0.96,
        }.get(intensity, tp * 1.20)

        # seedベースでセッションバリエーション選択 (毎回違うメニュー)
        import hashlib as _hlib
        _seed_val = int(_hlib.md5(f"{phase}|{dur}|{intensity}|run".encode()).hexdigest()[:8], 16)

        if intensity == "hard":
            # 7タイプ:
            #   A: 閾値インターバル 4分×N (Seiler推奨の核心セッション)
            #   B: 1km VO2maxインターバル × N (Neal 2013: VO2max向上に最も有効)
            #   C: ファルトレク 30/60 × N (Stöggl 2014: 神経筋x代謝系の二重刺激)
            #   D: 400mスピードレップ × N (Muñoz 2014: 最高速度開発)
            #   E: 800mレースペース × N (Billat 2001: vVO2max維持による乳酸閾値向上)
            #   F: ヒル走 90秒上り×N (Barnes 2015: ランエコノミー改善・筋力向上)
            #   G: 3分HIITインターバル × N (Laursen 2002: VO2max刺激の最短経路)
            _type = _seed_val % 7
            wu_min = 10; cd_min = 5; main_min = dur - wu_min - cd_min
            ip  = _pace_to_icu(int(tp * 0.96))   # 閾値ペース (TP×0.96)
            rp  = _pace_to_icu(int(tp * 1.25))   # リカバリーペース
            vp  = _pace_to_icu(int(tp * 0.90))   # VO2maxペース≈5kmペース
            sp  = _pace_to_icu(int(tp * 0.86))   # スプリントペース≈1500mペース

            if _type == 0:
                reps = max(3, min(6, main_min // 6))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"Threshold Intervals {reps}x",
                    f"- 4m {ip}",
                    f"- 90s {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【閾値インターバル A】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_min}分(Z2) → 閾値 {ip} × 4分 × {reps}本(rest 90秒) → CD {cd_min}分\n"
                    f"📚 Seiler 2006: 4分以上の閾値インターバルが乳酸閾値を最も引き上げる\n"
                    f"💪 {motivation}"
                )
            elif _type == 1:
                reps = max(3, min(8, main_min // 6))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"VO2max Intervals {reps}x",
                    f"- 1km {vp}",
                    f"- 3m {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【1km VO2maxインターバル】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_min}分 → 1km({vp}) × {reps}本(rest 3分) → CD {cd_min}分\n"
                    f"📚 Neal 2013: VO2max刺激が偏極化の高強度20%側の核心\n"
                    f"💪 {motivation}"
                )
            elif _type == 2:
                pairs = max(8, min(15, main_min // 2))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"Fartlek {pairs}x",
                    f"- 30s {vp}",
                    f"- 60s {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【ファルトレク】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_min}分 → 30秒全力({vp})/60秒ジョグ({rp}) × {pairs}本 → CD {cd_min}分\n"
                    f"📚 Stöggl 2014: 短い高強度でも神経筋・代謝系に強い刺激をもたらす\n"
                    f"💪 {motivation}"
                )
            elif _type == 3:
                reps = max(4, min(10, main_min // 4))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"Speed Reps {reps}x",
                    f"- 400mtr {sp}",
                    f"- 2m {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【400mスピードレップ】{dur}分\n"
                    f"目的: 最高速度・神経筋出力向上\n"
                    f"WU {wu_min}分 → 400m({sp}) × {reps}本(rest 2分) → CD {cd_min}分\n"
                    f"📚 Muñoz 2014: 偏極化の高強度側はVO2maxを超える速度も有効\n"
                    f"💪 {motivation}"
                )
            elif _type == 4:
                # Type E: 800mレースペースインターバル (Billat 2001)
                r800p = _pace_to_icu(int(tp * 0.91))  # 800m相当ペース
                reps = max(3, min(8, main_min // 6))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"800m Race Pace {reps}x",
                    f"- 800mtr {r800p}",
                    f"- 90s {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【800mレースペースインターバル】{dur}分\n"
                    f"目的: vVO2max維持時間の延伸・乳酸耐性\n"
                    f"WU {wu_min}分 → 800m({r800p}) × {reps}本(rest 90秒) → CD {cd_min}分\n"
                    f"📚 Billat 2001: vVO2max付近での反復が最大酸素摂取量の実用閾値を押し上げる\n"
                    f"💪 {motivation}"
                )
            elif _type == 5:
                # Type F: ヒル走 (Barnes & Kilding 2015 - ランエコノミー改善)
                hill_p = _pace_to_icu(int(tp * 0.95))  # ヒルアップはフラット閾値ペース相当
                reps = max(4, min(10, main_min // 4))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"Hill Reps {reps}x",
                    f"- 90s {hill_p}",
                    f"- 90s {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【ヒル走】{dur}分\n"
                    f"目的: ランエコノミー向上・股関節伸展筋力強化\n"
                    f"WU {wu_min}分 → 坂道90秒({hill_p})×{reps}本(下り90秒ジョグ) → CD {cd_min}分\n"
                    f"📚 Barnes 2015 Sports Med: ヒル走はストライド長・地面接触時間を改善\n"
                    f"💡 勾配4-8%の坂を強い前傾で上る / Garmin CoachのHill Sprint対応\n"
                    f"💪 {motivation}"
                )
            else:
                # Type G: 3分HIITインターバル (Laursen & Jenkins 2002)
                hiit_p = _pace_to_icu(int(tp * 0.92))
                reps = max(3, min(6, main_min // 6))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"HIIT {reps}x",
                    f"- 3m {hiit_p}",
                    f"- 3m {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【3分HIITインターバル】{dur}分\n"
                    f"目的: VO2max刺激・有酸素・無酸素の境界を拡張\n"
                    f"WU {wu_min}分 → 3分({hiit_p})/{reps}本(3分ジョグ) → CD {cd_min}分\n"
                    f"📚 Laursen 2002 Sports Med: 3-5分HIITが最もVO2max向上効率が高い\n"
                    f"📚 Millet 2011 Eur J Appl Physiol: トライアスロンのランパフォーマンスは\n"
                    f"     VO2maxと乳酸閾値の両方に依存する\n"
                    f"💪 {motivation}"
                )

        elif intensity in ("moderate", "tempo"):
            # 5タイプ: A=テンポ連続, B=クルーズインターバル, C=ビルドアップ
            #           D=マラソンペース走, E=テンポ+スピードフィニッシュ
            _type = _seed_val % 5
            wu_min = 10; cd_min = 5; main_min = dur - wu_min - cd_min
            t_slow = _pace_to_icu(int(tp * 1.08))
            t_fast = _pace_to_icu(int(tp * 1.02))
            rp     = _pace_to_icu(int(tp * 1.20))
            z4p    = _pace_to_icu(int(tp * 0.98))
            mp     = _pace_to_icu(int(tp * 1.12))  # マラソンペース (TP+12%)

            if _type == 0:
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    "Tempo",
                    f"- {main_min}m Z3 Pace",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【テンポラン】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_min}分 → テンポ {main_min}分(Z3:{t_slow}〜{t_fast}) → CD {cd_min}分\n"
                    f"📚 Esteve-Lanao 2007: 閾値付近の持続走が乳酸処理能力を向上\n"
                    f"💪 {motivation}"
                )
            elif _type == 1:
                reps = max(2, min(4, main_min // 12))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"Cruise Intervals {reps}x",
                    f"- 10m {t_slow}-{t_fast} Pace",
                    f"- 2m {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【クルーズインターバル】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_min}分 → 10分テンポ × {reps}本(rest 2分ジョグ) → CD {cd_min}分\n"
                    f"📚 Daniels 2014 Running Formula: クルーズインターバルはLT強化の最効率手段\n"
                    f"💡 テンポ中は「何とか会話できる」程度の強度で\n"
                    f"💪 {motivation}"
                )
            elif _type == 2:
                seg = main_min // 3
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z1 Pace",
                    "",
                    "Build Phase 1",
                    f"- {seg}m Z2 Pace",
                    "",
                    "Build Phase 2",
                    f"- {seg}m Z3 Pace",
                    "",
                    "Build Phase 3",
                    f"- {seg}m {t_fast}-{z4p} Pace",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【ビルドアップラン】{dur}分\n"
                    f"目的: ペース感覚・エネルギー切り替え訓練\n"
                    f"WU {wu_min}分 → Z2({seg}分) → Z3({seg}分) → Z4({seg}分) → CD {cd_min}分\n"
                    f"📚 後半に向けてペースを上げる → レースの後半強さに直結\n"
                    f"💪 {motivation}"
                )
            elif _type == 3:
                # Type D: マラソンペース走 (Midrace-specific conditioning)
                marathon_seg = main_min
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    "Marathon Pace",
                    f"- {marathon_seg}m {mp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【マラソンペース走】{dur}分\n"
                    f"目的: レース特異的ペース感覚・脂肪+糖質混合燃焼訓練\n"
                    f"WU {wu_min}分 → MPペース({mp}) {marathon_seg}分 → CD {cd_min}分\n"
                    f"📚 Daniels 2014: Mペース走は高強度セッションの前後に配置すると効果的\n"
                    f"💡 Garmin CoachのMarathon Pace Run対応ペース帯\n"
                    f"💪 {motivation}"
                )
            else:
                # Type E: テンポ+スピードフィニッシュ (negative split training)
                seg1 = main_min * 2 // 3
                seg2 = main_min - seg1
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    "Tempo Phase",
                    f"- {seg1}m {t_slow} Pace",
                    "",
                    "Speed Finish",
                    f"- {seg2}m {t_fast}-{z4p} Pace",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【テンポ+スピードフィニッシュ】{dur}分\n"
                    f"目的: ネガティブスプリット習得・後半加速能力\n"
                    f"WU {wu_min}分 → テンポ{seg1}分({t_slow}) → スピード{seg2}分({t_fast}〜{z4p}) → CD {cd_min}分\n"
                    f"📚 レース後半に加速できる選手はネガティブスプリット習慣を持つ傾向がある\n"
                    f"💪 {motivation}"
                )

        else:  # easy / recovery
            # 5タイプ: A=Z2ステディ, B=ストライドあり, C=ロング走
            #           D=ドリルラン(ランエコノミー), E=走り込み(Z1→Z2プログレッシブ)
            _type = _seed_val % 5
            pz = run_pace_zones(tp)
            z_num = 1 if intensity == "recovery" else 2
            z_info = pz[z_num]
            zone_tag = f"Z{z_num} Pace"
            km = round(dur * 60 / pace_base, 1)
            stride_p = _pace_to_icu(int(tp * 0.88))
            jog_p    = _pace_to_icu(int(tp * 1.20))

            if _type == 0 or intensity == "recovery":
                workout_doc = (
                    f"Easy Run\n"
                    f"- {dur}m {zone_tag}\n"
                )
                desc_text = (
                    f"【{label}】ラン {dur}分\n"
                    f"目的: 有酸素基盤・脂肪代謝向上\n"
                    f"目標ペース: {z_info['label']}  推定距離: {km}km\n"
                    f"({zone_tag} / TP={_fmt_pace(int(tp))}/km 基準)\n"
                    f"📚 Seiler: 80%のセッションをZ1-Z2で。会話できるペースを守る\n"
                    f"💪 {motivation}"
                )
            elif _type == 1:
                workout_doc = "\n".join([
                    f"Easy Run",
                    f"- {dur - 8}m {zone_tag}",
                    "",
                    "Strides 6x",
                    f"- 20s {stride_p}",
                    f"- 40s {jog_p}",
                    ""
                ])
                desc_text = (
                    f"【Z2+ストライド】{dur}分\n"
                    f"目的: 有酸素 + 神経筋活性化\n"
                    f"Z2 {dur-8}分 → 20秒ストライド × 6本(40秒ジョグ)\n"
                    f"📚 ストライドは「速く走れる感覚」を身体に思い出させる低コストドリル\n"
                    f"💪 {motivation}"
                )
            elif _type == 2:
                workout_doc = "\n".join([
                    "Long Easy Run",
                    f"- {dur // 4}m Z1 Pace",
                    "",
                    "Long Main",
                    f"- {dur - dur // 4 - 5}m Z2 Pace",
                    "",
                    "Finish",
                    f"- 5m Z1 Pace",
                    ""
                ])
                desc_text = (
                    f"【ロング走】{dur}分\n"
                    f"目的: 有酸素基盤・脂肪燃焼・精神的持久力\n"
                    f"最初{dur//4}分Z1 → {dur - dur//4 - 5}分Z2 → 5分Z1\n"
                    f"推定距離: {km}km\n"
                    f"📚 Stöggl 2014: 低強度長時間はミトコンドリア密度を最も効率よく向上\n"
                    f"💪 {motivation}"
                )
            elif _type == 3:
                # Type D: ドリルラン (Barnes 2015: ランニングエコノミー改善)
                drill_min = min(8, dur // 5)
                easy_min = dur - drill_min
                workout_doc = "\n".join([
                    "Drills",
                    f"- {drill_min}m Z1 Pace",
                    "",
                    "Easy Run",
                    f"- {easy_min}m {zone_tag}",
                    ""
                ])
                desc_text = (
                    f"【ドリル+イージーラン】{dur}分\n"
                    f"目的: ランニングフォーム改善・エコノミー向上\n"
                    f"ドリル{drill_min}分(Aスキップ/Bスキップ/バインディング/ハイニー) → Z2 {easy_min}分\n"
                    f"📚 Barnes 2015 Sports Med: ドリル継続でストライドコンタクト時間が短縮\n"
                    f"📚 Baechle 2008: ランドリルは神経筋コーディネーションを直接改善\n"
                    f"💪 {motivation}"
                )
            else:
                # Type E: Z1→Z2プログレッシブ (aerobic base building)
                seg1 = dur // 2; seg2 = dur - seg1
                workout_doc = "\n".join([
                    "Aerobic Phase 1",
                    f"- {seg1}m Z1 Pace",
                    "",
                    "Aerobic Phase 2",
                    f"- {seg2}m Z2 Pace",
                    ""
                ])
                desc_text = (
                    f"【プログレッシブ有酸素走】{dur}分\n"
                    f"目的: 有酸素基盤強化 + 後半の微加速習慣\n"
                    f"前半{seg1}分Z1({zone_tag}) → 後半{seg2}分Z2\n"
                    f"推定距離: {km}km\n"
                    f"📚 低強度から徐々に上げることで乳酸をゆっくり産生→回収の循環を作る\n"
                    f"💪 {motivation}"
                )

        if goal_targets and goal_targets.get("race_run_pace"):
            rp = goal_targets["race_run_pace"]
            desc_text += f"\n🎯 レース目標ペース: {_pace_to_icu(rp)} — 今日は基礎を積みます"

        return workout_doc, desc_text

    # ─── BIKE ─────────────────────────────────────────────────────
    # エビデンス参考文献:
    #   Coggan & Hunter 2003 (Training and Racing with a Power Meter): FTPゾーン定義
    #   Neal et al. 2013 (J Appl Physiol): 偏極化でVO2max・閾値とも向上
    #   Laursen & Jenkins 2002 (Sports Med): HIIT効果の包括的レビュー
    #   Rønnestad et al. 2014 (Int J Sports Physiol Perf): ミクロバースト(40/20)の有効性
    #   Seiler & Tønnessen 2009 (Int J Sports Physiol Perf): エリート持久系選手の強度配分
    #   Friel 2009 "The Cyclist's Training Bible": TSS/IF/SST理論
    #   Skiba et al. 2012 (J Sports Sci): W'bal(無酸素作業容量)とFTPの関係
    #   Vogt et al. 2008 (Int J Sports Med): プロレーサーの競技パワー配分
    #   Abbiss & Laursen 2008 (Sports Med): ペーシング戦略とサイクリングパフォーマンス
    #   Jeukendrup 2011 (J Sports Sci): 長時間運動中の糖質補給タイミング
    # Garmin FIT互換:
    #   "ramp"構文はGarminで線形変化パワーターゲットとして表示される
    #   "Watt値"はGarmin Connect → デバイスへ直接数値として送信される
    #   intervals.icu Settings > Garmin でワークアウト自動sync設定可能
    #   Garmin ConnectのBike Workout形式: stepType(warmup/active/rest/cooldown)
    #     targetType(power.3s/power.10s/power.30s/cadence/heart.rate/no.target)
    #     endConditionType(time/distance/calories/lapButton/iterations)
    #   パワーターゲット指定はGarmin Edge 530/830/1030/1040等のサイコンに対応
    #   "ramp"構文(例: "- 10m ramp 100w-220w")はGarmin上でpower ramp step として表示
    #   ケイデンス指定(例: "90rpm")はGarmin Cadence Alert として補助ターゲットに変換
    #   TSS (Training Stress Score) 計算式: TSS = (sec × NP × IF) / (FTP × 3600) × 100
    #   intervals.icu の "rpe" フィールドがGarmin Connect感覚的強度(RPE)フィールドと対応
    if sport == "bike":
        pct_ranges = {
            "recovery": (0.50, 0.55),
            "easy":     (0.56, 0.75),
            "moderate": (0.81, 0.90),
            "hard":     (0.95, 1.05),
        }
        lo_pct, hi_pct = pct_ranges.get(intensity, (0.56, 0.75))
        lo_w  = int(ftp * lo_pct)
        hi_w  = int(ftp * hi_pct)
        wu_w  = int(ftp * 0.55)
        cd_w  = int(ftp * 0.50)

        import hashlib as _hlib
        _seed_val = int(_hlib.md5(f"{phase}|{dur}|{intensity}|bike".encode()).hexdigest()[:8], 16)

        if intensity == "hard":
            # 7タイプ: A=閾値8分, B=VO2max5分, C=オーバー/アンダー, D=20minSS, E=30秒スプリント
            #           F=ミクロバースト40/20 (Rønnestad 2014), G=FTPラダー(Friel 2009)
            _type = _seed_val % 7
            wu = 10; cd = 5; main = dur - wu - cd
            wu_ramp_lo = int(ftp * 0.45)
            rest_w     = int(ftp * 0.50)
            z6_lo      = int(ftp * 1.15); z6_hi = int(ftp * 1.25)
            over_w     = int(ftp * 1.10); under_w = int(ftp * 0.88)
            ss_lo      = int(ftp * 0.88); ss_hi = int(ftp * 0.93)
            sprint_w   = int(ftp * 1.50)
            micro_w    = int(ftp * 1.30)  # ミクロバースト出力
            ladder1    = int(ftp * 0.90); ladder2 = int(ftp * 1.00); ladder3 = int(ftp * 1.08)

            if _type == 0:
                reps = max(2, min(5, main // 11))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 85-95rpm",
                    "",
                    f"Threshold {reps}x",
                    f"- 8m {lo_w}w-{hi_w}w 88-92rpm",
                    f"- 3m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【閾値インターバル】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu}分ランプ → {lo_w}-{hi_w}W × 8分 × {reps}本(rest 3分) → CD {cd}分\n"
                    f"📚 Coggan Z4: FTP95-105%で乳酸処理能力を鍛える\n"
                    f"💪 {motivation}"
                )
            elif _type == 1:
                reps = max(3, min(6, main // 8))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    f"VO2max {reps}x",
                    f"- 5m {z6_lo}w-{z6_hi}w 90-95rpm",
                    f"- 3m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【VO2maxインターバル】{dur}分\n"
                    f"目的: 最大酸素摂取量向上\n"
                    f"WU {wu}分 → {z6_lo}-{z6_hi}W × 5分 × {reps}本(rest 3分) → CD {cd}分\n"
                    f"📚 Laursen 2002: HIIT(VO2max付近)は4週で顕著な適応が起こる\n"
                    f"💪 {motivation}"
                )
            elif _type == 2:
                reps = max(2, min(4, main // 10))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90rpm",
                    "",
                    f"Over-Under {reps}x",
                    f"- 3m {over_w}w 88-92rpm",
                    f"- 3m {under_w}w 88-92rpm",
                    f"- 2m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【オーバー/アンダー】{dur}分\n"
                    f"目的: 乳酸バッファリング・閾値付近での持続力\n"
                    f"WU {wu}分 → {over_w}W/{under_w}W 交互 × {reps}本 → CD {cd}分\n"
                    f"📚 閾値を超えた乳酸をアンダー区間で回収する反復で閾値を引き上げる\n"
                    f"💪 {motivation}"
                )
            elif _type == 3:
                reps = max(1, min(3, main // 25))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    f"Sweet Spot {reps}x",
                    f"- 20m {ss_lo}w-{ss_hi}w 88-92rpm",
                    f"- 5m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【20分スイートスポット】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu}分 → {ss_lo}-{ss_hi}W(FTP88-93%) × 20分 × {reps}本 → CD {cd}分\n"
                    f"📚 Coggan: SST = 疲労少なくFTP向上できるコスパ最高のゾーン\n"
                    f"💪 {motivation}"
                )
            elif _type == 4:
                reps = max(6, min(10, main // 3))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    f"Sprints {reps}x",
                    f"- 30s {sprint_w}w 100-110rpm",
                    f"- 2m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【30秒スプリント】{dur}分\n"
                    f"目的: 最大出力・神経筋パワー\n"
                    f"WU {wu}分 → {sprint_w}W(150%FTP) × 30秒 × {reps}本(rest 2分) → CD {cd}分\n"
                    f"📚 偏極化高強度20%側: 全力スプリントでfast-twitch筋を最大刺激\n"
                    f"💪 {motivation}"
                )
            elif _type == 5:
                # Type F: ミクロバースト40/20 (Rønnestad et al. 2014)
                bursts = max(10, min(20, main * 60 // 60))  # ~1分/セット
                sets = max(2, min(4, main // 15))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    f"Micro Bursts {sets}x",
                    f"- 10x [40s {micro_w}w / 20s {rest_w}w]",
                    f"- 5m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【ミクロバースト 40/20】{dur}分\n"
                    f"目的: VO2max向上・無酸素容量W'balの拡大\n"
                    f"WU {wu}分 → [40秒{micro_w}W/20秒{rest_w}W]×10 × {sets}セット → CD {cd}分\n"
                    f"📚 Rønnestad 2014 Int J Sports Physiol Perf: 40/20プロトコルは\n"
                    f"     通統的5分HIITよりVO2max改善効率が高い\n"
                    f"💡 Garmin ConnectのHIIT Workout形式と互換 / Edge上でCadence Alert推奨\n"
                    f"💪 {motivation}"
                )
            else:
                # Type G: FTPラダー (Friel 2009 Cyclist's Training Bible)
                seg = max(5, main // 3)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90rpm",
                    "",
                    "Ladder Step 1",
                    f"- {seg}m {ladder1}w 90rpm",
                    "",
                    "Ladder Step 2",
                    f"- {seg}m {ladder2}w 90rpm",
                    "",
                    "Ladder Step 3",
                    f"- {seg}m {ladder3}w 88rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【FTPラダー】{dur}分\n"
                    f"目的: 閾値前後のパワー帯への適応・AT付近の耐性向上\n"
                    f"WU {wu}分 → {ladder1}W({seg}分)→{ladder2}W({seg}分)→{ladder3}W({seg}分) → CD {cd}分\n"
                    f"📚 Friel 2009: ラダー型セッションは疲労蓄積と閾値刺激を同時に与える\n"
                    f"📚 Abbiss 2008 Sports Med: FTP付近のペーシング変化が最大平均パワーを向上\n"
                    f"💪 {motivation}"
                )

        elif intensity == "moderate":
            # 5タイプ: A=SSTインターバル, B=テンポ連続, C=ピラミッド
            #           D=SS→Z4フィニッシャー, E=高ケイデンスドリル+Z3
            _type = _seed_val % 5
            wu = 10; cd = 5; main = dur - wu - cd
            wu_ramp_lo = int(ftp * 0.45)
            rest_w = int(ftp * 0.50)
            tempo_lo = int(ftp * 0.76); tempo_hi = int(ftp * 0.88)
            z4_lo = int(ftp * 0.86); z4_hi = int(ftp * 0.95)
            z3_lo = int(ftp * 0.76); z3_hi = int(ftp * 0.85)

            if _type == 0:
                block = 12 if dur < 75 else 15 if dur < 100 else 18
                reps  = max(2, min(4, main // (block + 4)))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    f"Sweet Spot {reps}x",
                    f"- {block}m {lo_w}w-{hi_w}w 88-92rpm",
                    f"- 4m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【スイートスポット】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu}分 → {lo_w}-{hi_w}W × {block}分 × {reps}本(rest 4分) → CD {cd}分\n"
                    f"📚 スイートスポット(FTP81-90%)はFTP向上効率が最も高いゾーン\n"
                    f"💪 {motivation}"
                )
            elif _type == 1:
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90rpm",
                    "",
                    "Tempo",
                    f"- {main}m {tempo_lo}w-{tempo_hi}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【テンポライド】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu}分 → テンポ{main}分({tempo_lo}-{tempo_hi}W, FTP76-88%) → CD {cd}分\n"
                    f"「ちょっとキツい」強度を長時間維持する能力を鍛える\n"
                    f"💪 {motivation}"
                )
            elif _type == 2:
                seg = main // 3
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90rpm",
                    "",
                    "Pyramid Phase 1",
                    f"- {seg}m {z3_lo}w-{z3_hi}w 90rpm",
                    "",
                    "Pyramid Phase 2",
                    f"- {seg}m {z4_lo}w-{z4_hi}w 90rpm",
                    "",
                    "Pyramid Phase 3",
                    f"- {seg}m {z3_lo}w-{z3_hi}w 88rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【ピラミッドライド】{dur}分\n"
                    f"目的: ペース変化への適応\n"
                    f"WU {wu}分 → Z3({seg}分)→Z4({seg}分)→Z3({seg}分) → CD {cd}分\n"
                    f"強度の上げ下げでオーバー/アンダーリカバリー両方を体験\n"
                    f"💪 {motivation}"
                )
            elif _type == 3:
                # Type D: SS→Z4フィニッシャー (Friel SST派生)
                ss_seg = main * 2 // 3
                z4_seg = main - ss_seg
                ss_w = int(ftp * 0.90)
                z4_w = int(ftp * 0.97)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    "Sweet Spot",
                    f"- {ss_seg}m {ss_w}w 90rpm",
                    "",
                    "Z4 Finisher",
                    f"- {z4_seg}m {z4_w}w 88rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【SS→Z4フィニッシャー】{dur}分\n"
                    f"目的: SSTから閾値への橋渡し・後半の粘り強さ\n"
                    f"WU {wu}分 → SS {ss_w}W({ss_seg}分) → Z4 {z4_w}W({z4_seg}分) → CD {cd}分\n"
                    f"📚 Friel 2009: SSTで疲労蓄積後に閾値刺激を入れると閾値向上が加速\n"
                    f"💡 後半はケイデンスを88rpm以下に落とさないよう意識\n"
                    f"💪 {motivation}"
                )
            else:
                # Type E: 高ケイデンスドリル + Z3 (神経筋効率・ペダリング改善)
                drill_min = min(10, main // 4)
                z3_seg = main - drill_min
                hi_cad_w = int(ftp * 0.65)  # 高ケイデンス時はパワーを下げる
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90rpm",
                    "",
                    "Cadence Drill",
                    f"- {drill_min}m {hi_cad_w}w 100-110rpm",
                    "",
                    "Tempo Z3",
                    f"- {z3_seg}m {z3_lo}w-{z3_hi}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【高ケイデンスドリル+Z3テンポ】{dur}分\n"
                    f"目的: ペダリング効率向上・神経筋スムーズ化\n"
                    f"WU {wu}分 → ケイデンスドリル100-110rpm {drill_min}分 → Z3テンポ{z3_seg}分 → CD {cd}分\n"
                    f"📚 Vogt 2008: 高ケイデンス(>100rpm)ドリルはペダリング効率を改善\n"
                    f"💡 Garmin ConnectのCadence Alertを100-110rpmにセット推奨\n"
                    f"💪 {motivation}"
                )

        elif intensity == "easy":
            # 4タイプ: A=Z2ステディ, B=Z2+テンポスニペット
            #           C=ワンレッグドリル+Z2, D=ロングZ2(補給練習)
            _type = _seed_val % 4
            wu = 10; main = dur - wu - 5
            z3_w = int(ftp * 0.82)
            snip = min(10, main // 4)
            main1 = (main - snip) // 2; main2 = main - snip - main1

            if _type == 0:
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {int(ftp*0.45)}w-{lo_w}w 90-95rpm",
                    "",
                    "Endurance",
                    f"- {main}m {lo_w}w-{hi_w}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- 5m ramp {hi_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【Z2エンデュランス】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu}分ランプ → Z2 {lo_w}-{hi_w}W {main}分 → CD 5分\n"
                    f"会話できる強度。ケイデンス88-92rpm。\n"
                    f"📚 低強度量がミトコンドリア密度と脂肪酸化を向上させる\n"
                    f"💪 {motivation}"
                )
            elif _type == 1:
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {int(ftp*0.45)}w-{lo_w}w 90rpm",
                    "",
                    "Endurance Phase 1",
                    f"- {main1}m {lo_w}w-{hi_w}w 88-92rpm",
                    "",
                    "Tempo Snippet",
                    f"- {snip}m {z3_w}w 88rpm",
                    "",
                    "Endurance Phase 2",
                    f"- {main2}m {lo_w}w-{hi_w}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- 5m ramp {hi_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【Z2+テンポスニペット】{dur}分\n"
                    f"目的: {purpose} + 閾値刺激\n"
                    f"Z2 → 中盤 {z3_w}W({snip}分) → Z2 → CD\n"
                    f"💡 長い有酸素の中に短い閾値刺激を差し込む\n"
                    f"💪 {motivation}"
                )
            elif _type == 2:
                # Type C: ワンレッグドリル (ペダリング効率・弱い脚の均等化)
                drill_min = min(8, main // 5)
                z2_seg = main - drill_min
                drill_w = int(ftp * 0.55)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {int(ftp*0.45)}w-{lo_w}w 90rpm",
                    "",
                    "One Leg Drill",
                    f"- {drill_min}m {drill_w}w 60-70rpm",
                    "",
                    "Endurance",
                    f"- {z2_seg}m {lo_w}w-{hi_w}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- 5m ramp {hi_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【ワンレッグドリル+Z2】{dur}分\n"
                    f"目的: ペダリング均等化・死点克服・神経筋効率\n"
                    f"WU {wu}分 → ワンレッグ各30秒×交互 {drill_min}分 → Z2 {z2_seg}分 → CD 5分\n"
                    f"📚 ワンレッグドリルは引き足・プッシュの両フェーズ均等活性化に有効\n"
                    f"💡 Garmin Pedaling Dynamicsでバランス確認推奨 (Vector/Favero対応)\n"
                    f"💪 {motivation}"
                )
            else:
                # Type D: ロングZ2 (補給戦略の練習 Jeukendrup 2011)
                z2_lo_w = lo_w; z2_hi_w = hi_w
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {int(ftp*0.45)}w-{z2_lo_w}w 90rpm",
                    "",
                    "Long Endurance",
                    f"- {main}m {z2_lo_w}w-{z2_hi_w}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- 5m ramp {z2_hi_w}w-{cd_w}w 85rpm",
                    ""
                ])
                desc_text = (
                    f"【ロングZ2エンデュランス(補給練習)】{dur}分\n"
                    f"目的: 有酸素基盤・脂肪代謝・補給タイミングの体得\n"
                    f"WU {wu}分 → Z2 {z2_lo_w}-{z2_hi_w}W {main}分 → CD 5分\n"
                    f"📚 Jeukendrup 2011: 60分超のライドでは45-60分毎に30-60g/hの補給が有効\n"
                    f"💡 45分毎にジェルまたはバー補給。レース補給戦略のシミュレーション\n"
                    f"💡 Garmin Connect: ワークアウトにAuto Lap (30min)を設定して補給リマインド\n"
                    f"💪 {motivation}"
                )

        else:  # recovery
            workout_doc = "\n".join([
                "Recovery Ride",
                f"- {dur}m {lo_w}w-{hi_w}w 90-100rpm",
                ""
            ])
            desc_text = (
                f"【{label}】リカバリーライド {dur}分\n"
                f"目的: {purpose}\n"
                f"目標パワー: {lo_w}-{hi_w}W (FTPの{int(lo_pct*100)}-{int(hi_pct*100)}%)\n"
                f"脚を回すだけ。心拍120以下・力まない。\n"
                f"💪 {motivation}"
            )

        if goal_targets and goal_targets.get("race_bike_w"):
            rw = goal_targets["race_bike_w"]
            desc_text += f"\n🎯 レース目標NP: {rw}W — 今日は{lo_w}-{hi_w}Wで土台を積みます"

        return workout_doc, desc_text


    # ─── YOGA / MOBILITY ───────────────────────────────────────────
    # エビデンス参考文献:
    #   Mikkola et al. 2011: ストレッチ・可動性がランエコノミーを改善
    #   Behm & Chaouachi 2011 (Eur J Appl Physiol): 動的ストレッチの効果
    #   Cramer et al. 2013 (Clinical Rehab): ヨガがVO2maxと柔軟性を向上
    # intervals.icu: yoga/mobilityはworkout_doc=テキストのみ (FIT非対応)
    # Garmin: yoga活動はFIT形式ではなくテキスト説明のみ
    if sport in ("yoga", "mobility", "stretch"):
        import hashlib as _hlib
        _seed_val = int(_hlib.md5(f"{phase}|{dur}|{intensity}|yoga".encode()).hexdigest()[:8], 16)
        _type = _seed_val % 4

        if _type == 0:
            # アクティブリカバリーヨガ (疲労回復・副交感神経活性化)
            workout_doc = "\n".join([
                f"Active Recovery Yoga {dur}min",
                "",
                "Warmup ~5min",
                "- 猫・牛のポーズ (Cat-Cow) 10回",
                "- 子供のポーズ (Child's Pose) 30秒",
                "",
                "Main Sequence",
                "- 下向きの犬 (Downward Dog) 45秒",
                "- ランジ+ツイスト (Low Lunge+Twist) 30秒/側",
                "- 鳩のポーズ (Pigeon Pose) 60秒/側",
                "- 仰向けの脊椎ツイスト 45秒/側",
                "- 橋のポーズ (Bridge) 30秒 × 3",
                "",
                "Cooldown",
                "- サバサナ (Savasana) 5分",
                ""
            ])
            desc_text = (
                f"【アクティブリカバリーヨガ】{dur}分\n"
                f"目的: 疲労回復・副交感神経活性化・可動域回復\n"
                f"📚 Cramer 2013: ヨガはVO2max向上と柔軟性改善に有効\n"
                f"💡 呼吸に集中しながら、力を抜いて行う\n"
                f"💪 {motivation}"
            )
        elif _type == 1:
            # トライアスロン特化モビリティ (股関節・肩・体幹)
            workout_doc = "\n".join([
                f"Triathlon Mobility {dur}min",
                "",
                "Hip Flexor Release ~10min",
                "- ヒップフレクサーストレッチ 60秒/側",
                "- 90/90 ヒップストレッチ 45秒/側",
                "- レッグスイング前後 10回/側",
                "",
                "Shoulder & Back ~10min",
                "- ドアフレーム胸ストレッチ 30秒 × 3",
                "- 胸椎回旋ストレッチ 10回/側",
                "- バンドプルアパート 15回 × 3",
                "",
                "Run-Specific ~10min",
                "- ハムストレッチ立位 45秒/側",
                "- カーフストレッチ (壁) 45秒/側",
                "- アキレス腱回し 10回/側",
                ""
            ])
            desc_text = (
                f"【トライアスロン特化モビリティ】{dur}分\n"
                f"目的: スイム・バイク・ランに直結する可動域改善\n"
                f"📚 Mikkola 2011: 可動性訓練がランニングエコノミーを改善\n"
                f"💡 筋肉を引っ張るのではなく関節の動きに注目して行う\n"
                f"💪 {motivation}"
            )
        elif _type == 2:
            # 動的ストレッチ + コアアクティベーション
            workout_doc = "\n".join([
                f"Dynamic Stretch + Core Activation {dur}min",
                "",
                "Dynamic Warmup ~8min",
                "- レッグスイング前後・横 10回/側",
                "- アームサークル大 15回/方向",
                "- インチワーム 8回",
                "- ラテラルランジ 10回/側",
                "",
                "Core Activation ~12min",
                "- デッドバグ 10回/側 × 3",
                "- クラムシェル(バンド) 15回/側 × 3",
                "- パルオフプレス(バンド) 12回/側 × 3",
                "",
                "Cool Stretch ~5min",
                "- ピジョンポーズ 45秒/側",
                "- 子供のポーズ 30秒",
                ""
            ])
            desc_text = (
                f"【動的ストレッチ+コア活性化】{dur}分\n"
                f"目的: 怪我予防・次セッション前の神経筋準備\n"
                f"📚 Behm & Chaouachi 2011: 動的ストレッチは静的より実施後のパフォーマンスを高く保つ\n"
                f"💡 トレーニング前日の夜や、翌日のセッション前に特に有効\n"
                f"💪 {motivation}"
            )
        else:
            # フォームローラー + ディープストレッチ (筋膜リリース)
            workout_doc = "\n".join([
                f"Foam Roll + Deep Stretch {dur}min",
                "",
                "Foam Rolling ~10min",
                "- 大腿四頭筋ローリング 2分/側",
                "- IT バンドローリング 2分/側",
                "- 背中・広背筋ローリング 3分",
                "",
                "Deep Stretch ~15min",
                "- ハーフスプリットストレッチ 60秒/側",
                "- リクライニングバタフライ 2分",
                "- スフィンクスポーズ (腰・腹部) 2分",
                "- 対角ストレッチ 30秒 × 4",
                "",
                "Breathwork",
                "- 腹式呼吸 4-7-8 × 5サイクル",
                ""
            ])
            desc_text = (
                f"【筋膜リリース+ディープストレッチ】{dur}分\n"
                f"目的: 筋膜リリース・睡眠前リラクゼーション・翌日の疲労軽減\n"
                f"📚 筋膜リリースは関節可動域を即座に改善し、翌日のパフォーマンスを保護\n"
                f"💡 就寝前30〜60分に実施すると睡眠の質も向上\n"
                f"💪 {motivation}"
            )

        return workout_doc, desc_text

    # ─── SWIM ─────────────────────────────────────────────────────
    # エビデンス参考文献:
    #   Pla et al. 2021 (Front Physiol): 偏極化80/20トレーニングの有効性
    #   Toubekis & Tokmakidis 2013 (J Strength Cond Res): CSS法による強度配分
    #   Olbrecht 2000 "The Science of Winning": 乳酸プロファイルに基づくスイムゾーン
    #   Costill et al. 1992 (J Appl Physiol): 泳ぎのボリュームと強度のトレードオフ
    #   Rodríguez et al. 2003 (J Sports Med Phys Fitness): スイム特異的VO2max
    #   Aspenes & Karlsen 2012 (Sports Med): マスタースイマーへの強度介入効果
    #   Fernandes & Vilas-Boas 2012 (J Aquat Sport Res): 呼吸リズムとパフォーマンス
    #   Seiler 2010 (Int J Sports Physiol Perf): 持久系スポーツ共通の偏極化エビデンス
    # Garmin互換:
    #   Garmin Swim 2 / Garmin Forerunner 945/955/965 はPool Swim Workoutに対応
    #   intervals.icu の swim workout_doc → Garmin Connect Pool Workout に変換される
    #   distances("100mtr"/"200mtr") → Garmin Pool Swim Distance steps に対応
    #   rest time("20s rest") → Garmin Rest step に対応
    #   Garmin Swim Workoutの構成: warmup/active/rest/cooldown steps
    #   Garmin ConnectのCritical Swim Speedを設定すると pace zone が自動計算される
    #   intervals.icu Settings > Garmin でスイムワークアウトの自動sync対応
    #   Garmin Pool Swim の距離設定: 25m/33.3m/50m プールに対応
    if sport == "swim":
        # CSS (Critical Swim Speed) — 優先順位: 引数css > goal_targets > デフォルト125
        # fetch_athlete_data() で intervals.icu から自動取得して渡される
        _css = css or (goal_targets.get("race_swim_css") if goal_targets else None) or 125

        # ゾーン別ペース (秒/100m)
        css_z1  = _css * 1.25   # リカバリー  ~2:36/100m
        css_z2  = _css * 1.15   # 有酸素      ~2:24/100m
        css_z3  = _css * 1.05   # テンポ      ~2:11/100m
        css_z4  = _css * 0.98   # 閾値インターバル ~2:03/100m

        # 推定総距離 (ゾーン別平均ペースで算出)
        avg_pace = {"recovery": css_z1, "easy": css_z2, "moderate": css_z3, "hard": css_z4}.get(intensity, css_z2)
        # total_m: dur(分) * 60(秒/分) / avg_pace(秒/100m) * 100(m) = 総距離m
        # avg_paceは秒/100m単位であることを保証
        _ap = float(avg_pace)
        if _ap < 60:   # 明らかに秒/kmで混入している場合(60秒/km未満はあり得ない)
            _ap = _ap * 100  # 秒/km → 秒/100m に補正
        total_m = min(10000, (int(dur * 60 / _ap * 100) // 50) * 50)  # 50m単位・上限10km

        # ──────────────────────────────────────────────────────────────
        # スイムメニュー生成 (偏極化80/20, CSS基準, セッション多様化)
        # 参考: Pla et al. 2021 Frontiers Physiology / Swim Smooth CSS法
        # intensity: hard=インターバル, moderate=テンポ, easy=有酸素, recovery=回復
        # ──────────────────────────────────────────────────────────────

        # セッション種別: phase + dur の組み合わせで毎回異なるバリエーションを選択
        # 週ごと・フェーズごとに変わるよう設計
        import hashlib as _hlib
        _seed_str = f"{phase}|{dur}|{intensity}"
        _seed_val = int(_hlib.md5(_seed_str.encode()).hexdigest()[:8], 16)

        if intensity == "hard":
            # 4タイプをローテーション
            # Type A: CSS インターバル 100m×N (20s rest) — スピード持久
            # Type B: ピラミッド 50-100-200-100-50 — 変化をつけた強度
            # Type C: 200m×N (30s rest) — 乳酸処理
            # Type D: 50mスプリント×N (45s rest) — 最大速度刺激 (偏極化20%側)
            _type = _seed_val % 4

            wu_m = 400; cd_m = 200
            main_m = max(400, total_m - wu_m - cd_m)
            p_wu_i = _swim_pace_icu(css_z2)
            p_cd_i = _swim_pace_icu(css_z1)
            p_wu   = _swim_pace(css_z2)

            if _type == 0:  # CSS 100mインターバル
                reps = min(max(6, main_m // 100), 20)
                p_int_i = _swim_pace_icu(css_z4)
                p_int   = _swim_pace(css_z4)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"CSS Intervals {reps}x",
                    f"- 100mtr {p_int_i}",
                    "- 20s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                    ""
                ])
                desc_text = (
                    f"【CSSインターバル】{dur}分 / 推定{total_m}m\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_m}m → 100m({p_int}) ×{reps}本 rest20秒 → CD {cd_m}m\n"
                    f"CSS: {_swim_pace(_css)} ← 全本イーブンペースが鍵\n"
                    f"📚 Swim Smooth: CSSの20秒レストで乳酸耐性を高める\n"
                    f"💪 {motivation}"
                )
            elif _type == 1:  # ピラミッド
                p_fast_i = _swim_pace_icu(css_z4)
                p_fast   = _swim_pace(css_z4)
                pyramid = [50, 100, 200, 100, 50]
                pyr_lines = []
                for d in pyramid:
                    pyr_lines.append(f"- {d}mtr {p_fast_i}")
                    pyr_lines.append("- 20s Rest")
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    "Pyramid Set",
                    *pyr_lines,
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                    ""
                ])
                desc_text = (
                    f"【ピラミッドスイム】{dur}分 / 推定{total_m}m\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_m}m → 50-100-200-100-50m({p_fast}) each rest20s → CD {cd_m}m\n"
                    f"200mで距離感覚を掴み、降りで乳酸をプッシュ\n"
                    f"💪 {motivation}"
                )
            elif _type == 2:  # 200m スレッショルド
                reps200 = max(3, main_m // 200)
                p_t_i = _swim_pace_icu(css_z3)
                p_t   = _swim_pace(css_z3)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"Threshold Set {reps200}x",
                    f"- 200mtr {p_t_i}",
                    "- 30s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                    ""
                ])
                desc_text = (
                    f"【200mスレッショルド】{dur}分 / 推定{total_m}m\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_m}m → 200m({p_t}) ×{reps200}本 rest30秒 → CD {cd_m}m\n"
                    f"CSS+5秒/100m のテンポ維持 — 後半落ちたら一本減らす\n"
                    f"💪 {motivation}"
                )
            else:  # 50mスプリント (偏極化の高強度側)
                reps50 = min(12, max(6, main_m // 50))
                p_sp_i = _swim_pace_icu(css_z4 * 0.92)  # CSS×0.92 = ほぼ全力
                p_sp   = _swim_pace(css_z4 * 0.92)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"Sprint Set {reps50}x",
                    f"- 50mtr {p_sp_i}",
                    "- 45s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                    ""
                ])
                desc_text = (
                    f"【50mスプリント】{dur}分 / 推定{total_m}m\n"
                    f"目的: 最大速度刺激・神経系活性化\n"
                    f"WU {wu_m}m → 50m全力({p_sp}) ×{reps50}本 rest45秒 → CD {cd_m}m\n"
                    f"📚 偏極化20%側: 毎本全力で神経系を鍛える\n"
                    f"💪 {motivation}"
                )

        elif intensity == "moderate":
            # 3タイプ: A=400mスレッショルド B=混合セット C=CSS-4秒テンポ
            _type = _seed_val % 3
            wu_m = 400; cd_m = 200
            main_m = max(400, total_m - wu_m - cd_m)
            p_wu_i = _swim_pace_icu(css_z2)
            p_cd_i = _swim_pace_icu(css_z1)
            p_wu   = _swim_pace(css_z2)

            if _type == 0:  # 400mスレッショルド
                reps = max(2, main_m // 400)
                p_t_i = _swim_pace_icu(css_z3)
                p_t   = _swim_pace(css_z3)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"Threshold {reps}x",
                    f"- 400mtr {p_t_i}",
                    "- 30s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                    ""
                ])
                desc_text = (
                    f"【400mテンポ】{dur}分 / 推定{total_m}m\n"
                    f"400m({p_t}) ×{reps}本 / CSSより5秒遅め → フォーム崩さずに持続\n"
                    f"💪 {motivation}"
                )
            elif _type == 1:  # 混合セット (12×100m alternating pace)
                reps = min(12, max(6, main_m // 100))
                p_fast_i = _swim_pace_icu(css_z4)
                p_slow_i = _swim_pace_icu(css_z2)
                p_fast   = _swim_pace(css_z4)
                p_slow   = _swim_pace(css_z2)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"Mixed Pace {reps}x (alternating)",
                    f"- 100mtr {p_fast_i}",
                    "- 20s Rest",
                    f"- 100mtr {p_slow_i}",
                    "- 15s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                    ""
                ])
                desc_text = (
                    f"【混合ペースセット】{dur}分 / 推定{total_m}m\n"
                    f"100m速({p_fast}) → 100m緩({p_slow}) を{reps}サイクル\n"
                    f"📚 T100 Triathlon推奨: ペース変化でレースの前後追いを想定\n"
                    f"💪 {motivation}"
                )
            else:  # 500-800mロングインターバル
                block = 600 if main_m >= 1200 else 500
                reps = max(2, main_m // block)
                p_lo_i = _swim_pace_icu(css_z2)
                p_lo   = _swim_pace(css_z2)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"Long Endurance {reps}x",
                    f"- {block}mtr {p_lo_i}",
                    "- 20s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                    ""
                ])
                desc_text = (
                    f"【ロングインターバル】{dur}分 / 推定{total_m}m\n"
                    f"{block}m({p_lo}) ×{reps}本 / 距離感・レースペース慣れ\n"
                    f"📚 swimcoachapp推奨: 500-1000mでレース距離感を養う\n"
                    f"💪 {motivation}"
                )

        elif intensity == "easy":
            # 3タイプ: A=Z2ステディ B=ドリル込みテクニック C=プルブイ+ドリル
            _type = _seed_val % 3
            wu_m  = min(400, total_m // 5)
            cd_m  = 200
            main_m = max(200, total_m - wu_m - cd_m)
            p_wu_i = _swim_pace_icu(css_z1)
            p_main_i = _swim_pace_icu(css_z2)
            p_wu   = _swim_pace(css_z1)
            p_main = _swim_pace(css_z2)

            if _type == 0:  # Z2ステディ
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    "Aerobic Steady",
                    f"- {main_m}mtr {p_main_i}",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_wu_i}",
                    ""
                ])
                desc_text = (
                    f"【Z2エンデュランス】{dur}分 / 推定{total_m}m\n"
                    f"WU {wu_m}m → Z2 {main_m}m({p_main}) → CD {cd_m}m\n"
                    f"📚 偏極化80%側: 会話できる強度を徹底する\n"
                    f"💪 {motivation}"
                )
            elif _type == 1:  # テクニック+エンデュランス
                drill_m = min(200, main_m // 3)
                steady_m = main_m - drill_m
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    "Technique Drills 4x",
                    f"- {drill_m // 4}mtr {p_wu_i}",
                    "- 15s Rest",
                    "",
                    "Aerobic Steady",
                    f"- {steady_m}mtr {p_main_i}",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_wu_i}",
                    ""
                ])
                desc_text = (
                    f"【テクニック+エンデュランス】{dur}分 / 推定{total_m}m\n"
                    f"ドリル({drill_m // 4}m×4本) → Z2 {steady_m}m\n"
                    f"ドリル例: キャッチアップ/シングルアーム/フィンガーチップドラッグ\n"
                    f"📚 Swim Smooth: ドリルは直後にフルストロークへ移行\n"
                    f"💪 {motivation}"
                )
            else:  # プルブイセット (上半身強化・脚疲労時に有効)
                pull_m = main_m
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    "Pull Buoy Steady",
                    f"- {pull_m}mtr {p_main_i}",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_wu_i}",
                    ""
                ])
                desc_text = (
                    f"【プルブイセット】{dur}分 / 推定{total_m}m\n"
                    f"WU {wu_m}m → プルブイ {pull_m}m({p_main}) → CD {cd_m}m\n"
                    f"📚 swimcoachapp: バイク/ランで脚が疲れた日は上半身重点\n"
                    f"上半身: ラット・三角筋・前腕の引き付けを意識\n"
                    f"💪 {motivation}"
                )

        else:  # recovery
            wu_m   = min(200, total_m // 4)
            main_m = max(100, total_m - wu_m - 100)
            p_main_i = _swim_pace_icu(css_z1)
            p_main   = _swim_pace(css_z1)
            workout_doc = "\n".join([
                "Easy Recovery Swim",
                f"- {wu_m}mtr {p_main_i}",
                "",
                "Steady Easy",
                f"- {main_m}mtr {p_main_i}",
                "",
                "Cooldown",
                f"- 100mtr {p_main_i}",
                ""
            ])
            desc_text = (
                f"【リカバリースイム】{dur}分 / 推定{total_m}m\n"
                f"全て {p_main} (Z1) — 水中ストレッチ感覚で\n"
                f"💡 キック板・プルブイ等の補助道具活用でOK\n"
                f"💪 {motivation}"
            )

        return workout_doc, desc_text

    # ─── STRENGTH ──────────────────────────────────────────────────────
    if sport == "strength":
        # ペース・ワット不要。フェーズ・強度別の具体的なエクササイズを生成。
        # エビデンス: Rønnestad 2015 / Mikkola 2011 — 複合運動中心、フェーズ別強度
        level_map = {"recovery":"base","easy":"base","moderate":"build","hard":"peak"}
        level     = level_map.get(intensity, "base")
        warm_min  = min(5, dur // 6)
        cool_min  = min(3, dur // 10)
        main_min  = max(5, dur - warm_min - cool_min)

        def _ex_lines(cat, lv):
            """STRENGTH_DBからexercise行を生成"""
            rows = STRENGTH_DB.get((cat, lv)) or STRENGTH_DB.get((cat, "base")) or []
            lines = []
            for name, sets, reps, rest_s, note in rows:
                rest_str = f"{rest_s//60}分" if rest_s >= 60 else f"{rest_s}秒"
                line = f"- {name} {sets}x{reps}  rest{rest_str}"
                if note:
                    line += f"  /{note}"
                lines.append(line)
            return lines

        # フェーズ・時間に応じてフォーカスエリアを決める
        # 30分以下: core + upper, 40分: core + lower, 50分以上: core + lower + upper
        if dur <= 30:
            focus_cats = ["core", "upper"]
        elif dur <= 40:
            focus_cats = ["core", "lower"]
        else:
            focus_cats = ["core", "lower", "upper"]

        _wlines  = _ex_lines("warmup",   level) or ["- ジャンピングジャック 30秒", "- グルートブリッジ 20回", "- バードドッグ 10回/側"]
        _cdlines = _ex_lines("cooldown", level) or ["- ハムストレッチ 30秒/側", "- 胸・肩ストレッチ 30秒"]

        # メインセット: カテゴリごとに区切りを入れる
        _mlines = []
        cat_labels = {"core":"Core", "lower":"Lower Body", "upper":"Upper Body"}
        for cat in focus_cats:
            exlines = _ex_lines(cat, level)
            if exlines:
                n = max(2, (main_min // len(focus_cats)) // 4)  # 1種目あたり想定本数
                _mlines.append(f"{cat_labels[cat]}")
                _mlines.extend(exlines[:n+1])
                _mlines.append("")

        workout_doc = "\n".join([
            f"Strength [{level.capitalize()}] {dur}min",
            "",
            f"Warmup ~{warm_min}min",
            *_wlines,
            "",
            f"Main Set ~{main_min}min",
            *_mlines,
            "Cooldown",
            *_cdlines,
        ])

        # desc_text: 日本語の詳細説明
        cat_jp = {"core":"体幹", "lower":"下半身(バイク出力)", "upper":"上半身(スイム推進力)"}
        focus_jp = " + ".join(cat_jp.get(c, c) for c in focus_cats)
        desc_text = (
            f"【筋トレ / {level}レベル】{dur}分\n"
            f"フォーカス: {focus_jp}\n"
            f"目的: トライアスロン全種目の出力基盤強化\n"
            f"📚 Rønnestad 2015: 複合運動が持久力パフォーマンスを直接改善\n"
            f"💪 {motivation}\n\n"
            f"■ ウォームアップ ({warm_min}分)\n"
        )
        for ln in _wlines:
            desc_text += f"  {ln.lstrip('- ')}\n"
        desc_text += f"\n■ メイン ({main_min}分)\n"
        for cat in focus_cats:
            exrows = STRENGTH_DB.get((cat, level)) or STRENGTH_DB.get((cat, "base")) or []
            n = max(2, (main_min // len(focus_cats)) // 4)
            desc_text += f"  [{cat_jp.get(cat, cat)}]\n"
            for name, sets, reps, rest_s, note in exrows[:n+1]:
                rs = f"{rest_s//60}分" if rest_s >= 60 else f"{rest_s}秒"
                desc_text += f"    {name} {sets}×{reps} (休憩{rs})"
                if note:
                    desc_text += f" ※{note}"
                desc_text += "\n"
            desc_text += "\n"
        desc_text += (
            f"■ クールダウン ({cool_min}分)\n"
            f"  ストレッチで可動域を維持・次のセッションへの準備\n"
        )
        return workout_doc, desc_text

    # ─── YOGA ──────────────────────────────────────────────────────────
    # エビデンス参考文献:
    #   Moran et al. 2011 (Int J Yoga): 8週ヨガでVO2max・柔軟性・バランスが向上
    #   Tanaka et al. 2014 (J Phys Ther Sci): ヨガは自律神経回復を促進
    #   Cramer et al. 2013 (Clin J Sport Med): スポーツ選手のヨガ実践と怪我予防
    #   Smith et al. 2011 (J Strength Cond Res): アスリートの可動域とパフォーマンス相関
    #   Wiese et al. 2019 (Int J Environ Res): 呼吸法(プラナヤマ)が持久力パフォーマンス向上
    #   Woodyard 2011 (Int J Yoga): ヨガの心身両面への恩恵の体系的レビュー
    #   Morgan et al. 2021 (Front Psychol): マインドフルネスとアスリートの回復品質
    # Garmin互換:
    #   Garmin ConnectのYoga Activity Type に対応 (Activity Type: yoga)
    #   Garmin Body Battery はヨガ・瞑想中の自律神経回復をスコア化
    #   インターバル走後に30分ヨガを追加するとBody Batteryの回復スコアが高まる傾向
    if sport == "yoga":
        warm_min = min(3, dur // 6)
        flow_min = max(5, dur - warm_min - 3)
        cool_min = min(3, dur - warm_min - flow_min)

        import hashlib as _hlib
        _seed_val = int(_hlib.md5(f"{phase}|{dur}|{intensity}|yoga".encode()).hexdigest()[:8], 16)

        # 強度別ヨガスタイル x フォーカス
        # 6タイプ:
        #   A: スイム&バイクリカバリー (肩/背中/股関節)
        #   B: バイク&ランリカバリー (ハムスト/腸腰/ふくらはぎ)
        #   C: 全身バランスフロー (均等可動域維持)
        #   D: ブレス&マインドフルネス (自律神経リセット, 翌日高強度前に)
        #   E: コアヨガ (プランク系ポーズでヨガ+体幹を両立)
        #   F: リストラティブヨガ (完全受動・神経系リセット)

        _type = _seed_val % 6

        if _type == 0:
            # Type A: スイム&バイクリカバリーフォーカス
            poses_main = (
                "ダウンドッグ×30秒 / チャイルドポーズ×45秒 /\n"
                "  スレッドザニードル(左右各30秒) / スフィンクスポーズ×40秒 /\n"
                "  コブラポーズ×30秒 / 肩甲骨プルバック×20回"
            )
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m breathing\n"
                f"- {flow_min}m shoulder-back flow\n"
                f"- {cool_min}m savasana\n"
            )
            desc_text = (
                f"【スイム&バイクリカバリーヨガ】{dur}分\n"
                f"目的: 肩/背中/股関節の緊張解放 → スイムストロークとバイクポジション改善\n"
                f"💪 {motivation}\n\n"
                f"■ ブレス & ウォームアップ ({warm_min}分)\n"
                f"  腹式呼吸×8回 / 首回し / 肩甲骨ストレッチ\n\n"
                f"■ フォーカスフロー ({flow_min}分)\n"
                f"  {poses_main}\n\n"
                f"■ シャバアーサナ ({cool_min}分)\n"
                f"  仰向けで完全脱力。深呼吸で副交感神経を優位に\n\n"
                f"📚 Cramer 2013: 肩関節可動域の向上がスイムストローク効率に直結\n"
                f"💡 Garmin Connect: この後30分休憩するとBody Battery回復を確認推奨"
            )
        elif _type == 1:
            # Type B: バイク&ランリカバリーフォーカス
            poses_main = (
                "ランナーズランジ(左右各45秒) / 鳩のポーズ(左右各60秒) /\n"
                "  ハムストリングストレッチ(各45秒) / 腸腰筋ストレッチ(各30秒) /\n"
                "  ガス抜きポーズ / ハッピーベイビー×45秒"
            )
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m breathing\n"
                f"- {flow_min}m hip-hamstring flow\n"
                f"- {cool_min}m savasana\n"
            )
            desc_text = (
                f"【バイク&ランリカバリーヨガ】{dur}分\n"
                f"目的: 腸腰筋/ハムスト/ふくらはぎの疲労解放 → 翌日のランエコノミー改善\n"
                f"💪 {motivation}\n\n"
                f"■ ブレス & ウォームアップ ({warm_min}分)\n"
                f"  腹式呼吸×8回 / 骨盤前後傾 / 足首回し\n\n"
                f"■ 下半身フォーカスフロー ({flow_min}分)\n"
                f"  {poses_main}\n\n"
                f"■ シャバアーサナ ({cool_min}分)\n"
                f"  仰向けで全身脱力\n\n"
                f"📚 Smith 2011: 股関節可動域向上はストライド長を改善してランエコノミーを向上\n"
                f"📚 Barnes 2015: ランエコノミーの鍵は股関節伸展可動域"
            )
        elif _type == 2:
            # Type C: 全身バランスフロー
            if phase in ("peak", "build"):
                poses_main = (
                    "戦士のポーズⅠ×30秒 / 戦士のポーズⅡ×30秒 / \n"
                    "  三角のポーズ(左右各30秒) / 木のポーズ(左右各30秒) /\n"
                    "  舟のポーズ×20秒×3 / 橋のポーズ×30秒"
                )
            else:
                poses_main = (
                    "猫牛のポーズ×10回 / チャイルドポーズ×45秒 /\n"
                    "  座位前屈×45秒 / 開脚前屈×45秒 /\n"
                    "  仰向けツイスト(左右各30秒) / ハッピーベイビー×30秒"
                )
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m breathing\n"
                f"- {flow_min}m balance flow\n"
                f"- {cool_min}m savasana\n"
            )
            desc_text = (
                f"【全身バランスヨガフロー】{dur}分\n"
                f"目的: 均等な可動域維持・体幹バランス強化\n"
                f"💪 {motivation}\n\n"
                f"■ ブレス & ウォームアップ ({warm_min}分)\n"
                f"  腹式呼吸×5回 / 首・肩回し / 骨盤回し\n\n"
                f"■ バランスフロー ({flow_min}分)\n"
                f"  {poses_main}\n\n"
                f"■ シャバアーサナ ({cool_min}分)\n"
                f"  仰向けで全身脱力\n\n"
                f"📚 Moran 2011: 8週ヨガプログラムでバランス・柔軟性・VO2maxが有意に向上"
            )
        elif _type == 3:
            # Type D: ブレス&マインドフルネスヨガ (高強度セッション前後推奨)
            breath_min = dur // 3
            mindful_min = dur - breath_min - cool_min - warm_min
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m body scan\n"
                f"- {breath_min}m pranayama\n"
                f"- {mindful_min}m gentle flow\n"
                f"- {cool_min}m savasana\n"
            )
            desc_text = (
                f"【ブレス&マインドフルネスヨガ】{dur}分\n"
                f"目的: 自律神経リセット・HRV改善・精神的回復\n"
                f"💪 {motivation}\n\n"
                f"■ ボディスキャン ({warm_min}分)\n"
                f"  頭頂→爪先まで意識を向ける / 緊張箇所を意識\n\n"
                f"■ プラナヤマ呼吸法 ({breath_min}分)\n"
                f"  4拍吸気→2拍保持→6拍呼気 (4-2-6呼吸) × 20セット\n"
                f"  ボックスブリージング(4-4-4-4) × 10セット\n\n"
                f"■ ジェントルフロー ({mindful_min}分)\n"
                f"  猫牛×10回 / チャイルドポーズ×60秒 / スープタバッダコナアーサナ\n\n"
                f"■ シャバアーサナ ({cool_min}分)\n"
                f"  完全脱力・思考を手放す\n\n"
                f"📚 Wiese 2019 Int J Environ Res: プラナヤマ呼吸法は8週で持久力パフォーマンスを向上\n"
                f"📚 Tanaka 2014: ヨガ後の副交感神経優位がHRV改善と翌日パフォーマンスに寄与\n"
                f"📚 Morgan 2021 Front Psychol: マインドフルネス実践でアスリートの回復品質向上\n"
                f"💡 Garmin HRV Status: このセッション翌朝にHRV計測してBody Battery回復確認"
            )
        elif _type == 4:
            # Type E: コアヨガ (体幹+バランス強化)
            core_min = max(10, flow_min * 2 // 3)
            flex_min = flow_min - core_min
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m breathing\n"
                f"- {core_min}m core yoga\n"
                f"- {flex_min}m flexibility\n"
                f"- {cool_min}m savasana\n"
            )
            desc_text = (
                f"【コアヨガ】{dur}分\n"
                f"目的: 体幹安定性強化 + 可動域維持の両立\n"
                f"💪 {motivation}\n\n"
                f"■ ブレス & ウォームアップ ({warm_min}分)\n"
                f"  腹式呼吸×5回 / 肩甲骨ストレッチ\n\n"
                f"■ コアヨガシーケンス ({core_min}分)\n"
                f"  プランクポーズ×60秒 / サイドプランク(左右各30秒) /\n"
                f"  舟のポーズ×20秒×3 / ワイルドシング×30秒(左右) /\n"
                f"  四肢のポーズ→ダウンドッグ往復×5\n\n"
                f"■ 柔軟性フロー ({flex_min}分)\n"
                f"  鳩のポーズ(左右各45秒) / 座位前屈×45秒 / 仰向けツイスト\n\n"
                f"■ シャバアーサナ ({cool_min}分)\n"
                f"  仰向けで全身脱力\n\n"
                f"📚 Cramer 2013: コアポーズの組み合わせで体幹筋の持続的活性化が確認"
            )
        else:
            # Type F: リストラティブヨガ (完全受動・神経系リセット)
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m body scan\n"
                f"- {flow_min}m restorative poses\n"
                f"- {cool_min}m yoga nidra\n"
            )
            desc_text = (
                f"【リストラティブヨガ】{dur}分\n"
                f"目的: 副交感神経優位・深部筋膜の受動的リリース・神経系リセット\n"
                f"💪 {motivation}\n\n"
                f"■ ボディスキャン ({warm_min}分)\n"
                f"  全身の緊張を観察するだけ\n\n"
                f"■ 受動的ポーズ ({flow_min}分)\n"
                f"  スプタバッダコナアーサナ×3分 / サポートブリッジ×3分 /\n"
                f"  レッグアップザウォール×5分 / サポートチャイルドポーズ×3分\n"
                f"  ※ブロック・ボルスター・毛布を積極的に使用\n\n"
                f"■ ヨガニドラ ({cool_min}分)\n"
                f"  全身リラクゼーション誘導 / 思考を手放す\n\n"
                f"📚 Woodyard 2011: リストラティブポーズは筋膜の受動的伸長で翌日の筋痛を軽減\n"
                f"📚 Tanaka 2014: 受動的ヨガはHRVを積極的ストレッチより顕著に改善\n"
                f"💡 ハードセッションの翌日・または週末の疲労蓄積時に特に推奨"
            )

        return workout_doc, desc_text

    # ─── フォールバック ────────────────────────────────────────────
    workout_doc = f"- {dur}m Z2\n"
    desc_text   = f"{sport} {dur}分"
    return workout_doc, desc_text


def session_desc(sport, intensity, dur, phase, tp, ftp, goal_targets=None):
    """後方互換ラッパー: build_workout の desc_text のみ返す"""
    _, desc = build_workout(sport, intensity, dur, phase, tp, ftp, goal_targets)
    return desc



# ============================================================
# 筋トレメニュー（目的・モチベ付き）
# ============================================================
# ============================================================
# STRENGTH_DB — トライアスロン向け筋トレデータベース (v8)
# エビデンス:
#   - Rønnestad et al. 2015: 複合運動がトライアスロンパフォーマンス向上
#   - Mikkola et al. 2011: 筋力トレーニングがランニングエコノミー改善
#   - フェーズ別強度: base=3×15(適応), build=4×8(筋力), peak=3×5(爆発力)
#   - 全種目に効く複合運動優先: スクワット/デッドリフト/ランジ/プッシュアップ系
# ============================================================
STRENGTH_DB = {
    # ── コア（体幹安定 — 全種目共通の出力基盤）──────────────────────
    ("core","base"): [
        ("プランク",             3, "45秒", 30, "頭からかかとを一直線"),
        ("グルートブリッジ",      3, "15回", 30, "膝90度・お尻を上げてキープ"),
        ("デッドバグ",            3, "10回", 30, "腰を床につけたまま対角を伸ばす"),
        ("バードドッグ",          3, "10回", 30, "四つ這い→対角線を3秒キープ"),
        ("サイドプランク",        3, "30秒", 30, "左右各"),
    ],
    ("core","build"): [
        ("プランク",              4, "60秒", 20, ""),
        ("マウンテンクライマー",   3, "20回", 20, "テンポよく交互に"),
        ("レッグレイズ",          3, "15回", 30, "腰を浮かさない"),
        ("ロシアンツイスト",       3, "20回", 25, "重りを持てばなお良"),
        ("サイドプランク+ヒップディップ", 2, "12回", 30, "体幹側屈を加える"),
        ("ヒップスラスト",         3, "15回", 30, "バイク出力向上"),
    ],
    ("core","peak"): [
        ("プランク",              4, "75秒", 15, ""),
        ("マウンテンクライマー",   4, "30秒", 15, "最速で"),
        ("V字クランチ",           3, "15回", 30, ""),
        ("ドラゴンフラッグ(助走)", 2, "8回",  40, "バリエーション可"),
        ("ウッドチョップ",         3, "12回", 25, "回旋力強化"),
    ],
    ("core","maintenance"): [
        ("プランク",              3, "60秒", 20, ""),
        ("デッドバグ",            3, "12回", 25, ""),
        ("サイドプランク",        3, "45秒", 25, ""),
    ],

    # ── 下半身（バイク出力・ランエコノミー）──────────────────────────
    ("lower","base"): [
        ("ゴブレットスクワット",   3, "15回", 45, "かかとを浮かさない"),
        ("リバースランジ",         3, "12回", 45, "左右各・股関節を前に出す"),
        ("グルートブリッジ",       3, "20回", 30, "臀筋を意識"),
        ("カーフレイズ",           3, "20回", 30, "踵は床下まで"),
    ],
    ("lower","build"): [
        ("ブルガリアンスクワット", 4, "8回",  60, "後脚を台に乗せる・片脚ずつ"),
        ("デッドリフト(RDL)",     4, "8回",  60, "ハムスト強化・バイク直結"),
        ("ステップアップ",         3, "10回", 45, "膝が内側に入らないよう"),
        ("ランジ(ウォーキング)",   3, "12回", 40, "ストライド強化"),
        ("シングルレッグカーフレイズ", 3, "15回", 30, "足首安定"),
    ],
    ("lower","peak"): [
        ("ジャンプスクワット",     4, "6回",  60, "爆発的に着地はソフトに"),
        ("ケトルベルスイング",     4, "12回", 45, "お尻で押し上げる"),
        ("ボックスジャンプ",       3, "5回",  60, "最大跳躍"),
        ("シングルレッグデッドリフト", 3, "8回", 50, "体幹×臀筋×ハムスト"),
    ],
    ("lower","maintenance"): [
        ("スクワット",             3, "12回", 40, ""),
        ("ランジ",                 3, "10回", 35, ""),
        ("グルートブリッジ",       3, "15回", 30, ""),
    ],

    # ── 上半身（スイム推進力・バイクポジション保持）──────────────────
    ("upper","base"): [
        ("プッシュアップ",         3, "12回", 60, ""),
        ("パイクプッシュアップ",   3, "10回", 60, "三角筋重点"),
        ("インバーテッドロー",     3, "10回", 60, "テーブル下でOK"),
        ("シングルアームロー",     3, "10回", 60, "ダンベル or 重い荷物"),
    ],
    ("upper","build"): [
        ("アーチャープッシュアップ", 3, "8回", 60, "肘を横に開かない"),
        ("ディップス(椅子)",       3, "12回", 60, "上腕三頭筋"),
        ("パイクPU",               3, "12回", 50, ""),
        ("ラットプルダウン(バンド)", 3, "12回", 45, "広背筋=スイム引き"),
        ("フェイスプル(バンド)",   3, "15回", 40, "肩の後部=インナーマッスル"),
    ],
    ("upper","peak"): [
        ("クラップPU",             3, "8回",  60, "爆発的に"),
        ("アーチャーPU",           3, "10回", 60, ""),
        ("ウォールウォーク",       3, "5回",  60, "逆立ち練習"),
        ("ディップス",             3, "15回", 45, ""),
    ],
    ("upper","maintenance"): [
        ("プッシュアップ",         3, "15回", 45, ""),
        ("パイクPU",               3, "12回", 45, ""),
        ("インバーテッドロー",     3, "10回", 45, ""),
    ],

    # ── ウォームアップ ────────────────────────────────────────────────
    ("warmup","base"): [
        ("フォームローラー全身",   1, "3分",  0,  ""),
        ("ヒップフレクサーストレッチ", 1, "30秒/側", 0, "腸腰筋"),
        ("グルートブリッジ(活性化)", 1, "20回", 0, "臀筋スイッチ"),
        ("レッグスイング",         1, "10回/側", 0, "股関節可動域"),
    ],
    ("warmup","build"): [
        ("フォームローラー大腿+背中", 1, "2分", 0, ""),
        ("バンドウォーク横歩き",   1, "20歩/側", 0, "中殿筋活性化"),
        ("インチワーム",           1, "8回",  0,  "体幹+ハムスト"),
        ("アームサークル",         1, "15回/方向", 0, "肩関節"),
    ],
    ("warmup","peak"): [
        ("フォームローラー全身",   1, "2分",  0,  ""),
        ("ジャンピングジャック",   1, "30秒", 0,  ""),
        ("ジャンプスクワット軽め", 1, "5回",  0,  ""),
        ("アームサークル大",       1, "10回", 0,  ""),
    ],

    # ── クールダウン ──────────────────────────────────────────────────
    ("cooldown","base"): [
        ("ハムストレッチ",          1, "30秒/側", 0, ""),
        ("腸腰筋ストレッチ(膝つき)", 1, "30秒/側", 0, ""),
        ("胸椎回旋",                1, "10回/側", 0, ""),
    ],
    ("cooldown","build"): [
        ("ピジョンポーズ",          1, "45秒/側", 0, "股関節"),
        ("胸・肩ストレッチ",        1, "30秒×2", 0, ""),
        ("子供のポーズ",            1, "45秒",  0,  ""),
    ],
    ("cooldown","peak"): [
        ("ハムストレッチ",          1, "30秒/側", 0, ""),
        ("ピジョンポーズ",          1, "45秒/側", 0, ""),
        ("ショルダーストレッチ",    1, "30秒/側", 0, ""),
    ],
}

def gen_strength_menu(strength_cfg, phase, cond_info, str_prog, dur):
    cond  = cond_info["condition"]
    level = str_prog.get("level","base")
    if cond in ("fatigued","depleted"): level="base"; dur=min(dur,20)
    focus = strength_cfg.get("focus_areas",["core","upper_body"])
    motivation = PHASE_MOTIVATIONS.get(phase,"")
    goal_wks = str_prog.get("weeks_to_goal",0)
    goal_kg  = str_prog.get("goal_muscle_kg",0)

    lines = [f"【筋トレ / {level}レベル】{dur}分",
             f"目的: 体幹・上半身強化によるトライアスロン全種目のパフォーマンス向上",
             f"🎯 目標筋肉量 {goal_kg}kg まで残り{goal_wks}週  現在レベル: {level}",
             f"💪 {motivation}","",
             "■ ウォームアップ（5分）",
             "  関節回し(首→肩→腰→膝→足首) 各10回 / ジャンピングジャック30秒",""]
    time_each = max(5,(dur-7)//len(focus))
    for area in focus:
        key = (area,level)
        exs = STRENGTH_DB.get(key, STRENGTH_DB.get((area,"base"),[]))
        label= {"core":"体幹","upper_body":"上半身","lower_body":"下半身"}.get(area,area)
        purpose={"core":"体軸安定・全種目の出力基盤","upper_body":"プル力・バイクポジション安定"}.get(area,"")
        n = max(2,time_each//5)
        lines.append(f"■ {label}（{time_each}分） ← {purpose}")
        for name,sets,reps,rest,note in exs[:n]:
            r = f"{rest//60}分" if rest>=60 else f"{rest}秒"
            lines.append(f"  {name}  {sets}×{reps}  休憩{r}" + (f"  ※{note}" if note else ""))
        lines.append("")
    lines += ["■ クールダウン（2分）","  大腿四頭筋/ハムスト/胸・肩 各30秒"]
    return "\n".join(lines)

# ============================================================
# Garmin Connect ワークアウト JSON 生成
# ============================================================
# 参考:
#   ThomasRondof/GarminWorkoutAItoJSON (Garmin API リバースエンジニアリング)
#   Garmin Training API: developer.garmin.com/gc-developer-program/training-api/
#   FIT SDK: developer.garmin.com/fit/file-types/workout/
#
# Garmin JSON Key:
#   sportType.sportTypeKey: "running"/"cycling"/"swimming"/"strength_training"
#   stepType.stepTypeKey: "warmup"/"interval"/"recovery"/"cooldown"/"rest"
#   endCondition.conditionTypeKey: "time"(秒)/"distance"(m)/"lap.button"
#   targetType.workoutTargetTypeKey:
#     "pace.zone" → targetValueOne/Two: m/s
#     "power.zone" → targetValueOne/Two: W
#     "heart.rate.zone" → targetValueOne/Two: bpm
#     "no.target"
# ============================================================

def _garmin_step(step_type, duration_type, duration_val, target_type="no.target",
                 target_low=None, target_high=None, order=1, desc=""):
    """Garmin Connect ワークアウトステップ辞書を生成"""
    step = {
        "type": "ExecutableStepDTO",
        "stepId": None,
        "stepOrder": order,
        "stepType": {"stepTypeId": {
            "warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5
        }.get(step_type, 3), "stepTypeKey": step_type},
        "childStepId": None,
        "description": desc,
        "endCondition": {"conditionTypeId": {
            "time": 2, "distance": 3, "lap.button": 1
        }.get(duration_type, 2), "conditionTypeKey": duration_type},
        "endConditionValue": duration_val,
        "preferredEndConditionUnit": None,
        "endConditionCompare": None,
        "endConditionZone": None,
        "targetType": {
            "workoutTargetTypeId": {
                "no.target": 1, "power.zone": 2, "cadence.zone": 3,
                "heart.rate.zone": 4, "speed.zone": 5, "pace.zone": 6
            }.get(target_type, 1),
            "workoutTargetTypeKey": target_type
        },
        "targetValueOne": target_low,
        "targetValueTwo": target_high,
        "zoneNumber": None,
    }
    return step

def _garmin_repeat(steps, iterations, order=1):
    """Garmin Connect リピートブロックを生成"""
    return {
        "type": "RepeatGroupDTO",
        "stepId": None,
        "stepOrder": order,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
        "childStepId": 1,
        "numberOfIterations": iterations,
        "smartRepeat": False,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
        "endConditionValue": iterations,
        "workoutSteps": steps,
    }

def _pace_to_ms(pace_sec_per_km):
    """ペース(秒/km)→速度(m/s)に変換 (Garmin API はm/s単位)"""
    if pace_sec_per_km <= 0:
        return 0.0
    return round(1000.0 / pace_sec_per_km, 4)

def build_garmin_workout(sport, intensity, dur, phase, tp, ftp, css=None, goal_targets=None):
    """
    Garmin Connect 互換のワークアウトJSONを生成する。
    intervals.icuのworkout_docと並行して生成し、Garminデバイスへ直接送信可能。

    Args:
        sport: "run" / "bike" / "swim" / "strength" / "yoga"
        intensity: "hard" / "moderate" / "easy" / "recovery"
        dur: 合計時間(分)
        phase: トレーニングフェーズ
        tp: 閾値ペース(秒/km)
        ftp: FTP(W)
        css: Critical Swim Speed(秒/100m)
        goal_targets: レース目標辞書

    Returns:
        dict: Garmin Connect APIに投稿可能なワークアウトJSON
    """
    import hashlib as _gh
    _seed = int(_gh.md5(f"{phase}|{dur}|{intensity}".encode()).hexdigest()[:8], 16)

    sport_map = {
        "run": "running", "bike": "cycling", "swim": "swimming",
        "strength": "strength_training", "yoga": "yoga",
        "brick": "other", "race": "triathlon",
    }
    sport_key = sport_map.get(sport, "other")

    steps = []
    order = [1]  # mutableカウンタ

    def add_step(s):
        s["stepOrder"] = order[0]
        steps.append(s)
        order[0] += 1

    def add_repeat(reps_list, iterations):
        blk = _garmin_repeat(reps_list, iterations, order[0])
        for i, s in enumerate(reps_list):
            s["stepOrder"] = i + 1
        steps.append(blk)
        order[0] += 1

    # ── ラン ──────────────────────────────────────────────────────
    if sport == "run":
        _tp = tp
        wu_pace_lo = _pace_to_ms(int(_tp * 1.30))
        wu_pace_hi = _pace_to_ms(int(_tp * 1.10))
        cd_pace = _pace_to_ms(int(_tp * 1.35))
        z2_lo = _pace_to_ms(int(_tp * 1.25))
        z2_hi = _pace_to_ms(int(_tp * 1.10))
        _type = _seed % 5 if intensity == "hard" else (
                _seed % 3 if intensity in ("moderate","tempo") else _seed % 3)

        if intensity == "hard":
            # WU
            add_step(_garmin_step("warmup","time", 600,
                "pace.zone", wu_pace_lo, wu_pace_hi, desc="Z2 Warmup"))
            if _type == 0:   # 4分インターバル
                ip_lo = _pace_to_ms(int(_tp * 1.00))
                ip_hi = _pace_to_ms(int(_tp * 0.93))
                rp_lo = _pace_to_ms(int(_tp * 1.35))
                rp_hi = _pace_to_ms(int(_tp * 1.20))
                reps  = max(3, min(6, (dur-15)//5))
                add_repeat([
                    _garmin_step("interval","time",240,"pace.zone",ip_lo,ip_hi,desc="Interval"),
                    _garmin_step("recovery","time",90,"pace.zone",rp_lo,rp_hi,desc="Recovery jog"),
                ], reps)
            elif _type == 1:  # 1000m
                ip_lo = _pace_to_ms(int(_tp * 0.97))
                ip_hi = _pace_to_ms(int(_tp * 0.91))
                rp_lo = _pace_to_ms(int(_tp * 1.35))
                rp_hi = _pace_to_ms(int(_tp * 1.20))
                reps  = max(3, min(5, (dur-15)//6))
                add_repeat([
                    _garmin_step("interval","distance",1000,"pace.zone",ip_lo,ip_hi,desc="1km"),
                    _garmin_step("recovery","time",120,"pace.zone",rp_lo,rp_hi,desc="Rest"),
                ], reps)
            elif _type == 2:  # 30/30
                sp_lo = _pace_to_ms(int(_tp * 0.95))
                sp_hi = _pace_to_ms(int(_tp * 0.88))
                rp_lo = _pace_to_ms(int(_tp * 1.35))
                rp_hi = _pace_to_ms(int(_tp * 1.20))
                reps  = max(6, min(12, (dur-15)//2))
                add_repeat([
                    _garmin_step("interval","time",30,"pace.zone",sp_lo,sp_hi,desc="Fast"),
                    _garmin_step("recovery","time",30,"pace.zone",rp_lo,rp_hi,desc="Float"),
                ], reps)
            elif _type == 3:  # 閾値ラン
                tp_lo = _pace_to_ms(int(_tp * 1.02))
                tp_hi = _pace_to_ms(int(_tp * 0.98))
                add_step(_garmin_step("interval","time",(dur-15)*60,
                    "pace.zone",tp_lo,tp_hi,desc="Threshold"))
            else:  # ファルトレク
                ip_lo = _pace_to_ms(int(_tp * 1.00))
                ip_hi = _pace_to_ms(int(_tp * 0.94))
                rp_lo = _pace_to_ms(int(_tp * 1.35))
                rp_hi = _pace_to_ms(int(_tp * 1.20))
                reps  = max(4, min(8, (dur-15)//4))
                add_repeat([
                    _garmin_step("interval","time",120,"pace.zone",ip_lo,ip_hi,desc="Fast"),
                    _garmin_step("recovery","time",120,"pace.zone",rp_lo,rp_hi,desc="Easy"),
                ], reps)
            # CD
            add_step(_garmin_step("cooldown","time",300,"pace.zone",cd_pace,z2_lo,desc="Cooldown"))

        elif intensity in ("moderate","tempo"):
            add_step(_garmin_step("warmup","time",600,"pace.zone",wu_pace_lo,wu_pace_hi,desc="Warmup"))
            t_lo = _pace_to_ms(int(_tp * 1.10))
            t_hi = _pace_to_ms(int(_tp * 1.03))
            if _type == 0:  # テンポ持続
                add_step(_garmin_step("interval","time",(dur-15)*60,"pace.zone",t_lo,t_hi,desc="Tempo"))
            elif _type == 1:  # クルーズインターバル
                cruise = 720 if dur>=60 else 480
                reps = max(2,(dur-15)//(cruise//60+2))
                add_repeat([
                    _garmin_step("interval","time",cruise,"pace.zone",t_lo,t_hi,desc="Cruise"),
                    _garmin_step("recovery","time",120,"pace.zone",wu_pace_lo,wu_pace_hi,desc="Float"),
                ], reps)
            else:  # プログレッション
                easy_s = (dur//3)*60
                prog_lo = _pace_to_ms(int(_tp * 1.04))
                prog_hi = _pace_to_ms(int(_tp * 1.00))
                add_step(_garmin_step("interval","time",easy_s,"pace.zone",z2_lo,z2_hi,desc="Easy build"))
                add_step(_garmin_step("interval","time",(dur-dur//3-15)*60,"pace.zone",prog_lo,prog_hi,desc="Push"))
            add_step(_garmin_step("cooldown","time",300,"pace.zone",cd_pace,z2_lo,desc="Cooldown"))

        else:  # easy / recovery
            z_lo = _pace_to_ms(int(_tp * 1.35)) if intensity=="recovery" else z2_lo
            z_hi = _pace_to_ms(int(_tp * 1.20)) if intensity=="recovery" else z2_hi
            add_step(_garmin_step("warmup","time",300,"pace.zone",z_lo,z_hi,desc="Easy start"))
            if _type == 1:  # ストライドつき
                add_step(_garmin_step("interval","time",(dur-3)*60,"pace.zone",z_lo,z_hi,desc="Easy run"))
                add_repeat([
                    _garmin_step("interval","time",20,"pace.zone",
                        _pace_to_ms(int(_tp*0.93)),_pace_to_ms(int(_tp*0.90)),desc="Stride"),
                    _garmin_step("recovery","time",40,"pace.zone",z_lo,z_hi,desc="Float"),
                ], 4)
            else:
                add_step(_garmin_step("interval","time",dur*60,"pace.zone",z_lo,z_hi,desc="Easy run"))

    # ── バイク ────────────────────────────────────────────────────
    elif sport == "bike":
        wu_lo = int(ftp * 0.45); wu_hi = int(ftp * 0.55)
        cd_lo = int(ftp * 0.40); cd_hi = int(ftp * 0.50)
        _type = (_seed%5 if intensity=="hard" else
                 _seed%4 if intensity=="moderate" else
                 _seed%3 if intensity=="easy" else 0)

        if intensity == "hard":
            add_step(_garmin_step("warmup","time",600,"power.zone",wu_lo,int(ftp*0.55),desc="Ramp WU"))
            if _type == 0:  # 閾値8分
                reps = max(2,min(5,(dur-15)//11))
                add_repeat([
                    _garmin_step("interval","time",480,"power.zone",int(ftp*0.95),int(ftp*1.05),desc="FTP"),
                    _garmin_step("recovery","time",180,"power.zone",cd_lo,cd_hi,desc="Rest"),
                ], reps)
            elif _type == 1:  # VO2max
                vo2_dur = 4 if dur<75 else 5
                reps = max(3,min(6,(dur-15)//(vo2_dur+3)))
                add_repeat([
                    _garmin_step("interval","time",vo2_dur*60,"power.zone",int(ftp*1.06),int(ftp*1.20),desc="VO2max"),
                    _garmin_step("recovery","time",vo2_dur*60,"power.zone",cd_lo,cd_hi,desc="Rest"),
                ], reps)
            elif _type == 2:  # スプリント30秒
                reps = max(6,min(12,(dur-15)//2))
                add_repeat([
                    _garmin_step("interval","time",30,"power.zone",int(ftp*1.40),int(ftp*1.60),desc="Sprint"),
                    _garmin_step("recovery","time",90,"power.zone",cd_lo,cd_hi,desc="Rest"),
                ], reps)
            elif _type == 3:  # SS→閾値ビルド
                main = dur-15
                add_step(_garmin_step("interval","time",main*60*2//3,"power.zone",int(ftp*0.85),int(ftp*0.90),desc="Sweet Spot"))
                add_step(_garmin_step("interval","time",main*60//3,"power.zone",int(ftp*0.95),int(ftp*1.05),desc="Threshold push"))
            else:  # 5分ピーク
                reps = max(2,min(4,(dur-15)//8))
                add_repeat([
                    _garmin_step("interval","time",300,"power.zone",int(ftp*1.15),int(ftp*1.20),desc="5min peak"),
                    _garmin_step("recovery","time",180,"power.zone",cd_lo,cd_hi,desc="Rest"),
                ], reps)
            add_step(_garmin_step("cooldown","time",300,"power.zone",cd_lo,cd_hi,desc="CD"))

        elif intensity == "moderate":
            add_step(_garmin_step("warmup","time",600,"power.zone",wu_lo,int(ftp*0.55),desc="WU"))
            if _type == 0:  # スイートスポット
                block = 720 if dur<75 else 900
                reps = max(2,min(4,(dur-15)//(block//60+4)))
                add_repeat([
                    _garmin_step("interval","time",block,"power.zone",int(ftp*0.84),int(ftp*0.94),desc="Sweet Spot"),
                    _garmin_step("recovery","time",240,"power.zone",cd_lo,cd_hi,desc="Rest"),
                ], reps)
            elif _type == 1:  # テンポ
                add_step(_garmin_step("interval","time",(dur-15)*60,"power.zone",int(ftp*0.76),int(ftp*0.88),desc="Tempo"))
            elif _type == 2:  # ハイケイデンス
                add_repeat([
                    _garmin_step("interval","time",300,"power.zone",int(ftp*0.84),int(ftp*0.90),desc="High cadence"),
                    _garmin_step("recovery","time",180,"power.zone",cd_lo,cd_hi,desc="Easy"),
                ], 4)
                add_step(_garmin_step("interval","time",max(300,(dur-15-32)*60),"power.zone",int(ftp*0.65),int(ftp*0.75),desc="Endurance"))
            else:  # アンダー/オーバー
                reps = max(2,min(4,(dur-15)//8))
                add_repeat([
                    _garmin_step("interval","time",180,"power.zone",int(ftp*0.85),int(ftp*0.90),desc="Under"),
                    _garmin_step("interval","time",180,"power.zone",int(ftp*0.98),int(ftp*1.06),desc="Over"),
                    _garmin_step("recovery","time",120,"power.zone",cd_lo,cd_hi,desc="Easy"),
                ], reps)
            add_step(_garmin_step("cooldown","time",300,"power.zone",cd_lo,cd_hi,desc="CD"))

        else:  # easy / recovery
            lo_w = int(ftp*(0.50 if intensity=="recovery" else 0.56))
            hi_w = int(ftp*(0.55 if intensity=="recovery" else 0.75))
            add_step(_garmin_step("warmup","time",600,"power.zone",wu_lo,int(ftp*0.55),desc="WU"))
            add_step(_garmin_step("interval","time",(dur-15)*60,"power.zone",lo_w,hi_w,desc="Z2 Endurance"))
            add_step(_garmin_step("cooldown","time",300,"power.zone",cd_lo,cd_hi,desc="CD"))

    # ── スイム ────────────────────────────────────────────────────
    elif sport == "swim":
        _css = css or 125  # 秒/100m
        # Garminスイムはdistanceステップが基本 (m単位)
        wu_pace_lo = _css * 1.25 / 100  # m/s
        wu_pace_hi = _css * 1.10 / 100
        _type = _seed % 4 if intensity=="hard" else _seed%3 if intensity=="moderate" else _seed%3

        if intensity == "hard":
            add_step(_garmin_step("warmup","distance",400,"pace.zone",wu_pace_hi,wu_pace_lo,desc="WU easy"))
            if _type == 0:  # CSS 100mインターバル
                reps = min(20, max(6, int((dur*60/(_css*1.05+20))*100//100)))
                add_repeat([
                    _garmin_step("interval","distance",100,"pace.zone",_css*1.02/100,_css*0.98/100,desc="CSS"),
                    _garmin_step("rest","time",20,desc="Rest"),
                ], reps)
            elif _type == 1:  # ピラミッド
                for d in [50,100,200,100,50]:
                    add_step(_garmin_step("interval","distance",d,"pace.zone",_css*1.02/100,_css*0.98/100,desc=f"{d}m"))
                    add_step(_garmin_step("rest","time",20,desc="Rest"))
            elif _type == 2:  # 200mスレッショルド
                reps = max(3, min(8, int((dur*60/(_css*1.10+30))*100//200)))
                add_repeat([
                    _garmin_step("interval","distance",200,"pace.zone",_css*1.08/100,_css*1.02/100,desc="Threshold"),
                    _garmin_step("rest","time",30,desc="Rest"),
                ], reps)
            else:  # 50mスプリント
                reps = min(12, max(6, int((dur-10)*60//95)))
                add_repeat([
                    _garmin_step("interval","distance",50,"pace.zone",_css*0.95/100,_css*0.90/100,desc="Sprint"),
                    _garmin_step("rest","time",45,desc="Rest"),
                ], reps)
            add_step(_garmin_step("cooldown","distance",200,"pace.zone",wu_pace_hi,wu_pace_lo,desc="CD"))

        elif intensity == "moderate":
            add_step(_garmin_step("warmup","distance",400,"pace.zone",wu_pace_hi,wu_pace_lo,desc="WU"))
            if _type == 0:  # 400mスレッショルド
                reps = max(2, int((dur-10)*60//(_css*1.10*4+30)))
                add_repeat([
                    _garmin_step("interval","distance",400,"pace.zone",_css*1.08/100,_css*1.02/100,desc="Threshold"),
                    _garmin_step("rest","time",30,desc="Rest"),
                ], reps)
            elif _type == 1:  # 混合ペース
                reps = min(12, max(6, int((dur-10)*60//(_css*1.05*2+35))))
                add_repeat([
                    _garmin_step("interval","distance",100,"pace.zone",_css*1.02/100,_css*0.98/100,desc="Fast"),
                    _garmin_step("rest","time",20,desc=""),
                    _garmin_step("interval","distance",100,"pace.zone",_css*1.18/100,_css*1.10/100,desc="Easy"),
                    _garmin_step("rest","time",15,desc=""),
                ], reps)
            else:  # ロングインターバル
                block = 600 if dur>=60 else 500
                reps = max(2, int((dur-10)*60//(block+20)))
                add_repeat([
                    _garmin_step("interval","distance",block,"pace.zone",_css*1.18/100,_css*1.10/100,desc="Endurance"),
                    _garmin_step("rest","time",20,desc=""),
                ], reps)
            add_step(_garmin_step("cooldown","distance",200,"pace.zone",wu_pace_hi,wu_pace_lo,desc="CD"))

        else:  # easy / recovery
            pace_lo = _css*1.28/100; pace_hi = _css*1.18/100
            total_m = max(500, int(dur * 60 / _css * 100))
            add_step(_garmin_step("warmup","distance",min(400,total_m//5),"pace.zone",pace_lo,pace_hi,desc="Easy"))
            add_step(_garmin_step("interval","distance",max(200,total_m-400-200),"pace.zone",pace_lo,pace_hi,desc="Aerobic"))
            add_step(_garmin_step("cooldown","distance",200,"pace.zone",pace_lo,pace_hi,desc="CD"))

    # ── 筋トレ / ヨガ (Garminはノーターゲットのtime stepで構成) ──
    else:
        warm_sec = min(5,dur//8) * 60
        cool_sec = 3 * 60
        main_sec = max(60, dur*60 - warm_sec - cool_sec)
        add_step(_garmin_step("warmup","time",warm_sec,desc="Warmup"))
        add_step(_garmin_step("interval","time",main_sec,desc="Main"))
        add_step(_garmin_step("cooldown","time",cool_sec,desc="Cooldown / Stretch"))

    # ── ワークアウトJSONを組み立て ────────────────────────────────
    workout_name = {
        "run": {"hard":"インターバルラン","moderate":"テンポラン","easy":"イージーラン","recovery":"リカバリーラン"},
        "bike": {"hard":"閾値バイク","moderate":"スイートスポットライド","easy":"Z2ライド","recovery":"リカバリーライド"},
        "swim": {"hard":"インターバルスイム","moderate":"テンポスイム","easy":"エンデュランススイム","recovery":"リカバリースイム"},
        "strength": {"hard":"筋トレ[Peak]","moderate":"筋トレ[Build]","easy":"筋トレ[Base]","recovery":"筋トレ[回復]"},
        "yoga": {"hard":"ダイナミックヨガ","moderate":"アクティブフロー","easy":"リカバリーフロー","recovery":"リストラティブヨガ"},
    }.get(sport, {}).get(intensity, f"{sport} {intensity}")

    return {
        "sportType": {
            "sportTypeId": {"running":1,"cycling":2,"swimming":5,"strength_training":13,"yoga":26,"other":174}.get(sport_key,174),
            "sportTypeKey": sport_key,
        },
        "subSportType": None,
        "workoutName": f"{workout_name} {dur}min",
        "description": f"Generated by smart_plan v9 | {phase} | {intensity} | {dur}min",
        "estimatedDurationInSecs": dur * 60,
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeKey": sport_key},
            "workoutSteps": steps,
        }],
    }


def export_garmin_workout_json(sport, intensity, dur, phase, tp, ftp, css=None):
    """ワークアウトJSONをファイルに書き出す (デバッグ・手動インポート用)"""
    import json, pathlib, datetime
    data = build_garmin_workout(sport, intensity, dur, phase, tp, ftp, css)
    fn = pathlib.Path(f"garmin_{sport}_{intensity}_{dur}min_{phase}.json")
    fn.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return fn


# ── intervals.icu との並列生成ラッパー ──────────────────────────────
def build_workout_both(sport, intensity, dur, phase, tp, ftp, css=None, goal_targets=None):
    """
    intervals.icu workout_doc と Garmin JSON を同時生成する。
    intervals.icu にアップロードしながら、Garmin deivceにも構造化ワークアウトを送れる。

    Returns:
        (workout_doc, desc_text, garmin_json)
    """
    wdoc, desc = build_workout(sport, intensity, dur, phase, tp, ftp, css, goal_targets)
    garmin = build_garmin_workout(sport, intensity, dur, phase, tp, ftp, css, goal_targets)
    return wdoc, desc, garmin

# ============================================================
# 栄養計算
# ============================================================
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
    if not cfg["nutrition"].get("uses_protein_supplement"):
        notes.append("鶏むね・卵・魚・豆腐・納豆でタンパク補給")
    if cond["condition"] in ("fatigued", "depleted"):
        notes.append("疲労回復中：青魚・ベリー・ショウガ（抗炎症）")

    return {
        "kcal": kcal, "prot": prot, "carb": carb, "fat": fat,
        "p_per_kg": round(p_r, 1),
        "exercise_kcal": round(exercise_kcal),
        "notes": notes,
    }

# ============================================================
# 週間プラン生成
# ============================================================
EMOJI={"run":"🏃","bike":"🚴","swim":"🏊","strength":"💪",
       "yoga":"🧘","stretch":"🤸","hiit":"🔥","rest":"😴","race":"🏁"}
SHORT_THRESH = 31

def generate_days(cfg, athlete, cond_info, race_info, gcal_days,
                  str_prog, start_date, goal_targets, num_days=10):
    """
    num_days 日分のトレーニング計画を生成する（デフォルト10日）。
    フェーズテンプレートは7日周期で繰り返す。
    カレンダーの練習会コメントにディレクティブがある場合はテンプレートを動的差し替え。
    """
    phase       = race_info["phase"]
    cond        = cond_info["condition"]
    base_template = PHASE_TEMPLATES.get(phase, PHASE_TEMPLATES["base"])
    # FAIGUEDはベーステンプレートを recovery に落とすが、
    # ディレクティブ(練習会指示)がある週末セッションは優先する
    use_recovery = cond in ("fatigued","depleted")
    if use_recovery:
        base_template = PHASE_TEMPLATES["recovery"]

    # ── ディレクティブ検出 ─────────────────────────────────────
    # 今日以降のすべてのカレンダーイベントからディレクティブを探す
    # (gcal_daysはスパースなので、存在するエントリを全検索する)
    directive = None
    all_drv_entries = sorted(
        [(d_str, d_info.get("directive"))
         for d_str, d_info in gcal_days.items()
         if d_info.get("directive")],
        key=lambda x: x[0]
    )
    # 今日以降の最初のディレクティブを採用
    start_str = start_date.strftime("%Y-%m-%d")
    for drv_date, drv in all_drv_entries:
        if drv_date >= start_str:
            directive = drv
            break
    # 見つからなければ最も近い過去のもの
    if not directive and all_drv_entries:
        directive = all_drv_entries[-1][1]

    # ディレクティブがあればテンプレートを動的生成
    # FAIGUEDでも週末セッションはディレクティブを尊重（ただし平日は回復優先）
    if directive and directive.get("target_distances"):
        if use_recovery:
            directive_template = build_directive_template(directive, base_template, num_days, start_date)
            template = None
        else:
            template = build_directive_template(directive, base_template, num_days, start_date)
            directive_template = template
        directive_label = directive.get("description","")
    else:
        template = base_template
        directive_template = None
        directive_label = None

    strength_cfg = cfg["strength"]
    str_sessions = 0
    str_max      = strength_cfg.get("sessions_per_week", 2)
    str_dur      = strength_cfg.get("session_duration_min", 30)
    deficient    = detect_deficient_sports(athlete.get("weekly_counts",{}), cfg)

    # ── extra_sessions 挿入ヘルパー ────────────────────────────
    # 全ての continue ブランチ（brick / short / strength / yoga / 有酸素）から
    # 共通で呼び出す。これにより「追加」リクエストがどの種目日でも確実に反映される。
    def _flush_extra(gcal_entry, day_str_):
        # ── GCal予約セッション（スイム/ラン/バイク）を最優先で組み込み ──
        for _fs in gcal_entry.get("forced_sessions", []):
            _sp    = _fs["sport"]
            _fdur  = _fs["duration"]
            _fname = _fs.get("name", _sp)
            # 同日・同種目のテンプレート生成セッションを削除して予約で上書き
            _dup = [p for p in plan if p.get("date") == day_str_ and p.get("sport") == _sp]
            for _d in _dup:
                plan.remove(_d)
                print(f"  🔄 GCal予約[{_fname}] → テンプレート{_sp}を差し替え")
            _gt  = goal_targets.get("targets") if isinstance(goal_targets, dict) else None
            _css = athlete.get("css", 125)
            _wdoc, _desc = build_workout(_sp, "easy", _fdur, phase,
                                         athlete["tp_sec"], athlete["ftp"],
                                         goal_targets=_gt, css=_css)
            _jp  = {"swim":"🏊 スイム","run":"🏃 ラン","bike":"🚴 バイク"}.get(_sp, _sp)
            plan.append({
                "date":         day_str_,
                "sport":        _sp,
                "intensity":    "easy",
                "duration_min": _fdur,
                "name":         f"{_jp}（GCal予約）",
                "description":  _desc,
                "workout_doc":  _wdoc,
                "gcal_notes":   [f"📅 GCal予約: {_fname} {_fdur}分"],
                "reduce_next":  False,
                "nutrition":    calc_nutrition(cfg, athlete, cond_info, phase,
                                              _fdur / 60, sport=_sp),
            })
            print(f"  📅 GCal予約: {_fname} ({_sp} {_fdur}分) [{day_str_}]")

        # ── ユーザー追加セッション（extra_sessions） ──
        for ex in gcal_entry.get("extra_sessions", []):
            ex_sport     = ex["sport"]
            ex_mins      = ex["mins"]
            ex_note      = ex.get("note", "")
            ex_intensity = decide_intensity(phase, ex_sport, cond)
            ex_wdoc, ex_desc = build_workout(ex_sport, ex_intensity, ex_mins, phase,
                                             athlete["tp_sec"], athlete["ftp"],
                                             goal_targets.get("targets"),
                                             css=athlete.get("css"))
            ex_desc  = f"➕ 【追加セッション】\n{ex_desc}"
            ex_n_jp  = {"run":"🏃 ラン","bike":"🚴 バイク","swim":"🏊 スイム"}.get(ex_sport, ex_sport)
            ex_il    = {"recovery":"リカバリー","easy":"イージー","moderate":"テンポ",
                        "hard":"インターバル/閾値"}.get(ex_intensity, "")
            plan.append({
                "date":         day_str_,
                "sport":        ex_sport,
                "name":         f"{ex_n_jp} – {ex_il}（追加）",
                "description":  ex_desc,
                "workout_doc":  ex_wdoc,
                "duration_min": ex_mins,
                "intensity":    ex_intensity,
                "gcal_notes":   [ex_note] if ex_note else [],
                "reduce_next":  False,
                "is_extra":     True,
                "nutrition":    calc_nutrition(cfg, athlete, cond_info, phase,
                                              ex_mins / 60, sport=ex_sport),
            })

    plan = []
    for i in range(num_days):
        day     = start_date + timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        is_weekend = (day.weekday() >= 5)

        # テンプレート選択: FAIGUEDの場合は平日=recovery、週末=directive
        if directive_template is not None and template is None:
            # FAIGUEDでディレクティブあり
            if is_weekend:
                row = directive_template[i % len(directive_template)]
            else:
                row = base_template[i % len(base_template)]
        elif template is not None:
            row = template[i % len(template)]
        else:
            row = base_template[i % len(base_template)]

        sport, sub, default_dur = row
        gcal    = gcal_days.get(day_str, {})
        avail   = gcal.get("available_min", 60 if day.weekday() < 5 else default_dur)
        races   = gcal.get("races", [])
        notes   = gcal.get("gcal_notes", gcal.get("notes", []))
        reduce_ = gcal.get("reduce_next_morning", False)
        day_directive = gcal.get("active_directive") or gcal.get("directive") or directive

        # ── ユーザーリクエストによる種目・時間の強制上書き ──────────
        # _apply_requests_to_gcal が設定した force_sport / force_min / intensity_shift を反映
        if gcal.get("force_sport"):
            sport = gcal["force_sport"]
            sub   = "easy"           # 強度はデフォルトeasy（後段で adjust）
            # force_min が指定されていればそれを avail に反映
            if gcal.get("force_min"):
                avail = max(avail, gcal["force_min"])
                default_dur = gcal["force_min"]


        if races:
            plan.append({"date":day_str,"sport":"race","name":races[0]["name"],
                         "description":"🏁 レース当日","duration_min":0,
                         "gcal_notes":notes,"reduce_next":reduce_,
                         "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,0,sport="race")})
            continue

        if sport == "rest" or avail == 0:
            plan.append({"date":day_str,"sport":"rest","name":"REST",
                         "description":"完全休養\n目的: 超回復のトリガー。何もしないことが最強のトレーニングです。\n💪 休む勇気を持つことも一流アスリートの条件です。",
                         "duration_min":0,"gcal_notes":notes,"reduce_next":reduce_,
                         "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,0,sport="rest")})
            _flush_extra(gcal, day_str)  # GCal予約（スイム/ラン等）は REST日でも組み込む
            continue

        # ── ブリックセッション → バイクとランの2セッションに分割 ──
        if sport == "brick":
            m_br = re.match(r'bike(\d+)\+run(\d+)', sub or "")
            bike_min = int(m_br.group(1)) if m_br else 90
            run_min  = int(m_br.group(2)) if m_br else 40
            total_min = bike_min + run_min
            if avail > 60:
                ratio = avail / total_min
                bike_min = int(bike_min * ratio)
                run_min  = int(run_min  * ratio)

            ftp_v = athlete.get("ftp", 200)
            tp_v  = athlete.get("tp_sec", 288)
            lo_w  = int(ftp_v * 0.85)
            hi_w  = int(ftp_v * 1.00)
            run_pace = _pace_to_icu(tp_v * 1.05)

            # ── バイク部分 ──
            bike_wdoc = (
                f"Brick Bike\n"
                f"- 10m ramp 50%-75% 85-95rpm\n"
                f"\n"
                f"Main Set\n"
                f"- {bike_min - 15}m {lo_w}-{hi_w}w 88-92rpm\n"
                f"\n"
                f"Build Finish\n"
                f"- 5m {int(ftp_v*0.90)}-{int(ftp_v*0.97)}w 90-95rpm\n"
            )
            bike_desc = (
                f"【ブリック】バイク {bike_min}分（ラン{run_min}分に続く）\n"
                f"目的: バイクからランへの切り替えに体を慣らす\n"
                f"W-up 10分 → {lo_w}-{hi_w}W {bike_min-15}分 → 残り5分で強度UP\n"
                f"ケイデンス85〜95rpm維持。T2は本番同様1〜2分で切り替えること。"
            )

            # ── ラン部分 ──
            _rp_slow = _pace_to_icu(int(tp_v * 1.05))  # 遅い方(5:02/km形式)
            _rp_fast = _pace_to_icu(int(tp_v * 1.00))  # 速い方(4:48/km形式)
            run_wdoc = (
                f"Brick Run\n"
                f"- {run_min}m {_rp_slow}-{_rp_fast} Pace\n"
            )
            run_desc = (
                f"【ブリック】ラン {run_min}分（バイク{bike_min}分の直後）\n"
                f"目的: バイク後の脚の切り替え体感・レース終盤の走りを刷り込む\n"
                f"目標ペース: {run_pace} 〜 最初1kmは慣性でオーバーペースに注意\n"
                f"脚が残っていない状態での走りがそのままレース終盤になります。"
            )

            nutr = calc_nutrition(cfg, athlete, cond_info, phase,
                                  (bike_min + run_min) / 60, sport="brick")

            plan.append({
                "date": day_str, "sport": "bike",
                "name": f"🚴→🏃 ブリック ① バイク {bike_min}分",
                "description": bike_desc, "workout_doc": bike_wdoc,
                "duration_min": bike_min, "intensity": "race_sim",
                "gcal_notes": notes, "reduce_next": reduce_,
                "is_brick": True, "brick_part": "bike",
                "nutrition": nutr,
            })
            plan.append({
                "date": day_str, "sport": "run",
                "name": f"🚴→🏃 ブリック ② ラン {run_min}分",
                "description": run_desc, "workout_doc": run_wdoc,
                "duration_min": run_min, "intensity": "race_sim",
                "gcal_notes": [], "reduce_next": reduce_,
                "is_brick": True, "brick_part": "run",
                "nutrition": nutr,
            })
            _flush_extra(gcal, day_str)
            continue

        # 短時間セッション
        if avail <= SHORT_THRESH and sport not in ("rest","yoga","stretch"):
            short = pick_short_session(avail, cond_info, phase, deficient,
                                       strength_cfg, str_prog)
            plan.append({**short,"date":day_str,"gcal_notes":notes,"reduce_next":reduce_,
                         "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,short["duration_min"]/60,sport=short.get("sport"))})
            _flush_extra(gcal, day_str)
            continue

        # 筋トレ — 週2回上限
        week_num = i // 7
        if sport == "strength":
            if str_sessions >= str_max * (week_num + 1) or avail < 20:
                short = pick_short_session(min(30, avail), cond_info, phase, deficient,
                                           strength_cfg, str_prog)
                plan.append({**short,"date":day_str,"gcal_notes":notes,"reduce_next":reduce_,
                             "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,0.3,sport="strength")})
                _flush_extra(gcal, day_str)
                continue
            str_sessions += 1
            dur  = min(str_dur, avail)
            menu = gen_strength_menu(strength_cfg, phase, cond_info, str_prog, dur)
            plan.append({"date":day_str,"sport":"strength","name":f"筋トレ [{str_prog['level']}]",
                         "description":menu,"duration_min":dur,"gcal_notes":notes,
                         "reduce_next":reduce_,
                         "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,0.5,sport="strength")})
            _flush_extra(gcal, day_str)
            continue

        if sport == "yoga":
            short = pick_short_session(min(30, avail), cond_info, phase, deficient,
                                       strength_cfg, str_prog)
            plan.append({**short,"date":day_str,"gcal_notes":notes,"reduce_next":reduce_,
                         "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,0.2,sport=sport)})
            _flush_extra(gcal, day_str)
            continue

        # 有酸素セッション — ディレクティブがある場合は強度を上書き
        if day_directive and sub in ("race_sim","threshold","sweetspot"):
            intensity = "hard" if sub in ("race_sim","threshold") else "moderate"
        else:
            intensity = decide_intensity(phase, sport, cond)

        prev_str = (day - timedelta(1)).strftime("%Y-%m-%d")
        if gcal_days.get(prev_str, {}).get("reduce_next_morning"):
            idx = INTENSITY_ORDER.index(intensity) if intensity in INTENSITY_ORDER else 1
            intensity = INTENSITY_ORDER[max(0, idx - 1)]

        # ── ユーザー指定の強度シフト（intensity_shift: "up"/"down"） ──
        intensity_shift = gcal.get("intensity_shift")
        if intensity_shift:
            INTENSITY_ORDER_LOCAL = ["recovery", "easy", "moderate", "hard"]
            idx = INTENSITY_ORDER_LOCAL.index(intensity) if intensity in INTENSITY_ORDER_LOCAL else 1
            if intensity_shift == "down":
                intensity = INTENSITY_ORDER_LOCAL[max(0, idx - 1)]
            elif intensity_shift == "up":
                intensity = INTENSITY_ORDER_LOCAL[min(len(INTENSITY_ORDER_LOCAL)-1, idx + 1)]

        dur  = max(20, min(default_dur, avail))
        workout_doc, desc = build_workout(sport, intensity, dur, phase,
                                          athlete["tp_sec"], athlete["ftp"],
                                          goal_targets.get("targets"),
                                          css=athlete.get("css"))

        # ディレクティブ由来のセッションには目標メモを付加（desc_text と workout_doc 両方に）
        if day_directive and day_directive.get("target_distances"):
            td = day_directive["target_distances"]
            target_event = day_directive.get("target_event","練習会")
            if sport in td:
                dist = td[sport]
                target_pace_str = ""
                if sport == "run":
                    target_pace_str = f"  目標ペース: {_fmt_pace(athlete['tp_sec'])}/km"
                elif sport == "bike":
                    target_pace_str = f"  目標出力: {int(athlete.get('ftp',200)*0.85)}〜{athlete.get('ftp',200)}W"
                goal_note = (f"\n\n🎯 【{target_event}対策】本番距離{dist:.0f}km に向けた"
                             f"{'本番強度' if sub=='race_sim' else '閾値'}セッション。{target_pace_str}")
                desc        += goal_note
                workout_doc += f"\n# 🎯 {target_event}対策 本番{dist:.0f}km 向け\n"

        n_jp = {"run":"🏃 ラン","bike":"🚴 バイク","swim":"🏊 スイム"}.get(sport, sport)
        il   = {"recovery":"リカバリー","easy":"イージー","moderate":"テンポ",
                "hard":"インターバル/閾値"}.get(intensity, "")
        plan.append({"date":day_str,"sport":sport,"name":f"{n_jp} – {il}",
                     "description":desc,"workout_doc":workout_doc,
                     "duration_min":dur,"intensity":intensity,
                     "gcal_notes":notes,"reduce_next":reduce_,
                     "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,dur/60,sport=sport)})

        _flush_extra(gcal, day_str)

    # 整合性チェック
    plan, check_notes = consistency_check(plan)
    if check_notes:
        print(f"  🔧 整合性チェック: {len(check_notes)}件修正")
        for cn in check_notes:
            print(f"     • {cn}")

    return plan


def _build_brick_session(sub_str, directive, phase, athlete, goal_targets):
    """
    ブリックセッション（バイク→ラン）の説明文と合計時間を生成する。
    sub_str: "bike{N}+run{M}" 形式
    """
    bike_min, run_min = 90, 40  # デフォルト
    m = re.match(r'bike(\d+)\+run(\d+)', sub_str or "")
    if m:
        bike_min = int(m.group(1))
        run_min  = int(m.group(2))

    total_min = bike_min + run_min
    ftp = athlete.get("ftp", 200)
    tp  = athlete.get("tp_sec", 288)
    target_event = (directive or {}).get("target_event", "練習会")
    td = (directive or {}).get("target_distances", {})
    bike_dist = td.get("bike", 0)
    run_dist  = td.get("run", 0)

    bike_pct = int(bike_min / (bike_dist / 30 * 60) * 100) if bike_dist else 70
    run_pct  = int(run_min  / (run_dist  / 10 * 60) * 100) if run_dist  else 70

    desc = f"""【ブリック – バイク→ラン連続】 合計{total_min}分
目的: バイクからランへの切り替えに体を慣らす最重要セッション。脚が残っていない状態でのランがそのままレース終盤の走りになります。

🚴 バイク {bike_min}分（本番距離{bike_dist:.0f}kmの約{bike_pct}%）
  強度: {int(ftp*0.85)}〜{ftp}W（FTPの85〜100%）
  ケイデンス85〜95rpmを維持。残り10分で90〜95%まで上げて脚を追い込む。

🏃 ラン {run_min}分（本番距離{run_dist:.0f}kmの約{run_pct}%）  ← バイク直後に即スタート
  目標ペース: {_fmt_pace(tp)}〜{_fmt_pace(int(tp*1.05))}/km
  最初の1kmはバイクの慣性で速くなりがち。ペースを意識して入ること。
  本番のラストのきつさを体に刷り込む時間です。

💡 トランジション（T2）は本番同様1〜2分で切り替えること。
🎯 【{target_event}対策】バイク{bike_dist:.0f}km＋ラン{run_dist:.0f}kmをこなすための最重要ブリック練。"""

    return desc, total_min


def consistency_check(plan):
    """
    生成されたプランの整合性をチェックし、問題を修正して返す。
    チェック内容:
      - 同一日に同種目が2件 → 強度の低い方を削除
      - スイム+レスト → レストを削除
      - レスト+レスト → 1件に統合
      - ブリックとスイムが同日 → スイムをブリック翌日に移動
      - 1日4件以上 → 超過分を翌日に押し出し
    Returns: (修正済みplan, [修正メモ])
    """
    from collections import defaultdict
    notes = []

    # 日付ごとにグループ化
    by_date = defaultdict(list)
    for item in plan:
        by_date[item["date"]].append(item)

    result = []
    dates  = sorted(by_date.keys())

    for i, d in enumerate(dates):
        items = by_date[d]

        # ── レスト×2 統合 ──
        rests  = [x for x in items if x["sport"] == "rest"]
        others = [x for x in items if x["sport"] != "rest"]
        if len(rests) > 1:
            items = others + [rests[0]]
            notes.append(f"{d}: レスト重複 → 1件に統合")

        # ── 同種目重複 → 強度低い方を削除 ──
        INTENSITY_ORDER_L = ["recovery","easy","moderate","hard","very_hard"]
        sport_seen = {}
        keep = []
        for item in sorted(items, key=lambda x: INTENSITY_ORDER_L.index(
                x.get("intensity","easy")) if x.get("intensity","easy") in INTENSITY_ORDER_L else 2,
                reverse=True):  # 高強度を優先
            sp = item["sport"]
            if sp == "rest":
                keep.append(item); continue
            if sp not in sport_seen:
                sport_seen[sp] = item
                keep.append(item)
            else:
                # 低強度の重複を除去
                notes.append(f"{d}: {sp}重複 → 強度低い方({item.get('intensity','?')})を削除")
        items = keep

        # ── 運動セッション+レスト混在 → レスト削除 ──
        # 「スイム削除→レストのみ」の状態も修正（レストが非休息日に単独残る場合は保持）
        has_workout = any(x["sport"] not in ("rest",) for x in items)
        if has_workout and any(x["sport"] == "rest" for x in items):
            items = [x for x in items if x["sport"] != "rest"]
            notes.append(f"{d}: 運動+レスト混在 → レスト削除")

        # ── ブリックとスイムが同日 → スイムを翌日に移動 ──
        has_brick = any(x["sport"] == "brick" for x in items)
        swim_items = [x for x in items if x["sport"] == "swim"]
        if has_brick and swim_items:
            # 翌日に移動
            next_d = dates[i+1] if i+1 < len(dates) else None
            if next_d:
                for sw in swim_items:
                    sw_copy = dict(sw); sw_copy["date"] = next_d
                    by_date[next_d].insert(0, sw_copy)
                items = [x for x in items if x["sport"] != "swim"]
                notes.append(f"{d}: ブリック+スイム同日 → スイムを{next_d}に移動")

        # ── 1日4件超 → 超過分を翌日に押し出し ──
        MAX_PER_DAY = 3
        if len(items) > MAX_PER_DAY:
            overflow = items[MAX_PER_DAY:]
            items    = items[:MAX_PER_DAY]
            next_d   = dates[i+1] if i+1 < len(dates) else None
            if next_d:
                by_date[next_d] = overflow + by_date[next_d]
                notes.append(f"{d}: {len(overflow)}件超過 → {next_d}に押し出し")

        result.extend(items)

    return result, notes


# 後方互換エイリアス
def generate_week(cfg, athlete, cond_info, race_info, gcal_days,
                  str_prog, start_date, goal_targets):
    return generate_days(cfg, athlete, cond_info, race_info, gcal_days,
                         str_prog, start_date, goal_targets, num_days=7)

# ============================================================
# 出力
# ============================================================

def print_calorie_summary(plan, cfg):
    """
    プランの日次カロリー・栄養素サマリーを表示する。
    各日のセッション消費カロリー + 推定TDEEを表示。

    引数:
        plan: generate_days()の戻り値リスト
        cfg:  設定辞書 (athleteセクション含む)
    """
    if not plan:
        return

    athlete_cfg = cfg.get("athlete", {})
    weight = float(athlete_cfg.get("weight_kg", 68.4))

    # MET値テーブル (種目別おおよそのMET)
    MET_MAP = {
        "run":      9.8,   # ランニング (中強度)
        "bike":     7.5,   # サイクリング (中強度)
        "swim":     8.0,   # 水泳
        "strength": 4.5,   # 筋トレ
        "yoga":     3.0,   # ヨガ
        "walk":     3.5,   # ウォーキング
        "rest":     0.0,
        "race":    10.0,
    }

    # 強度係数
    INTENSITY_MET = {
        "recovery": 0.70,
        "easy":     0.85,
        "moderate": 1.00,
        "hard":     1.25,
    }

    print(f"\n{'─'*64}")
    print(f"  🍽  カロリー & 栄養素サマリー")
    print(f"{'─'*64}")
    print(f"  {'日付':<12} {'種目':<8} {'時間':>5} {'消費kcal':>8}  {'目安P/C/F'}")
    print(f"  {'─'*60}")

    total_kcal = 0
    for day in plan:
        sport    = day.get("sport", "rest")
        dur      = day.get("duration", 0)
        intens   = day.get("intensity", "easy")
        dt_str   = day.get("date", "")

        if sport == "rest":
            # 安静日のBMR概算 (Mifflin-St Jeor 男性)
            bmr = int(10 * weight + 6.25 * 170 - 5 * 40 + 5)
            print(f"  {dt_str:<12} {'REST':<8} {'─':>5} {'─':>8}  BMR≒{bmr}kcal")
            continue

        met_base = MET_MAP.get(sport, 5.0)
        met_mod  = INTENSITY_MET.get(intens, 1.0)
        met_eff  = met_base * met_mod
        kcal     = int(met_eff * weight * (dur / 60))
        total_kcal += kcal

        # 簡易栄養素 (P=2g/kg + 運動量, C/Fは比例)
        tdee_est = 2000 + kcal
        prot = min(180, int(weight * 2.0 + dur * 0.3))
        fat  = int(tdee_est * 0.25 / 9)
        carb = int((tdee_est - prot * 4 - fat * 9) / 4)

        sport_jp = {"run":"ラン","bike":"バイク","swim":"スイム",
                    "strength":"筋トレ","yoga":"ヨガ","race":"レース"}.get(sport, sport)
        print(f"  {dt_str:<12} {sport_jp:<8} {dur:>4}分 {kcal:>7}kcal  "
              f"P{prot}g / C{carb}g / F{fat}g")

    print(f"  {'─'*60}")
    print(f"  {'合計運動消費':30s} {total_kcal:>7}kcal")
    avg = total_kcal // max(1, len([d for d in plan if d.get("sport","rest") != "rest"]))
    print(f"  {'運動日平均消費':30s} {avg:>7}kcal")
    print(f"\n  ⚠️  上記は概算値。正確な消費kcalはGarmin Connectの実測値を参照してください。")
    print(f"  💡 Garmin ConnectのNutritionセクションと連携すると詳細な栄養管理が可能です。")
    print(f"{'─'*64}\n")


def print_plan(plan, race_info, cond_info, athlete, goal_targets, cfg=None, today_mode=False, str_prog=None, gcal_days=None, num_days=10):
    # ── GCalスケジュールサマリ（メニュー生成期間分） ──
    if gcal_days is not None:
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
    weight      = float(athlete.get("weight_kg", 68.4))
    # 体脂肪率・除脂肪体重 (lbm) をathleteから取得（なければ推定）
    body_fat_pct = float(athlete.get("body_fat_pct") or cfg.get("athlete",{}).get("body_fat_pct", 18.0))
    fat_kg       = weight * body_fat_pct / 100
    lean_kg      = weight - fat_kg   # 除脂肪体重（筋肉+骨+内臓等）
    # 骨・内臓等(体重の約15%)を除いた推定「骨格筋量」
    # 除脂肪体重の約55%が骨格筋（成人男性平均）
    estimated_muscle = lean_kg * 0.55

    goal_muscle  = float(str_prog.get("goal_muscle_kg") or 0)
    goal_date    = str_prog.get("goal_date","")
    weeks_left   = str_prog.get("weeks_to_goal", 0)
    level        = str_prog.get("level","base")

    # Aレース日の目標体重（筋肉量を増やしながら体重は維持or軽減が理想）
    a_race = race_info.get("race")
    a_race_date = a_race["date"] if a_race else ""
    weeks_to_a  = race_info.get("weeks_to_race", 0)

    print(f"\n  ─── 体組成ステータス ───")
    print(f"  現在  体重:{weight:.1f}kg  推定骨格筋量:{estimated_muscle:.1f}kg  "
          f"体脂肪:{body_fat_pct:.0f}%({fat_kg:.1f}kg)  除脂肪:{lean_kg:.1f}kg")

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



# ============================================================
# アップロード
# ============================================================
def upload_plan(plan, cfg, dry_run=False):
    """
    トレーニング計画を Intervals.icu にアップロードする。

    アップロード前に対象期間の既存 WORKOUT イベントを削除して上書きを防ぐ。
    dry_run=True の場合は削除・投稿ともにスキップ（プレビューのみ）。
    """
    aid     = cfg["athlete"]["intervals_icu_athlete_id"]
    api_key = cfg["athlete"]["intervals_icu_api_key"]
    base    = f"https://intervals.icu/api/v1/athlete/{aid}/events"
    tmap    = {"run":"Run","bike":"Ride","swim":"Swim","brick":"Ride",
               "strength":"WeightTraining","yoga":"Yoga",
               "stretch":"Workout","hiit":"WeightTraining"}

    # ── 対象日付範囲を計算 ─────────────────────────────────────
    dates = sorted(set(item["date"] for item in plan
                       if item["sport"] not in ("rest","race")))
    if not dates:
        print("  アップロード対象なし")
        return 0

    oldest = dates[0]
    newest = dates[-1]

    if not dry_run:
        # ── STEP 1: 対象期間の既存 WORKOUT イベントを取得して削除 ──
        print(f"\n  🗑  既存ワークアウトを削除中 ({oldest} 〜 {newest})...")
        existing = icu_get(base, api_key, {
            "oldest": oldest,
            "newest": newest,
        }) or []

        # category=="WORKOUT" のものだけ対象（レース・休養は触らない）
        to_delete = [ev for ev in existing
                     if ev.get("category") == "WORKOUT"]

        deleted = 0
        for ev in to_delete:
            ev_id  = ev.get("id")
            ev_url = f"{base}/{ev_id}"
            ev_name = ev.get("name","(無名)")
            ev_date = (ev.get("start_date_local") or "")[:10]
            ok = icu_delete(ev_url, api_key)
            status = "🗑 削除" if ok else "⚠️ 削除失敗"
            print(f"    {status}: {ev_date} {ev_name}")
            if ok:
                deleted += 1

        print(f"  ✅ {deleted}/{len(to_delete)} 件を削除しました\n")

    # ── STEP 2: 新しい計画をアップロード ──────────────────────
    ok = 0
    for item in plan:
        if item["sport"] in ("rest", "race"):
            continue

        payload = {
            "start_date_local": f"{item['date']}T00:00:00",
            "category":         "WORKOUT",
            "type":             tmap.get(item["sport"], "Workout"),
            "name":             item["name"],
            "description":      (lambda w,d: (w+"\n\n"+d) if w and d and d not in w else (w or d))(
                                    item.get("workout_doc",""), item.get("description","")),
            "moving_time":      item["duration_min"] * 60,
        }

        tag = "[DRY] " if dry_run else ""
        print(f"  {tag}📤 {item['date']} {item['name']}", end=" ")

        if dry_run:
            print("(skip)")
            ok += 1
        else:
            r = icu_post(base, api_key, payload)
            print("✅" if r else "❌")
            if r:
                ok += 1

    return ok


# ============================================================
# Googleカレンダー → 週間データ変換
# ============================================================

# 仕事関連キーワード（レースや旅行は別処理）
WORK_KWS   = ["出社","在宅","テレワーク","リモート","半休","早退","休暇","有給","会議","打合","出張"]
FLIGHT_KWS = ["フライト","flight","JL","NH","搭乗"]
TRIP_KWS   = ["出張","travel","trip"]
RACE_KWS   = ["トライアスロン","triathlon","マラソン","marathon","大会","レース","race","duathlon",
              "アイアン","スプリント","オリンピック","ラン大会","swim meet"]

def parse_gcal_events_to_days(events, cfg_cal, athlete=None, cfg=None):
    """
    Gcalイベントリスト → {date_str: day_info} と races リストを返す
    """
    gcal_days     = {}
    races_from_cal = []

    for ev in events:
        title = (ev.get("summary") or "").strip()
        desc  = (ev.get("description") or "")
        tl    = title.lower()

        # 日付取得（終日 or 時刻指定）
        start_raw = ev.get("start",{})
        if start_raw.get("date"):
            day_str = start_raw["date"]
        elif start_raw.get("dateTime"):
            day_str = start_raw["dateTime"][:10]
        else:
            continue

        try:
            day = date.fromisoformat(day_str)
        except:
            continue

        # 既存エントリ初期化
        if day_str not in gcal_days:
            is_weekend = day.weekday() >= 5
            gcal_days[day_str] = {
                "available_min": cfg_cal["default_availability"]["weekend_max_min"] if is_weekend else 60,
                "morning_ok": True,
                "is_trip": False,
                "races": [],
                "notes": [],
                "gcal_notes": [],
                "reduce_next_morning": False,
            }
        d = gcal_days[day_str]

        # ── レース判定 ────────────────────────────────────────
        if any(k.lower() in tl for k in RACE_KWS):
            # 過去タイムをdescから抽出
            result_m = re.search(
                r'(?:過去|前回|リザルト|result|タイム)[\s:：]\s*(\d{1,2}:\d{2}:\d{2})',
                desc, re.IGNORECASE)
            # ライバル
            rival_m = re.search(r'(?:目標|ライバル|rival)[\s:：No\d]*\s*([^\d\n]{2,20})\s+(\d{1,2}:\d{2}:\d{2})', desc)
            if not rival_m:
                rival_m2 = re.search(r'(?:目標|ライバル)[\s:：]\s*(.{2,25})', desc)

            past_time_s = None
            past_time_str = ""
            if result_m:
                t = result_m.group(1)
                parts = t.split(":")
                try:
                    past_time_s = int(parts[0])*3600+int(parts[1])*60+int(parts[2])
                    past_time_str = t
                except: pass

            rival_name = None
            rival_time_s = None
            rival_time_str = ""
            if rival_m:
                try:
                    rival_name = rival_m.group(1).strip()
                    rt = rival_m.group(2)
                    rp = rt.split(":")
                    rival_time_s = int(rp[0])*3600+int(rp[1])*60+int(rp[2])
                    rival_time_str = rt
                except: pass
            elif 'rival_m2' in dir() and rival_m2:
                rival_name = rival_m2.group(1).strip()

            attachments = ev.get("attachments") or []
            location = ev.get("location","")

            # 天気・気温情報を取得
            weather = fetch_weather_for_race(location, day_str)

            # スプリットタイム取得の優先順位:
            #   1) カレンダー説明欄内のファイル名 or パス（PDF/Excel/CSV）
            #      → config.yaml の results.folder を基点に自動検索
            #   2) 説明欄テキストから正規表現パース
            splits_from_desc = {}
            file_source = None

            # ① Google Drive添付ファイルからPDF取得を試みる
            for _att in attachments:
                _att_id    = _att.get("fileId","")
                _att_title = _att.get("title","")
                if _att_id and _att_title.lower().endswith(".pdf") and not splits_from_desc:
                    _gtext, _gerr = fetch_gdrive_pdf_via_api(_att_id, cfg)
                    if _gtext:
                        splits_from_desc = parse_split_times_from_text(_gtext)
                        file_source = f"gdrive:{_att_id}"
                        print(f"    ☁️  GDrive PDF: {_att_title}  スプリット{len(splits_from_desc)}種  ✅")
                    elif _gerr:
                        print(f"    ☁️  GDrive [{_att_title}]: {_gerr[:70]}")

            # ② 説明欄からファイル名/パスを探す（パターン優先度順）
            FILE_PATTERNS = [
                # 「ファイル: xxx.xlsx」「file: xxx.pdf」形式
                r'(?:ファイル|file|path|リザルト|result)[\s:：]+([^\s\n]+\.(?:xlsx?|csv|pdf))',
                # フルパス形式（~/... , /... , ./...）
                r'((?:~|\.\.?)?/[^\s\n]+\.(?:xlsx?|csv|pdf))',
                # ファイル名だけ（行頭 or スペース区切り）
                r'(?:^|[\s　])([^\s/\n]+\.(?:xlsx?|csv|pdf))(?:\s|$)',
            ]
            found_file = None
            for pat in FILE_PATTERNS:
                m = re.search(pat, desc, re.IGNORECASE | re.MULTILINE)
                if m:
                    found_file = m.group(1).strip()
                    break

            if found_file:
                resolved_path, _rl = resolve_result_path(found_file, cfg)
                print(f"    🔍 Resultsフォルダ検索: '{found_file}'")
                for _rlog in _rl:
                    print(f"       {_rlog}")
                _use_path = resolved_path or found_file
                file_result = parse_result_file(_use_path, cfg=cfg)
                if file_result.get("splits"):
                    splits_from_desc = file_result["splits"]
                    resolved = file_result.get("resolved_path") or found_file
                    file_source = resolved
                    rname = Path(resolved).name if resolved else found_file
                    method = file_result.get("method","")
                    method_jp = {"PyPDF2":"PyPDF2","zlib":"zlib独自","text_direct":"テキスト直接",
                                 "openpyxl":"Excel","csv":"CSV","activities_csv":"intervals.icu CSV"}.get(method, method)
                    print(f"    📂 リザルト読込: {rname}  [{method_jp}]  "
                          f"スプリット{len(splits_from_desc)}種  ✅")
                    for _k,_v in list(splits_from_desc.items())[:6]:
                        if isinstance(_v,dict) and _v.get("str"):
                            print(f"       {_k}: {_v['str']}")
                elif file_result.get("error"):
                    print(f"    ⚠️  リザルト読込失敗: {Path(found_file).name}")
                    print(f"       理由: {file_result['error'][:80]}")
                    # CSV fallback: activities_detail.csvから試みる
                    csv_path = _find_activities_csv()
                    if csv_path and csv_path.exists():
                        kws = title.split()[:3]
                        csv_splits = load_race_splits_from_csv(str(csv_path), race_name_kws=kws)
                        if csv_splits.get("swim") or csv_splits.get("bike"):
                            splits_from_desc = csv_splits
                            file_source = str(csv_path)
                            print(f"    📂 CSVフォールバック: activities_detail.csv  ✅")

            # ファイルパース失敗 or ファイルなし → テキストから抽出
            if not splits_from_desc:
                splits_from_desc = parse_split_times_from_text(desc)

            race_entry = {
                "name":          title,
                "date":          day_str,
                "type":          _detect_race_type(tl),
                "distance":      _detect_race_distance(tl),
                "priority":      _parse_race_priority(desc, title),
                "rival":         rival_name,
                "rival_time_s":  rival_time_s,
                "rival_time_str":rival_time_str,
                "past_time_s":   past_time_s,
                "past_time_str": past_time_str,
                "location":      location,
                "attachments":   [a.get("title","") for a in attachments],
                "splits":        splits_from_desc,
                "splits_source": file_source or "calendar_text",
                "weather":       weather,
                "past_result":   {"time_s": past_time_s, "time_str": past_time_str,
                                  "source": file_source or "calendar_text",
                                  "splits": splits_from_desc} if past_time_s else None,
            }
            d["races"].append(race_entry)
            d["available_min"] = 0
            races_from_cal.append(race_entry)

            note = f"🏁 {title}"
            if past_time_str: note += f"  前回:{past_time_str}"
            if rival_name:     note += f"  目標:{rival_name}"
            if rival_time_str: note += f" {rival_time_str}"
            d["gcal_notes"].append(note)
            continue

        # ── フライト ──────────────────────────────────────────
        if any(k in title for k in FLIGHT_KWS):
            # 出発日・到着日を出張として扱う（前後日の練習を制限）
            d["is_trip"] = True
            d["available_min"] = min(d["available_min"], 30)
            dest = ev.get("location","").split("\n")[0][:20]
            d["gcal_notes"].append(f"✈️ フライト {title[:20]}")
            continue

        # ── 出張 ─────────────────────────────────────────────
        if any(k in title for k in TRIP_KWS):
            d["is_trip"] = True
            d["available_min"] = min(d["available_min"], 30)
            d["gcal_notes"].append(f"✈️ 出張: {title}")
            continue

        # ── 在宅・テレワーク ──────────────────────────────────
        if any(k in title for k in ["在宅","テレワーク","リモート"]):
            d["morning_ok"] = True
            d["available_min"] = min(
                d["available_min"] + 30,
                cfg_cal["default_availability"]["weekend_max_min"])
            d["gcal_notes"].append(f"🏠 {title} → 朝練可・+30分")
            continue

        # ── 出社 ─────────────────────────────────────────────
        if any(k in title for k in ["出社"]):
            d["morning_ok"] = False
            d["gcal_notes"].append(f"🏢 出社 → 夜練60分")
            continue

        # ── クラブ予約スイム・ラン・バイク（QUOX等） ──────────────────────
        # タイトルに「スイム」「ラン」「バイク」を含む時間指定イベント → forced_sessions
        _GCAL_SPORT_MAP = {
            "swim": ["スイム", "swim", "水泳", "プール"],
            "run":  ["ラン", "run", "ランニング", "jog", "ジョグ"],
            "bike": ["バイク", "bike", "cycle", "ライド"],
        }
        _ev_sport = None
        for _sk, _kws in _GCAL_SPORT_MAP.items():
            if any(_k.lower() in tl for _k in _kws):
                _ev_sport = _sk; break

        if _ev_sport and "_GCAL_SPORT_MAP" not in str(d.get("forced_sessions",[])):
            _ev_dur = 60
            if ev.get("start", {}).get("dateTime"):
                try:
                    import datetime as _dt2
                    _st2 = _dt2.datetime.fromisoformat(ev["start"]["dateTime"])
                    _en2 = _dt2.datetime.fromisoformat(ev["end"]["dateTime"])
                    _ev_dur = max(20, int((_en2 - _st2).total_seconds() / 60))
                except: pass
            _loc2 = ev.get("location","").split(",")[0][:20].strip()
            _ls2  = f" @ {_loc2}" if _loc2 else ""
            d["gcal_notes"].append(f"📅 予約{_ev_sport.upper()} {_ev_dur}分{_ls2}")
            if "forced_sessions" not in d: d["forced_sessions"] = []
            if not any(f["sport"] == _ev_sport for f in d["forced_sessions"]):
                d["forced_sessions"].append({
                    "sport": _ev_sport, "duration": _ev_dur,
                    "source": "gcal_reservation", "name": title,
                })
            continue

        # ── 飲み会・会食 ─────────────────────────────────────
        if any(k in title for k in ["飲み会","会食","懇親"]):
            d["available_min"] = 0
            d["reduce_next_morning"] = True
            d["gcal_notes"].append(f"🍺 {title} → 練習なし・翌朝軽め")
            continue

        # ── 朝ラン可能など直接指定 ────────────────────────────
        direct_m = re.search(r'(\d{1,3})\s*分.*(?:練習|トレーニング|ラン|バイク)可能', title+desc)
        morning_m = re.search(r'朝ラン可能|朝練可能|morning run', title+desc, re.IGNORECASE)
        if direct_m:
            d["available_min"] = int(direct_m.group(1))
            d["gcal_notes"].append(f"⏱ {direct_m.group(1)}分練習可")
        elif morning_m:
            d["morning_ok"] = True
            d["gcal_notes"].append(f"🌅 朝ラン可能")

        # ── 練習会・グループラン ──────────────────────────────
        if any(k in title for k in ["練習会","グループラン","チーム練習","グループ練"]):
            directive = parse_training_directive(title, desc)
            d["gcal_notes"].append(f"👥 {title}")
            if directive["target_distances"] or directive["is_race_sim"]:
                d["gcal_notes"].append(f"🎯 目標: {directive['description']}")
            d["directive"] = directive
            d["directive_target_date"] = day_str

    # ── 練習会ディレクティブを今日〜その日までの全日に伝播 ──────────
    # 最も近い upcoming_directive を探して、その日以前の全日に適用する
    all_directives = [(d_str, d_info["directive"], d_info["directive_target_date"])
                      for d_str, d_info in gcal_days.items()
                      if d_info.get("directive")]
    if all_directives:
        all_directives.sort(key=lambda x: x[0])  # 日付昇順
        for day_str, day_info in gcal_days.items():
            if day_info.get("directive"):
                continue  # 練習会当日はスキップ
            # 自分より後にある最初のディレクティブを適用
            applicable = next(
                (drv for drv_date, drv, _ in all_directives
                 if drv_date > day_str),
                None
            )
            if applicable:
                day_info["active_directive"] = applicable

    return gcal_days, races_from_cal






# ============================================================
# サマリ1: レーススケジュール
# ============================================================
PHASE_JP = {
    "base":"ベース期","build":"ビルド期","peak":"ピーク期",
    "taper":"テーパー期","race_week":"レース週","recovery":"回復期",
}
RACE_TYPE_JP = {
    "triathlon":"トライアスロン","marathon":"マラソン",
    "cycling":"サイクリング","swim":"スイム","race":"レース",
}
DIST_JP = {
    "sprint":"スプリント(25.75km)","olympic":"オリンピック(51.5km)",
    "middle":"ミドル(113km)","half":"ミドル(113km)","iron":"アイアン(226km)",
    "sprint":"スプリント(25.75km)","marathon":"フルマラソン(42.195km)",
    "half_run":"ハーフマラソン(21.097km)","full":"フルマラソン(42.195km)",
    "10k":"10kmレース","5k":"5kmレース","duathlon":"デュアスロン","unknown":"",
}

def _phase_from_weeks(weeks):
    if   weeks > 16: return "base"
    elif weeks > 8:  return "build"
    elif weeks > 3:  return "peak"
    elif weeks > 1:  return "taper"
    elif weeks == 0: return "race_week"
    else:            return "recovery"

def print_race_schedule_summary(races, athlete, cfg):
    today = date.today()
    upcoming = sorted([r for r in races if date.fromisoformat(r["date"]) >= today],
                      key=lambda r: r["date"])
    past_races = sorted([r for r in races if date.fromisoformat(r["date"]) < today],
                        key=lambda r: r["date"], reverse=True)

    print(f"\n{'═'*64}")
    print(f"  🏁 レーススケジュール  予定:{len(upcoming)}件  完了:{len(past_races)}件")
    print(f"{'═'*64}")

    if not upcoming:
        print("  予定レースなし — Googleカレンダーにレースイベントを追加してください")
        return

    for r in upcoming:
        rd    = date.fromisoformat(r["date"])
        days  = (rd - today).days
        weeks = days // 7
        phase = _phase_from_weeks(weeks)

        # 目標タイム計算
        goal_str = ""
        if r.get("past_time_s"):
            goal_s   = int(r["past_time_s"] * 0.97)
            goal_str = f"目標:{_fmt_time(goal_s)}  (前回{r['past_time_str']}の-3%)"
        elif r.get("rival_time_s"):
            goal_str = f"目標:{r['rival_time_str']}  (ライバル:{r.get('rival_name','?')}に追いつく)"
        elif r.get("rival_name"):
            goal_str = f"目標ライバル: {r['rival_name']}"

        rtype = RACE_TYPE_JP.get(r.get("type","race"), r.get("type",""))
        dist  = DIST_JP.get(r.get("distance","unknown"), "")
        loc   = r.get("location","").split(",")[0].split("\n")[0][:20]

        days_str = f"あと{days}日" if days < 14 else f"あと{weeks}週"
        print(f"\n  📅 {r['date']}  {r['name']}")
        print(f"     {rtype} {dist}" + (f"  📍{loc}" if loc else ""))
        print(f"     {days_str}  [{PHASE_JP.get(phase,phase)}]")
        if goal_str:
            print(f"     🎯 {goal_str}")

        # ── スプリットタイム（前回実績）────────────────────
        splits = r.get("splits") or {}
        if splits:
            parts = []
            for key, emoji in [("swim","🏊"),("t1","→"),("bike","🚴"),("t2","→"),("run","🏃")]:
                if key in splits:
                    parts.append(f"{emoji}{splits[key]['str']}")
            if parts:
                total = splits.get("total",{}).get("str","")
                total_str = f"  合計:{total}" if total else ""
                src = r.get("splits_source","")
                src_label = ""
                if src and src != "calendar_text":
                    fname = Path(src).name if "/" in src or "\\" in src else src
                    ext   = Path(fname).suffix.upper().lstrip(".")
                    src_label = f"  [{ext}]"
                print(f"     📊 前回スプリット: {' '.join(parts)}{total_str}{src_label}")
            # 目標スプリット（-3%）
            if r.get("past_time_s") and splits:
                goal_splits = []
                for key, emoji in [("swim","🏊"),("bike","🚴"),("run","🏃")]:
                    if key in splits and splits[key]["sec"] > 0:
                        gs = int(splits[key]["sec"] * 0.97)
                        goal_splits.append(f"{emoji}{_fmt_time(gs)}")
                if goal_splits:
                    print(f"     🎯 目標スプリット: {' '.join(goal_splits)}")

        # ── 天気・気温情報 ────────────────────────────────
        wx = r.get("weather")
        if wx and not wx.get("error"):
            tmax = wx.get("temp_max","?")
            tmin = wx.get("temp_min","?")
            precip = wx.get("precip",0)
            wind   = wx.get("wind","?")
            weather_icon = "🌧" if precip and float(precip) > 1 else "☀️"
            src = wx.get("source","")
            print(f"     {weather_icon} 気象({src}): {tmin}〜{tmax}℃  "
                  f"降水{precip}mm  風{wind}km/h  {wx.get('weather','')}")
        elif wx and wx.get("error"):
            print(f"     🌡 気象: 取得失敗 ({wx['venue']})")
        else:
            coords = extract_venue_coords(r.get("location",""))
            if not coords:
                print(f"     🌡 気象: 会場未登録（場所情報を追加すると取得可能）")

        if r.get("attachments"):
            for att in r["attachments"]:
                print(f"     📎 {att}")

    if past_races:
        print(f"\n  【完了レース】")
        for r in past_races[:3]:
            res = f"  結果:{r['past_time_str']}" if r.get("past_time_str") else ""
            print(f"  ✅ {r['date']}  {r['name']}{res}")


# ============================================================
# サマリ2: ピリオダイゼーション
# ============================================================
def print_periodization_summary(races):
    today = date.today()
    upcoming = sorted([r for r in races if date.fromisoformat(r["date"]) >= today],
                      key=lambda r: r["date"])
    if not upcoming:
        print(f"\n  ピリオダイゼーション: 登録レースなし — ベース期継続")
        return

    final = upcoming[-1]
    final_date  = date.fromisoformat(final["date"])
    total_weeks = max(1, (final_date - today).days // 7)

    print(f"\n{'═'*64}")
    print(f"  📊 ピリオダイゼーション計画")
    print(f"{'═'*64}")
    print(f"  今日: {today}  →  最終レース: {final['date']}  (計{total_weeks}週)")
    print()

    # フェーズブロックを構築
    phase_blocks = []
    for r in upcoming:
        rd    = date.fromisoformat(r["date"])
        weeks = (rd - today).days // 7
        ph    = _phase_from_weeks(weeks)
        phase_blocks.append((r["name"], r["date"], ph, weeks))

    # 今日〜最終レースまでのフェーズを週単位で集約
    segments = []
    cur = today
    for r in upcoming:
        rd = date.fromisoformat(r["date"])
        while cur < rd:
            wl = (rd - cur).days // 7
            ph = _phase_from_weeks(wl)
            if not segments or segments[-1][0] != ph:
                segments.append([ph, 0])
            segments[-1][1] += 1
            cur += timedelta(weeks=1)
        # レース日のみ、直前セグメントがtaperかrace_weekならレースマーカー追加
        if not segments or segments[-1][0] != "race":
            segments.append(["race", 1])
        else:
            segments[-1][1] += 1
        cur = rd + timedelta(days=1)

    # タイムラインバー表示（幅=週数×2、最低3）
    BAR_CH  = {"base":"░","build":"▒","peak":"▓","taper":"╌",
               "race":"█","recovery":"·"}
    bar_line = "  "
    lbl_line = "  "
    ph_line  = "  "
    for ph, wks in segments:
        ch  = BAR_CH.get(ph, "·")
        col = max(wks * 2, 3)
        bar_line += ch * col
        lbl_line += f"{wks}w".center(col)
        jp = {"base":"基礎","build":"ビルド","peak":"ピーク",
              "taper":"テーパー","race":"RACE","recovery":"回復"}.get(ph, ph)
        ph_line  += f"{jp}".center(col)

    print(f"{ph_line}")
    print(f"{bar_line}")
    print(f"{lbl_line}")
    print()

    # フェーズ目標一覧
    PHASE_GOALS = {
        "base":     "有酸素基盤構築・ロング耐性・週間ボリューム確立",
        "build":    "スイートスポット・テンポラン・種目移行練習",
        "peak":     "レースペース練習・インターバル・最大強度セッション",
        "taper":    "強度維持・量削減・疲労抜き・グリコーゲン蓄積",
        "race":     "軽めの刺激入れ・メンタル準備・体を整える",
        "recovery": "完全回復・次サイクルの計画立案",
    }
    shown = set()
    print("  【フェーズ別目標】")
    for ph, wks in segments:
        if ph in shown: continue
        shown.add(ph)
        jp   = PHASE_JP.get(ph, {"race":"レース週","recovery":"回復期"}.get(ph,ph))
        goal = PHASE_GOALS.get(ph,"")
        print(f"  {jp:8s}({wks:2d}週): {goal}")

    # 各レースまでの距離
    if len(upcoming) > 1:
        print()
        print("  【レースまでの距離】")
        for r in upcoming:
            rd    = date.fromisoformat(r["date"])
            weeks = (rd - today).days // 7
            ph    = _phase_from_weeks(weeks)
            gt_str = ""
            if r.get("past_time_s"):
                goal_s = int(r["past_time_s"] * 0.97)
                gt_str = f"  目標:{_fmt_time(goal_s)}"
            elif r.get("rival_time_s"):
                gt_str = f"  目標:{r['rival_time_str']}"
            print(f"  {r['date']}  {r['name'][:22]:22s}  残り{weeks:2d}週  "
                  f"[{PHASE_JP.get(ph,ph)}]{gt_str}")


# ============================================================
# サマリ3: 今週の仕事スケジュール
# ============================================================
def print_work_schedule_summary(gcal_days, start_date, num_days=10):
    """
    GCalから認識した予定を num_days 分だけサマリ表示する。
    指定フォーマット:
      ═════ 🗓 GCalスケジュール確認 (03/10〜03/19 / 10日間) ════...
      2026-03-10(火) 90分/朝練可 ◀今日
        🏠 在宅 → 朝練可・+30分
      2026-03-14(土)【週末】 180分/朝練可  ★予約: 🏊スイム（BIG）(75分)
    """
    DOW_JP = ["月","火","水","木","金","土","日"]
    end_date = start_date + timedelta(days=num_days - 1)

    header = f"═════ 🗓 GCalスケジュール確認 ({start_date.strftime('%m/%d')}〜{end_date.strftime('%m/%d')} / {num_days}日間) "
    print(f"\n{header}{'═' * max(1, 80 - len(header))}")

    if not gcal_days:
        print("  ⚠️  カレンダー未取得 — デフォルト設定で生成")
        return

    for i in range(num_days):
        day      = start_date + timedelta(days=i)
        day_str  = day.strftime("%Y-%m-%d")
        dow      = DOW_JP[day.weekday()]
        is_wknd  = day.weekday() >= 5
        gcal     = gcal_days.get(day_str, {})
        notes    = gcal.get("gcal_notes", [])
        races_d  = gcal.get("races", [])
        forced   = gcal.get("forced_sessions", [])
        avail    = gcal.get("available_min", 120 if is_wknd else 60)
        morning  = gcal.get("morning_ok", not is_wknd)
        is_today = (day == date.today())

        today_str = " ◀今日" if is_today else ""
        avail_str = f"{avail}分" if avail > 0 else "練習なし"
        time_str  = "朝練可" if morning else "夜練"
        wknd_str  = "【週末】" if is_wknd else ""

        # ★予約 セッション
        forced_str = ""
        if forced:
            parts = []
            for fs in forced:
                icon = {"swim":"🏊","run":"🏃","bike":"🚴"}.get(fs["sport"], "📅")
                parts.append(f"{icon}{fs['name']}({fs['duration']}分)")
            forced_str = "  ★予約: " + " / ".join(parts)

        # レース
        for r in races_d:
            forced_str += f"  🏁{r.get('name','レース')}"

        # ヘッダ行（ユーザー指定フォーマット通り）
        print(f"  {day_str}({dow}){wknd_str} {avail_str}/{time_str}{today_str}{forced_str}")

        # 在宅/出社/出張等のノート（予約ノートを除外してインデント付きで表示）
        for n in notes:
            # "📅 予約SWIM/RUN/BIKE" 形式のノートは forced_str で表示済みなのでスキップ
            if "予約SWIM" not in n and "予約RUN" not in n and "予約BIKE" not in n:
                print(f"    {n}")


def main():
    parser = argparse.ArgumentParser(description="スマートトレーニングプラン v6")
    parser.add_argument("--preview",   action="store_true", help="アップロードせずに表示のみ")
    parser.add_argument("--days",      type=int, default=10, help="生成する日数 (デフォルト: 10日)")
    parser.add_argument("--weeks",     type=int, default=None, help="週数指定 (--days の代わり)")
    parser.add_argument("--start",     type=str, default=None, help="開始日 YYYY-MM-DD")
    parser.add_argument("--today",     action="store_true", help="今日の計画のみ表示")
    parser.add_argument("--no-upload", action="store_true", help="Intervals.icuへのアップロードをスキップ")
    parser.add_argument("--server",    action="store_true", help="HTMLチャットUIサーバーを起動 (localhost:8765)")
    parser.add_argument("--port",      type=int, default=8765, help="サーバーポート (デフォルト: 8765)")
    parser.add_argument("--diagnose",  action="store_true", help="Garminヘルス診断のみ実行して終了")
    parser.add_argument("--no-garmin", action="store_true", help="Garmin診断をスキップ")
    args = parser.parse_args()

    # ── サーバーモード ──────────────────────────────────────────
    if args.server:
        run_chat_server(args.port)
        return

    # --weeks 指定時は日数に変換
    num_days = args.weeks * 7 if args.weeks else args.days

    print("="*64)
    print("  🏊🚴🏃 コーチ AIトレーニングプラン v11 (Garmin診断統合版)")
    print("="*64)

    cfg      = load_config()
    athlete  = fetch_athlete_data(cfg)
    hrv_cfg  = cfg.get("hrv_scoring", {})
    cond     = calc_hrv_score(athlete, hrv_cfg)
    str_prog = calc_strength_progression(cfg)
    cfg_cal  = cfg.get("google_calendar", {})

    # ── Garmin ヘルス診断 (v11新機能) ────────────────────────
    if not getattr(args, "no_garmin", False):
        garmin_diag = _run_garmin_diagnosis(cfg, athlete.get("weight", 68.4))
        if garmin_diag:
            cond = _merge_garmin_into_cond(cond, garmin_diag)
            print("  ✅ Garmin診断完了 → コンディションスコアに統合")
    else:
        print("  ℹ️  --no-garmin: Garmin診断スキップ")

    # --diagnose モード: 診断のみ表示して終了
    if getattr(args, "diagnose", False):
        if cond.get("garmin_diag"):
            print_diagnosis(cond["garmin_diag"])
        else:
            print("  ❌ Garmin診断データなし (garminconnect未インストール or 認証失敗)")
        return

    print(f"\n  🧠 HRVスコア: {cond['score']}/10 → {cond['condition'].upper()}")
    for r in cond.get("reasons",[]): print(f"     ⚠️  {r}")
    if str_prog["weeks_to_goal"]:
        print(f"  💪 筋肉量目標: {str_prog['goal_muscle_kg']}kg "
              f"残り{str_prog['weeks_to_goal']}週 Lv:{str_prog['level']}")

    deficient = detect_deficient_sports(athlete.get("weekly_counts",{}), cfg)
    if deficient:
        print(f"  📊 直近2週の不足種目: {' / '.join(deficient)}")

    if athlete.get("past_results"):
        print(f"\n  📈 直近レース結果:")
        for r in athlete["past_results"][:3]:
            print(f"     {r['date']}  {r['name'][:22]:22s}  {r['time_str']}")

    RAW_GCAL_EVENTS = _fetch_gcal_events_auto()
    print("\n  📅 Googleカレンダー解析中...")
    gcal_days, races_from_cal = parse_gcal_events_to_days(RAW_GCAL_EVENTS, cfg_cal, athlete, cfg)
    gcal_days = apply_trip_adjacency(gcal_days)
    print(f"  ✅ イベント解析完了: {len(RAW_GCAL_EVENTS)}件  レース:{len(races_from_cal)}件")

    start = date.today() + timedelta(days=1)
    if args.start: start = date.fromisoformat(args.start)

    if args.today:
        print_race_schedule_summary(races_from_cal, athlete, cfg)
        # Garmin診断サマリー表示
        _print_garmin_summary_block(cond)
        ri   = get_race_phase(races_from_cal, date.today())
        gt   = calc_goal_targets(ri, athlete, cfg)
        _inject_cal_rival(gt, ri, races_from_cal, athlete)
        plan = generate_days(cfg, athlete, cond, ri, gcal_days, str_prog,
                             date.today(), gt, num_days=1)
        # Garmin modifier 適用
        if cond.get("garmin_diag") and cond.get("garmin_modifier",0) != 0:
            plan = apply_garmin_modifier(plan, cond["garmin_modifier"], cond["garmin_diag"])
        print(f"\n{'─'*64}")
        print(f"  【当日朝モード】{date.today().isoformat()}  [{ri['phase'].upper()}]")
        print_plan(plan, ri, cond, athlete, gt, cfg, today_mode=True, str_prog=str_prog, gcal_days=gcal_days, num_days=num_days)
        print("="*64)
        return

    # ══════════════════════════════════════════════════
    # 対話モード: まずコーチとチャットしてからプラン生成
    # ══════════════════════════════════════════════════
    user_ctx = _cli_chat_session(athlete, cond, races_from_cal, num_days)

    # ユーザーの申告コンディションを反映
    cond = _apply_feeling_to_cond(cond, user_ctx.get("feeling",""))

    # 強度オーバーライド
    force_intensity = user_ctx.get("force_intensity")

    # スイム/リクエストを gcal_days に反映
    gcal_days = _apply_requests_to_gcal(gcal_days, user_ctx.get("requests", []))

    while True:
        ri   = get_race_phase(races_from_cal, start)
        gt   = calc_goal_targets(ri, athlete, cfg)
        _inject_cal_rival(gt, ri, races_from_cal, athlete)
        plan = generate_days(cfg, athlete, cond, ri, gcal_days, str_prog,
                             start, gt, num_days=num_days)

        # Garmin modifier 適用 (force_intensityがない場合のみ)
        if not force_intensity and cond.get("garmin_diag") and cond.get("garmin_modifier",0) != 0:
            plan = apply_garmin_modifier(plan, cond["garmin_modifier"], cond["garmin_diag"])

        # 強度上書き (ユーザー申告が最優先)
        if force_intensity:
            plan = _override_intensity(plan, force_intensity)

        end_date = (start + timedelta(days=num_days - 1)).strftime("%Y-%m-%d")
        print(f"\n{'═'*64}")
        print(f"  📋 {num_days}日間プラン: {start.strftime('%Y-%m-%d')} 〜 {end_date}")
        print(f"     [{ri['phase'].upper()}フェーズ]")
        _print_garmin_summary_block(cond)
        print_plan(plan, ri, cond, athlete, gt, cfg, str_prog=str_prog, gcal_days=gcal_days, num_days=num_days)
        print_calorie_summary(plan, cfg)
        print(f"{'═'*64}")

        # ── アップロード or 再構築 確認 ──────────────────────────
        print("\n  このプランをどうしますか？")
        print("  [1] Intervals.icu にアップロード")
        print("  [2] 修正リクエストを追加して再構築")
        print("  [3] このままキャンセル（アップロードしない）")
        print()
        choice = input("  選択 (1/2/3): ").strip()

        if choice == "1":
            if not args.no_upload:
                print(f"\n{'─'*64}")
                n = upload_plan(plan, cfg, dry_run=args.preview)
                total = len([p for p in plan if p["sport"] not in ("rest","race")])
                print(f"\n  {'プレビュー' if args.preview else 'アップロード'}完了: {n}/{total}件")
            break

        elif choice == "2":
            print("\n  修正リクエストを入力してください（例: 木曜はスイムに変えて / 週末は強度を上げて）")
            print("  ※複数入力可。空行で確定。\n")
            extra_requests = []
            while True:
                line = input("  > ").strip()
                if not line:
                    break
                extra_requests.append(line)
            if extra_requests:
                user_ctx["requests"] = user_ctx.get("requests", []) + extra_requests
                gcal_days = _apply_requests_to_gcal(gcal_days, extra_requests)
                # 強度リクエストも再チェック
                for req in extra_requests:
                    rl = req.lower()
                    if any(k in rl for k in ["強度高","高強度","きつめ","ハード","本番強度","疲労高くても","練習会"]):
                        force_intensity = "high"
                    elif any(k in rl for k in ["軽め","回復","楽に","ゆっくり"]):
                        force_intensity = "low"
            print("\n  ✅ 再構築中...\n")

        else:
            print("\n  ⛔ キャンセルしました。アップロードは行いません。")
            break

    print("="*64)


# ============================================================
# 対話ユーティリティ
# ============================================================

def _cli_chat_session(athlete, cond, races, num_days):
    """
    CLI での対話セッション。
    コンディション、特別リクエストを対話形式で収集し user_ctx を返す。
    """
    DIVIDER = "─" * 64

    phase_jp = {"base":"ベース期","build":"ビルド期","peak":"ピーク期",
                "taper":"テーパー期","race_week":"レース週","recovery":"回復期"}
    ri = get_race_phase(races, date.today())

    print(f"\n{DIVIDER}")
    print(f"  👋 コーチAIです！トレーニングプランを一緒に作りましょう。")
    print(DIVIDER)

    # コンディション状況を表示
    icons = {"peak":"🔥","good":"✅","normal":"😐","fatigued":"😓","depleted":"🛑"}
    cond_icon = icons.get(cond["condition"], "")
    print(f"\n  現在のコンディション: {cond_icon} {cond['condition'].upper()}"
          f"  (HRV:{athlete.get('hrv',0):.0f} Form:{athlete.get('form',0):.1f})")

    race = ri.get("race")
    if race:
        prio = race.get("priority","B")
        print(f"  次のレース: {'🅐 本命' if prio=='A' else '🅑 練習'}レース "
              f"{race['name']} ({race['date']}) 残り{ri['weeks_to_race']}週 "
              f"[{phase_jp.get(ri['phase'], ri['phase'])}]")

    print()

    user_ctx = {"requests": [], "force_intensity": None}

    # ── Q1: 今日の調子 ─────────────────────────────────────────
    print(f"  💬 今日の調子はどうですか？")
    print(f"     1) 絶好調💪  2) 普通😊  3) 疲れ気味😓  4) ボロボロ🛑")
    print(f"     または自由に入力（例: 脚が重い、よく眠れた など）\n")
    ans = input("  > ").strip()

    feeling_map = {
        "1":"絶好調","絶好調":"絶好調","最高":"絶好調","元気":"絶好調",
        "2":"普通","普通":"普通","まあまあ":"普通",
        "3":"疲れ気味","疲れ":"疲れ気味","しんどい":"疲れ気味","きつい":"疲れ気味",
        "4":"ボロボロ","ボロボロ":"ボロボロ","最悪":"ボロボロ",
    }
    feeling = "普通"
    for k, v in feeling_map.items():
        if k in ans:
            feeling = v
            break
    user_ctx["feeling"] = feeling

    feeling_comment = {
        "絶好調": "🔥 いい調子ですね！その勢いを活かしたメニューにします。",
        "普通":   "😊 標準的なトレーニング強度で組みます。",
        "疲れ気味": "😓 疲労を考慮して無理のないプランにします。",
        "ボロボロ": "🛑 今週は回復最優先。無理は禁物です。",
    }
    print(f"\n  {feeling_comment.get(feeling, '')}")

    # ── Q2: 特別リクエスト ─────────────────────────────────────
    print(f"\n{DIVIDER}")
    print(f"  💬 何か特別なリクエストはありますか？")
    print(f"     例: 「土日の朝はスイム1.5時間入れて」")
    print(f"         「練習会が近いから強度高めで」")
    print(f"         「疲れてるから軽めに」")
    print(f"         「水曜は仕事が遅いから短めに」")
    print(f"     複数入力可。なければそのままEnter。\n")

    while True:
        req = input("  > ").strip()
        if not req:
            break
        user_ctx["requests"].append(req)
        rl = req.lower()

        # リアルタイムフィードバック
        _del_kws = ["削除","なし","休み","休養","cancel","remove","delete",
                    "なくして","取り消し","やめ","オフ","off","スキップ","skip"]
        _int_down_kws = ["強度下","強度を下","強度落","強度低","軽め","楽に","回復","ゆっくり","イージー","easy"]
        _int_up_kws   = ["強度上","強度を上","強度高","きつめ","ハード","強化","インターバル","閾値"]
        _change_pat   = re.search(r'(スイム|swim|バイク|bike|ラン|run|筋トレ|strength)(?:を|から|→)(スイム|swim|バイク|bike|ラン|run|筋トレ|strength)', req)
        if any(k in req for k in _del_kws):
            print(f"  🗑  その日のセッションをREST（削除）に変更します")
        elif _change_pat:
            from_jp = _change_pat.group(1)
            to_jp   = _change_pat.group(2)
            print(f"  🔄 種目変更: {from_jp} → {to_jp}（強度は同程度で設定）")
        elif any(k in req for k in _int_down_kws):
            print(f"  ⬇️  強度を1段階下げます（例: テンポ→イージー）")
        elif any(k in req for k in _int_up_kws):
            print(f"  ⬆️  強度を1段階上げます（例: テンポ→インターバル）")
        elif any(k in rl for k in ["スイム","swim"]):
            m = re.search(r'(\d+(?:\.\d+)?)\s*(?:時間|h)', rl)
            h = float(m.group(1)) if m else 1.5
            print(f"  ✅ スイム {int(h*60)}分 をメニューに追加します")
        if any(k in rl for k in ["強度高","高強度","きつめ","ハード","練習会","本番強度","疲労高くても"]):
            user_ctx["force_intensity"] = "high"
            print(f"  🔥 強度優先モードに設定しました")
        elif any(k in rl for k in ["軽め","回復","楽に","ゆっくり","疲れ"]):
            user_ctx["force_intensity"] = "low"
            print(f"  🧘 回復重視モードに設定しました")

        print(f"  （他にあれば続けて入力、なければEnter）\n")

    # ── Q3: 日数確認 ─────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print(f"  💬 何日分のプランを作りますか？（デフォルト: {num_days}日）")
    print(f"     そのままEnterで {num_days}日、または数字を入力\n")
    days_input = input("  > ").strip()
    if days_input.isdigit():
        user_ctx["num_days"] = int(days_input)
    else:
        user_ctx["num_days"] = num_days

    print(f"\n  ✅ {user_ctx['num_days']}日間プランを生成します...\n")
    return user_ctx


def _apply_feeling_to_cond(cond, feeling):
    """ユーザーの申告コンディションを HRV スコアに反映する"""
    cond = dict(cond)
    delta_map = {"絶好調": +1.5, "普通": 0, "疲れ気味": -1.5, "ボロボロ": -3.0}
    cond_override_map = {"絶好調": "peak", "普通": "normal", "疲れ気味": "fatigued", "ボロボロ": "depleted"}
    if feeling in delta_map:
        cond["score"] = max(0, min(10, cond["score"] + delta_map[feeling]))
        cond["condition"] = cond_override_map[feeling]
        cond.setdefault("reasons", []).append(f"自己申告: {feeling}")
    return cond


def _apply_requests_to_gcal(gcal_days, requests):
    """
    ユーザーのリクエスト文字列を解析して gcal_days を上書きする。

    対応パターン:
      日付指定:  「3/14にスイム追加」「3月14日 スイム1時間」「14日はスイム」
      土日指定:  「土日朝はスイム1.5時間」「週末はスイム」
      種目変更:  「水曜をスイムに変えて」「木曜はラン」
      時間指定:  「スイム90分」「1.5時間スイム」
      セッション追加: 「〜を追加」「〜を入れて」
    """
    # gcal_days のキーは "YYYY-MM-DD" 形式
    # まず今年の月/日 → YYYY-MM-DD に変換するヘルパー
    today = date.today()

    def _resolve_date(month, day):
        """月/日 → 最も近い未来の date を返す"""
        for year in [today.year, today.year + 1]:
            try:
                d = date(year, int(month), int(day))
                if d >= today:
                    return d
            except ValueError:
                pass
        return None

    # gcal_days に存在しない日付でも追加できるよう、デフォルト構造を用意
    def _ensure_day(d_str):
        if d_str not in gcal_days:
            gcal_days[d_str] = {
                "available_min": 90,
                "gcal_notes": [],
                "notes": [],
                "races": [],
                "is_trip": False,
                "directives": [],
            }
        gcal_days[d_str].setdefault("gcal_notes", [])
        return gcal_days[d_str]

    SPORT_ALIASES = {
        "スイム": "swim", "swim": "swim", "泳ぎ": "swim", "水泳": "swim",
        "バイク": "bike", "bike": "bike", "自転車": "bike", "ライド": "bike", "乗り": "bike",
        "ラン":   "run",  "run":  "run",  "走り": "run",  "ジョグ": "run",
        "筋トレ": "strength", "strength": "strength", "ウェイト": "strength",
        "ヨガ":   "yoga", "yoga": "yoga", "ストレッチ": "stretch",
    }

    gcal_days = {k: dict(v) for k, v in gcal_days.items()}  # shallow copy

    for req in requests:
        if not req:
            continue
        orig = req
        rl   = req.lower()

        # ── 0. 削除／REST リクエストを最優先で処理 ─────────────────
        DELETE_KWS = ["削除","なし","休み","休養","cancel","remove","delete",
                      "なくして","取り消し","やめ","オフ","off","スキップ","skip"]
        is_delete = any(k in orig for k in DELETE_KWS)

        if is_delete:
            # 対象日を特定（種目は不要）
            del_dates = []
            today_d = date.today()

            # 「3/13削除」「3月13日削除」「13日削除」
            del_date_pats = [
                (r'(\d{1,2})[/／](\d{1,2})', 2),
                (r'(\d{1,2})月(\d{1,2})日',   2),
                (r'(?<!\d)(\d{1,2})日(?!\d)',   1),
            ]
            for pat, ngrp in del_date_pats:
                for m in re.finditer(pat, orig):
                    if ngrp == 2:
                        month, day = m.group(1), m.group(2)
                    else:
                        month, day = str(today_d.month), m.group(1)
                    d = _resolve_date(month, day)
                    if d:
                        del_dates.append(d.strftime("%Y-%m-%d"))

            # 曜日指定「水曜削除」
            DOW_MAP2 = {"月曜":0,"火曜":1,"水曜":2,"木曜":3,"金曜":4,"土曜":5,"日曜":6,
                        "月曜日":0,"火曜日":1,"水曜日":2,"木曜日":3,"金曜日":4,"土曜日":5,"日曜日":6}
            for dow_name, dow_idx in DOW_MAP2.items():
                if dow_name in orig:
                    for i in range(14):
                        d = today_d + timedelta(days=i+1)
                        if d.weekday() == dow_idx:
                            del_dates.append(d.strftime("%Y-%m-%d"))

            if not del_dates:
                print(f"  ⚠️  削除対象日が特定できませんでした: 「{orig}」")
                continue

            del_dates = list(dict.fromkeys(del_dates))  # 重複除去（順序保持）
            for d_str in del_dates:
                d_info = _ensure_day(d_str)
                d_info["available_min"]  = 0    # generate_days が REST 扱いにする
                d_info["force_sport"]    = "rest"
                d_info["extra_sessions"] = []   # 追加セッションもクリア
                note = f"🗑 削除: REST日に変更"
                if note not in d_info["gcal_notes"]:
                    d_info["gcal_notes"].append(note)

            print(f"  🗑  リクエスト反映: 「{orig}」 → {len(del_dates)}日をREST（削除）")
            continue  # 削除処理終了 → 次のリクエストへ


        mins = None
        time_m = re.search(r'(\d+(?:\.\d+)?)\s*(?:時間|h(?:r|our)?s?)\b', rl)
        min_m  = re.search(r'(\d+)\s*(?:分|min)', rl)
        if time_m:
            mins = int(float(time_m.group(1)) * 60)
        elif min_m:
            mins = int(min_m.group(1))

        # ── 2. 強度変更リクエストを判定（種目なしでもOK） ────────────
        INTENSITY_DOWN_KWS = ["強度下","強度を下","強度落","強度低","軽め","楽に","回復","ゆっくり","イージー","easy"]
        INTENSITY_UP_KWS   = ["強度上","強度を上","強度高","きつめ","ハード","強化","インターバル","閾値"]
        is_intensity_down = any(k in orig for k in INTENSITY_DOWN_KWS)
        is_intensity_up   = any(k in orig for k in INTENSITY_UP_KWS)
        is_intensity_change = is_intensity_down or is_intensity_up

        # ── 3. 種目を抽出（「AをBに変更」パターンに対応） ─────────────
        # 「AをBに」「AからBへ」「AをBに変更」パターン: 変更先(B)を sport にする
        sport_from = None  # 変更元（省略可）
        sport      = None  # 変更先 = force_sport

        # 変換パターン: 「スイムをランに変更」「バイクをスイムにして」など
        _SPORT_RE = r'(スイム|swim|水泳|バイク|bike|自転車|ライド|ラン|run|走り|ジョグ|筋トレ|strength|ウェイト|ヨガ|yoga)'
        change_pat = re.search(
            _SPORT_RE + r'(?:を|から|→)' + _SPORT_RE,
            orig, re.IGNORECASE
        )
        if change_pat:
            sport_from = SPORT_ALIASES.get(change_pat.group(1), change_pat.group(1).lower())
            sport      = SPORT_ALIASES.get(change_pat.group(2), change_pat.group(2).lower())
        else:
            for alias, sp in SPORT_ALIASES.items():
                if alias in orig or alias.lower() in rl:
                    sport = sp
                    break

        if not sport and not is_intensity_change:
            continue  # 種目も強度指定もなければスキップ

        # ── 3. 対象日を特定 ──────────────────────────────────────
        target_dates = []  # [(date_str, label)]

        # パターンA: 「3/14」「3月14日」「14日」
        date_patterns = [
            r'(\d{1,2})[/／](\d{1,2})',        # 3/14
            r'(\d{1,2})月(\d{1,2})日',           # 3月14日
            r'(?<!\d)(\d{1,2})日(?!\d)',          # 14日（月なし）
        ]
        found_specific_date = False
        for pat in date_patterns:
            for m in re.finditer(pat, orig):
                if len(m.groups()) == 2:
                    month, day = m.group(1), m.group(2)
                elif len(m.groups()) == 1:
                    month, day = str(today.month), m.group(1)
                else:
                    continue
                d = _resolve_date(month, day)
                if d:
                    target_dates.append((d.strftime("%Y-%m-%d"), f"{m.group()}"))
                    found_specific_date = True

        # パターンB: 「土日」「週末」「土曜」「日曜」
        if not found_specific_date and any(k in rl for k in ["土日","週末","土曜","日曜"]):
            for d_str in list(gcal_days.keys()):
                d = date.fromisoformat(d_str)
                if d.weekday() >= 5 and d >= today:
                    target_dates.append((d_str, "土日"))
            # gcal_days に未登録の土日も追加（今後14日分）
            for i in range(14):
                d = today + timedelta(days=i+1)
                if d.weekday() >= 5:
                    d_str = d.strftime("%Y-%m-%d")
                    if not any(t[0] == d_str for t in target_dates):
                        target_dates.append((d_str, "土日"))

        # パターンC: 「毎日」「全部」「全て」「毎朝」
        if not target_dates and any(k in rl for k in ["毎日","全部","全て","毎朝","every"]):
            for d_str in list(gcal_days.keys()):
                if date.fromisoformat(d_str) >= today:
                    target_dates.append((d_str, "毎日"))

        # パターンD: 曜日指定「水曜」「木曜」など
        DOW_MAP = {"月曜":0,"火曜":1,"水曜":2,"木曜":3,"金曜":4,"土曜":5,"日曜":6,
                   "月曜日":0,"火曜日":1,"水曜日":2,"木曜日":3,"金曜日":4,"土曜日":5,"日曜日":6}
        for dow_name, dow_idx in DOW_MAP.items():
            if dow_name in orig:
                for i in range(14):
                    d = today + timedelta(days=i+1)
                    if d.weekday() == dow_idx:
                        d_str = d.strftime("%Y-%m-%d")
                        if not any(t[0] == d_str for t in target_dates):
                            target_dates.append((d_str, dow_name))

        if not target_dates:
            # 日付指定なし = 全期間の同種目スロットに適用
            for d_str in list(gcal_days.keys()):
                if date.fromisoformat(d_str) >= today:
                    target_dates.append((d_str, "全日"))

        # ── 4. 対象日に force_sport / force_min / force_intensity を設定 ─
        is_add  = any(k in orig for k in ["追加","入れて","加えて","増やして","add"])
        is_morning = any(k in rl for k in ["朝","morning","am","午前"])
        default_mins = {"swim": 90, "bike": 90, "run": 60, "strength": 40, "yoga": 30}.get(sport or "run", 60)
        actual_mins = mins or default_mins

        # 強度変更の方向を決定
        intensity_shift = None
        if is_intensity_down:
            intensity_shift = "down"
        elif is_intensity_up:
            intensity_shift = "up"

        for d_str, label in target_dates:
            d_info = _ensure_day(d_str)

            sport_jp = {"swim":"🏊 スイム","bike":"🚴 バイク","run":"🏃 ラン",
                        "strength":"💪 筋トレ","yoga":"🧘 ヨガ","stretch":"🤸 ストレッチ"}.get(sport or "", sport or "")
            time_label = f"{actual_mins}分" + ("（朝）" if is_morning else "")

            if intensity_shift and not is_add:
                # ── 強度変更: 種目はそのまま、強度だけシフト ──
                d_info["intensity_shift"] = intensity_shift
                # 種目も同時に指定されていればforce_sportも設定
                if sport:
                    d_info["force_sport"] = sport
                    d_info["force_min"]   = actual_mins
                shift_jp = "⬇️ 強度DOWN" if intensity_shift == "down" else "⬆️ 強度UP"
                note = f"📝 {shift_jp}" + (f": {sport_jp}" if sport else "")
            elif is_add:
                # ── 追加: 既存セッションを残して extra_sessions に積む ──
                d_info.setdefault("extra_sessions", [])
                already = any(
                    e["sport"] == sport and e["mins"] == actual_mins
                    for e in d_info["extra_sessions"]
                )
                if not already:
                    d_info["extra_sessions"].append({
                        "sport": sport,
                        "mins":  actual_mins,
                        "note":  f"➕ 追加: {sport_jp} {time_label}",
                    })
                note = f"➕ 追加: {sport_jp} {time_label}"
            else:
                # ── 種目変更: 既存スロットを上書き ──
                d_info["force_sport"] = sport
                d_info["force_min"]   = actual_mins
                # 変更元種目を記録（強度引き継ぎ用）
                if sport_from:
                    d_info["sport_from"] = sport_from
                note = f"📝 変更: {sport_jp} {time_label}"

            if note not in d_info["gcal_notes"]:
                d_info["gcal_notes"].append(note)

        if intensity_shift:
            shift_jp = "⬇️ 強度DOWN" if intensity_shift == "down" else "⬆️ 強度UP"
            print(f"  {shift_jp} リクエスト反映: 「{orig}」× {len(target_dates)}日")
        else:
            action = "追加" if is_add else "変更"
            print(f"  📝 リクエスト反映: 「{orig}」 → {sport} {actual_mins}分 × {len(target_dates)}日 [{action}]")

    return gcal_days



def _override_intensity(plan, force_intensity):
    """生成済みプランの強度を上書きする"""
    result = []
    for item in plan:
        item = dict(item)
        if item["sport"] in ("run","bike","swim","brick"):
            if force_intensity == "high":
                item["description"] = "🔥 【強度UP指示あり】\n" + item.get("description","")
                if "イージー" in item["name"]:
                    item["name"] = item["name"].replace("イージー","テンポ/閾値")
            elif force_intensity == "low":
                item["description"] = "🧘 【回復重視指示あり】\n" + item.get("description","")
                for kw in ["インターバル","閾値","テンポ","ハード"]:
                    if kw in item["name"]:
                        item["name"] = item["name"].replace(kw,"イージー")
        result.append(item)
    return result


# ============================================================
# HTTP チャットUIサーバー
# ============================================================

def run_chat_server(port=8765):
    """
    HTMLチャットUIのためのローカルHTTPサーバーを起動する。

    エンドポイント:
      GET  /               → coach_chat.html を返す
      GET  /api/status     → アスリート情報・コンディション・レース一覧
      POST /api/chat       → 対話1ターン処理 { message, history, context }
      POST /api/plan       → プラン生成      { context }
      POST /api/upload     → ICUアップロード { plan }
    """
    import http.server
    import socketserver
    import webbrowser
    import threading

    # サーバー起動時に一度データ取得
    print(f"\n{'='*64}")
    print(f"  🌐 コーチAI チャットサーバー起動")
    print(f"{'='*64}")
    print(f"  URL: http://localhost:{port}")
    print(f"  ⛔ 停止: Ctrl+C\n")

    _state = {"plan": None, "context": {}}

    # ブラウザは起動確認後に開く（下の _open_verified で制御）

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args): pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def do_OPTIONS(self):
            self.send_response(200); self._cors(); self.end_headers()

        def _read_json(self):
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n)) if n else {}

        def _send_json(self, data, code=200):
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                # coach_chat.html をスクリプトと同じフォルダから探す
                # coach_chat.html を複数パスから探す
                _html_candidates = [
                    Path(__file__).parent / "coach_chat.html",
                    Path(__file__).parent / "Coach_chat.html",
                    Path.cwd() / "coach_chat.html",
                ]
                html_path = next((p for p in _html_candidates if p.exists()), None)
                if html_path:
                    body = html_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", len(body))
                    self._cors(); self.end_headers()
                    self.wfile.write(body)
                else:
                    # HTMLが見つからない場合はシンプルな案内ページを返す
                    html_fallback = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Coach AI</title></head><body style="font-family:sans-serif;padding:2em">
<h2>コーチAIサーバー起動中</h2>
<p>coach_chat.html が見つかりません。<br>
smart_plan_v7.py と同じフォルダに coach_chat.html を配置してください。</p>
<p><a href="/api/status">/api/status</a> &mdash; ステータス確認</p>
</body></html>""".encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", len(html_fallback))
                    self._cors(); self.end_headers()
                    self.wfile.write(html_fallback)

            elif self.path == "/api/status":
                try:
                    cfg      = load_config()
                    athlete  = fetch_athlete_data(cfg)
                    cond     = calc_hrv_score(athlete, cfg.get("hrv_scoring",{}))
                    str_prog = calc_strength_progression(cfg)
                    RAW      = _fetch_gcal_events_auto()
                    _, races = parse_gcal_events_to_days(
                        RAW, cfg.get("google_calendar",{}), athlete, cfg)
                    self._send_json({
                        "ok": True,
                        "athlete": {
                            "weight_kg":  athlete.get("weight_kg"),
                            "ftp":        athlete.get("ftp"),
                            "ctl":        round(athlete.get("ctl",0),1),
                            "atl":        round(athlete.get("atl",0),1),
                            "form":       round(athlete.get("form",0),1),
                            "hrv":        round(athlete.get("hrv",0),1),
                        },
                        "cond": {
                            "condition": cond["condition"],
                            "score":     cond["score"],
                            "reasons":   cond.get("reasons",[]),
                        },
                        "str_prog": str_prog,
                        "races": [{"name":r["name"],"date":r["date"],
                                   "priority":r.get("priority","B"),
                                   "past_time_str":r.get("past_time_str","")}
                                  for r in races],
                    })
                except Exception as e:
                    import traceback
                    self._send_json({"ok":False,"error":str(e),
                                     "trace":traceback.format_exc()}, 500)
            else:
                self.send_response(404); self.end_headers()

        def do_POST(self):
            data = self._read_json()

            # POST /api/chat
            if self.path == "/api/chat":
                msg     = data.get("message","")
                history = data.get("history",[])
                ctx     = data.get("context", _state["context"])
                reply, ctx = _server_chat_turn(msg, history, ctx)
                _state["context"] = ctx
                self._send_json({
                    "ok":    True,
                    "reply": reply,
                    "context": ctx,
                    "ready": ctx.get("ready_to_generate", False),
                })

            # POST /api/plan
            elif self.path == "/api/plan":
                ctx = data.get("context", _state["context"])
                try:
                    result = _generate_plan_from_context(ctx)
                    _state["plan"] = result["plan"]
                    _state["context"] = ctx
                    self._send_json({"ok": True, **_plan_to_api_json(result)})
                except Exception as e:
                    import traceback
                    self._send_json({"ok":False,"error":str(e),
                                     "trace":traceback.format_exc()}, 500)

            # POST /api/upload
            elif self.path == "/api/upload":
                plan = data.get("plan") or _state.get("plan")
                if not plan:
                    self._send_json({"ok":False,"error":"plan が空です"}, 400)
                    return
                try:
                    cfg = load_config()
                    n   = upload_plan(plan, cfg, dry_run=False)
                    self._send_json({"ok":True,"uploaded":n})
                except Exception as e:
                    self._send_json({"ok":False,"error":str(e)}, 500)
            else:
                self.send_response(404); self.end_headers()

    socketserver.TCPServer.allow_reuse_address = True

    # ポートが使用中なら +1 〜 +9 を自動試行
    _httpd = None
    _actual_port = port
    for _try_port in range(port, port + 10):
        try:
            _httpd = socketserver.TCPServer(("", _try_port), Handler)
            _actual_port = _try_port
            break
        except OSError:
            print(f"  ⚠️  ポート {_try_port} 使用中 → 次を試みます")

    if _httpd is None:
        print(f"  ❌ ポート {port}〜{port+9} 全て使用中。--port で別ポートを指定してください")
        return

    if _actual_port != port:
        print(f"  ℹ️  ポート変更: {port} → {_actual_port}")

    # 起動確認後にブラウザを開く
    def _open_verified():
        import time, urllib.request as _ur
        for _ in range(30):
            time.sleep(0.3)
            try:
                _ur.urlopen(f"http://localhost:{_actual_port}/api/status", timeout=1)
                print(f"  ✅ サーバー起動確認 → ブラウザを開きます http://localhost:{_actual_port}")
                webbrowser.open(f"http://localhost:{_actual_port}")
                return
            except: pass
        print(f"  ⚠️  サーバー応答なし。手動で開いてください: http://localhost:{_actual_port}")
    threading.Thread(target=_open_verified, daemon=True).start()

    with _httpd:
        try:
            _httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n  ⛔ サーバー停止")


def _server_chat_turn(message, history, ctx):
    """
    サーバーモードの1ターン対話処理。
    コンテキストを更新してコーチの返答文字列を返す。
    """
    ml = message.lower()
    parts = []
    ctx = dict(ctx)

    # ── コンディション ──────────────────────────────────────────
    for kws, feeling in [
        (["絶好調","最高","バリバリ","元気満々"],"絶好調"),
        (["普通","まあまあ","そこそこ"],"普通"),
        (["疲れ","しんどい","きつい","だるい","重い"],"疲れ気味"),
        (["ボロボロ","最悪","動けない"],"ボロボロ"),
    ]:
        if any(k in ml for k in kws):
            ctx["feeling"] = feeling
            break
    # 数字1〜4での回答
    if message.strip() in ("1","１"): ctx["feeling"] = "絶好調"
    elif message.strip() in ("2","２"): ctx["feeling"] = "普通"
    elif message.strip() in ("3","３"): ctx["feeling"] = "疲れ気味"
    elif message.strip() in ("4","４"): ctx["feeling"] = "ボロボロ"

    # ── 強度リクエスト ──────────────────────────────────────────
    if any(k in ml for k in ["強度高","高強度","きつめ","ハード","本番強度","練習会","疲労高くても","無理して"]):
        ctx["force_intensity"] = "high"
        parts.append("🔥 了解！強度優先で組みます。疲労が出ても本番に向けて頑張りましょう。")
    elif any(k in ml for k in ["軽め","回復","楽に","ゆっくり","やさしく"]):
        ctx["force_intensity"] = "low"
        parts.append("🧘 回復重視で無理なく組みます。")

    # ── スイムリクエスト ──────────────────────────────────────────
    swim_m = re.search(r'(\d+(?:\.\d+)?)\s*(?:時間|h)', ml)
    if swim_m and any(k in ml for k in ["スイム","swim"]):
        h = float(swim_m.group(1))
        ctx.setdefault("requests",[])
        ctx["requests"].append(message)
        weekend = any(k in ml for k in ["土日","週末","土曜","日曜"])
        parts.append(f"🏊 スイム{int(h*60)}分を{'土日朝' if weekend else '毎日'}に組み込みます。")

    # ── 日数指定 ──────────────────────────────────────────────
    days_m = re.search(r'(\d+)\s*日', ml)
    weeks_m = re.search(r'(\d+)\s*週', ml)
    if days_m and int(days_m.group(1)) <= 30:
        ctx["num_days"] = int(days_m.group(1))
        parts.append(f"📅 {ctx['num_days']}日間プランで作成します。")
    elif weeks_m:
        ctx["num_days"] = int(weeks_m.group(1)) * 7
        parts.append(f"📅 {ctx['num_days']}日間（{weeks_m.group(1)}週）プランで作成します。")

    # ── 自由リクエスト保存 ──────────────────────────────────────
    if message and not any(parts):
        ctx.setdefault("requests",[])
        ctx["requests"].append(message)

    # ── 生成トリガー ──────────────────────────────────────────
    gen_kws = ["作って","生成","プランを","計画を","出して","ok","オーケー",
               "よろしく","お願い","はい","いいよ","始めて","スタート"]
    if any(k in ml for k in gen_kws):
        ctx["ready_to_generate"] = True
        feeling = ctx.get("feeling","普通")
        reqs = ctx.get("requests",[])
        fi = ctx.get("force_intensity")
        summary = [f"体調: {feeling}"]
        if reqs: summary.append(f"リクエスト: {', '.join(reqs[:3])}")
        if fi: summary.append("強度: " + ("高め🔥" if fi=="high" else "低め🧘"))
        parts.append("✅ 設定内容:\n  " + "\n  ".join(summary) + "\n\nプランを生成します...")

    # ── まだコンディション未入力なら聞く ──────────────────────
    if not parts:
        if not ctx.get("feeling"):
            parts.append(
                "今日の調子を教えてください😊\n\n"
                "1️⃣ 絶好調💪\n2️⃣ 普通😊\n3️⃣ 疲れ気味😓\n4️⃣ ボロボロ🛑\n\n"
                "または自由に入力してもOKです（例:「脚が重い」「よく眠れた」）"
            )
        elif not ctx.get("requests") and not ctx.get("force_intensity"):
            parts.append(
                "ありがとうございます！特別なリクエストはありますか？\n\n"
                "例:\n"
                "・「土日の朝はスイム1.5時間入れて」\n"
                "・「練習会が近いから強度高めで」\n"
                "・「疲れてるから軽めにして」\n"
                "・「水曜は短めに」\n\n"
                "なければ「プランを作って」と言ってください 🏊🚴🏃"
            )
        else:
            parts.append("了解です！他に希望があれば教えてください。\nよろしければ「プランを作って」と言ってください 🏊🚴🏃")

    return "\n".join(parts), ctx


def _generate_plan_from_context(ctx):
    """サーバーAPI用プラン生成。user_context dict を受け取りプランデータを返す"""
    cfg      = load_config()
    athlete  = fetch_athlete_data(cfg)
    cond     = calc_hrv_score(athlete, cfg.get("hrv_scoring",{}))
    str_prog = calc_strength_progression(cfg)
    cfg_cal  = cfg.get("google_calendar",{})

    cond     = _apply_feeling_to_cond(cond, ctx.get("feeling",""))
    num_days = int(ctx.get("num_days", 10))
    start    = date.today() + timedelta(days=1)

    RAW_GCAL_EVENTS = _fetch_gcal_events_auto()
    gcal_days, races_from_cal = parse_gcal_events_to_days(RAW_GCAL_EVENTS, cfg_cal, athlete, cfg)
    gcal_days = apply_trip_adjacency(gcal_days)
    gcal_days = _apply_requests_to_gcal(gcal_days, ctx.get("requests",[]))

    ri = get_race_phase(races_from_cal, start)
    gt = calc_goal_targets(ri, athlete, cfg)
    _inject_cal_rival(gt, ri, races_from_cal, athlete)

    plan = generate_days(cfg, athlete, cond, ri, gcal_days, str_prog, start, gt, num_days=num_days)
    if ctx.get("force_intensity"):
        plan = _override_intensity(plan, ctx["force_intensity"])

    phase_jp = {"base":"ベース期","build":"ビルド期","peak":"ピーク期",
                "taper":"テーパー期","race_week":"レース週","recovery":"回復期"}
    race = ri.get("race")
    summary = [
        f"📋 {num_days}日間プラン [{phase_jp.get(ri['phase'],ri['phase'])}]",
        f"📅 {start.strftime('%Y/%m/%d')} 〜 {(start+timedelta(days=num_days-1)).strftime('%m/%d')}",
    ]
    if race:
        summary.append(f"🏁 {race.get('priority','B')}レース: {race['name']} 残り{ri['weeks_to_race']}週")
    active = [p for p in plan if p["sport"] not in ("rest","race")]
    summary.append(f"⏱ {sum(p['duration_min'] for p in active)/60:.1f}h / {len(active)}セッション")

    return {
        "plan": plan, "athlete": athlete, "cond": cond,
        "race_info": ri, "goal_targets": gt, "str_prog": str_prog,
        "races": races_from_cal, "summary_text": "\n".join(summary),
    }


def _plan_to_api_json(result):
    """プランデータをHTTP API向けJSONに変換"""
    plan = result["plan"]
    ri   = result["race_info"]
    cond = result["cond"]
    sp   = result["str_prog"]
    race = ri.get("race")

    items = []
    for item in plan:
        items.append({
            "date":         item["date"],
            "sport":        item["sport"],
            "name":         item["name"],
            "description":  item.get("description",""),
            "duration_min": item.get("duration_min",0),
            "intensity":    item.get("intensity",""),
            "gcal_notes":   item.get("gcal_notes",[]),
        })
    return {
        "plan":         items,
        "summary_text": result.get("summary_text",""),
        "cond":   {"condition":cond["condition"],"score":cond["score"],"reasons":cond.get("reasons",[])},
        "race":   {"name":race["name"],"date":race["date"],"priority":race.get("priority","B")} if race else None,
        "races":  [{"name":r["name"],"date":r["date"],"priority":r.get("priority","B")}
                   for r in result.get("races",[])],
        "str_prog": sp,
        "athlete": {"ctl":round(result["athlete"].get("ctl",0),1),
                    "form":round(result["athlete"].get("form",0),1),
                    "hrv":round(result["athlete"].get("hrv",0),1)},
    }


def _fetch_gcal_events_auto():
    """
    GoogleカレンダーAPIから今後6ヶ月のイベントを自動取得する。

    接続方法（優先順位）:
      1. 環境変数 GCAL_MCP_ENABLED=1 が設定されている場合 → MCP経由
      2. ~/.credentials/gcal_token.json が存在する場合 → OAuth直接
      3. 上記いずれも利用不可 → フォールバックリストを返す

    フォールバックリストは最後にGoogleカレンダーと同期した内容です。
    定期的に「python smart_plan_v7.py --sync-gcal」で更新してください。
    """
    import os

    # ── MCP / OAuth 経由の取得（将来の実装用スタブ）───────────────
    # MCP統合が有効な場合はここで gcal API を呼び出す
    # 現在は未接続のため常にフォールバックを使用
    if os.environ.get("GCAL_MCP_ENABLED") == "1":
        try:
            pass  # MCP gcal_list_events 呼び出しをここに実装
        except Exception as e:
            print(f"  ⚠️  Gcal MCP取得失敗: {e} → フォールバックを使用")

    # ── フォールバック（最終同期: 2026-03-11 ← Googleカレンダー MCP 自動取得）──
    # ※ このリストは claude.ai の Google Calendar MCP から取得した実データです。
    #   内容が変わった場合は「python smart_plan_v7.py --sync-gcal」で更新してください。
    return [
        # ── 出社/在宅 (3/11〜3/13) ──────────────────────────────────────
        {"summary":"出社","start":{"date":"2026-03-11"},"end":{"date":"2026-03-13"}},
        {"summary":"在宅","start":{"date":"2026-03-13"},"end":{"date":"2026-03-14"}},
        # ── スイム予約 3/14 (QUOX TRIATHLON CLUB, 6:45-8:00) ────────────
        {"summary":"スイム（BIG）",
         "description":"予約カテゴリ スケジュール\nスペース 4",
         "location":"QUOX TRIATHLON CLUB,  ",
         "start":{"dateTime":"2026-03-14T06:45:00+09:00","timeZone":"Asia/Tokyo"},
         "end":  {"dateTime":"2026-03-14T08:00:00+09:00","timeZone":"Asia/Tokyo"}},
        # ── 出張(3/18) / 出社(3/19) ─────────────────────────────────────
        {"summary":"出張","start":{"date":"2026-03-18"},"end":{"date":"2026-03-19"}},
        {"summary":"出社","start":{"date":"2026-03-19"},"end":{"date":"2026-03-20"}},
        # ── スイム予約 3/20, 3/21 (QUOX, 6:45-8:00) ────────────────────
        {"summary":"スイム（BIG）",
         "description":"予約カテゴリ スケジュール\nスペース 3",
         "location":"QUOX TRIATHLON CLUB,  ",
         "start":{"dateTime":"2026-03-20T06:45:00+09:00","timeZone":"Asia/Tokyo"},
         "end":  {"dateTime":"2026-03-20T08:00:00+09:00","timeZone":"Asia/Tokyo"}},
        {"summary":"スイム（BIG）",
         "description":"予約カテゴリ スケジュール\nスペース 4",
         "location":"QUOX TRIATHLON CLUB,  ",
         "start":{"dateTime":"2026-03-21T06:45:00+09:00","timeZone":"Asia/Tokyo"},
         "end":  {"dateTime":"2026-03-21T08:00:00+09:00","timeZone":"Asia/Tokyo"}},
        # ── スイム予約 3/28 (QUOX, スイム（GG）, 6:45-8:15) ────────────
        {"summary":"スイム（GG）",
         "description":"予約カテゴリ スケジュール\nスペース 3",
         "location":"QUOX TRIATHLON CLUB,  ",
         "start":{"dateTime":"2026-03-28T06:45:00+09:00","timeZone":"Asia/Tokyo"},
         "end":  {"dateTime":"2026-03-28T08:15:00+09:00","timeZone":"Asia/Tokyo"}},
        # ── STU練習会（3/29〜30、渡良瀬遊水池）──────────────────────────
        # Bike100km + Run20km の実戦形式練習会
        # コーチングコメント: 「本番に対応できるメニューにして」
        {"summary":"STU練習会",
         "description":"2025/3/30に参加　データはinverval.icuに登録済みを読み込んで\n\n100㎞バイク20㎞ランをするSTU練習会まで週末はできるだけ本番に対応できるメニューにして",
         "location":"渡良瀬遊水池, 日本、〒323-1104 栃木県栃木市藤岡町藤岡",
         "start":{"date":"2026-03-29"},"end":{"date":"2026-03-30"}},
        # ── ソウル出張（4/3〜5）────────────────────────────────────────────
        {"summary":"ソウル　朝ラン可能",
         "start":{"dateTime":"2026-04-03T15:00:00+09:00"},"end":{"dateTime":"2026-04-03T16:00:00+09:00"}},
        {"summary":"JL095東京（羽田） - ソウル（金浦）",
         "description":"出発\n東京（羽田） HND 18:55\n\n到着\nソウル（金浦） GMP 21:15",
         "start":{"dateTime":"2026-04-03T18:55:00+09:00"},"end":{"dateTime":"2026-04-03T21:15:00+09:00"}},
        {"summary":"ソウル　朝ラン可能",
         "start":{"date":"2026-04-05"},"end":{"date":"2026-04-06"}},
        {"summary":"JL092ソウル（金浦） - 東京（羽田）",
         "description":"出発\nソウル（金浦） GMP 12:05\n\n到着\n東京（羽田） HND 14:20",
         "start":{"dateTime":"2026-04-05T12:05:00+09:00"},"end":{"dateTime":"2026-04-05T14:20:00+09:00"}},
        # ── 横浜トライアスロン OD（5/17、優先度A）────────────────────────
        # 前回タイム: 2:23:56 / 添付: 横浜2025result_age_standard.pdf（Google Drive）
        {"summary":"横浜トライアスロン　OD",
         "description":"過去　 2:23:56\n\n横浜2025result_age_standard.pdf\n\n優先度A",
         "location":"山下公園, 日本、〒231-0023 神奈川県横浜市中区山下町２７９",
         "start":{"date":"2026-05-17"},"end":{"date":"2026-05-18"},
         "attachments":[{
             "fileUrl":"https://drive.google.com/open?id=11O_jv9Pf0CkD6DRfzRN7Ap-TBNyfht6g",
             "title":"横浜2025result_age_standard.pdf",
             "mimeType":"application/pdf",
             "fileId":"11O_jv9Pf0CkD6DRfzRN7Ap-TBNyfht6g"}]},
        # ── 渡良瀬トライアスロン OD（5/24、優先度B）──────────────────────
        # ライバル: 釈迦野 亮 No1037 2:26:04
        {"summary":"渡良瀬トライアスロン　OD",
         "description":"ライバル　No1037 釈迦野 亮 に追いつく　2:26:04\n2025渡良瀬ODリザルト.pdf\n\n優先度B",
         "location":"渡良瀬遊水池, 日本、〒323-1104 栃木県栃木市藤岡町藤岡",
         "start":{"date":"2026-05-24"},"end":{"date":"2026-05-25"}},
        # ── 奄美・徳之島遠征（7/3〜7）────────────────────────────────────
        {"summary":"フライト: JL 659、HND発ASJ行","description":"確認番号: DBJVVB",
         "location":"羽田空港",
         "start":{"dateTime":"2026-07-03T11:25:00+09:00"},"end":{"dateTime":"2026-07-03T13:30:00+09:00"}},
        {"summary":"フライト: JL 3843、ASJ発TKN行","description":"確認番号: DBJVVB",
         "location":"奄美空港",
         "start":{"dateTime":"2026-07-03T18:00:00+09:00"},"end":{"dateTime":"2026-07-03T18:30:00+09:00"}},
        # ── 徳之島トライアスロン ミドル（7/5、目標5:00:00）──────────────
        {"summary":"徳之島トライアスロン　ミドル",
         "description":"目標タイム　5:00:00",
         "location":"徳之島町\n鹿児島県大島郡",
         "start":{"date":"2026-07-05"},"end":{"date":"2026-07-06"}},
        {"summary":"フライト: JL 3842、TKN発ASJ行","description":"確認番号: DBJVVB",
         "location":"徳之島空港",
         "start":{"dateTime":"2026-07-07T09:40:00+09:00"},"end":{"dateTime":"2026-07-07T10:10:00+09:00"}},
        {"summary":"フライト: JL 658、ASJ発HND行","description":"確認番号: DBJVVB",
         "location":"奄美空港",
         "start":{"dateTime":"2026-07-07T14:15:00+09:00"},"end":{"dateTime":"2026-07-07T16:30:00+09:00"}},
    ]



def _inject_cal_rival(gt, race_info, races_from_cal, athlete):
    """カレンダーのライバル情報をゴール設定に注入する"""
    race = race_info.get("race") or {}
    race_name = race.get("name","")
    for r in races_from_cal:
        if r["name"] == race_name or race_name in r["name"]:
            if r.get("past_time_s") and not gt.get("best_result"):
                gt["goal_time_sec"] = int(r["past_time_s"] * 0.97)
                gt["goal_src"] = (f"カレンダー: {r['name']} 前回{r['past_time_str']} → -3%目標 "
                                  f"({_fmt_time(gt['goal_time_sec'])})")
            elif r.get("rival_time_s") and not gt.get("goal_time_sec"):
                gt["goal_time_sec"] = r["rival_time_s"]
                gt["goal_src"] = f"カレンダー: 目標ライバル {r.get('rival_name','')} {r['rival_time_str']}"
            if r.get("rival_name"):
                gt["rival_name"] = r["rival_name"]
            break


if __name__ == "__main__":
    main()
