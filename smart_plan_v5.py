"""
smart_plan_v5.py — トレーニングプラン生成 v5
=============================================
【v5 変更点】
  - 過去リザルト取得の強化:
      1) Intervals.icuのRACEカテゴリアクティビティから自動取得
      2) Googleカレンダーのレースイベント説明欄テキストからパース
      3) 説明欄に「レース結果から自動取得」と書いてあればレース名でICUを検索
      4) config.yaml の manual_results にも対応
  - ライバル設定: config.yaml の rivals セクション + カレンダー説明欄の「目標:」両方対応
  - 不足種目チェックをオプション化（detect_deficient_sports の use_balance フラグ）
  - 短時間セッション: 疲労度×フェーズの2軸で選択（不足種目は設定次第）
  - Googleカレンダー接続後に gcal_fetch_week() が呼ばれ説明欄・添付を自動解析

使い方:
  python smart_plan_v5.py --preview
  python smart_plan_v5.py --weeks 4
  python smart_plan_v5.py --today
"""

import yaml, json, base64, argparse, re, math
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, date
from pathlib import Path

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
    wellness= icu_get(f"{base}/wellness", api_key, {"oldest":ago30,"newest":today}) or []
    wellness= sorted(wellness, key=lambda x: x.get("id",""))
    acts    = icu_get(f"{base}/activities", api_key, {"oldest":ago7,"newest":today}) or []
    ftps    = [float(a.get("icu_rolling_ftp") or 0) for a in acts if a.get("icu_rolling_ftp")]
    ftp     = max(ftps) if ftps else 223.0
    pace_secs=[]
    for a in acts:
        if a.get("type") in ("Run","VirtualRun") and float(a.get("distance",0))>15000:
            dist=float(a.get("distance",1)); move=float(a.get("moving_time",1))
            if dist>0 and move>0: pace_secs.append(move/(dist/1000))
    tp_sec = min(pace_secs)*1.05 if pace_secs else 288.0

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
        return _default_athlete(weight, ftp, tp_sec, past_results, weekly_counts)
    latest   = wellness[-1]
    ctl      = float(latest.get("ctl") or 70.5)
    atl      = float(latest.get("atl") or 97.1)
    hrv      = float(latest.get("hrv") or 86.0)
    sleep_h  = float(latest.get("sleepSecs") or 0)/3600
    rhr      = float(latest.get("restingHR") or 38.0)
    ftp      = ftp or float(latest.get("icu_pm_ftp") or 223)
    hrv_vals = [float(w.get("hrv") or 0) for w in wellness[-7:] if w.get("hrv")]
    hrv_7d   = sum(hrv_vals)/len(hrv_vals) if hrv_vals else hrv
    rhr_vals = [float(w.get("restingHR") or 0) for w in wellness if w.get("restingHR")]
    rhr_avg  = sum(rhr_vals)/len(rhr_vals) if rhr_vals else rhr
    print(f"  ✅ 体重={weight}kg FTP={ftp:.0f}W TP={_fmt_pace(int(tp_sec))}/km")
    print(f"     CTL={ctl:.1f} ATL={atl:.1f} Form={ctl-atl:.1f}")
    print(f"     HRV={hrv:.0f}(7d:{hrv_7d:.0f}) 睡眠={sleep_h:.1f}h RHR={rhr:.0f}bpm")
    return {"weight":weight,"ftp":ftp,"tp_sec":tp_sec,
            "ctl":ctl,"atl":atl,"form":ctl-atl,
            "hrv":hrv,"hrv_7d_avg":hrv_7d,
            "sleep_h":sleep_h,"rhr":rhr,"rhr_avg":rhr_avg,
            "wellness_history":wellness,
            "past_results":past_results,
            "weekly_counts":weekly_counts,
            "_icu_base":base, "_api_key":api_key}  # ICU検索用に保持

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

def _default_athlete(weight=68.4,ftp=223,tp_sec=288,past_results=None,weekly_counts=None):
    return {"weight":weight,"ftp":ftp,"tp_sec":tp_sec,
            "ctl":70.5,"atl":97.1,"form":-26.6,
            "hrv":86.0,"hrv_7d_avg":86.0,"sleep_h":6.5,"rhr":38.0,"rhr_avg":38.0,
            "wellness_history":[],"past_results":past_results or [],
            "weekly_counts":weekly_counts or {"run":0,"bike":0,"swim":0,"strength":0}}

