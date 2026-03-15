"""
icu_api.py — Intervals.icu API通信モジュール
smart_plan_v10.py line 48-88 から抽出
"""

import json
import base64
import urllib.request
import urllib.parse
import urllib.error


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


def icu_delete(url, api_key):
    """Intervals.icu のイベントを DELETE する"""
    req = urllib.request.Request(url, headers=icu_headers(api_key), method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return True
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True   # すでに存在しない = 削除済み扱い
        print(f"  [DELETE HTTP {e.code}] {e.read().decode()[:100]}")
        return False
    except Exception as e:
        print(f"  [DELETE エラー] {e}")
        return False
