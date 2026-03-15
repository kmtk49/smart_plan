"""
workout_builder.py — build_workout / session_desc
smart_plan_v10.py line 2404-3992 から抽出
"""

from .athlete_model import _pace_to_icu, _fmt_pace, _swim_pace, _swim_pace_icu, run_pace_zones
from .phase_engine import INTENSITY_LABELS, PHASE_MOTIVATIONS


def build_workout(sport, intensity, dur, phase, tp, ftp, goal_targets=None, css=None):
    """
    intervals.icu のワークアウトビルダー形式テキスト (workout_doc) と
    CLI 表示用の日本語説明文 (desc_text) を返す。

    workout_doc は intervals.icu の description フィールドに渡すと
    構造化されたステップグラフが生成される。

    Returns: (workout_doc: str, desc_text: str)
    """
    label, purpose = INTENSITY_LABELS.get(intensity, ("", ""))
    motivation = PHASE_MOTIVATIONS.get(phase, "")

    # ─── RUN ──────────────────────────────────────────────────────
    # エビデンス参考文献:
    #   Seiler & Kjerland 2006 (Scand J Med Sci Sports): 偏極化80/20の実証
    #   Muñoz et al. 2014 (Int J Sports Physiol Perf): ランで偏極化>閾値
    #   Neal et al. 2013 (J Appl Physiol): 偏極化でVO2max・閾値とも向上
    #   Stöggl & Sperlich 2014 (Front Physiol): 偏極化>閾値>HIIT>HVT
    #   Billat et al. 2001 (Med Sci Sports Exerc): 30/30インターバルでvVO2maxを維持
    #   Daniels 2014 "Daniels' Running Formula": クルーズインターバル理論
    #   Esteve-Lanao et al. 2007 (J Strength Cond Res): 偏極化vs閾値5か月比較
    #   Millet et al. 2011 (Eur J Appl Physiol): トライアスロン特化ラン強度配分
    #   Laursen & Jenkins 2002 (Sports Med): HIIT効果の包括的レビュー
    #   Baechle & Earle 2008 "Essentials of Strength and Conditioning": ランドリル
    #   Barnes & Kilding 2015 (Sports Med): ランニングエコノミー向上因子
    # Garmin FIT互換メモ:
    #   intervals.icu からGarmin Connectへ自動sync可能 (Settings > Garmin)
    #   "Warmup"/"Cooldown"ヘッダー→GarminのStep Type warmup/cooldown に自動変換
    #   "Z2 Pace"等のゾーン指定→アスリートのTP設定に基づきGarminに展開される
    #   距離指定("400mtr")はGarmin対応 / 時間指定("4m")もGarmin対応
    #   Garmin Connectワークアウト形式: stepType(warmup/active/rest/cooldown)
    #     endConditionType(time/distance/lapButton)
    #     targetType(pace/heartRate/power/cadence/speed/no.target)
    #   intervals.icuのワークアウトは Settings > Connected Apps > Garmin で
    #   自動的にGarmin Connectに同期される (Garmin Connect API v2 互換)
    #   ペースゾーン(Z1-Z5)はGarmin上ではアスリートのテンポ走ペース(TP)から自動計算
    #   Garmin CoachのRun Easy/Tempo/Long/Interval分類と対応:
    #     recovery/easy → Garmin "Easy Run"  moderate → Garmin "Tempo Run"
    #     hard → Garmin "Interval Run"  long easy → Garmin "Long Run"
    if sport == "run":
        pace_base = {
            "recovery": tp * 1.40,
            "easy":     tp * 1.20,
            "moderate": tp * 1.06,
            "tempo":    tp * 1.09,
            "hard":     tp * 0.96,
        }.get(intensity, tp * 1.20)

        # seedベースでセッションバリエーション選択 (毎回違うメニュー)
        import hashlib as _hlib
        _seed_val = int(_hlib.md5(f"{phase}|{dur}|{intensity}|run".encode()).hexdigest()[:8], 16)

        if intensity == "hard":
            # 7タイプ:
            #   A: 閾値インターバル 4分×N (Seiler推奨の核心セッション)
            #   B: 1km VO2maxインターバル × N (Neal 2013: VO2max向上に最も有効)
            #   C: ファルトレク 30/60 × N (Stöggl 2014: 神経筋x代謝系の二重刺激)
            #   D: 400mスピードレップ × N (Muñoz 2014: 最高速度開発)
            #   E: 800mレースペース × N (Billat 2001: vVO2max維持による乳酸閾値向上)
            #   F: ヒル走 90秒上り×N (Barnes 2015: ランエコノミー改善・筋力向上)
            #   G: 3分HIITインターバル × N (Laursen 2002: VO2max刺激の最短経路)
            _type = _seed_val % 7
            wu_min = 10; cd_min = 5; main_min = dur - wu_min - cd_min
            ip  = _pace_to_icu(int(tp * 0.96))   # 閾値ペース (TP×0.96)
            rp  = _pace_to_icu(int(tp * 1.25))   # リカバリーペース
            vp  = _pace_to_icu(int(tp * 0.90))   # VO2maxペース≈5kmペース
            sp  = _pace_to_icu(int(tp * 0.86))   # スプリントペース≈1500mペース

            if _type == 0:
                reps = max(3, min(6, main_min // 6))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"Threshold Intervals {reps}x",
                    f"- 4m {ip}",
                    f"- 90s {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【閾値インターバル A】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_min}分(Z2) → 閾値 {ip} × 4分 × {reps}本(rest 90秒) → CD {cd_min}分\n"
                    f"📚 Seiler 2006: 4分以上の閾値インターバルが乳酸閾値を最も引き上げる\n"
                )
            elif _type == 1:
                reps = max(3, min(8, main_min // 6))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"VO2max Intervals {reps}x",
                    f"- 1km {vp}",
                    f"- 3m {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【1km VO2maxインターバル】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_min}分 → 1km({vp}) × {reps}本(rest 3分) → CD {cd_min}分\n"
                    f"📚 Neal 2013: VO2max刺激が偏極化の高強度20%側の核心\n"
                )
            elif _type == 2:
                pairs = max(8, min(15, main_min // 2))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"Fartlek {pairs}x",
                    f"- 30s {vp}",
                    f"- 60s {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【ファルトレク】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_min}分 → 30秒全力({vp})/60秒ジョグ({rp}) × {pairs}本 → CD {cd_min}分\n"
                    f"📚 Stöggl 2014: 短い高強度でも神経筋・代謝系に強い刺激をもたらす\n"
                )
            elif _type == 3:
                reps = max(4, min(10, main_min // 4))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"Speed Reps {reps}x",
                    f"- 400mtr {sp}",
                    f"- 2m {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【400mスピードレップ】{dur}分\n"
                    f"目的: 最高速度・神経筋出力向上\n"
                    f"WU {wu_min}分 → 400m({sp}) × {reps}本(rest 2分) → CD {cd_min}分\n"
                    f"📚 Muñoz 2014: 偏極化の高強度側はVO2maxを超える速度も有効\n"
                )
            elif _type == 4:
                # Type E: 800mレースペースインターバル (Billat 2001)
                r800p = _pace_to_icu(int(tp * 0.91))  # 800m相当ペース
                reps = max(3, min(8, main_min // 6))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"800m Race Pace {reps}x",
                    f"- 800mtr {r800p}",
                    f"- 90s {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【800mレースペースインターバル】{dur}分\n"
                    f"目的: vVO2max維持時間の延伸・乳酸耐性\n"
                    f"WU {wu_min}分 → 800m({r800p}) × {reps}本(rest 90秒) → CD {cd_min}分\n"
                    f"📚 Billat 2001: vVO2max付近での反復が最大酸素摂取量の実用閾値を押し上げる\n"
                )
            elif _type == 5:
                # Type F: ヒル走 (Barnes & Kilding 2015 - ランエコノミー改善)
                hill_p = _pace_to_icu(int(tp * 0.95))  # ヒルアップはフラット閾値ペース相当
                reps = max(4, min(10, main_min // 4))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"Hill Reps {reps}x",
                    f"- 90s {hill_p}",
                    f"- 90s {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【ヒル走】{dur}分\n"
                    f"目的: ランエコノミー向上・股関節伸展筋力強化\n"
                    f"WU {wu_min}分 → 坂道90秒({hill_p})×{reps}本(下り90秒ジョグ) → CD {cd_min}分\n"
                    f"📚 Barnes 2015 Sports Med: ヒル走はストライド長・地面接触時間を改善\n"
                    f"💡 勾配4-8%の坂を強い前傾で上る / Garmin CoachのHill Sprint対応\n"
                )
            else:
                # Type G: 3分HIITインターバル (Laursen & Jenkins 2002)
                hiit_p = _pace_to_icu(int(tp * 0.92))
                reps = max(3, min(6, main_min // 6))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"HIIT {reps}x",
                    f"- 3m {hiit_p}",
                    f"- 3m {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【3分HIITインターバル】{dur}分\n"
                    f"目的: VO2max刺激・有酸素・無酸素の境界を拡張\n"
                    f"WU {wu_min}分 → 3分({hiit_p})/{reps}本(3分ジョグ) → CD {cd_min}分\n"
                    f"📚 Laursen 2002 Sports Med: 3-5分HIITが最もVO2max向上効率が高い\n"
                    f"📚 Millet 2011 Eur J Appl Physiol: トライアスロンのランパフォーマンスは\n"
                    f"     VO2maxと乳酸閾値の両方に依存する\n"
                )

        elif intensity in ("moderate", "tempo"):
            # 5タイプ: A=テンポ連続, B=クルーズインターバル, C=ビルドアップ
            #           D=マラソンペース走, E=テンポ+スピードフィニッシュ
            _type = _seed_val % 5
            wu_min = 10; cd_min = 5; main_min = dur - wu_min - cd_min
            t_slow = _pace_to_icu(int(tp * 1.08))
            t_fast = _pace_to_icu(int(tp * 1.02))
            rp     = _pace_to_icu(int(tp * 1.20))
            z4p    = _pace_to_icu(int(tp * 0.98))
            mp     = _pace_to_icu(int(tp * 1.12))  # マラソンペース (TP+12%)

            if _type == 0:
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    "Tempo",
                    f"- {main_min}m Z3 Pace",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【テンポラン】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_min}分 → テンポ {main_min}分(Z3:{t_slow}〜{t_fast}) → CD {cd_min}分\n"
                    f"📚 Esteve-Lanao 2007: 閾値付近の持続走が乳酸処理能力を向上\n"
                )
            elif _type == 1:
                reps = max(2, min(4, main_min // 12))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    f"Cruise Intervals {reps}x",
                    f"- 10m {t_slow}-{t_fast} Pace",
                    f"- 2m {rp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【クルーズインターバル】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_min}分 → 10分テンポ × {reps}本(rest 2分ジョグ) → CD {cd_min}分\n"
                    f"📚 Daniels 2014 Running Formula: クルーズインターバルはLT強化の最効率手段\n"
                    f"💡 テンポ中は「何とか会話できる」程度の強度で\n"
                )
            elif _type == 2:
                seg = main_min // 3
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z1 Pace",
                    "",
                    "Build Phase 1",
                    f"- {seg}m Z2 Pace",
                    "",
                    "Build Phase 2",
                    f"- {seg}m Z3 Pace",
                    "",
                    "Build Phase 3",
                    f"- {seg}m {t_fast}-{z4p} Pace",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【ビルドアップラン】{dur}分\n"
                    f"目的: ペース感覚・エネルギー切り替え訓練\n"
                    f"WU {wu_min}分 → Z2({seg}分) → Z3({seg}分) → Z4({seg}分) → CD {cd_min}分\n"
                    f"📚 後半に向けてペースを上げる → レースの後半強さに直結\n"
                )
            elif _type == 3:
                # Type D: マラソンペース走 (Midrace-specific conditioning)
                marathon_seg = main_min
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    "Marathon Pace",
                    f"- {marathon_seg}m {mp}",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【マラソンペース走】{dur}分\n"
                    f"目的: レース特異的ペース感覚・脂肪+糖質混合燃焼訓練\n"
                    f"WU {wu_min}分 → MPペース({mp}) {marathon_seg}分 → CD {cd_min}分\n"
                    f"📚 Daniels 2014: Mペース走は高強度セッションの前後に配置すると効果的\n"
                    f"💡 Garmin CoachのMarathon Pace Run対応ペース帯\n"
                )
            else:
                # Type E: テンポ+スピードフィニッシュ (negative split training)
                seg1 = main_min * 2 // 3
                seg2 = main_min - seg1
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_min}m Z2 Pace",
                    "",
                    "Tempo Phase",
                    f"- {seg1}m {t_slow} Pace",
                    "",
                    "Speed Finish",
                    f"- {seg2}m {t_fast}-{z4p} Pace",
                    "",
                    "Cooldown",
                    f"- {cd_min}m Z1 Pace",
                ])
                desc_text = (
                    f"【テンポ+スピードフィニッシュ】{dur}分\n"
                    f"目的: ネガティブスプリット習得・後半加速能力\n"
                    f"WU {wu_min}分 → テンポ{seg1}分({t_slow}) → スピード{seg2}分({t_fast}〜{z4p}) → CD {cd_min}分\n"
                    f"📚 レース後半に加速できる選手はネガティブスプリット習慣を持つ傾向がある\n"
                )

        else:  # easy / recovery
            # 5タイプ: A=Z2ステディ, B=ストライドあり, C=ロング走
            #           D=ドリルラン(ランエコノミー), E=走り込み(Z1→Z2プログレッシブ)
            _type = _seed_val % 5
            pz = run_pace_zones(tp)
            z_num = 1 if intensity == "recovery" else 2
            z_info = pz[z_num]
            zone_tag = f"Z{z_num} Pace"
            km = round(dur * 60 / pace_base, 1)
            stride_p = _pace_to_icu(int(tp * 0.88))
            jog_p    = _pace_to_icu(int(tp * 1.20))

            if _type == 0 or intensity == "recovery":
                workout_doc = (
                    f"Easy Run\n"
                    f"- {dur}m {zone_tag}\n"
                )
                desc_text = (
                    f"【{label}】ラン {dur}分\n"
                    f"目的: 有酸素基盤・脂肪代謝向上\n"
                    f"目標ペース: {z_info['label']}  推定距離: {km}km\n"
                    f"({zone_tag} / TP={_fmt_pace(int(tp))}/km 基準)\n"
                    f"📚 Seiler: 80%のセッションをZ1-Z2で。会話できるペースを守る\n"
                )
            elif _type == 1:
                workout_doc = "\n".join([
                    f"Easy Run",
                    f"- {dur - 8}m {zone_tag}",
                    "",
                    "Strides 6x",
                    f"- 20s {stride_p}",
                    f"- 40s {jog_p}",
                ])
                desc_text = (
                    f"【Z2+ストライド】{dur}分\n"
                    f"目的: 有酸素 + 神経筋活性化\n"
                    f"Z2 {dur-8}分 → 20秒ストライド × 6本(40秒ジョグ)\n"
                    f"📚 ストライドは「速く走れる感覚」を身体に思い出させる低コストドリル\n"
                )
            elif _type == 2:
                workout_doc = "\n".join([
                    "Long Easy Run",
                    f"- {dur // 4}m Z1 Pace",
                    "",
                    "Long Main",
                    f"- {dur - dur // 4 - 5}m Z2 Pace",
                    "",
                    "Finish",
                    f"- 5m Z1 Pace",
                ])
                desc_text = (
                    f"【ロング走】{dur}分\n"
                    f"目的: 有酸素基盤・脂肪燃焼・精神的持久力\n"
                    f"最初{dur//4}分Z1 → {dur - dur//4 - 5}分Z2 → 5分Z1\n"
                    f"推定距離: {km}km\n"
                    f"📚 Stöggl 2014: 低強度長時間はミトコンドリア密度を最も効率よく向上\n"
                )
            elif _type == 3:
                # Type D: ドリルラン (Barnes 2015: ランニングエコノミー改善)
                drill_min = min(8, dur // 5)
                easy_min = dur - drill_min
                workout_doc = "\n".join([
                    "Drills",
                    f"- {drill_min}m Z1 Pace",
                    "",
                    "Easy Run",
                    f"- {easy_min}m {zone_tag}",
                ])
                desc_text = (
                    f"【ドリル+イージーラン】{dur}分\n"
                    f"目的: ランニングフォーム改善・エコノミー向上\n"
                    f"ドリル{drill_min}分(Aスキップ/Bスキップ/バインディング/ハイニー) → Z2 {easy_min}分\n"
                    f"📚 Barnes 2015 Sports Med: ドリル継続でストライドコンタクト時間が短縮\n"
                    f"📚 Baechle 2008: ランドリルは神経筋コーディネーションを直接改善\n"
                )
            else:
                # Type E: Z1→Z2プログレッシブ (aerobic base building)
                seg1 = dur // 2; seg2 = dur - seg1
                workout_doc = "\n".join([
                    "Aerobic Phase 1",
                    f"- {seg1}m Z1 Pace",
                    "",
                    "Aerobic Phase 2",
                    f"- {seg2}m Z2 Pace",
                ])
                desc_text = (
                    f"【プログレッシブ有酸素走】{dur}分\n"
                    f"目的: 有酸素基盤強化 + 後半の微加速習慣\n"
                    f"前半{seg1}分Z1({zone_tag}) → 後半{seg2}分Z2\n"
                    f"推定距離: {km}km\n"
                    f"📚 低強度から徐々に上げることで乳酸をゆっくり産生→回収の循環を作る\n"
                )

        if goal_targets and goal_targets.get("race_run_pace"):
            rp = goal_targets["race_run_pace"]
            desc_text += f"\n🎯 レース目標ペース: {_pace_to_icu(rp)} — 今日は基礎を積みます"

        return workout_doc, desc_text

    # ─── BIKE ─────────────────────────────────────────────────────
    # エビデンス参考文献:
    #   Coggan & Hunter 2003 (Training and Racing with a Power Meter): FTPゾーン定義
    #   Neal et al. 2013 (J Appl Physiol): 偏極化でVO2max・閾値とも向上
    #   Laursen & Jenkins 2002 (Sports Med): HIIT効果の包括的レビュー
    #   Rønnestad et al. 2014 (Int J Sports Physiol Perf): ミクロバースト(40/20)の有効性
    #   Seiler & Tønnessen 2009 (Int J Sports Physiol Perf): エリート持久系選手の強度配分
    #   Friel 2009 "The Cyclist's Training Bible": TSS/IF/SST理論
    #   Skiba et al. 2012 (J Sports Sci): W'bal(無酸素作業容量)とFTPの関係
    #   Vogt et al. 2008 (Int J Sports Med): プロレーサーの競技パワー配分
    #   Abbiss & Laursen 2008 (Sports Med): ペーシング戦略とサイクリングパフォーマンス
    #   Jeukendrup 2011 (J Sports Sci): 長時間運動中の糖質補給タイミング
    # Garmin FIT互換:
    #   "ramp"構文はGarminで線形変化パワーターゲットとして表示される
    #   "Watt値"はGarmin Connect → デバイスへ直接数値として送信される
    #   intervals.icu Settings > Garmin でワークアウト自動sync設定可能
    #   Garmin ConnectのBike Workout形式: stepType(warmup/active/rest/cooldown)
    #     targetType(power.3s/power.10s/power.30s/cadence/heart.rate/no.target)
    #     endConditionType(time/distance/calories/lapButton/iterations)
    #   パワーターゲット指定はGarmin Edge 530/830/1030/1040等のサイコンに対応
    #   "ramp"構文(例: "- 10m ramp 100w-220w")はGarmin上でpower ramp step として表示
    #   ケイデンス指定(例: "90rpm")はGarmin Cadence Alert として補助ターゲットに変換
    #   TSS (Training Stress Score) 計算式: TSS = (sec × NP × IF) / (FTP × 3600) × 100
    #   intervals.icu の "rpe" フィールドがGarmin Connect感覚的強度(RPE)フィールドと対応
    if sport == "bike":
        pct_ranges = {
            "recovery": (0.50, 0.55),
            "easy":     (0.56, 0.75),
            "moderate": (0.81, 0.90),
            "hard":     (0.95, 1.05),
        }
        lo_pct, hi_pct = pct_ranges.get(intensity, (0.56, 0.75))
        lo_w  = int(ftp * lo_pct)
        hi_w  = int(ftp * hi_pct)
        wu_w  = int(ftp * 0.55)
        cd_w  = int(ftp * 0.50)

        import hashlib as _hlib
        _seed_val = int(_hlib.md5(f"{phase}|{dur}|{intensity}|bike".encode()).hexdigest()[:8], 16)

        if intensity == "hard":
            # 7タイプ: A=閾値8分, B=VO2max5分, C=オーバー/アンダー, D=20minSS, E=30秒スプリント
            #           F=ミクロバースト40/20 (Rønnestad 2014), G=FTPラダー(Friel 2009)
            _type = _seed_val % 7
            wu = 10; cd = 5; main = dur - wu - cd
            wu_ramp_lo = int(ftp * 0.45)
            rest_w     = int(ftp * 0.50)
            z6_lo      = int(ftp * 1.15); z6_hi = int(ftp * 1.25)
            over_w     = int(ftp * 1.10); under_w = int(ftp * 0.88)
            ss_lo      = int(ftp * 0.88); ss_hi = int(ftp * 0.93)
            sprint_w   = int(ftp * 1.50)
            micro_w    = int(ftp * 1.30)  # ミクロバースト出力
            ladder1    = int(ftp * 0.90); ladder2 = int(ftp * 1.00); ladder3 = int(ftp * 1.08)

            if _type == 0:
                reps = max(2, min(5, main // 11))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 85-95rpm",
                    "",
                    f"Threshold {reps}x",
                    f"- 8m {lo_w}w-{hi_w}w 88-92rpm",
                    f"- 3m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【閾値インターバル】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu}分ランプ → {lo_w}W(FTP{int(lo_w/ftp*100)}%)-{hi_w}W(FTP{int(hi_w/ftp*100)}%) × 8分 × {reps}本(rest 3分) → CD {cd}分\n"
                    f"📚 Coggan Z4: FTP95-105%で乳酸処理能力を鍛える"
                )
            elif _type == 1:
                reps = max(3, min(6, main // 8))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    f"VO2max {reps}x",
                    f"- 5m {z6_lo}w-{z6_hi}w 90-95rpm",
                    f"- 3m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【VO2maxインターバル】{dur}分\n"
                    f"目的: 最大酸素摂取量向上\n"
                    f"WU {wu}分 → {z6_lo}W(FTP{int(z6_lo/ftp*100)}%)-{z6_hi}W(FTP{int(z6_hi/ftp*100)}%) × 5分 × {reps}本(rest 3分) → CD {cd}分\n"
                    f"📚 Laursen 2002: HIIT(VO2max付近)は4週で顕著な適応が起こる"
                )
            elif _type == 2:
                reps = max(2, min(4, main // 10))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90rpm",
                    "",
                    f"Over-Under {reps}x",
                    f"- 3m {over_w}w 88-92rpm",
                    f"- 3m {under_w}w 88-92rpm",
                    f"- 2m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【オーバー/アンダー】{dur}分\n"
                    f"目的: 乳酸バッファリング・閾値付近での持続力\n"
                    f"WU {wu}分 → {over_w}W(FTP{int(over_w/ftp*100)}%)/{under_w}W(FTP{int(under_w/ftp*100)}%) 交互 × {reps}本 → CD {cd}分\n"
                    f"📚 閾値を超えた乳酸をアンダー区間で回収する反復で閾値を引き上げる"
                )
            elif _type == 3:
                reps = max(1, min(3, main // 25))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    f"Sweet Spot {reps}x",
                    f"- 20m {ss_lo}w-{ss_hi}w 88-92rpm",
                    f"- 5m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【20分スイートスポット】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu}分 → {ss_lo}W-{ss_hi}W(FTP{int(ss_lo/ftp*100)}-{int(ss_hi/ftp*100)}%) × 20分 × {reps}本 → CD {cd}分\n"
                    "📚 Coggan: SST = 疲労少なくFTP向上できるコスパ最高のゾーン"
                )
            elif _type == 4:
                reps = max(6, min(10, main // 3))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    f"Sprints {reps}x",
                    f"- 30s {sprint_w}w 100-110rpm",
                    f"- 2m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【30秒スプリント】{dur}分\n"
                    f"目的: 最大出力・神経筋パワー\n"
                    f"WU {wu}分 → {sprint_w}W(FTP{int(sprint_w/ftp*100)}%) × 30秒 × {reps}本(rest 2分) → CD {cd}分\n"
                    f"📚 偏極化高強度20%側: 全力スプリントでfast-twitch筋を最大刺激\n"
                )
            elif _type == 5:
                # Type F: ミクロバースト40/20 (Rønnestad et al. 2014)
                bursts = max(10, min(20, main * 60 // 60))  # ~1分/セット
                sets = max(2, min(4, main // 15))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    f"Micro Bursts {sets}x",
                    f"- 10x [40s {micro_w}w / 20s {rest_w}w]",
                    f"- 5m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【ミクロバースト 40/20】{dur}分\n"
                    f"目的: VO2max向上・無酸素容量W'balの拡大\n"
                    f"WU {wu}分 → [40秒{micro_w}W/20秒{rest_w}W]×10 × {sets}セット → CD {cd}分\n"
                    f"📚 Rønnestad 2014 Int J Sports Physiol Perf: 40/20プロトコルは\n"
                    f"     通統的5分HIITよりVO2max改善効率が高い\n"
                    f"💡 Garmin ConnectのHIIT Workout形式と互換 / Edge上でCadence Alert推奨\n"
                )
            else:
                # Type G: FTPラダー (Friel 2009 Cyclist's Training Bible)
                seg = max(5, main // 3)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90rpm",
                    "",
                    "Ladder Step 1",
                    f"- {seg}m {ladder1}w 90rpm",
                    "",
                    "Ladder Step 2",
                    f"- {seg}m {ladder2}w 90rpm",
                    "",
                    "Ladder Step 3",
                    f"- {seg}m {ladder3}w 88rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【FTPラダー】{dur}分\n"
                    f"目的: 閾値前後のパワー帯への適応・AT付近の耐性向上\n"
                    f"WU {wu}分 → {ladder1}W(FTP90%,{seg}分)→{ladder2}W(FTP100%,{seg}分)→{ladder3}W(FTP108%,{seg}分) → CD {cd}分\n"
                    "📚 Friel 2009: ラダー型セッションは疲労蓄積と閾値刺激を同時に与える"
                )

        elif intensity == "moderate":
            # 5タイプ: A=SSTインターバル, B=テンポ連続, C=ピラミッド
            #           D=SS→Z4フィニッシャー, E=高ケイデンスドリル+Z3
            _type = _seed_val % 5
            wu = 10; cd = 5; main = dur - wu - cd
            wu_ramp_lo = int(ftp * 0.45)
            rest_w = int(ftp * 0.50)
            tempo_lo = int(ftp * 0.76); tempo_hi = int(ftp * 0.88)
            z4_lo = int(ftp * 0.86); z4_hi = int(ftp * 0.95)
            z3_lo = int(ftp * 0.76); z3_hi = int(ftp * 0.85)

            if _type == 0:
                block = 12 if dur < 75 else 15 if dur < 100 else 18
                reps  = max(2, min(4, main // (block + 4)))
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    f"Sweet Spot {reps}x",
                    f"- {block}m {lo_w}w-{hi_w}w 88-92rpm",
                    f"- 4m {rest_w}w 90rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【スイートスポット】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu}分 → {lo_w}W({int(lo_w/ftp*100)}%FTP)-{hi_w}W({int(hi_w/ftp*100)}%FTP) × {block}分 × {reps}本(rest 4分) → CD {cd}分\n"
                    f"📚 スイートスポット(FTP81-90%)はFTP向上効率が最も高いゾーン\n"
                )
            elif _type == 1:
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90rpm",
                    "",
                    "Tempo",
                    f"- {main}m {tempo_lo}w-{tempo_hi}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【テンポライド】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu}分 → テンポ{main}分({tempo_lo}-{tempo_hi}W, FTP{int(tempo_lo/ftp*100)}-{int(tempo_hi/ftp*100)}%) → CD {cd}分\n"
                    "「ちょっとキツい」強度を長時間維持する能力を鍛える"
                )
            elif _type == 2:
                seg = main // 3
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90rpm",
                    "",
                    "Pyramid Phase 1",
                    f"- {seg}m {z3_lo}w-{z3_hi}w 90rpm",
                    "",
                    "Pyramid Phase 2",
                    f"- {seg}m {z4_lo}w-{z4_hi}w 90rpm",
                    "",
                    "Pyramid Phase 3",
                    f"- {seg}m {z3_lo}w-{z3_hi}w 88rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【ピラミッドライド】{dur}分\n"
                    f"目的: ペース変化への適応\n"
                    f"WU {wu}分 → Z3({z3_lo}-{z3_hi}W, FTP{int(z3_lo/ftp*100)}-{int(z3_hi/ftp*100)}%, {seg}分)→Z4({z4_lo}-{z4_hi}W, FTP{int(z4_lo/ftp*100)}-{int(z4_hi/ftp*100)}%, {seg}分)→Z3({seg}分) → CD {cd}分\n"
                    f"強度の上げ下げでオーバー/アンダーリカバリー両方を体験"
                )
            elif _type == 3:
                # Type D: SS→Z4フィニッシャー (Friel SST派生)
                ss_seg = main * 2 // 3
                z4_seg = main - ss_seg
                ss_w = int(ftp * 0.90)
                z4_w = int(ftp * 0.97)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90-95rpm",
                    "",
                    "Sweet Spot",
                    f"- {ss_seg}m {ss_w}w 90rpm",
                    "",
                    "Z4 Finisher",
                    f"- {z4_seg}m {z4_w}w 88rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【SS→Z4フィニッシャー】{dur}分\n"
                    f"目的: SSTから閾値への橋渡し・後半の粘り強さ\n"
                    f"WU {wu}分 → SS {ss_w}W(FTP{int(ss_w/ftp*100)}%, {ss_seg}分) → Z4 {z4_w}W(FTP{int(z4_w/ftp*100)}%, {z4_seg}分) → CD {cd}分\n"
                    f"📚 Friel 2009: SSTで疲労蓄積後に閾値刺激を入れると閾値向上が加速\n"
                    f"💡 後半はケイデンスを88rpm以下に落とさないよう意識"
                )
            else:
                # Type E: 高ケイデンスドリル + Z3 (神経筋効率・ペダリング改善)
                drill_min = min(10, main // 4)
                z3_seg = main - drill_min
                hi_cad_w = int(ftp * 0.65)  # 高ケイデンス時はパワーを下げる
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {wu_ramp_lo}w-{wu_w}w 90rpm",
                    "",
                    "Cadence Drill",
                    f"- {drill_min}m {hi_cad_w}w 100-110rpm",
                    "",
                    "Tempo Z3",
                    f"- {z3_seg}m {z3_lo}w-{z3_hi}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- {cd}m ramp {wu_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【高ケイデンスドリル+Z3テンポ】{dur}分\n"
                    f"目的: ペダリング効率向上・神経筋スムーズ化\n"
                    f"WU {wu}分 → ケイデンスドリル100-110rpm {drill_min}分 → Z3テンポ{z3_seg}分 → CD {cd}分\n"
                    f"📚 Vogt 2008: 高ケイデンス(>100rpm)ドリルはペダリング効率を改善\n"
                    f"💡 Garmin ConnectのCadence Alertを100-110rpmにセット推奨\n"
                )

        elif intensity == "easy":
            # 4タイプ: A=Z2ステディ, B=Z2+テンポスニペット
            #           C=ワンレッグドリル+Z2, D=ロングZ2(補給練習)
            _type = _seed_val % 4
            wu = 10; main = dur - wu - 5
            z3_w = int(ftp * 0.82)
            snip = min(10, main // 4)
            main1 = (main - snip) // 2; main2 = main - snip - main1

            if _type == 0:
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {int(ftp*0.45)}w-{lo_w}w 90-95rpm",
                    "",
                    "Endurance",
                    f"- {main}m {lo_w}w-{hi_w}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- 5m ramp {hi_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【Z2エンデュランス】{dur}分\n"
                    f"目的: {purpose}\n"
                    f"WU {wu}分ランプ → Z2 {lo_w}-{hi_w}W {main}分 → CD 5分\n"
                    f"会話できる強度。ケイデンス88-92rpm。\n"
                    f"📚 低強度量がミトコンドリア密度と脂肪酸化を向上させる\n"
                )
            elif _type == 1:
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {int(ftp*0.45)}w-{lo_w}w 90rpm",
                    "",
                    "Endurance Phase 1",
                    f"- {main1}m {lo_w}w-{hi_w}w 88-92rpm",
                    "",
                    "Tempo Snippet",
                    f"- {snip}m {z3_w}w 88rpm",
                    "",
                    "Endurance Phase 2",
                    f"- {main2}m {lo_w}w-{hi_w}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- 5m ramp {hi_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【Z2+テンポスニペット】{dur}分\n"
                    f"目的: {purpose} + 閾値刺激\n"
                    f"Z2 → 中盤 {z3_w}W({snip}分) → Z2 → CD\n"
                    f"💡 長い有酸素の中に短い閾値刺激を差し込む\n"
                )
            elif _type == 2:
                # Type C: ワンレッグドリル (ペダリング効率・弱い脚の均等化)
                drill_min = min(8, main // 5)
                z2_seg = main - drill_min
                drill_w = int(ftp * 0.55)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {int(ftp*0.45)}w-{lo_w}w 90rpm",
                    "",
                    "One Leg Drill",
                    f"- {drill_min}m {drill_w}w 60-70rpm",
                    "",
                    "Endurance",
                    f"- {z2_seg}m {lo_w}w-{hi_w}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- 5m ramp {hi_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【ワンレッグドリル+Z2】{dur}分\n"
                    f"目的: ペダリング均等化・死点克服・神経筋効率\n"
                    f"WU {wu}分 → ワンレッグ各30秒×交互 {drill_min}分 → Z2 {z2_seg}分 → CD 5分\n"
                    f"📚 ワンレッグドリルは引き足・プッシュの両フェーズ均等活性化に有効\n"
                    f"💡 Garmin Pedaling Dynamicsでバランス確認推奨 (Vector/Favero対応)\n"
                )
            else:
                # Type D: ロングZ2 (補給戦略の練習 Jeukendrup 2011)
                z2_lo_w = lo_w; z2_hi_w = hi_w
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu}m ramp {int(ftp*0.45)}w-{z2_lo_w}w 90rpm",
                    "",
                    "Long Endurance",
                    f"- {main}m {z2_lo_w}w-{z2_hi_w}w 88-92rpm",
                    "",
                    "Cooldown",
                    f"- 5m ramp {z2_hi_w}w-{cd_w}w 85rpm",
                ])
                desc_text = (
                    f"【ロングZ2エンデュランス(補給練習)】{dur}分\n"
                    f"目的: 有酸素基盤・脂肪代謝・補給タイミングの体得\n"
                    f"WU {wu}分 → Z2 {z2_lo_w}-{z2_hi_w}W {main}分 → CD 5分\n"
                    f"📚 Jeukendrup 2011: 60分超のライドでは45-60分毎に30-60g/hの補給が有効\n"
                    f"💡 45分毎にジェルまたはバー補給。レース補給戦略のシミュレーション\n"
                    f"💡 Garmin Connect: ワークアウトにAuto Lap (30min)を設定して補給リマインド\n"
                )

        else:  # recovery
            workout_doc = "\n".join([
                "Recovery Ride",
                f"- {dur}m {lo_w}w-{hi_w}w 90-100rpm",
            ])
            desc_text = (
                f"【{label}】リカバリーライド {dur}分\n"
                f"目的: {purpose}\n"
                f"目標パワー: {lo_w}-{hi_w}W (FTPの{int(lo_pct*100)}-{int(hi_pct*100)}%)\n"
                f"脚を回すだけ。心拍120以下・力まない。\n"
            )

        if goal_targets and goal_targets.get("race_bike_w"):
            rw = goal_targets["race_bike_w"]
            desc_text += f"\n🎯 レース目標NP: {rw}W — 今日は{lo_w}-{hi_w}Wで土台を積みます"

        return workout_doc, desc_text


    # ─── YOGA / MOBILITY ───────────────────────────────────────────
    # エビデンス参考文献:
    #   Mikkola et al. 2011: ストレッチ・可動性がランエコノミーを改善
    #   Behm & Chaouachi 2011 (Eur J Appl Physiol): 動的ストレッチの効果
    #   Cramer et al. 2013 (Clinical Rehab): ヨガがVO2maxと柔軟性を向上
    # intervals.icu: yoga/mobilityはworkout_doc=テキストのみ (FIT非対応)
    # Garmin: yoga活動はFIT形式ではなくテキスト説明のみ
    if sport in ("yoga", "mobility", "stretch"):
        import hashlib as _hlib
        _seed_val = int(_hlib.md5(f"{phase}|{dur}|{intensity}|yoga".encode()).hexdigest()[:8], 16)
        _type = _seed_val % 4

        if _type == 0:
            # アクティブリカバリーヨガ (疲労回復・副交感神経活性化)
            workout_doc = "\n".join([
                f"Active Recovery Yoga {dur}min",
                "",
                "Warmup ~5min",
                "- 猫・牛のポーズ (Cat-Cow) 10回",
                "- 子供のポーズ (Child's Pose) 30秒",
                "",
                "Main Sequence",
                "- 下向きの犬 (Downward Dog) 45秒",
                "- ランジ+ツイスト (Low Lunge+Twist) 30秒/側",
                "- 鳩のポーズ (Pigeon Pose) 60秒/側",
                "- 仰向けの脊椎ツイスト 45秒/側",
                "- 橋のポーズ (Bridge) 30秒 × 3",
                "",
                "Cooldown",
                "- サバサナ (Savasana) 5分",
            ])
            desc_text = (
                f"【アクティブリカバリーヨガ】{dur}分\n"
                f"目的: 疲労回復・副交感神経活性化・可動域回復\n"
                f"📚 Cramer 2013: ヨガはVO2max向上と柔軟性改善に有効\n"
                f"💡 呼吸に集中しながら、力を抜いて行う\n"
            )
        elif _type == 1:
            # トライアスロン特化モビリティ (股関節・肩・体幹)
            workout_doc = "\n".join([
                f"Triathlon Mobility {dur}min",
                "",
                "Hip Flexor Release ~10min",
                "- ヒップフレクサーストレッチ 60秒/側",
                "- 90/90 ヒップストレッチ 45秒/側",
                "- レッグスイング前後 10回/側",
                "",
                "Shoulder & Back ~10min",
                "- ドアフレーム胸ストレッチ 30秒 × 3",
                "- 胸椎回旋ストレッチ 10回/側",
                "- バンドプルアパート 15回 × 3",
                "",
                "Run-Specific ~10min",
                "- ハムストレッチ立位 45秒/側",
                "- カーフストレッチ (壁) 45秒/側",
                "- アキレス腱回し 10回/側",
            ])
            desc_text = (
                f"【トライアスロン特化モビリティ】{dur}分\n"
                f"目的: スイム・バイク・ランに直結する可動域改善\n"
                f"📚 Mikkola 2011: 可動性訓練がランニングエコノミーを改善\n"
                f"💡 筋肉を引っ張るのではなく関節の動きに注目して行う\n"
            )
        elif _type == 2:
            # 動的ストレッチ + コアアクティベーション
            workout_doc = "\n".join([
                f"Dynamic Stretch + Core Activation {dur}min",
                "",
                "Dynamic Warmup ~8min",
                "- レッグスイング前後・横 10回/側",
                "- アームサークル大 15回/方向",
                "- インチワーム 8回",
                "- ラテラルランジ 10回/側",
                "",
                "Core Activation ~12min",
                "- デッドバグ 10回/側 × 3",
                "- クラムシェル(バンド) 15回/側 × 3",
                "- パルオフプレス(バンド) 12回/側 × 3",
                "",
                "Cool Stretch ~5min",
                "- ピジョンポーズ 45秒/側",
                "- 子供のポーズ 30秒",
            ])
            desc_text = (
                f"【動的ストレッチ+コア活性化】{dur}分\n"
                f"目的: 怪我予防・次セッション前の神経筋準備\n"
                f"📚 Behm & Chaouachi 2011: 動的ストレッチは静的より実施後のパフォーマンスを高く保つ\n"
                f"💡 トレーニング前日の夜や、翌日のセッション前に特に有効\n"
            )
        else:
            # フォームローラー + ディープストレッチ (筋膜リリース)
            workout_doc = "\n".join([
                f"Foam Roll + Deep Stretch {dur}min",
                "",
                "Foam Rolling ~10min",
                "- 大腿四頭筋ローリング 2分/側",
                "- IT バンドローリング 2分/側",
                "- 背中・広背筋ローリング 3分",
                "",
                "Deep Stretch ~15min",
                "- ハーフスプリットストレッチ 60秒/側",
                "- リクライニングバタフライ 2分",
                "- スフィンクスポーズ (腰・腹部) 2分",
                "- 対角ストレッチ 30秒 × 4",
                "",
                "Breathwork",
                "- 腹式呼吸 4-7-8 × 5サイクル",
            ])
            desc_text = (
                f"【筋膜リリース+ディープストレッチ】{dur}分\n"
                f"目的: 筋膜リリース・睡眠前リラクゼーション・翌日の疲労軽減\n"
                f"📚 筋膜リリースは関節可動域を即座に改善し、翌日のパフォーマンスを保護\n"
                f"💡 就寝前30〜60分に実施すると睡眠の質も向上\n"
            )

        return workout_doc, desc_text

    # ─── SWIM ─────────────────────────────────────────────────────
    # エビデンス参考文献:
    #   Pla et al. 2021 (Front Physiol): 偏極化80/20トレーニングの有効性
    #   Toubekis & Tokmakidis 2013 (J Strength Cond Res): CSS法による強度配分
    #   Olbrecht 2000 "The Science of Winning": 乳酸プロファイルに基づくスイムゾーン
    #   Costill et al. 1992 (J Appl Physiol): 泳ぎのボリュームと強度のトレードオフ
    #   Rodríguez et al. 2003 (J Sports Med Phys Fitness): スイム特異的VO2max
    #   Aspenes & Karlsen 2012 (Sports Med): マスタースイマーへの強度介入効果
    #   Fernandes & Vilas-Boas 2012 (J Aquat Sport Res): 呼吸リズムとパフォーマンス
    #   Seiler 2010 (Int J Sports Physiol Perf): 持久系スポーツ共通の偏極化エビデンス
    # Garmin互換:
    #   Garmin Swim 2 / Garmin Forerunner 945/955/965 はPool Swim Workoutに対応
    #   intervals.icu の swim workout_doc → Garmin Connect Pool Workout に変換される
    #   distances("100mtr"/"200mtr") → Garmin Pool Swim Distance steps に対応
    #   rest time("20s rest") → Garmin Rest step に対応
    #   Garmin Swim Workoutの構成: warmup/active/rest/cooldown steps
    #   Garmin ConnectのCritical Swim Speedを設定すると pace zone が自動計算される
    #   intervals.icu Settings > Garmin でスイムワークアウトの自動sync対応
    #   Garmin Pool Swim の距離設定: 25m/33.3m/50m プールに対応
    if sport == "swim":
        # CSS (Critical Swim Speed) — 優先順位: 引数css > goal_targets > デフォルト125
        # fetch_athlete_data() で intervals.icu から自動取得して渡される
        _css = css or (goal_targets.get("race_swim_css") if goal_targets else None) or 125

        # ゾーン別ペース (秒/100m)
        css_z1  = _css * 1.25   # リカバリー  ~2:36/100m
        css_z2  = _css * 1.15   # 有酸素      ~2:24/100m
        css_z3  = _css * 1.05   # テンポ      ~2:11/100m
        css_z4  = _css * 0.98   # 閾値インターバル ~2:03/100m

        # 推定総距離 (ゾーン別平均ペースで算出)
        avg_pace = {"recovery": css_z1, "easy": css_z2, "moderate": css_z3, "hard": css_z4}.get(intensity, css_z2)
        # total_m: dur(分) * 60(秒/分) / avg_pace(秒/100m) * 100(m) = 総距離m
        # avg_paceは秒/100m単位であることを保証
        _ap = float(avg_pace)
        if _ap < 60:   # 明らかに秒/kmで混入している場合(60秒/km未満はあり得ない)
            _ap = _ap * 100  # 秒/km → 秒/100m に補正
        total_m = min(10000, (int(dur * 60 / _ap * 100) // 50) * 50)  # 50m単位・上限10km

        # ──────────────────────────────────────────────────────────────
        # スイムメニュー生成 (偏極化80/20, CSS基準, セッション多様化)
        # 参考: Pla et al. 2021 Frontiers Physiology / Swim Smooth CSS法
        # intensity: hard=インターバル, moderate=テンポ, easy=有酸素, recovery=回復
        # ──────────────────────────────────────────────────────────────

        # セッション種別: phase + dur の組み合わせで毎回異なるバリエーションを選択
        # 週ごと・フェーズごとに変わるよう設計
        import hashlib as _hlib
        _seed_str = f"{phase}|{dur}|{intensity}"
        _seed_val = int(_hlib.md5(_seed_str.encode()).hexdigest()[:8], 16)

        if intensity == "hard":
            # 4タイプをローテーション
            # Type A: CSS インターバル 100m×N (20s rest) — スピード持久
            # Type B: ピラミッド 50-100-200-100-50 — 変化をつけた強度
            # Type C: 200m×N (30s rest) — 乳酸処理
            # Type D: 50mスプリント×N (45s rest) — 最大速度刺激 (偏極化20%側)
            _type = _seed_val % 4

            wu_m = 400; cd_m = 200
            main_m = max(400, total_m - wu_m - cd_m)
            p_wu_i = _swim_pace_icu(css_z2)
            p_cd_i = _swim_pace_icu(css_z1)
            p_wu   = _swim_pace(css_z2)

            if _type == 0:  # CSS 100mインターバル
                reps = min(max(6, main_m // 100), 20)
                p_int_i = _swim_pace_icu(css_z4)
                p_int   = _swim_pace(css_z4)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"CSS Intervals {reps}x",
                    f"- 100mtr {p_int_i}",
                    "- 20s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                ])
                desc_text = (
                    f"【CSSインターバル】{dur}分 / 推定{total_m}m\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_m}m → 100m({p_int}) ×{reps}本 rest20秒 → CD {cd_m}m\n"
                    f"CSS: {_swim_pace(_css)} ← 全本イーブンペースが鍵\n"
                    f"📚 Swim Smooth: CSSの20秒レストで乳酸耐性を高める\n"
                )
            elif _type == 1:  # ピラミッド
                p_fast_i = _swim_pace_icu(css_z4)
                p_fast   = _swim_pace(css_z4)
                pyramid = [50, 100, 200, 100, 50]
                pyr_lines = []
                for d in pyramid:
                    pyr_lines.append(f"- {d}mtr {p_fast_i}")
                    pyr_lines.append("- 20s Rest")
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    "Pyramid Set",
                    *pyr_lines,
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                ])
                desc_text = (
                    f"【ピラミッドスイム】{dur}分 / 推定{total_m}m\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_m}m → 50-100-200-100-50m({p_fast}) each rest20s → CD {cd_m}m\n"
                    f"200mで距離感覚を掴み、降りで乳酸をプッシュ\n"
                )
            elif _type == 2:  # 200m スレッショルド
                reps200 = max(3, main_m // 200)
                p_t_i = _swim_pace_icu(css_z3)
                p_t   = _swim_pace(css_z3)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"Threshold Set {reps200}x",
                    f"- 200mtr {p_t_i}",
                    "- 30s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                ])
                desc_text = (
                    f"【200mスレッショルド】{dur}分 / 推定{total_m}m\n"
                    f"目的: {purpose}\n"
                    f"WU {wu_m}m → 200m({p_t}) ×{reps200}本 rest30秒 → CD {cd_m}m\n"
                    f"CSS+5秒/100m のテンポ維持 — 後半落ちたら一本減らす\n"
                )
            else:  # 50mスプリント (偏極化の高強度側)
                reps50 = min(12, max(6, main_m // 50))
                p_sp_i = _swim_pace_icu(css_z4 * 0.92)  # CSS×0.92 = ほぼ全力
                p_sp   = _swim_pace(css_z4 * 0.92)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"Sprint Set {reps50}x",
                    f"- 50mtr {p_sp_i}",
                    "- 45s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                ])
                desc_text = (
                    f"【50mスプリント】{dur}分 / 推定{total_m}m\n"
                    f"目的: 最大速度刺激・神経系活性化\n"
                    f"WU {wu_m}m → 50m全力({p_sp}) ×{reps50}本 rest45秒 → CD {cd_m}m\n"
                    f"📚 偏極化20%側: 毎本全力で神経系を鍛える\n"
                )

        elif intensity == "moderate":
            # 3タイプ: A=400mスレッショルド B=混合セット C=CSS-4秒テンポ
            _type = _seed_val % 3
            wu_m = 400; cd_m = 200
            main_m = max(400, total_m - wu_m - cd_m)
            p_wu_i = _swim_pace_icu(css_z2)
            p_cd_i = _swim_pace_icu(css_z1)
            p_wu   = _swim_pace(css_z2)

            if _type == 0:  # 400mスレッショルド
                reps = max(2, main_m // 400)
                p_t_i = _swim_pace_icu(css_z3)
                p_t   = _swim_pace(css_z3)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"Threshold {reps}x",
                    f"- 400mtr {p_t_i}",
                    "- 30s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                ])
                desc_text = (
                    f"【400mテンポ】{dur}分 / 推定{total_m}m\n"
                    f"400m({p_t}) ×{reps}本 / CSSより5秒遅め → フォーム崩さずに持続\n"
                )
            elif _type == 1:  # 混合セット (12×100m alternating pace)
                reps = min(12, max(6, main_m // 100))
                p_fast_i = _swim_pace_icu(css_z4)
                p_slow_i = _swim_pace_icu(css_z2)
                p_fast   = _swim_pace(css_z4)
                p_slow   = _swim_pace(css_z2)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"Mixed Pace {reps}x (alternating)",
                    f"- 100mtr {p_fast_i}",
                    "- 20s Rest",
                    f"- 100mtr {p_slow_i}",
                    "- 15s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                ])
                desc_text = (
                    f"【混合ペースセット】{dur}分 / 推定{total_m}m\n"
                    f"100m速({p_fast}) → 100m緩({p_slow}) を{reps}サイクル\n"
                    f"📚 T100 Triathlon推奨: ペース変化でレースの前後追いを想定\n"
                )
            else:  # 500-800mロングインターバル
                block = 600 if main_m >= 1200 else 500
                reps = max(2, main_m // block)
                p_lo_i = _swim_pace_icu(css_z2)
                p_lo   = _swim_pace(css_z2)
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    f"Long Endurance {reps}x",
                    f"- {block}mtr {p_lo_i}",
                    "- 20s Rest",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_cd_i}",
                ])
                desc_text = (
                    f"【ロングインターバル】{dur}分 / 推定{total_m}m\n"
                    f"{block}m({p_lo}) ×{reps}本 / 距離感・レースペース慣れ\n"
                    f"📚 swimcoachapp推奨: 500-1000mでレース距離感を養う\n"
                )

        elif intensity == "easy":
            # 3タイプ: A=Z2ステディ B=ドリル込みテクニック C=プルブイ+ドリル
            _type = _seed_val % 3
            wu_m  = min(400, total_m // 5)
            cd_m  = 200
            main_m = max(200, total_m - wu_m - cd_m)
            p_wu_i = _swim_pace_icu(css_z1)
            p_main_i = _swim_pace_icu(css_z2)
            p_wu   = _swim_pace(css_z1)
            p_main = _swim_pace(css_z2)

            if _type == 0:  # Z2ステディ
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    "Aerobic Steady",
                    f"- {main_m}mtr {p_main_i}",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_wu_i}",
                ])
                desc_text = (
                    f"【Z2エンデュランス】{dur}分 / 推定{total_m}m\n"
                    f"WU {wu_m}m → Z2 {main_m}m({p_main}) → CD {cd_m}m\n"
                    f"📚 偏極化80%側: 会話できる強度を徹底する\n"
                )
            elif _type == 1:  # テクニック+エンデュランス
                drill_m = min(200, main_m // 3)
                steady_m = main_m - drill_m
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    "Technique Drills 4x",
                    f"- {drill_m // 4}mtr {p_wu_i}",
                    "- 15s Rest",
                    "",
                    "Aerobic Steady",
                    f"- {steady_m}mtr {p_main_i}",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_wu_i}",
                ])
                desc_text = (
                    f"【テクニック+エンデュランス】{dur}分 / 推定{total_m}m\n"
                    f"ドリル({drill_m // 4}m×4本) → Z2 {steady_m}m\n"
                    f"ドリル例: キャッチアップ/シングルアーム/フィンガーチップドラッグ\n"
                    f"📚 Swim Smooth: ドリルは直後にフルストロークへ移行\n"
                )
            else:  # プルブイセット (上半身強化・脚疲労時に有効)
                pull_m = main_m
                workout_doc = "\n".join([
                    "Warmup",
                    f"- {wu_m}mtr {p_wu_i}",
                    "",
                    "Pull Buoy Steady",
                    f"- {pull_m}mtr {p_main_i}",
                    "",
                    "Cooldown",
                    f"- {cd_m}mtr {p_wu_i}",
                ])
                desc_text = (
                    f"【プルブイセット】{dur}分 / 推定{total_m}m\n"
                    f"WU {wu_m}m → プルブイ {pull_m}m({p_main}) → CD {cd_m}m\n"
                    f"📚 swimcoachapp: バイク/ランで脚が疲れた日は上半身重点\n"
                    f"上半身: ラット・三角筋・前腕の引き付けを意識\n"
                )

        else:  # recovery
            wu_m   = min(200, total_m // 4)
            main_m = max(100, total_m - wu_m - 100)
            p_main_i = _swim_pace_icu(css_z1)
            p_main   = _swim_pace(css_z1)
            workout_doc = "\n".join([
                "Easy Recovery Swim",
                f"- {wu_m}mtr {p_main_i}",
                "",
                "Steady Easy",
                f"- {main_m}mtr {p_main_i}",
                "",
                "Cooldown",
                f"- 100mtr {p_main_i}",
            ])
            desc_text = (
                f"【リカバリースイム】{dur}分 / 推定{total_m}m\n"
                f"全て {p_main} (Z1) — 水中ストレッチ感覚で\n"
                f"💡 キック板・プルブイ等の補助道具活用でOK\n"
            )

        return workout_doc, desc_text

    # ─── STRENGTH ──────────────────────────────────────────────────────
    if sport == "strength":
        # ペース・ワット不要。フェーズ・強度別の具体的なエクササイズを生成。
        # エビデンス: Rønnestad 2015 / Mikkola 2011 — 複合運動中心、フェーズ別強度
        from .strength import STRENGTH_DB
        level_map = {"recovery":"base","easy":"base","moderate":"build","hard":"peak"}
        level     = level_map.get(intensity, "base")
        warm_min  = min(5, dur // 6)
        cool_min  = min(3, dur // 10)
        main_min  = max(5, dur - warm_min - cool_min)

        def _ex_lines(cat, lv):
            """STRENGTH_DBからexercise行を生成"""
            rows = STRENGTH_DB.get((cat, lv)) or STRENGTH_DB.get((cat, "base")) or []
            lines = []
            for name, sets, reps, rest_s, note in rows:
                rest_str = f"{rest_s//60}分" if rest_s >= 60 else f"{rest_s}秒"
                line = f"- {name} {sets}x{reps}  rest{rest_str}"
                if note:
                    line += f"  /{note}"
                lines.append(line)
            return lines

        # フェーズ・時間に応じてフォーカスエリアを決める
        # 30分以下: core + upper, 40分: core + lower, 50分以上: core + lower + upper
        if dur <= 30:
            focus_cats = ["core", "upper"]
        elif dur <= 40:
            focus_cats = ["core", "lower"]
        else:
            focus_cats = ["core", "lower", "upper"]

        _wlines  = _ex_lines("warmup",   level) or ["- ジャンピングジャック 30秒", "- グルートブリッジ 20回", "- バードドッグ 10回/側"]
        _cdlines = _ex_lines("cooldown", level) or ["- ハムストレッチ 30秒/側", "- 胸・肩ストレッチ 30秒"]

        # メインセット: カテゴリごとに区切りを入れる
        _mlines = []
        cat_labels = {"core":"Core", "lower":"Lower Body", "upper":"Upper Body"}
        for cat in focus_cats:
            exlines = _ex_lines(cat, level)
            if exlines:
                n = max(2, (main_min // len(focus_cats)) // 4)  # 1種目あたり想定本数
                _mlines.append(f"{cat_labels[cat]}")
                _mlines.extend(exlines[:n+1])
                _mlines.append("")

        workout_doc = "\n".join([
            f"Strength [{level.capitalize()}] {dur}min",
            "",
            f"Warmup ~{warm_min}min",
            *_wlines,
            "",
            f"Main Set ~{main_min}min",
            *_mlines,
            "Cooldown",
            *_cdlines,
        ])

        # desc_text: 日本語の詳細説明
        cat_jp = {"core":"体幹", "lower":"下半身(バイク出力)", "upper":"上半身(スイム推進力)"}
        focus_jp = " + ".join(cat_jp.get(c, c) for c in focus_cats)
        desc_text = (
            f"【筋トレ / {level}レベル】{dur}分\n"
            f"フォーカス: {focus_jp}\n"
            f"目的: トライアスロン全種目の出力基盤強化\n"
            f"📚 Rønnestad 2015: 複合運動が持久力パフォーマンスを直接改善\n"
            f"■ ウォームアップ ({warm_min}分)\n"
        )
        for ln in _wlines:
            desc_text += f"  {ln.lstrip('- ')}\n"
        desc_text += f"\n■ メイン ({main_min}分)\n"
        for cat in focus_cats:
            exrows = STRENGTH_DB.get((cat, level)) or STRENGTH_DB.get((cat, "base")) or []
            n = max(2, (main_min // len(focus_cats)) // 4)
            desc_text += f"  [{cat_jp.get(cat, cat)}]\n"
            for name, sets, reps, rest_s, note in exrows[:n+1]:
                rs = f"{rest_s//60}分" if rest_s >= 60 else f"{rest_s}秒"
                desc_text += f"    {name} {sets}×{reps} (休憩{rs})"
                if note:
                    desc_text += f" ※{note}"
                desc_text += "\n"
            desc_text += "\n"
        desc_text += (
            f"■ クールダウン ({cool_min}分)\n"
            f"  ストレッチで可動域を維持・次のセッションへの準備\n"
        )
        return workout_doc, desc_text

    # ─── YOGA ──────────────────────────────────────────────────────────
    # エビデンス参考文献:
    #   Moran et al. 2011 (Int J Yoga): 8週ヨガでVO2max・柔軟性・バランスが向上
    #   Tanaka et al. 2014 (J Phys Ther Sci): ヨガは自律神経回復を促進
    #   Cramer et al. 2013 (Clin J Sport Med): スポーツ選手のヨガ実践と怪我予防
    #   Smith et al. 2011 (J Strength Cond Res): アスリートの可動域とパフォーマンス相関
    #   Wiese et al. 2019 (Int J Environ Res): 呼吸法(プラナヤマ)が持久力パフォーマンス向上
    #   Woodyard 2011 (Int J Yoga): ヨガの心身両面への恩恵の体系的レビュー
    #   Morgan et al. 2021 (Front Psychol): マインドフルネスとアスリートの回復品質
    # Garmin互換:
    #   Garmin ConnectのYoga Activity Type に対応 (Activity Type: yoga)
    #   Garmin Body Battery はヨガ・瞑想中の自律神経回復をスコア化
    #   インターバル走後に30分ヨガを追加するとBody Batteryの回復スコアが高まる傾向
    if sport == "yoga":
        warm_min = min(3, dur // 6)
        flow_min = max(5, dur - warm_min - 3)
        cool_min = min(3, dur - warm_min - flow_min)

        import hashlib as _hlib
        _seed_val = int(_hlib.md5(f"{phase}|{dur}|{intensity}|yoga".encode()).hexdigest()[:8], 16)

        # 強度別ヨガスタイル x フォーカス
        # 6タイプ:
        #   A: スイム&バイクリカバリー (肩/背中/股関節)
        #   B: バイク&ランリカバリー (ハムスト/腸腰/ふくらはぎ)
        #   C: 全身バランスフロー (均等可動域維持)
        #   D: ブレス&マインドフルネス (自律神経リセット, 翌日高強度前に)
        #   E: コアヨガ (プランク系ポーズでヨガ+体幹を両立)
        #   F: リストラティブヨガ (完全受動・神経系リセット)

        _type = _seed_val % 6

        if _type == 0:
            # Type A: スイム&バイクリカバリーフォーカス
            poses_main = (
                "ダウンドッグ×30秒 / チャイルドポーズ×45秒 /\n"
                "  スレッドザニードル(左右各30秒) / スフィンクスポーズ×40秒 /\n"
                "  コブラポーズ×30秒 / 肩甲骨プルバック×20回"
            )
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m breathing\n"
                f"- {flow_min}m shoulder-back flow\n"
                f"- {cool_min}m savasana\n"
            )
            desc_text = (
                f"【スイム&バイクリカバリーヨガ】{dur}分\n"
                f"目的: 肩/背中/股関節の緊張解放 → スイムストロークとバイクポジション改善\n"
                f"■ ブレス & ウォームアップ ({warm_min}分)\n"
                f"  腹式呼吸×8回 / 首回し / 肩甲骨ストレッチ\n\n"
                f"■ フォーカスフロー ({flow_min}分)\n"
                f"  {poses_main}\n\n"
                f"■ シャバアーサナ ({cool_min}分)\n"
                f"  仰向けで完全脱力。深呼吸で副交感神経を優位に\n\n"
                f"📚 Cramer 2013: 肩関節可動域の向上がスイムストローク効率に直結\n"
                f"💡 Garmin Connect: この後30分休憩するとBody Battery回復を確認推奨"
            )
        elif _type == 1:
            # Type B: バイク&ランリカバリーフォーカス
            poses_main = (
                "ランナーズランジ(左右各45秒) / 鳩のポーズ(左右各60秒) /\n"
                "  ハムストリングストレッチ(各45秒) / 腸腰筋ストレッチ(各30秒) /\n"
                "  ガス抜きポーズ / ハッピーベイビー×45秒"
            )
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m breathing\n"
                f"- {flow_min}m hip-hamstring flow\n"
                f"- {cool_min}m savasana\n"
            )
            desc_text = (
                f"【バイク&ランリカバリーヨガ】{dur}分\n"
                f"目的: 腸腰筋/ハムスト/ふくらはぎの疲労解放 → 翌日のランエコノミー改善\n"
                f"■ ブレス & ウォームアップ ({warm_min}分)\n"
                f"  腹式呼吸×8回 / 骨盤前後傾 / 足首回し\n\n"
                f"■ 下半身フォーカスフロー ({flow_min}分)\n"
                f"  {poses_main}\n\n"
                f"■ シャバアーサナ ({cool_min}分)\n"
                f"  仰向けで全身脱力\n\n"
                f"📚 Smith 2011: 股関節可動域向上はストライド長を改善してランエコノミーを向上\n"
                f"📚 Barnes 2015: ランエコノミーの鍵は股関節伸展可動域"
            )
        elif _type == 2:
            # Type C: 全身バランスフロー
            if phase in ("peak", "build"):
                poses_main = (
                    "戦士のポーズⅠ×30秒 / 戦士のポーズⅡ×30秒 / \n"
                    "  三角のポーズ(左右各30秒) / 木のポーズ(左右各30秒) /\n"
                    "  舟のポーズ×20秒×3 / 橋のポーズ×30秒"
                )
            else:
                poses_main = (
                    "猫牛のポーズ×10回 / チャイルドポーズ×45秒 /\n"
                    "  座位前屈×45秒 / 開脚前屈×45秒 /\n"
                    "  仰向けツイスト(左右各30秒) / ハッピーベイビー×30秒"
                )
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m breathing\n"
                f"- {flow_min}m balance flow\n"
                f"- {cool_min}m savasana\n"
            )
            desc_text = (
                f"【全身バランスヨガフロー】{dur}分\n"
                f"目的: 均等な可動域維持・体幹バランス強化\n"
                f"■ ブレス & ウォームアップ ({warm_min}分)\n"
                f"  腹式呼吸×5回 / 首・肩回し / 骨盤回し\n\n"
                f"■ バランスフロー ({flow_min}分)\n"
                f"  {poses_main}\n\n"
                f"■ シャバアーサナ ({cool_min}分)\n"
                f"  仰向けで全身脱力\n\n"
                f"📚 Moran 2011: 8週ヨガプログラムでバランス・柔軟性・VO2maxが有意に向上"
            )
        elif _type == 3:
            # Type D: ブレス&マインドフルネスヨガ (高強度セッション前後推奨)
            breath_min = dur // 3
            mindful_min = dur - breath_min - cool_min - warm_min
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m body scan\n"
                f"- {breath_min}m pranayama\n"
                f"- {mindful_min}m gentle flow\n"
                f"- {cool_min}m savasana\n"
            )
            desc_text = (
                f"【ブレス&マインドフルネスヨガ】{dur}分\n"
                f"目的: 自律神経リセット・HRV改善・精神的回復\n"
                f"■ ボディスキャン ({warm_min}分)\n"
                f"  頭頂→爪先まで意識を向ける / 緊張箇所を意識\n\n"
                f"■ プラナヤマ呼吸法 ({breath_min}分)\n"
                f"  4拍吸気→2拍保持→6拍呼気 (4-2-6呼吸) × 20セット\n"
                f"  ボックスブリージング(4-4-4-4) × 10セット\n\n"
                f"■ ジェントルフロー ({mindful_min}分)\n"
                f"  猫牛×10回 / チャイルドポーズ×60秒 / スープタバッダコナアーサナ\n\n"
                f"■ シャバアーサナ ({cool_min}分)\n"
                f"  完全脱力・思考を手放す\n\n"
                f"📚 Wiese 2019 Int J Environ Res: プラナヤマ呼吸法は8週で持久力パフォーマンスを向上\n"
                f"📚 Tanaka 2014: ヨガ後の副交感神経優位がHRV改善と翌日パフォーマンスに寄与\n"
                f"📚 Morgan 2021 Front Psychol: マインドフルネス実践でアスリートの回復品質向上\n"
                f"💡 Garmin HRV Status: このセッション翌朝にHRV計測してBody Battery回復確認"
            )
        elif _type == 4:
            # Type E: コアヨガ (体幹+バランス強化)
            core_min = max(10, flow_min * 2 // 3)
            flex_min = flow_min - core_min
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m breathing\n"
                f"- {core_min}m core yoga\n"
                f"- {flex_min}m flexibility\n"
                f"- {cool_min}m savasana\n"
            )
            desc_text = (
                f"【コアヨガ】{dur}分\n"
                f"目的: 体幹安定性強化 + 可動域維持の両立\n"
                f"■ ブレス & ウォームアップ ({warm_min}分)\n"
                f"  腹式呼吸×5回 / 肩甲骨ストレッチ\n\n"
                f"■ コアヨガシーケンス ({core_min}分)\n"
                f"  プランクポーズ×60秒 / サイドプランク(左右各30秒) /\n"
                f"  舟のポーズ×20秒×3 / ワイルドシング×30秒(左右) /\n"
                f"  四肢のポーズ→ダウンドッグ往復×5\n\n"
                f"■ 柔軟性フロー ({flex_min}分)\n"
                f"  鳩のポーズ(左右各45秒) / 座位前屈×45秒 / 仰向けツイスト\n\n"
                f"■ シャバアーサナ ({cool_min}分)\n"
                f"  仰向けで全身脱力\n\n"
                f"📚 Cramer 2013: コアポーズの組み合わせで体幹筋の持続的活性化が確認"
            )
        else:
            # Type F: リストラティブヨガ (完全受動・神経系リセット)
            workout_doc = (
                f"yoga {dur}m\n"
                f"- {warm_min}m body scan\n"
                f"- {flow_min}m restorative poses\n"
                f"- {cool_min}m yoga nidra\n"
            )
            desc_text = (
                f"【リストラティブヨガ】{dur}分\n"
                f"目的: 副交感神経優位・深部筋膜の受動的リリース・神経系リセット\n"
                f"■ ボディスキャン ({warm_min}分)\n"
                f"  全身の緊張を観察するだけ\n\n"
                f"■ 受動的ポーズ ({flow_min}分)\n"
                f"  スプタバッダコナアーサナ×3分 / サポートブリッジ×3分 /\n"
                f"  レッグアップザウォール×5分 / サポートチャイルドポーズ×3分\n"
                f"  ※ブロック・ボルスター・毛布を積極的に使用\n\n"
                f"■ ヨガニドラ ({cool_min}分)\n"
                f"  全身リラクゼーション誘導 / 思考を手放す\n\n"
                f"📚 Woodyard 2011: リストラティブポーズは筋膜の受動的伸長で翌日の筋痛を軽減\n"
                f"📚 Tanaka 2014: 受動的ヨガはHRVを積極的ストレッチより顕著に改善\n"
                f"💡 ハードセッションの翌日・または週末の疲労蓄積時に特に推奨"
            )

        return workout_doc, desc_text

    # ─── フォールバック ────────────────────────────────────────────
    workout_doc = f"- {dur}m Z2\n"
    desc_text   = f"{sport} {dur}分"
    return workout_doc, desc_text


def session_desc(sport, intensity, dur, phase, tp, ftp, goal_targets=None):
    """後方互換ラッパー: build_workout の desc_text のみ返す"""
    _, desc = build_workout(sport, intensity, dur, phase, tp, ftp, goal_targets)
    return desc