def _fmt_pace(sec):
    m,s = divmod(int(sec),60); return f"{m}:{s:02d}"

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
        goal_sec = int(best_result["time_s"] * 0.97)
        goal_src = (f"本人ベスト({best_result['date']}) {best_result['time_str']} → -3%目標"
                    f"  [出典: {best_result.get('source','icu')}]")
        if rival_name:
            goal_src += f"  / 参考ライバル: {rival_name}"
            if rival_notes: goal_src += f"({rival_notes})"
    elif rival_time:
        goal_sec = rival_time
        goal_src = f"ライバル目標: {rival_name} {_fmt_time(rival_time)}"
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
                    "half":{"swim":1900,"bike":90,"run":21.1},
                    "iron":{"swim":3800,"bike":180,"run":42.2}}
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
IDEAL_WEEKLY = {"run":3,"bike":2,"swim":3,"strength":2}

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

    deficient = []
    for sport, ideal in IDEAL_WEEKLY.items():
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
    if any(k in text for k in ["triathlon","トライアスロン","duathlon"]): return "triathlon"
    if any(k in text for k in ["marathon","マラソン"]): return "marathon"
    if any(k in text for k in ["swim","スイム","水泳"]): return "swim"
    if any(k in text for k in ["cycling","ライド","サイクル"]): return "cycling"
    return "race"

def _detect_race_distance(text):
    for k,v in [("iron","iron"),("アイアン","iron"),("226","iron"),
                ("half","half"),("ハーフ","half"),("113","half"),("70.3","half"),
                ("olympic","olympic"),("オリンピック","olympic"),("51.5","olympic"),
                ("sprint","sprint"),("スプリント","sprint"),
                ("full","full"),("フル","full"),("42.","full")]:
        if k in text: return v
    return "unknown"

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
# レースフェーズ
# ============================================================
def get_race_phase(races, target_date):
    if isinstance(target_date, datetime): target_date = target_date.date()
    upcoming = sorted([r for r in races
                       if date.fromisoformat(r["date"]) >= target_date],
                      key=lambda r: r["date"])
    a_races  = [r for r in upcoming if r.get("priority","B")=="A"]
    nearest  = (a_races or upcoming or [None])[0]
    if not nearest: return {"phase":"base","weeks_to_race":999,"race":None}
    weeks = (date.fromisoformat(nearest["date"])-target_date).days//7
    if   weeks>16: phase="base"
    elif weeks>8:  phase="build"
    elif weeks>3:  phase="peak"
    elif weeks>1:  phase="taper"
    elif weeks==0: phase="race_week"
    else:          phase="recovery"
    return {"phase":phase,"weeks_to_race":weeks,"race":nearest}

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

