"""安全规则：任何异常都只暂停并请求确认，绝不自动增加训练量。"""

from __future__ import annotations

from .models import ABNORMAL_HR_RATIO, RESTING_HR_ELEVATION, Finding

WORKOUT_WINDOW_DAYS = 14
WELLNESS_WINDOW_DAYS = 3


def evaluate_rules(user, workouts, hr_samples, sleep_samples, fatigue_samples,
                   injuries, now):
    """逐条评估规则，返回 (findings, rules_log)。

    rules_log 记录每条规则的判定过程，随建议一起保存，供解释与事后回放。
    """
    findings = []
    rules_log = []

    def log(rule_id, summary, outcome, detail=None):
        rules_log.append({"rule_id": rule_id, "summary": summary,
                          "outcome": outcome, "detail": detail or {}})

    # 1. 异常心率：训练中心率过高，或静息心率明显高于个人基线。
    abnormal = [
        {"record_id": w.record_id, "local_date": w.local_date,
         "avg_hr": w.avg_hr, "ratio": round(w.avg_hr / user.max_hr, 3)}
        for w in workouts
        if w.avg_hr and w.avg_hr > ABNORMAL_HR_RATIO * user.max_hr
    ]
    resting = [s for s in hr_samples if s.value.get("resting")]
    elevated = [
        {"recorded_at": s.recorded_at.isoformat(), "bpm": s.value.get("bpm")}
        for s in resting
        if (s.value.get("bpm") or 0) > RESTING_HR_ELEVATION * user.resting_hr
    ]
    if abnormal or elevated:
        findings.append(Finding(
            rule_id="abnormal-heart-rate", severity="hold",
            summary="检测到异常心率，暂停加量并等待医疗确认",
            detail={"threshold_ratio": ABNORMAL_HR_RATIO,
                    "abnormal_workouts": abnormal,
                    "elevated_resting_hr": elevated,
                    "baseline_resting_hr": user.resting_hr},
            confirmer="medical"))
        log("abnormal-heart-rate", "异常心率", "hold",
            {"abnormal_workouts": len(abnormal), "elevated_resting_hr": len(elevated)})
    else:
        log("abnormal-heart-rate", "异常心率", "pass")

    # 2. 伤病标签：存在进行中的伤病标签时暂停相关安排。
    if injuries:
        findings.append(Finding(
            rule_id="active-injury", severity="hold",
            summary="存在进行中的伤病标签，暂停相关训练安排",
            detail={"labels": [t.label for t in injuries]},
            confirmer="medical"))
        log("active-injury", "伤病标签", "hold",
            {"labels": [t.label for t in injuries]})
    else:
        log("active-injury", "伤病标签", "pass")

    # 3. 时区变化：设备时区与常驻时区不一致，训练时间可能归因错误。
    if user.last_seen_tz and user.last_seen_tz != user.home_tz:
        findings.append(Finding(
            rule_id="timezone-change", severity="hold",
            summary="检测到时区变化，暂停安排并请本人确认",
            detail={"home_tz": user.home_tz, "device_tz": user.last_seen_tz},
            confirmer="runner"))
        log("timezone-change", "时区变化", "hold",
            {"home_tz": user.home_tz, "device_tz": user.last_seen_tz})
    else:
        log("timezone-change", "时区变化", "pass")

    # 4. 关键数据缺失：缺训练、睡眠、主观疲劳或心率时，不足以支撑加量。
    missing = []
    if not workouts:
        missing.append(f"近{WORKOUT_WINDOW_DAYS}天无训练记录")
    if not sleep_samples:
        missing.append(f"近{WELLNESS_WINDOW_DAYS}天无睡眠数据")
    if not fatigue_samples:
        missing.append(f"近{WELLNESS_WINDOW_DAYS}天无主观疲劳反馈")
    workouts_without_hr = [w.record_id for w in workouts if w.avg_hr is None]
    if workouts_without_hr:
        missing.append("部分训练缺少心率数据")
    if missing:
        findings.append(Finding(
            rule_id="missing-key-data", severity="hold",
            summary="关键数据缺失，暂停安排并请本人确认",
            detail={"missing": missing,
                    "workouts_without_hr": workouts_without_hr},
            confirmer="runner"))
        log("missing-key-data", "关键数据缺失", "hold", {"missing": missing})
    else:
        log("missing-key-data", "关键数据缺失", "pass")

    return findings, rules_log
