"""
chat.py — CLIチャット・HTTPサーバー
smart_plan_v10.py line 6282-7248 から抽出
"""

import re
import json
from datetime import date, timedelta
from pathlib import Path

from .config import load_config
from .athlete_model import fetch_athlete_data, calc_hrv_score, calc_goal_targets, _fmt_time
from .phase_engine import get_race_phase, calc_strength_progression
from .plan_generator import generate_days
from .session_db import detect_deficient_sports
from .gcal_sync import parse_gcal_events_to_days
from .calendar_parser import apply_trip_adjacency
from .upload import upload_plan


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

    # RunMetrix フォームコメント（直近セッションのアラートがあれば表示）
    _rm = athlete.get("runmetrix_insights") or {}
    _rm_note = _rm.get("coach_note", "")
    if _rm_note:
        print(f"  📐 {_rm_note}")

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
                    Path(__file__).parent.parent / "coach_chat.html",
                    Path(__file__).parent.parent / "Coach_chat.html",
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
    start    = date.today()   # 当日朝モード: 当日から計画

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
        # ── 出社 3/16〜3/17 / 出張 3/18 / 出社 3/19, 3/24-3/26 ─────────
        {"summary":"出社","start":{"date":"2026-03-16"},"end":{"date":"2026-03-17"}},
        {"summary":"出社","start":{"date":"2026-03-17"},"end":{"date":"2026-03-18"}},
        {"summary":"出張","start":{"date":"2026-03-18"},"end":{"date":"2026-03-19"}},
        {"summary":"出社","start":{"date":"2026-03-19"},"end":{"date":"2026-03-20"}},
        {"summary":"出社","start":{"date":"2026-03-24"},"end":{"date":"2026-03-25"}},
        {"summary":"出社","start":{"date":"2026-03-25"},"end":{"date":"2026-03-26"}},
        {"summary":"出社","start":{"date":"2026-03-26"},"end":{"date":"2026-03-27"}},
        {"summary":"出社","start":{"date":"2026-04-01"},"end":{"date":"2026-04-02"}},
        {"summary":"出社","start":{"date":"2026-04-08"},"end":{"date":"2026-04-09"}},
        {"summary":"出社","start":{"date":"2026-04-14"},"end":{"date":"2026-04-15"}},
        {"summary":"出社","start":{"date":"2026-04-17"},"end":{"date":"2026-04-18"}},
        # ── スイム予約 3/14 (QUOX TRIATHLON CLUB, 6:45-8:00) ────────────
        {"summary":"スイム（BIG）",
         "description":"予約カテゴリ スケジュール\nスペース 4",
         "location":"QUOX TRIATHLON CLUB,  ",
         "start":{"dateTime":"2026-03-14T06:45:00+09:00","timeZone":"Asia/Tokyo"},
         "end":  {"dateTime":"2026-03-14T08:00:00+09:00","timeZone":"Asia/Tokyo"}},
        # ── 特別練習会/バイク・ショート 3/14 (QUOX, 9:30-12:00) ─────────
        {"summary":"特別練習会/バイク・ショート",
         "description":"予約カテゴリ スケジュール\nスペース 5",
         "location":"QUOX TRIATHLON CLUB,  ",
         "start":{"dateTime":"2026-03-14T09:30:00+09:00","timeZone":"Asia/Tokyo"},
         "end":  {"dateTime":"2026-03-14T12:00:00+09:00","timeZone":"Asia/Tokyo"}},
        # ── スイム予約 3/15 (QUOX, 6:45-8:00) ──────────────────────────
        {"summary":"スイム（BIG）",
         "description":"予約カテゴリ スケジュール\nスペース 4",
         "location":"QUOX TRIATHLON CLUB,  ",
         "start":{"dateTime":"2026-03-15T06:45:00+09:00","timeZone":"Asia/Tokyo"},
         "end":  {"dateTime":"2026-03-15T08:00:00+09:00","timeZone":"Asia/Tokyo"}},
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
        # ── スイム セントラル 3/22 (7:15-8:15) ──────────────────────────
        {"summary":"スイム　セントラル",
         "start":{"dateTime":"2026-03-22T07:15:00+09:00","timeZone":"Asia/Tokyo"},
         "end":  {"dateTime":"2026-03-22T08:15:00+09:00","timeZone":"Asia/Tokyo"}},
        # ── ラン 皇居 3/24 (18:30-19:30) ────────────────────────────────
        {"summary":"ラン　皇居",
         "start":{"dateTime":"2026-03-24T18:30:00+09:00","timeZone":"Asia/Tokyo"},
         "end":  {"dateTime":"2026-03-24T19:30:00+09:00","timeZone":"Asia/Tokyo"}},
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
