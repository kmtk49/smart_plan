"""
plan_generator.py — generate_days / generate_week
smart_plan_v10.py line 4642-5147 から抽出
"""

import re
from datetime import timedelta
from collections import defaultdict

from .phase_engine import PHASE_TEMPLATES, INTENSITY_ORDER, decide_intensity
from .workout_builder import build_workout
from .nutrition import calc_nutrition
from .calendar_parser import build_directive_template
from .session_db import detect_deficient_sports, pick_short_session
from .strength import gen_strength_menu
from .athlete_model import _pace_to_icu, _fmt_pace


EMOJI = {"run":"🏃","bike":"🚴","swim":"🏊","strength":"💪",
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
        # 1件目はgenerate_days本体でテンプレート差し替え済み → 2件目以降だけ追加
        _forced_list = gcal_entry.get("forced_sessions", [])
        _main_fs = _forced_list[0]["sport"] if _forced_list else None
        for _fs_idx, _fs in enumerate(_forced_list):
            # 1件目かつ既にメインセッションとして計上済みならスキップ
            if _fs_idx == 0 and _main_fs:
                # メインセッションが既にplanに存在するかチェック
                _already = any(p.get("date") == day_str_ and p.get("sport") == _main_fs
                               for p in plan)
                if _already:
                    continue  # 既に生成済みなのでスキップ
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

        # ── GCal強制セッションがある日はテンプレートsportを差し替え ──────
        # forced_sessionsが1件だけならそのスポーツをテンプレートの代わりに使う
        # 複数（例: swim+bikeの日）はメインセッション1本 + _flush_extraで追加
        forced = gcal.get("forced_sessions", [])
        if forced and not gcal.get("force_sport"):
            # テンプレートスポーツをforcedの最初のスポーツに差し替え
            sport = forced[0]["sport"]
            sub   = "easy"
            # forced_durationを優先
            default_dur = forced[0]["duration"]
            avail = max(avail, default_dur)

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
