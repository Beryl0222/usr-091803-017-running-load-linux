"""HTTP 端到端：注册 → 同步 → 建议 → 暂停/确认 → 覆盖 → 回放，以及错误语义。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import health_payload, make_handler
from training.store import Store

RUNNER_BODY = {
    "runner_id": "r-api",
    "resting_hr": 60,
    "max_hr": 180,
    "baseline_weekly_load": 100,
    "groups": ["beginner"],
    "home_timezone": "Asia/Shanghai",
}


def _sync_body(runner_id="r-api", sync_id="sync-1"):
    return {
        "sync_id": sync_id,
        "source": "watch-1",
        "runner_id": runner_id,
        "device_timezone": "Asia/Shanghai",
        "sessions": [
            {"external_id": f"s{i}", "started_at": f"2026-09-{14 + i}T07:00:00+08:00",
             "duration_min": 30, "avg_hr": 120, "cadence_spm": 170}
            for i in range(1, 4)
        ],
        "dailies": [
            {"external_id": "d1", "day": "2026-09-18", "sleep_hours": 7.5,
             "fatigue_score": 3, "morning_resting_hr": 60},
        ],
    }


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None, actor=None):
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor-Id"], headers["X-Actor-Role"] = actor
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=2) as resp:
                return resp.status, json.load(resp)
        except HTTPError as error:
            payload = json.loads(error.read().decode("utf-8"))
            error.close()
            return error.code, payload

    def _make_runner(self, body=None, actor=("ops", "system")):
        return self.request("POST", "/runners", body or RUNNER_BODY, actor)

    # ---- 基础契约 ----
    def test_health_and_unknown_route(self):
        status, payload = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, health_payload())
        status, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)

    def test_missing_actor_headers_is_401(self):
        status, payload = self.request("GET", "/runners/r-api")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_invalid_json_is_400(self):
        req = Request(self.base + "/runners", data=b"{not json",
                      headers={"X-Actor-Id": "ops", "X-Actor-Role": "system"},
                      method="POST")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=2)
        self.assertEqual(ctx.exception.code, 400)
        ctx.exception.close()

    def test_unknown_runner_is_404(self):
        status, _ = self.request("GET", "/runners/ghost", actor=("ops", "system"))
        self.assertEqual(status, 404)

    # ---- 主流程 ----
    def test_full_training_flow(self):
        status, _ = self._make_runner()
        self.assertEqual(status, 201)

        # 设备同步 + 重复同步只计一次
        status, payload = self.request("POST", "/syncs", _sync_body(), ("r-api", "runner"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["ingested"], 4)
        status, payload = self.request("POST", "/syncs", _sync_body(), ("r-api", "runner"))
        self.assertEqual(payload["status"], "duplicate")

        # 他人不能代同步
        status, _ = self.request("POST", "/syncs", _sync_body(sync_id="sync-x"),
                                 ("someone-else", "runner"))
        self.assertEqual(status, 403)

        # 生成建议（初学者：敏感系数 1.2，增幅上限 8%）
        status, rec = self.request("POST", "/recommendations",
                                   {"runner_id": "r-api"}, ("r-api", "runner"))
        self.assertEqual(status, 201)
        self.assertEqual(rec["status"], "active")
        self.assertAlmostEqual(rec["recent_load"], 54.0)       # 3 × 30 × 0.5 × 1.2
        self.assertAlmostEqual(rec["target_weekly_load"], 58.32)
        self.assertIn("input_snapshot", rec)                   # 本人看完整细节
        rec_id = rec["recommendation_id"]

        # 其他跑者无权查看
        status, _ = self.request("GET", f"/recommendations/{rec_id}",
                                 actor=("other", "runner"))
        self.assertEqual(status, 403)

        # 教练无授权 → 403；授权后 → 训练摘要（无原始健康细节）
        status, _ = self.request("GET", f"/recommendations/{rec_id}", actor=("c1", "coach"))
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/runners/r-api/authorizations", {
            "grantee_role": "coach", "grantee_id": "c1",
            "expires_at": "2026-09-25T00:00:00+08:00",
        }, ("r-api", "runner"))
        self.assertEqual(status, 201)
        status, coach_view = self.request("GET", f"/recommendations/{rec_id}",
                                          actor=("c1", "coach"))
        self.assertEqual(status, 200)
        self.assertNotIn("input_snapshot", coach_view)
        self.assertIn("planned_sessions", coach_view)

        # 教练覆盖：保留原因、期限与原版本
        status, overridden = self.request("POST", f"/recommendations/{rec_id}/overrides", {
            "reason": "跑者感冒初愈，减量一周",
            "expires_at": "2026-09-22T00:00:00+08:00",
            "changes": {"target_weekly_load": 40},
        }, ("c1", "coach"))
        self.assertEqual(status, 200)
        self.assertEqual(overridden["version"], 2)
        self.assertEqual(overridden["overrides"][0]["reason"], "跑者感冒初愈，减量一周")
        self.assertAlmostEqual(
            overridden["overrides"][0]["original"]["target_weekly_load"], 58.32)

        # 覆盖缺原因 → 400
        status, _ = self.request("POST", f"/recommendations/{rec_id}/overrides", {
            "reason": "", "expires_at": "2026-09-22T00:00:00+08:00",
            "changes": {"target_weekly_load": 40},
        }, ("c1", "coach"))
        self.assertEqual(status, 400)

        # 回放：原始记录 + 风险判断 + 人工调整
        status, replay = self.request("GET", f"/recommendations/{rec_id}/replay",
                                      actor=("r-api", "runner"))
        self.assertEqual(status, 200)
        self.assertEqual(len(replay["input_snapshot"]["sessions"]), 3)
        self.assertTrue(replay["adjusted_by_human"])
        self.assertEqual(replay["human_adjustments"][0]["type"], "override")

        # 事件流：本人可见完整审计
        status, events = self.request("GET", "/runners/r-api/events",
                                      actor=("r-api", "runner"))
        self.assertEqual(status, 200)
        types = {e["type"] for e in events["events"]}
        self.assertTrue({"sync_ingested", "recommendation_created",
                         "override_applied"} <= types)

    def test_pause_and_confirm_flow(self):
        status, _ = self._make_runner({**RUNNER_BODY, "runner_id": "r-pause",
                                       "injury_tags": [{"label": "脚踝扭伤"}]})
        self.assertEqual(status, 201)
        body = _sync_body(runner_id="r-pause")
        self.request("POST", "/syncs", body, ("r-pause", "runner"))

        status, rec = self.request("POST", "/recommendations",
                                   {"runner_id": "r-pause"}, ("r-pause", "runner"))
        self.assertEqual(status, 201)
        self.assertEqual(rec["status"], "paused")              # 伤病标签 → 暂停
        self.assertIn("active_injury", rec["risk_flags"])
        self.assertTrue(all(s["suspended"] for s in rec["planned_sessions"]))
        self.assertIsNotNone(rec["confirmation_request"])
        rec_id = rec["recommendation_id"]

        # 未授权教练不能确认
        status, _ = self.request("POST", f"/recommendations/{rec_id}/confirmations",
                                 {"note": "x"}, ("c9", "coach"))
        self.assertEqual(status, 403)

        # 本人确认后恢复
        status, confirmed = self.request("POST", f"/recommendations/{rec_id}/confirmations",
                                         {"note": "复查通过"}, ("r-pause", "runner"))
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "active")
        self.assertFalse(any(s["suspended"] for s in confirmed["planned_sessions"]))

        # 已生效的建议不能重复确认
        status, _ = self.request("POST", f"/recommendations/{rec_id}/confirmations",
                                 {}, ("r-pause", "runner"))
        self.assertEqual(status, 409)


if __name__ == "__main__":
    unittest.main()
