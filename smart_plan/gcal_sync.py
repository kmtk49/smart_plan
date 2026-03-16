"""
gcal_sync.py — GCal同期・変換
smart_plan_v10.py line 5537-5836 から抽出
"""

import os
import re
from datetime import date
from pathlib import Path

_CI = os.environ.get("CI") == "true"

from .result_parser import (
    fetch_weather_for_race, fetch_gdrive_pdf_via_api, parse_split_times_from_text,
    resolve_result_path, parse_result_file, load_race_splits_from_csv, _find_activities_csv,
)
from .calendar_parser import (
    _detect_race_type, _detect_race_distance, _parse_race_priority, parse_training_directive,
)


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
                "available_min": cfg_cal.get("default_availability", {}).get("weekend_max_min", 120) if is_weekend else 60,
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
            else:
                rival_m2 = None

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
            elif rival_m2:
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
                        if _CI:
                            print(f"    ☁️  GDrive [{_att_title}]: アクセス不可（CI環境）")
                        else:
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
                if not _CI:
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
                    if not _CI:
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
                cfg_cal.get("default_availability", {}).get("weekend_max_min", 120))
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
