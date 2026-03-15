"""
calendar_parser.py — GCal解析・指示解析モジュール
smart_plan_v10.py line 1817-2276 から抽出
"""

import re
from datetime import datetime, timedelta, date

from .athlete_model import search_icu_race_by_name


# ── 競技距離マスター ──────────────────────────────────────────────────────
# ITU / World Triathlon 競技規格準拠。各距離の swim/bike/run は km 単位。
RACE_DISTANCE_DEFS = {
    # ── トライアスロン ──────────────────────────────────────────────────
    "sprint":  {"rtype":"triathlon","swim":0.75, "bike":20,   "run":5,      "label":"スプリント(25.75km)"},
    "olympic": {"rtype":"triathlon","swim":1.5,  "bike":40,   "run":10,     "label":"オリンピック/OD(51.5km)"},
    "middle":  {"rtype":"triathlon","swim":1.9,  "bike":90,   "run":21.1,   "label":"ミドル/ハーフアイアン(113km)"},
    "iron":    {"rtype":"triathlon","swim":3.8,  "bike":180,  "run":42.2,   "label":"アイアンマン/フル(226km)"},
    "half":    {"rtype":"triathlon","swim":1.9,  "bike":90,   "run":21.1,   "label":"ミドル/ハーフアイアン(113km)"},
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
}


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
    return "race"


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
