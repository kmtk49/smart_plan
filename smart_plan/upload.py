"""
upload.py — Intervals.icuアップロード
smart_plan_v10.py line 5445-5523 から抽出
"""

from .icu_api import icu_get, icu_post, icu_delete


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