def session_desc(sport, intensity, dur, phase, tp, ftp, goal_targets=None):
    label, purpose = INTENSITY_LABELS.get(intensity, ("", ""))
    motivation = PHASE_MOTIVATIONS.get(phase, "")

    if sport == "run":
        zones = {"recovery":(tp*1.40,"Z1"),"easy":(tp*1.20,"Z2"),
                 "moderate":(tp*1.08,"Z3"),"hard":(tp*0.98,"Z4")}
        pb,_ = zones.get(intensity,(tp*1.20,"Z2"))
        lo,hi = _fmt_pace(int(pb*1.05)), _fmt_pace(int(pb*0.97))
        km = round(dur*60/pb,1)
        # 目標タイムがあれば追記
        goal_note = ""
        if goal_targets and goal_targets.get("race_run_pace"):
            rp = goal_targets["race_run_pace"]
            goal_note = f"\n🎯 レース目標ペース: {_fmt_pace(rp)}/km — 今日は{_fmt_pace(int(pb))}/kmで基礎を積みます"

        if intensity == "hard":
            desc = (f"【{label}】インターバルラン {dur}分\n"
                    f"目的: {purpose}\n"
                    f"W-up 10分(Z2) → {_fmt_pace(int(tp*0.95))}/km×4分×5本(rest 90秒) → C-down 5分\n"
                    f"推定: {km}km{goal_note}\n"
                    f"💪 {motivation}")
        elif intensity == "moderate":
            desc = (f"【{label}】テンポラン {dur}分\n"
                    f"目的: {purpose}\n"
                    f"W-up 10分 → {lo}〜{hi}/km で{dur-15}分 → C-down 5分\n"
                    f"推定: {km}km{goal_note}\n"
                    f"💪 {motivation}")
        else:
            desc = (f"【{label}】ラン {dur}分\n"
                    f"目的: {purpose}\n"
                    f"目標ペース: {lo}〜{hi}/km  推定: {km}km{goal_note}\n"
                    f"会話できるペースを維持してください\n"
                    f"💪 {motivation}")
        return desc

    if sport == "bike":
        zones = {"recovery":(0.50,"Z1"),"easy":(0.65,"Z2"),
                 "moderate":(0.88,"SS"),"hard":(0.95,"Z4")}
        pct,_ = zones.get(intensity,(0.65,"Z2"))
        lo_w,hi_w = int(ftp*(pct-0.07)), int(ftp*(pct+0.07))
        goal_note = ""
        if goal_targets and goal_targets.get("race_bike_w"):
            rw = goal_targets["race_bike_w"]
            goal_note = f"\n🎯 レース目標NP: {rw}W — 今日は{lo_w}〜{hi_w}Wで基礎を積みます"
        if intensity == "moderate" and dur >= 45:
            reps = max(2,(dur-20)//16)
            desc = (f"【{label}】スイートスポット {dur}分\n"
                    f"目的: {purpose}\n"
                    f"W-up 10分 → {lo_w}-{hi_w}W×12分×{reps}本(rest 4分) → C-down 5分\n"
                    f"FTPの{int((pct-0.07)*100)}-{int((pct+0.07)*100)}%{goal_note}\n"
                    f"💪 {motivation}")
        elif intensity == "hard":
            desc = (f"【{label}】閾値インターバル {dur}分\n"
                    f"目的: {purpose}\n"
                    f"W-up 10分 → {int(ftp*0.95)}-{int(ftp*1.05)}W×8分×3本(rest 3分) → C-down{goal_note}\n"
                    f"💪 {motivation}")
        else:
            desc = (f"【{label}】バイク {dur}分\n"
                    f"目的: {purpose}\n"
                    f"目標パワー: {lo_w}〜{hi_w}W ({int((pct-0.07)*100)}-{int((pct+0.07)*100)}%FTP){goal_note}\n"
                    f"💪 {motivation}")
        return desc

    if sport == "swim":
        desc = (f"【Z2スイム】{dur}分\n"
                f"目的: 有酸素基盤の向上・泳ぎのリズム・フォームの定着\n"
                f"2000m目安 / 楽なペースで泳ぎを整える\n"
                f"💪 {motivation}")
        return desc

    return f"{sport} {dur}分"

# ============================================================
# 筋トレメニュー（目的・モチベ付き）
# ============================================================
STRENGTH_DB = {
    ("core","base"):  [("プランク",3,"45秒",30,"体は一直線"),("デッドバグ",3,"10回",30,"腰を床に"),
                       ("バードドッグ",3,"10回",30,"四つ這いから対角"),("サイドプランク",3,"30秒",30,"左右各")],
    ("core","build"): [("プランク",4,"60秒",20,""),("マウンテンクライマー",3,"20回",20,""),
                       ("レッグレイズ",3,"15回",30,""),("ロシアンツイスト",3,"20回",25,""),
                       ("サイドプランク+リフト",2,"12回",30,"")],
    ("core","peak"):  [("プランク",4,"75秒",15,""),("マウンテンクライマー",4,"30秒",15,""),
                       ("V字クランチ",3,"15回",30,""),("スーパーマン",3,"15回",25,""),
                       ("ドラゴンフラッグ",2,"8回",40,"")],
    ("core","maintenance"): [("プランク",3,"60秒",20,""),("デッドバグ",3,"12回",25,""),
                              ("サイドプランク",3,"45秒",25,"")],
    ("upper_body","base"):  [("プッシュアップ",3,"12回",60,""),("パイクPU",3,"10回",60,""),
                              ("ダイヤモンドPU",2,"8回",60,"")],
    ("upper_body","build"): [("アーチャーPU",3,"8回",60,""),("ディップス(椅子)",3,"12回",60,""),
                              ("パイクPU",3,"12回",50,""),("プッシュアップ",3,"15回",40,"")],
    ("upper_body","peak"):  [("クラップPU",3,"8回",60,"爆発的に"),("アーチャーPU",3,"10回",60,""),
                              ("ディップス(椅子)",3,"15回",45,""),("パイクPU",3,"15回",40,"")],
    ("upper_body","maintenance"): [("プッシュアップ",3,"15回",45,""),("パイクPU",3,"12回",45,""),
                                   ("ダイヤモンドPU",2,"10回",45,"")],
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
# 栄養計算
# ============================================================
def calc_nutrition(cfg, athlete, cond, phase, train_h):
    w=athlete["weight"]; h=float(cfg["athlete"].get("height_cm",170))
    age=int(cfg["athlete"].get("age",35)); goal=cfg["athlete"].get("goal","performance")
    bmr=10*w+6.25*h-5*age+5
    acts={"base":1.55,"build":1.65,"peak":1.70,"taper":1.45,"race_week":1.35,"recovery":1.40}
    tdee=bmr*acts.get(phase,1.55)+train_h*w*7
    adj={"weight_loss":-300,"muscle_gain":+200}.get(goal,0)
    kcal=round(tdee+adj)
    p_r={"muscle_gain":2.2,"performance":2.0}.get(goal,1.8)
    if train_h>1.5: p_r+=0.2
    prot=round(w*p_r); fat=round(kcal*0.25/9); carb=round((kcal-prot*4-fat*9)/4)
    notes=[]
    if phase=="taper": notes.append("テーパー期：炭水化物多めでグリコーゲン蓄積")
    if phase=="build": notes.append("ビルド期：練習後30分以内にタンパク質摂取")
    if not cfg["nutrition"].get("uses_protein_supplement"):
        notes.append("鶏むね・卵・魚・豆腐・納豆でタンパク補給")
    if cond["condition"] in ("fatigued","depleted"):
        notes.append("疲労回復中：青魚・ベリー・ショウガ（抗炎症）")
    return {"kcal":kcal,"prot":prot,"carb":carb,"fat":fat,"p_per_kg":round(p_r,1),"notes":notes}

# ============================================================
# 週間プラン生成
# ============================================================
EMOJI={"run":"🏃","bike":"🚴","swim":"🏊","strength":"💪",
       "yoga":"🧘","stretch":"🤸","hiit":"🔥","rest":"😴","race":"🏁"}
SHORT_THRESH = 31

def generate_week(cfg, athlete, cond_info, race_info, gcal_days,
                  str_prog, start_date, goal_targets):
    phase       = race_info["phase"]
    cond        = cond_info["condition"]
    template    = PHASE_TEMPLATES.get(phase, PHASE_TEMPLATES["base"])
    if cond in ("fatigued","depleted"):
        template = PHASE_TEMPLATES["recovery"]

    strength_cfg= cfg["strength"]
    str_sessions= 0
    str_max     = strength_cfg.get("sessions_per_week",2)
    str_dur     = strength_cfg.get("session_duration_min",30)
    deficient   = detect_deficient_sports(athlete.get("weekly_counts",{}), cfg)

    plan = []
    for i,(sport,sub,default_dur) in enumerate(template):
        day     = start_date+timedelta(days=i)
        day_str = day.strftime("%Y-%m-%d")
        gcal    = gcal_days.get(day_str,{})
        avail   = gcal.get("available_min", 60 if day.weekday()<5 else default_dur)
        races   = gcal.get("races",[])
        notes   = gcal.get("notes",[])
        reduce_ = gcal.get("reduce_next_morning",False)

        if races:
            plan.append({"date":day_str,"sport":"race","name":races[0]["name"],
                         "description":"🏁 レース当日","duration_min":0,
                         "gcal_notes":notes,"reduce_next":reduce_,
                         "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,0)})
            continue

        if sport=="rest" or avail==0:
            plan.append({"date":day_str,"sport":"rest","name":"REST",
                         "description":"完全休養\n目的: 超回復のトリガー。何もしないことが最強のトレーニングです。\n💪 休む勇気を持つことも一流アスリートの条件です。",
                         "duration_min":0,"gcal_notes":notes,"reduce_next":reduce_,
                         "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,0)})
            continue

        # ── 短時間セッション（30分以下）──────────────────────
        if avail <= SHORT_THRESH and sport not in ("rest","yoga","stretch"):
            short = pick_short_session(avail, cond_info, phase, deficient,
                                       strength_cfg, str_prog)
            plan.append({**short,"date":day_str,"gcal_notes":notes,"reduce_next":reduce_,
                         "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,short["duration_min"]/60)})
            continue

        # 筋トレ
        if sport=="strength":
            if str_sessions>=str_max or avail<20:
                short = pick_short_session(min(30,avail), cond_info, phase, deficient,
                                           strength_cfg, str_prog)
                plan.append({**short,"date":day_str,"gcal_notes":notes,"reduce_next":reduce_,
                             "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,0.3)})
                continue
            str_sessions+=1
            dur=min(str_dur,avail)
            menu=gen_strength_menu(strength_cfg,phase,cond_info,str_prog,dur)
            plan.append({"date":day_str,"sport":"strength","name":f"筋トレ [{str_prog['level']}]",
                         "description":menu,"duration_min":dur,"gcal_notes":notes,
                         "reduce_next":reduce_,
                         "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,0.5)})
            continue

        if sport=="yoga":
            short = pick_short_session(min(30,avail), cond_info, phase, deficient,
                                       strength_cfg, str_prog)
            plan.append({**short,"date":day_str,"gcal_notes":notes,"reduce_next":reduce_,
                         "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,0.2)})
            continue

        # 有酸素
        intensity = decide_intensity(phase, sport, cond)
        prev_str = (day-timedelta(1)).strftime("%Y-%m-%d")
        if gcal_days.get(prev_str,{}).get("reduce_next_morning"):
            idx = INTENSITY_ORDER.index(intensity) if intensity in INTENSITY_ORDER else 1
            intensity = INTENSITY_ORDER[max(0,idx-1)]

        dur  = max(20,min(default_dur,avail))
        desc = session_desc(sport, intensity, dur, phase,
                            athlete["tp_sec"], athlete["ftp"], goal_targets.get("targets"))
        n_jp = {"run":"🏃 ラン","bike":"🚴 バイク","swim":"🏊 スイム"}.get(sport,sport)
        il   = {"recovery":"リカバリー","easy":"イージー","moderate":"テンポ","hard":"インターバル"}.get(intensity,"")
        plan.append({"date":day_str,"sport":sport,"name":f"{n_jp} – {il}",
                     "description":desc,"duration_min":dur,"intensity":intensity,
                     "gcal_notes":notes,"reduce_next":reduce_,
                     "nutrition":calc_nutrition(cfg,athlete,cond_info,phase,dur/60)})
    return plan

# ============================================================
# 出力
# ============================================================
def print_plan(plan, race_info, cond_info, athlete, goal_targets, cfg=None, today_mode=False):
    phase=race_info["phase"]; race=race_info["race"]; ci=cond_info
    icons={"peak":"🔥","good":"✅","normal":"😐","fatigued":"😓","depleted":"🛑"}
    if not today_mode:
        print(f"\n  コンディション: {icons.get(ci['condition'],'')} {ci['condition'].upper()}"
              f"  スコア {ci['score']}/10")
        print(f"  CTL={athlete['ctl']:.1f} ATL={athlete['atl']:.1f}"
              f" Form={athlete['form']:.1f} HRV={athlete['hrv']:.0f}"
              f"(7d:{athlete['hrv_7d_avg']:.0f}) 睡眠={athlete['sleep_h']:.1f}h")
        for r in ci.get("reasons",[]): print(f"  ⚠️  {r}")
        if race:
            print(f"  🏁 {race['name']} ({race['date']}) 残り{race_info['weeks_to_race']}週 [{phase.upper()}]")
        # ゴール情報
        gt = goal_targets
        if gt.get("goal_src"):
            print(f"  🎯 目標根拠: {gt['goal_src']}")
        if gt.get("targets",{}).get("race_run_pace"):
            print(f"     ラン目標: {_fmt_pace(gt['targets']['race_run_pace'])}/km"
                  f"  バイク目標: {gt['targets'].get('race_bike_w','-')}W")
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
            # REST/RECOVERYも説明を表示
            for line in item["description"].split("\n")[:2]:
                if line.strip(): print(f"    {line.strip()}")
            continue
        n   = item.get("nutrition",{})
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
        if n:
            print(f"    🍽  {n['kcal']}kcal  P:{n['prot']}g({n['p_per_kg']}g/kg)"
                  f"  C:{n['carb']}g  F:{n['fat']}g")
            for note in n.get("notes",[]): print(f"    💡 {note}")

# ============================================================
# アップロード
# ============================================================
def upload_plan(plan, cfg, dry_run=False):
    aid=cfg["athlete"]["intervals_icu_athlete_id"]
    api_key=cfg["athlete"]["intervals_icu_api_key"]
    base=f"https://intervals.icu/api/v1/athlete/{aid}/events"
    tmap={"run":"Run","bike":"Ride","swim":"Swim","strength":"WeightTraining",
          "yoga":"Yoga","stretch":"Workout","hiit":"WeightTraining"}
    ok=0
    for item in plan:
        if item["sport"] in ("rest","race"): continue
        payload={"start_date_local":f"{item['date']}T00:00:00","category":"WORKOUT",
                 "type":tmap.get(item["sport"],"Workout"),"name":item["name"],
                 "description":item["description"],"moving_time":item["duration_min"]*60}
        tag="[DRY] " if dry_run else ""
        print(f"  {tag}📤 {item['date']} {item['name']}",end=" ")
        if dry_run: print("(skip)"); ok+=1
        else:
            r=icu_post(base,api_key,payload)
            print("✅" if r else "❌")
            if r: ok+=1
    return ok

# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview",   action="store_true")
    parser.add_argument("--weeks",     type=int, default=1)
    parser.add_argument("--start",     type=str, default=None)
    parser.add_argument("--today",     action="store_true")
    parser.add_argument("--no-upload", action="store_true")
    args = parser.parse_args()

    print("="*64)
    print("  スマートトレーニングプラン v5")
    print("="*64)

    cfg     = load_config()
    athlete = fetch_athlete_data(cfg)
    hrv_cfg = cfg.get("hrv_scoring",{})
    cond    = calc_hrv_score(athlete, hrv_cfg)
    str_prog= calc_strength_progression(cfg)

    print(f"\n  🧠 HRVスコア: {cond['score']}/10 → {cond['condition'].upper()}")
    for r in cond.get("reasons",[]): print(f"     ⚠️  {r}")
    if str_prog["weeks_to_goal"]:
        print(f"  💪 筋肉量目標: {str_prog['goal_muscle_kg']}kg 残り{str_prog['weeks_to_goal']}週 Lv:{str_prog['level']}")

    # 不足種目
    deficient = detect_deficient_sports(athlete.get("weekly_counts",{}), cfg)
    if deficient:
        print(f"  📊 直近2週の不足種目: {' / '.join(deficient)}")

    # 過去リザルト表示
    if athlete.get("past_results"):
        print(f"\n  📈 直近レース結果:")
        for r in athlete["past_results"][:3]:
            print(f"     {r['date']} {r['name'][:20]:20s}  {r['time_str']}")

    print("\n  📅 Googleカレンダー確認中...")
    print("  ※ Google Calendar接続後に自動取得（現在はデフォルト設定）\n")
    gcal_days = {}
    races_from_cal = []

    start = date.today()+timedelta(days=1)
    if args.start: start = date.fromisoformat(args.start)

    if args.today:
        ri = get_race_phase(races_from_cal, date.today())
        gt = calc_goal_targets(ri, athlete, cfg)
        plan = generate_week(cfg,athlete,cond,ri,gcal_days,str_prog,date.today(),gt)
        print(f"{'─'*64}")
        print(f"  【当日朝モード】{date.today().isoformat()}  [{ri['phase'].upper()}]")
        print_plan(plan,ri,cond,athlete,gt,cfg,today_mode=True)
    else:
        all_plans = []
        for w in range(args.weeks):
            ws   = start+timedelta(weeks=w)
            ri   = get_race_phase(races_from_cal, ws)
            gt   = calc_goal_targets(ri, athlete, cfg)
            gcal = apply_trip_adjacency(gcal_days.copy())
            plan = generate_week(cfg,athlete,cond,ri,gcal,str_prog,ws,gt)
            print(f"\n{'─'*64}")
            print(f"  Week {w+1}: {ws.strftime('%Y-%m-%d')} 〜  [{ri['phase'].upper()}フェーズ]")
            print_plan(plan,ri,cond,athlete,gt,cfg)
            all_plans.extend(plan)

        if not args.no_upload:
            print(f"\n{'─'*64}")
            n = upload_plan(all_plans,cfg,dry_run=args.preview)
            total = len([p for p in all_plans if p["sport"] not in ("rest","race")])
            print(f"\n  {'プレビュー' if args.preview else 'アップロード'}完了: {n}/{total}件")

    print("="*64)

if __name__ == "__main__":
    main()
