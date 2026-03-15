"""
garmin_export.py — Garmin Connect JSON生成
smart_plan_v10.py line 4161-4569 から抽出
"""

from .workout_builder import build_workout


# ============================================================
# Garmin Connect ワークアウト JSON 生成
# ============================================================
# 参考:
#   ThomasRondof/GarminWorkoutAItoJSON (Garmin API リバースエンジニアリング)
#   Garmin Training API: developer.garmin.com/gc-developer-program/training-api/
#   FIT SDK: developer.garmin.com/fit/file-types/workout/
#
# Garmin JSON Key:
#   sportType.sportTypeKey: "running"/"cycling"/"swimming"/"strength_training"
#   stepType.stepTypeKey: "warmup"/"interval"/"recovery"/"cooldown"/"rest"
#   endCondition.conditionTypeKey: "time"(秒)/"distance"(m)/"lap.button"
#   targetType.workoutTargetTypeKey:
#     "pace.zone" → targetValueOne/Two: m/s
#     "power.zone" → targetValueOne/Two: W
#     "heart.rate.zone" → targetValueOne/Two: bpm
#     "no.target"
# ============================================================

def _garmin_step(step_type, duration_type, duration_val, target_type="no.target",
                 target_low=None, target_high=None, order=1, desc=""):
    """Garmin Connect ワークアウトステップ辞書を生成"""
    step = {
        "type": "ExecutableStepDTO",
        "stepId": None,
        "stepOrder": order,
        "stepType": {"stepTypeId": {
            "warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5
        }.get(step_type, 3), "stepTypeKey": step_type},
        "childStepId": None,
        "description": desc,
        "endCondition": {"conditionTypeId": {
            "time": 2, "distance": 3, "lap.button": 1
        }.get(duration_type, 2), "conditionTypeKey": duration_type},
        "endConditionValue": duration_val,
        "preferredEndConditionUnit": None,
        "endConditionCompare": None,
        "endConditionZone": None,
        "targetType": {
            "workoutTargetTypeId": {
                "no.target": 1, "power.zone": 2, "cadence.zone": 3,
                "heart.rate.zone": 4, "speed.zone": 5, "pace.zone": 6
            }.get(target_type, 1),
            "workoutTargetTypeKey": target_type
        },
        "targetValueOne": target_low,
        "targetValueTwo": target_high,
        "zoneNumber": None,
    }
    return step

def _garmin_repeat(steps, iterations, order=1):
    """Garmin Connect リピートブロックを生成"""
    return {
        "type": "RepeatGroupDTO",
        "stepId": None,
        "stepOrder": order,
        "stepType": {"stepTypeId": 6, "stepTypeKey": "repeat"},
        "childStepId": 1,
        "numberOfIterations": iterations,
        "smartRepeat": False,
        "endCondition": {"conditionTypeId": 7, "conditionTypeKey": "iterations"},
        "endConditionValue": iterations,
        "workoutSteps": steps,
    }

def _pace_to_ms(pace_sec_per_km):
    """ペース(秒/km)→速度(m/s)に変換 (Garmin API はm/s単位)"""
    if pace_sec_per_km <= 0:
        return 0.0
    return round(1000.0 / pace_sec_per_km, 4)

