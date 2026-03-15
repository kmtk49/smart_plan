"""
summary.py — レース/ピリオダイゼーション/仕事サマリ
smart_plan_v10.py line 5838-6127 から抽出
"""

from datetime import date, timedelta
from pathlib import Path

from .athlete_model import _fmt_time
from .result_parser import extract_venue_coords


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
    "marathon":"フルマラソン(42.195km)",
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
