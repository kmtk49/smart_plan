"""
result_parser.py — PDF/Excel リザルト解析モジュール
smart_plan_v10.py line 249-1141 から抽出
"""

import re
import json
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, date
from pathlib import Path

from .config import CONFIG_FILE
from .icu_api import icu_get

# PyPDF2はオプション（なくても動作）
try:
    import PyPDF2
    HAS_PYPDF2 = True
except ImportError:
    HAS_PYPDF2 = False

# openpyxl / pandas はオプション（なくても動作）
try:
    import openpyxl, pandas as pd
    HAS_EXCEL = True
except ImportError:
    HAS_EXCEL = False

# ============================================================
# 会場名→(緯度, 経度, 表示名) マッピング
# ============================================================
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
    script_dir = pathlib.Path(__file__).parent.parent
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
        pathlib.Path(__file__).parent.parent,
        pathlib.Path(__file__).parent.parent.parent,
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
    script_dir  = pathlib.Path(__file__).parent.parent.resolve()
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
