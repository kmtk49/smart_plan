"""
runmetrix_parser.py
===================
RunMetrix の lap_form_data CSV を解析してフォームインサイトを生成する。

対応ファイル:
  lap_form_data - YYYYMMDDHHMI.csv  (1kmラップごとの詳細フォームデータ)
  lap_form_score - YYYYMMDDHHMI.csv (1kmラップごとのスコアデータ ※任意)
"""

import csv
import re
from pathlib import Path
from datetime import datetime

# ── 主要列インデックス (lap_form_data, 0始まり) ──────────────────
COL = {
    "prog_dt":    1,   # プログラム日時
    "distance":   2,   # ラップ/距離 (m, 累積)
    "total_time": 3,   # タイム (累積)
    "lap_time":   4,   # ラップタイム
    "pace":       6,   # ペース
    "pitch_avg":  9,   # ピッチ平均 [steps/min]
    "pitch_l":    7,   "pitch_r":   8,
    "stride_avg": 12,  # ストライド平均 [m]
    "stride_l":   10,  "stride_r":  11,
    "vert_avg":   21,  # 上下動平均 [cm]
    "gct_avg":    48,  # 着地時間平均 [ms]
    "gct_l":      46,  "gct_r":     47,
    "gct_pct":    51,  # 着地時間率平均 [%]
    "impact_avg": 54,  # 着地衝撃平均 [m/s^2]
    "pushoff_spd":45,  # コロ出し速度平均 [m/s]
    "stiff_avg":  63,  # スティフネス平均 [kN/m·kg]
    "stiff_l":    61,  "stiff_r":   62,
}


def _parse_filename_dt(filename: str) -> datetime | None:
    """ファイル名から datetime を抽出: 202602211229 → datetime(2026,2,21,12,29)"""
    m = re.search(r'(\d{12})', filename)
    if not m:
        return None
    s = m.group(1)
    try:
        return datetime(int(s[:4]), int(s[4:6]), int(s[6:8]),
                        int(s[8:10]), int(s[10:12]))
    except Exception:
        return None


def _safe_float(v) -> float | None:
    try:
        return float(v)
    except Exception:
        return None


def _parse_time_str(s: str) -> float | None:
    """'H:MM:SS' or 'M:SS' → 秒数"""
    parts = s.strip().split(':')
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
    except Exception:
        return None