def build_garmin_workout(sport, intensity, dur, phase, tp, ftp, css=None, goal_targets=None):
    """
    Garmin Connect 互換のワークアウトJSONを生成する。
    intervals.icuのworkout_docと並行して生成し、Garminデバイスへ直接送信可能。

    Args:
        sport: "run" / "bike" / "swim" / "strength" / "yoga"
        intensity: "hard" / "moderate" / "easy" / "recovery"
        dur: 合計時間(分)
        phase: トレーニングフェーズ
        tp: 閾値ペース(秒/km)
        ftp: FTP(W)
        css: Critical Swim Speed(秒/100m)
        goal_targets: レース目標辞書

    Returns:
        dict: Garmin Connect APIに投稿可能なワークアウトJSON
    """
    import hashlib as _gh
    _seed = int(_gh.md5(f"{phase}|{dur}|{intensity}".encode()).hexdigest()[:8], 16)

    sport_map = {
        "run": "running", "bike": "cycling", "swim": "swimming",
        "strength": "strength_training", "yoga": "yoga",
        "brick": "other", "race": "triathlon",
    }
    sport_key = sport_map.get(sport, "other")

    steps = []
    order = [1]  # mutableカウンタ

    def add_step(s):
        s["stepOrder"] = order[0]
        steps.append(s)
        order[0] += 1

    def add_repeat(reps_list, iterations):
        blk = _garmin_repeat(reps_list, iterations, order[0])
        for i, s in enumerate(reps_list):
            s["stepOrder"] = i + 1
        steps.append(blk)
        order[0] += 1

    # ── ラン ──────────────────────────────────────────────────────
    if sport == "run":
        _tp = tp
        wu_pace_lo = _pace_to_ms(int(_tp * 1.30))
        wu_pace_hi = _pace_to_ms(int(_tp * 1.10))
        cd_pace = _pace_to_ms(int(_tp * 1.35))
        z2_lo = _pace_to_ms(int(_tp * 1.25))
        z2_hi = _pace_to_ms(int(_tp * 1.10))
        _type = _seed % 5 if intensity == "hard" else (
                _seed % 3 if intensity in ("moderate","tempo") else _seed % 3)

        if intensity == "hard":
            # WU
            add_step(_garmin_step("warmup","time", 600,
                "pace.zone", wu_pace_lo, wu_pace_hi, desc="Z2 Warmup"))
            if _type == 0:   # 4分インターバル
                ip_lo = _pace_to_ms(int(_tp * 1.00))
                ip_hi = _pace_to_ms(int(_tp * 0.93))
                rp_lo = _pace_to_ms(int(_tp * 1.35))
                rp_hi = _pace_to_ms(int(_tp * 1.20))
                reps  = max(3, min(6, (dur-15)//5))
                add_repeat([
                    _garmin_step("interval","time",240,"pace.zone",ip_lo,ip_hi,desc="Interval"),
                    _garmin_step("recovery","time",90,"pace.zone",rp_lo,rp_hi,desc="Recovery jog"),
                ], reps)
            elif _type == 1:  # 1000m
                ip_lo = _pace_to_ms(int(_tp * 0.97))
                ip_hi = _pace_to_ms(int(_tp * 0.91))
                rp_lo = _pace_to_ms(int(_tp * 1.35))
                rp_hi = _pace_to_ms(int(_tp * 1.20))
                reps  = max(3, min(5, (dur-15)//6))
                add_repeat([
                    _garmin_step("interval","distance",1000,"pace.zone",ip_lo,ip_hi,desc="1km"),
                    _garmin_step("recovery","time",120,"pace.zone",rp_lo,rp_hi,desc="Rest"),
                ], reps)
            elif _type == 2:  # 30/30
                sp_lo = _pace_to_ms(int(_tp * 0.95))
                sp_hi = _pace_to_ms(int(_tp * 0.88))
                rp_lo = _pace_to_ms(int(_tp * 1.35))
                rp_hi = _pace_to_ms(int(_tp * 1.20))
                reps  = max(6, min(12, (dur-15)//2))
                add_repeat([
                    _garmin_step("interval","time",30,"pace.zone",sp_lo,sp_hi,desc="Fast"),
                    _garmin_step("recovery","time",30,"pace.zone",rp_lo,rp_hi,desc="Float"),
                ], reps)
            elif _type == 3:  # 閾値ラン
                tp_lo = _pace_to_ms(int(_tp * 1.02))
                tp_hi = _pace_to_ms(int(_tp * 0.98))
                add_step(_garmin_step("interval","time",(dur-15)*60,
                    "pace.zone",tp_lo,tp_hi,desc="Threshold"))
            else:  # ファルトレク
                ip_lo = _pace_to_ms(int(_tp * 1.00))
                ip_hi = _pace_to_ms(int(_tp * 0.94))
                rp_lo = _pace_to_ms(int(_tp * 1.35))
                rp_hi = _pace_to_ms(int(_tp * 1.20))
                reps  = max(4, min(8, (dur-15)//4))
                add_repeat([
                    _garmin_step("interval","time",120,"pace.zone",ip_lo,ip_hi,desc="Fast"),
                    _garmin_step("recovery","time",120,"pace.zone",rp_lo,rp_hi,desc="Easy"),
                ], reps)
            # CD
            add_step(_garmin_step("cooldown","time",300,"pace.zone",cd_pace,z2_lo,desc="Cooldown"))

        elif intensity in ("moderate","tempo"):
            add_step(_garmin_step("warmup","time",600,"pace.zone",wu_pace_lo,wu_pace_hi,desc="Warmup"))
            t_lo = _pace_to_ms(int(_tp * 1.10))
            t_hi = _pace_to_ms(int(_tp * 1.03))
            if _type == 0:  # テンポ持続
                add_step(_garmin_step("interval","time",(dur-15)*60,"pace.zone",t_lo,t_hi,desc="Tempo"))
            elif _type == 1:  # クルーズインターバル
                cruise = 720 if dur>=60 else 480
                reps = max(2,(dur-15)//(cruise//60+2))
                add_repeat([
                    _garmin_step("interval","time",cruise,"pace.zone",t_lo,t_hi,desc="Cruise"),
                    _garmin_step("recovery","time",120,"pace.zone",wu_pace_lo,wu_pace_hi,desc="Float"),
                ], reps)
            else:  # プログレッション
                easy_s = (dur//3)*60
                prog_lo = _pace_to_ms(int(_tp * 1.04))
                prog_hi = _pace_to_ms(int(_tp * 1.00))
                add_step(_garmin_step("interval","time",easy_s,"pace.zone",z2_lo,z2_hi,desc="Easy build"))
                add_step(_garmin_step("interval","time",(dur-dur//3-15)*60,"pace.zone",prog_lo,prog_hi,desc="Push"))
            add_step(_garmin_step("cooldown","time",300,"pace.zone",cd_pace,z2_lo,desc="Cooldown"))

        else:  # easy / recovery
            z_lo = _pace_to_ms(int(_tp * 1.35)) if intensity=="recovery" else z2_lo
            z_hi = _pace_to_ms(int(_tp * 1.20)) if intensity=="recovery" else z2_hi
            add_step(_garmin_step("warmup","time",300,"pace.zone",z_lo,z_hi,desc="Easy start"))
            if _type == 1:  # ストライドつき
                add_step(_garmin_step("interval","time",(dur-3)*60,"pace.zone",z_lo,z_hi,desc="Easy run"))
                add_repeat([
                    _garmin_step("interval","time",20,"pace.zone",
                        _pace_to_ms(int(_tp*0.93)),_pace_to_ms(int(_tp*0.90)),desc="Stride"),
                    _garmin_step("recovery","time",40,"pace.zone",z_lo,z_hi,desc="Float"),
                ], 4)
            else:
                add_step(_garmin_step("interval","time",dur*60,"pace.zone",z_lo,z_hi,desc="Easy run"))

    # ── バイク ────────────────────────────────────────────────────
    elif sport == "bike":
        wu_lo = int(ftp * 0.45); wu_hi = int(ftp * 0.55)
        cd_lo = int(ftp * 0.40); cd_hi = int(ftp * 0.50)
        _type = (_seed%5 if intensity=="hard" else
                 _seed%4 if intensity=="moderate" else
                 _seed%3 if intensity=="easy" else 0)

        if intensity == "hard":
            add_step(_garmin_step("warmup","time",600,"power.zone",wu_lo,int(ftp*0.55),desc="Ramp WU"))
            if _type == 0:  # 閾値8分
                reps = max(2,min(5,(dur-15)//11))
                add_repeat([
                    _garmin_step("interval","time",480,"power.zone",int(ftp*0.95),int(ftp*1.05),desc="FTP"),
                    _garmin_step("recovery","time",180,"power.zone",cd_lo,cd_hi,desc="Rest"),
                ], reps)
            elif _type == 1:  # VO2max
                vo2_dur = 4 if dur<75 else 5
                reps = max(3,min(6,(dur-15)//(vo2_dur+3)))
                add_repeat([
                    _garmin_step("interval","time",vo2_dur*60,"power.zone",int(ftp*1.06),int(ftp*1.20),desc="VO2max"),
                    _garmin_step("recovery","time",vo2_dur*60,"power.zone",cd_lo,cd_hi,desc="Rest"),
                ], reps)
            elif _type == 2:  # スプリント30秒
                reps = max(6,min(12,(dur-15)//2))
                add_repeat([
                    _garmin_step("interval","time",30,"power.zone",int(ftp*1.40),int(ftp*1.60),desc="Sprint"),
                    _garmin_step("recovery","time",90,"power.zone",cd_lo,cd_hi,desc="Rest"),
                ], reps)
            elif _type == 3:  # SS→閾値ビルド
                main = dur-15
                add_step(_garmin_step("interval","time",main*60*2//3,"power.zone",int(ftp*0.85),int(ftp*0.90),desc="Sweet Spot"))
                add_step(_garmin_step("interval","time",main*60//3,"power.zone",int(ftp*0.95),int(ftp*1.05),desc="Threshold push"))
            else:  # 5分ピーク
                reps = max(2,min(4,(dur-15)//8))
                add_repeat([
                    _garmin_step("interval","time",300,"power.zone",int(ftp*1.15),int(ftp*1.20),desc="5min peak"),
                    _garmin_step("recovery","time",180,"power.zone",cd_lo,cd_hi,desc="Rest"),
                ], reps)
            add_step(_garmin_step("cooldown","time",300,"power.zone",cd_lo,cd_hi,desc="CD"))

        elif intensity == "moderate":
            add_step(_garmin_step("warmup","time",600,"power.zone",wu_lo,int(ftp*0.55),desc="WU"))
            if _type == 0:  # スイートスポット
                block = 720 if dur<75 else 900
                reps = max(2,min(4,(dur-15)//(block//60+4)))
                add_repeat([
                    _garmin_step("interval","time",block,"power.zone",int(ftp*0.84),int(ftp*0.94),desc="Sweet Spot"),
                    _garmin_step("recovery","time",240,"power.zone",cd_lo,cd_hi,desc="Rest"),
                ], reps)
            elif _type == 1:  # テンポ
                add_step(_garmin_step("interval","time",(dur-15)*60,"power.zone",int(ftp*0.76),int(ftp*0.88),desc="Tempo"))
            elif _type == 2:  # ハイケイデンス
                add_repeat([
                    _garmin_step("interval","time",300,"power.zone",int(ftp*0.84),int(ftp*0.90),desc="High cadence"),
                    _garmin_step("recovery","time",180,"power.zone",cd_lo,cd_hi,desc="Easy"),
                ], 4)
                add_step(_garmin_step("interval","time",max(300,(dur-15-32)*60),"power.zone",int(ftp*0.65),int(ftp*0.75),desc="Endurance"))
            else:  # アンダー/オーバー
                reps = max(2,min(4,(dur-15)//8))
                add_repeat([
                    _garmin_step("interval","time",180,"power.zone",int(ftp*0.85),int(ftp*0.90),desc="Under"),
                    _garmin_step("interval","time",180,"power.zone",int(ftp*0.98),int(ftp*1.06),desc="Over"),
                    _garmin_step("recovery","time",120,"power.zone",cd_lo,cd_hi,desc="Easy"),
                ], reps)
            add_step(_garmin_step("cooldown","time",300,"power.zone",cd_lo,cd_hi,desc="CD"))

        else:  # easy / recovery
            lo_w = int(ftp*(0.50 if intensity=="recovery" else 0.56))
            hi_w = int(ftp*(0.55 if intensity=="recovery" else 0.75))
            add_step(_garmin_step("warmup","time",600,"power.zone",wu_lo,int(ftp*0.55),desc="WU"))
            add_step(_garmin_step("interval","time",(dur-15)*60,"power.zone",lo_w,hi_w,desc="Z2 Endurance"))
            add_step(_garmin_step("cooldown","time",300,"power.zone",cd_lo,cd_hi,desc="CD"))

    # ── スイム ────────────────────────────────────────────────────
    elif sport == "swim":
        _css = css or 125  # 秒/100m
        # Garminスイムはdistanceステップが基本 (m単位)
        wu_pace_lo = _css * 1.25 / 100  # m/s
        wu_pace_hi = _css * 1.10 / 100
        _type = _seed % 4 if intensity=="hard" else _seed%3 if intensity=="moderate" else _seed%3

        if intensity == "hard":
            add_step(_garmin_step("warmup","distance",400,"pace.zone",wu_pace_hi,wu_pace_lo,desc="WU easy"))
            if _type == 0:  # CSS 100mインターバル
                reps = min(20, max(6, int((dur*60/(_css*1.05+20))*100//100)))
                add_repeat([
                    _garmin_step("interval","distance",100,"pace.zone",_css*1.02/100,_css*0.98/100,desc="CSS"),
                    _garmin_step("rest","time",20,desc="Rest"),
                ], reps)
            elif _type == 1:  # ピラミッド
                for d in [50,100,200,100,50]:
                    add_step(_garmin_step("interval","distance",d,"pace.zone",_css*1.02/100,_css*0.98/100,desc=f"{d}m"))
                    add_step(_garmin_step("rest","time",20,desc="Rest"))
            elif _type == 2:  # 200mスレッショルド
                reps = max(3, min(8, int((dur*60/(_css*1.10+30))*100//200)))
                add_repeat([
                    _garmin_step("interval","distance",200,"pace.zone",_css*1.08/100,_css*1.02/100,desc="Threshold"),
                    _garmin_step("rest","time",30,desc="Rest"),
                ], reps)
            else:  # 50mスプリント
                reps = min(12, max(6, int((dur-10)*60//95)))
                add_repeat([
                    _garmin_step("interval","distance",50,"pace.zone",_css*0.95/100,_css*0.90/100,desc="Sprint"),
                    _garmin_step("rest","time",45,desc="Rest"),
                ], reps)
            add_step(_garmin_step("cooldown","distance",200,"pace.zone",wu_pace_hi,wu_pace_lo,desc="CD"))

        elif intensity == "moderate":
            add_step(_garmin_step("warmup","distance",400,"pace.zone",wu_pace_hi,wu_pace_lo,desc="WU"))
            if _type == 0:  # 400mスレッショルド
                reps = max(2, int((dur-10)*60//(_css*1.10*4+30)))
                add_repeat([
                    _garmin_step("interval","distance",400,"pace.zone",_css*1.08/100,_css*1.02/100,desc="Threshold"),
                    _garmin_step("rest","time",30,desc="Rest"),
                ], reps)
            elif _type == 1:  # 混合ペース
                reps = min(12, max(6, int((dur-10)*60//(_css*1.05*2+35))))
                add_repeat([
                    _garmin_step("interval","distance",100,"pace.zone",_css*1.02/100,_css*0.98/100,desc="Fast"),
                    _garmin_step("rest","time",20,desc=""),
                    _garmin_step("interval","distance",100,"pace.zone",_css*1.18/100,_css*1.10/100,desc="Easy"),
                    _garmin_step("rest","time",15,desc=""),
                ], reps)
            else:  # ロングインターバル
                block = 600 if dur>=60 else 500
                reps = max(2, int((dur-10)*60//(block+20)))
                add_repeat([
                    _garmin_step("interval","distance",block,"pace.zone",_css*1.18/100,_css*1.10/100,desc="Endurance"),
                    _garmin_step("rest","time",20,desc=""),
                ], reps)
            add_step(_garmin_step("cooldown","distance",200,"pace.zone",wu_pace_hi,wu_pace_lo,desc="CD"))

        else:  # easy / recovery
            pace_lo = _css*1.28/100; pace_hi = _css*1.18/100
            total_m = max(500, int(dur * 60 / _css * 100))
            add_step(_garmin_step("warmup","distance",min(400,total_m//5),"pace.zone",pace_lo,pace_hi,desc="Easy"))
            add_step(_garmin_step("interval","distance",max(200,total_m-400-200),"pace.zone",pace_lo,pace_hi,desc="Aerobic"))
            add_step(_garmin_step("cooldown","distance",200,"pace.zone",pace_lo,pace_hi,desc="CD"))

    # ── 筋トレ / ヨガ (Garminはノーターゲットのtime stepで構成) ──
    else:
        warm_sec = min(5,dur//8) * 60
        cool_sec = 3 * 60
        main_sec = max(60, dur*60 - warm_sec - cool_sec)
        add_step(_garmin_step("warmup","time",warm_sec,desc="Warmup"))
        add_step(_garmin_step("interval","time",main_sec,desc="Main"))
        add_step(_garmin_step("cooldown","time",cool_sec,desc="Cooldown / Stretch"))

    # ── ワークアウトJSONを組み立て ────────────────────────────────
    workout_name = {
        "run": {"hard":"インターバルラン","moderate":"テンポラン","easy":"イージーラン","recovery":"リカバリーラン"},
        "bike": {"hard":"閾値バイク","moderate":"スイートスポットライド","easy":"Z2ライド","recovery":"リカバリーライド"},
        "swim": {"hard":"インターバルスイム","moderate":"テンポスイム","easy":"エンデュランススイム","recovery":"リカバリースイム"},
        "strength": {"hard":"筋トレ[Peak]","moderate":"筋トレ[Build]","easy":"筋トレ[Base]","recovery":"筋トレ[回復]"},
        "yoga": {"hard":"ダイナミックヨガ","moderate":"アクティブフロー","easy":"リカバリーフロー","recovery":"リストラティブヨガ"},
    }.get(sport, {}).get(intensity, f"{sport} {intensity}")

    return {
        "sportType": {
            "sportTypeId": {"running":1,"cycling":2,"swimming":5,"strength_training":13,"yoga":26,"other":174}.get(sport_key,174),
            "sportTypeKey": sport_key,
        },
        "subSportType": None,
        "workoutName": f"{workout_name} {dur}min",
        "description": f"Generated by smart_plan v9 | {phase} | {intensity} | {dur}min",
        "estimatedDurationInSecs": dur * 60,
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeKey": sport_key},
            "workoutSteps": steps,
        }],
    }


def export_garmin_workout_json(sport, intensity, dur, phase, tp, ftp, css=None):
    """ワークアウトJSONをファイルに書き出す (デバッグ・手動インポート用)"""
    import json, pathlib, datetime
    data = build_garmin_workout(sport, intensity, dur, phase, tp, ftp, css)
    fn = pathlib.Path(f"garmin_{sport}_{intensity}_{dur}min_{phase}.json")
    fn.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return fn


# ── intervals.icu との並列生成ラッパー ──────────────────────────────
def build_workout_both(sport, intensity, dur, phase, tp, ftp, css=None, goal_targets=None):
    """
    intervals.icu workout_doc と Garmin JSON を同時生成する。
    intervals.icu にアップロードしながら、Garmin deivceにも構造化ワークアウトを送れる。

    Returns:
        (workout_doc, desc_text, garmin_json)
    """
    wdoc, desc = build_workout(sport, intensity, dur, phase, tp, ftp, css, goal_targets)
    garmin = build_garmin_workout(sport, intensity, dur, phase, tp, ftp, css, goal_targets)
    return wdoc, desc, garmin
