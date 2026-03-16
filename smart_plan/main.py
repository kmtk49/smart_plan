"""
main.py — エントリーポイント
smart_plan_v10.py line 6129-6280 から抽出
"""

import argparse
from datetime import date, timedelta

from .config import load_config
from .athlete_model import fetch_athlete_data, calc_hrv_score, calc_goal_targets
from .phase_engine import get_race_phase, calc_strength_progression
from .plan_generator import generate_days
from .plan_output import print_plan, print_calorie_summary
from .session_db import detect_deficient_sports
from .gcal_sync import parse_gcal_events_to_days
from .calendar_parser import apply_trip_adjacency
from .summary import print_race_schedule_summary, print_work_schedule_summary
from .upload import upload_plan
from .chat import (
    _cli_chat_session, _apply_feeling_to_cond, _apply_requests_to_gcal,
    _override_intensity, _fetch_gcal_events_auto, _inject_cal_rival,
    run_chat_server,
)
from .runmetrix_parser import find_runmetrix_dir, load_all_sessions, get_form_insights


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
    args = parser.parse_args()

    # ── サーバーモード ──────────────────────────────────────────
    if args.server:
        run_chat_server(args.port)
        return

    # --weeks 指定時は日数に変換
    num_days = args.weeks * 7 if args.weeks else args.days

    print("="*64)
    print("  🏊🚴🏃 コーチ AIトレーニングプラン v6")
    print("="*64)

    cfg      = load_config()
    athlete  = fetch_athlete_data(cfg)
    hrv_cfg  = cfg.get("hrv_scoring", {})
    cond     = calc_hrv_score(athlete, hrv_cfg)
    str_prog = calc_strength_progression(cfg)
    cfg_cal  = cfg.get("google_calendar", {})

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

    start = date.today()   # 当日朝に体重計測 → 当日から計画
    if args.start: start = date.fromisoformat(args.start)

    if args.today:
        print_race_schedule_summary(races_from_cal, athlete, cfg)
        ri   = get_race_phase(races_from_cal, date.today())
        gt   = calc_goal_targets(ri, athlete, cfg)
        _inject_cal_rival(gt, ri, races_from_cal, athlete)
        plan = generate_days(cfg, athlete, cond, ri, gcal_days, str_prog,
                             date.today(), gt, num_days=1)
        print(f"\n{'─'*64}")
        print(f"  【当日朝モード】{date.today().isoformat()}  [{ri['phase'].upper()}]")
        print_plan(plan, ri, cond, athlete, gt, cfg, today_mode=True, str_prog=str_prog, gcal_days=gcal_days, num_days=num_days)
        print("="*64)
        return

    # ══════════════════════════════════════════════════
    # 対話モード: まずコーチとチャットしてからプラン生成
    # ══════════════════════════════════════════════════
    # ── 過去7日間ウェルネス実績サマリーをここで表示 ──────────────────
    print_calorie_summary(None, cfg, athlete=athlete)
    # ── GCalスケジュールサマリーをここで表示 ────────────────────────
    print_work_schedule_summary(gcal_days, date.today(), num_days=num_days)

    # ── RunMetrix フォーム解析 ────────────────────────────────────
    _rm_insights = {}
    _rm_dir = find_runmetrix_dir()
    if _rm_dir:
        _rm_sessions = load_all_sessions(_rm_dir)
        if _rm_sessions:
            _rm_insights = get_form_insights(_rm_sessions)
            print(_rm_insights["summary"])
            # intensity_hint が reduce の場合はコンディションを疲労側に倒す
            if _rm_insights.get("intensity_hint") == "reduce":
                _cur = cond.get("condition", "normal")
                _bump = {"peak": "good", "good": "normal",
                         "normal": "fatigued", "fatigued": "fatigued",
                         "depleted": "depleted"}
                _new = _bump.get(_cur, "normal")
                if _new != _cur:
                    print(f"  ⚠️  フォームデータから疲労傾向を検出 → "
                          f"コンディションを {_cur} → {_new} に調整")
                    cond["condition"] = _new
            # athlete dict に格納（プラン生成・チャットで参照）
            athlete["runmetrix_insights"] = _rm_insights

    user_ctx = _cli_chat_session(athlete, cond, races_from_cal, num_days)

    # ── ユーザーが日数を変更した場合は反映 ──────────────────────────
    num_days = user_ctx.get("num_days", num_days)

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

        # 強度上書き
        if force_intensity:
            plan = _override_intensity(plan, force_intensity)

        end_date = (start + timedelta(days=num_days - 1)).strftime("%Y-%m-%d")
        print(f"\n{'═'*64}")
        print(f"  📋 {num_days}日間プラン: {start.strftime('%Y-%m-%d')} 〜 {end_date}")
        print(f"     [{ri['phase'].upper()}フェーズ]")
        print_plan(plan, ri, cond, athlete, gt, cfg, str_prog=str_prog, gcal_days=gcal_days, num_days=num_days)
        print_calorie_summary(plan, cfg, athlete=athlete)
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


if __name__ == "__main__":
    main()