def _sec_to_pace_str(sec_per_km: float) -> str:
    """秒/km → 'M:SS' 表記"""
    m = int(sec_per_km // 60)
    s = int(sec_per_km % 60)
    return f"{m}:{s:02d}"


def parse_session(filepath: Path) -> dict | None:
    """
    1つの lap_form_data CSV を解析してセッション辞書を返す。

    戻り値:
    {
        "datetime":     datetime オブジェクト,
        "date_str":     "YYYY-MM-DD",
        "distance_km":  float,
        "avg_pace_str": "M:SS",
        "pitch":        float,   # ピッチ平均 [steps/min]
        "stride":       float,   # ストライド平均 [m]
        "vert_osc":     float,   # 上下動平均 [cm]
        "gct":          float,   # 着地時間平均 [ms]
        "gct_pct":      float,   # 着地時間率平均 [%]
        "impact":       float,   # 着地衝撃平均 [m/s^2]
        "stiffness":    float,   # スティフネス平均
        "asym_gct":     float,   # 着地時間左右差 [%]
        "asym_stride":  float,   # ストライド左右差 [%]
        "asym_stiff":   float,   # スティフネス左右差 [%]
        "laps":         list,    # ラップ別データ
        "filepath":     Path,
    }
    """
    dt = _parse_filename_dt(filepath.name)
    if not dt:
        return None

    raw_rows = []
    for enc in ('cp932', 'utf-8-sig', 'utf-8'):
        try:
            with open(filepath, encoding=enc) as f:
                reader = csv.reader(f)
                next(reader)  # ヘッダースキップ
                for row in reader:
                    if len(row) >= 64:
                        raw_rows.append(row)
            break
        except (UnicodeDecodeError, StopIteration):
            raw_rows = []
            continue

    if not raw_rows:
        return None

    laps = []
    for row in raw_rows:
        lap = {}
        for key, idx in COL.items():
            lap[key] = _safe_float(row[idx]) if idx < len(row) else None
        lap["_pace_str"] = row[COL["pace"]] if len(row) > COL["pace"] else ""
        laps.append(lap)

    # ── 総距離・平均ペース ──────────────────────────────────────
    last = laps[-1]
    total_dist_m  = last["distance"] or 0
    total_time_s  = _parse_time_str(raw_rows[-1][COL["total_time"]])
    avg_pace_str  = ""
    if total_dist_m > 0 and total_time_s:
        secs_per_km  = total_time_s / (total_dist_m / 1000)
        avg_pace_str = _sec_to_pace_str(secs_per_km)

    # ── セッション平均 (各ラップの単純平均) ─────────────────────
    def avg(key: str) -> float | None:
        vals = [l[key] for l in laps if l.get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    # ── 左右非対称性 (abs(L-R)/avg × 100%) ──────────────────────
    def asym_pct(l_key: str, r_key: str, avg_key: str) -> float | None:
        vals = []
        for lap in laps:
            lv = lap.get(l_key)
            rv = lap.get(r_key)
            av = lap.get(avg_key)
            if lv is not None and rv is not None and av and av != 0:
                vals.append(abs(lv - rv) / av * 100)
        return round(sum(vals) / len(vals), 1) if vals else None

    return {
        "datetime":     dt,
        "date_str":     dt.strftime("%Y-%m-%d"),
        "distance_km":  round(total_dist_m / 1000, 1),
        "avg_pace_str": avg_pace_str,
        "pitch":        avg("pitch_avg"),
        "stride":       avg("stride_avg"),
        "vert_osc":     avg("vert_avg"),
        "gct":          avg("gct_avg"),
        "gct_pct":      avg("gct_pct"),
        "impact":       avg("impact_avg"),
        "stiffness":    avg("stiff_avg"),
        "asym_gct":     asym_pct("gct_l",    "gct_r",    "gct_avg"),
        "asym_stride":  asym_pct("stride_l", "stride_r", "stride_avg"),
        "asym_stiff":   asym_pct("stiff_l",  "stiff_r",  "stiff_avg"),
        "laps":         laps,
        "filepath":     filepath,
    }


def load_all_sessions(runmetrix_dir: Path) -> list:
    """全 lap_form_data ファイルを日時順に読み込む"""
    sessions = []
    for f in sorted(runmetrix_dir.glob("lap_form_data*.csv")):
        s = parse_session(f)
        if s:
            sessions.append(s)
    sessions.sort(key=lambda x: x["datetime"])
    return sessions


def get_form_insights(sessions: list, n_recent: int = 5) -> dict:
    """
    直近 n_recent セッションを分析してインサイトを返す。

    戻り値:
    {
        "latest":         最新セッション dict,
        "recent":         直近 n セッションリスト,
        "alerts":         [str],          # ⚠️ 警告
        "positives":      [str],          # ✅ ポジティブ
        "summary":        str,            # 表示用サマリー文字列
        "intensity_hint": "reduce" | "maintain" | None,
        "coach_note":     str,            # プラン生成用の1行コメント
    }
    """
    if not sessions:
        return {"latest": None, "recent": [], "alerts": [], "positives": [],
                "summary": "", "intensity_hint": None, "coach_note": ""}

    recent = sessions[-n_recent:]
    latest = recent[-1]
    prev   = recent[-2] if len(recent) >= 2 else None

    alerts         = []
    positives      = []
    intensity_hint = None

    # ── 着地時間トレンド ──────────────────────────────────────────
    if latest.get("gct") and prev and prev.get("gct"):
        d = latest["gct"] - prev["gct"]
        if d > 8:
            alerts.append(f"着地時間が前回比+{d:.0f}ms増加（疲労・筋力低下のサイン）")
            intensity_hint = "reduce"
        elif d < -6:
            positives.append(f"着地時間が前回比{d:.0f}ms短縮（走力向上）")

    if latest.get("gct"):
        if latest["gct"] > 250:
            alerts.append(f"着地時間 {latest['gct']:.0f}ms（250ms超は改善余地あり）")
        elif latest["gct"] < 200:
            positives.append(f"着地時間 {latest['gct']:.0f}ms は優秀（200ms未満）")

    # ── 3回連続増加トレンド ───────────────────────────────────────
    if len(recent) >= 3:
        gct_vals = [s["gct"] for s in recent[-3:] if s.get("gct")]
        if len(gct_vals) == 3 and gct_vals[0] < gct_vals[1] < gct_vals[2]:
            alerts.append(
                f"着地時間が3回連続増加傾向 "
                f"({gct_vals[0]:.0f}→{gct_vals[1]:.0f}→{gct_vals[2]:.0f}ms)"
            )
            intensity_hint = "reduce"

    # ── 上下動 ───────────────────────────────────────────────────
    if latest.get("vert_osc") and prev and prev.get("vert_osc"):
        d = latest["vert_osc"] - prev["vert_osc"]
        if d > 0.5:
            alerts.append(f"上下動が前回比+{d:.1f}cm増加（エネルギーロス増）")
        elif d < -0.3:
            positives.append(f"上下動が前回比{d:.1f}cm改善")

    # ── 左右非対称性（着地時間）──────────────────────────────────
    if latest.get("asym_gct") is not None:
        ag = latest["asym_gct"]
        if ag > 10:
            alerts.append(f"着地時間の左右差 {ag:.1f}%（10%超は怪我リスク）")
            intensity_hint = "reduce"
        elif ag > 6:
            alerts.append(f"着地時間の左右差 {ag:.1f}%（やや大きめ）")
        else:
            positives.append(f"左右対称性 良好（着地差 {ag:.1f}%）")

    # ── ストライド左右差 ─────────────────────────────────────────
    if latest.get("asym_stride") is not None and latest["asym_stride"] > 7:
        alerts.append(f"ストライドの左右差 {latest['asym_stride']:.1f}%（要注意）")

    # ── コーチ向け1行コメント ────────────────────────────────────
    coach_note = ""
    if alerts:
        coach_note = f"RunMetrix最新フォーム: {alerts[0]}"
        if intensity_hint == "reduce":
            coach_note += " → ランの強度を下げることを推奨"
    elif positives:
        coach_note = f"RunMetrix最新フォーム: {positives[0]}"

    # ── サマリーテキスト（表示用）────────────────────────────────
    W = 62
    dist  = latest.get("distance_km", 0)
    pace  = latest.get("avg_pace_str", "")
    lines = []
    lines.append(f"\n  {'─'*W}")
    lines.append(
        f"  📐 RunMetrix フォーム解析  "
        f"{latest['date_str']}  {dist:.1f}km  {pace}/km"
    )
    lines.append(f"  {'─'*W}")

    # 指標テーブル
    def val_str(key, fmt, unit=""):
        v = latest.get(key)
        return f"{v:{fmt}}{unit}" if v is not None else "N/A"

    def delta_str(key, fmt, unit=""):
        v = latest.get(key)
        p = prev.get(key) if prev else None
        if v is None:
            return "N/A"
        base = f"{v:{fmt}}{unit}"
        if p is not None:
            d = v - p
            sign = "+" if d >= 0 else ""
            base += f"({sign}{d:{fmt}}{unit})"
        return base

    fn = delta_str if prev else val_str
    lines.append(f"  {'指標':<14} {'今回(前回差)':>18}    {'指標':<14} {'今回(前回差)':>18}")
    lines.append(f"  {'─'*W}")
    lines.append(
        f"  {'ピッチ':<14} {fn('pitch', '.0f', 'spm'):>18}    "
        f"{'着地時間':<14} {fn('gct', '.0f', 'ms'):>18}"
    )
    lines.append(
        f"  {'ストライド':<14} {fn('stride', '.2f', 'm'):>18}    "
        f"{'着地時間率':<14} {fn('gct_pct', '.0f', '%'):>18}"
    )
    lines.append(
        f"  {'上下動':<14} {fn('vert_osc', '.1f', 'cm'):>18}    "
        f"{'着地衝撃':<14} {fn('impact', '.1f', ''):>18}"
    )
    lines.append(
        f"  {'スティフネス':<14} {fn('stiffness', '.2f', ''):>18}    "
        f"{'左右差(着地)':<14} {val_str('asym_gct', '.1f', '%'):>18}"
    )
    lines.append(f"  {'─'*W}")

    if alerts:
        for a in alerts:
            lines.append(f"  ⚠️  {a}")
    if positives:
        for p in positives:
            lines.append(f"  ✅ {p}")
    if not alerts and not positives:
        lines.append(f"  ✅ 特記事項なし")
    lines.append(f"  {'─'*W}")

    return {
        "latest":         latest,
        "recent":         recent,
        "alerts":         alerts,
        "positives":      positives,
        "summary":        "\n".join(lines),
        "intensity_hint": intensity_hint,
        "coach_note":     coach_note,
    }


def find_runmetrix_dir() -> Path | None:
    """スクリプトの親ディレクトリから runmetrix_data フォルダを探す"""
    candidates = [
        Path(__file__).parent.parent / "runmetrix_data",
        Path(__file__).parent / "runmetrix_data",
        Path.cwd() / "runmetrix_data",
    ]
    for p in candidates:
        if p.is_dir():
            return p
    return None
