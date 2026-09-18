"""HTTP 接口的端到端测试：安全暂停、确认、覆盖、授权视图与回放。"""

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from domain import TrainingService
from service import make_handler


class HttpApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.service = TrainingService()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.service))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = Request(self.base + path, data=data, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            with urlopen(req, timeout=2) as resp:
                return resp.status, json.load(resp)
        except HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_full_safety_flow(self):
        now = datetime.now(timezone.utc)
        status, user = self.request("POST", "/users", {
            "user_id": "web_u1", "group": "beginner", "resting_hr": 60,
            "max_hr": 180, "home_tz": "Asia/Shanghai"})
        self.assertEqual(status, 201)
        self.assertEqual(user["user_id"], "web_u1")

        # 汇集数据：一次异常心率的训练 + 睡眠 + 主观疲劳。
        status, ingested = self.request("POST", "/users/web_u1/metrics", {
            "sync_id": "sync-1", "tz": "Asia/Shanghai", "samples": [
                {"kind": "workout",
                 "recorded_at": (now - timedelta(hours=20)).isoformat(),
                 "value": {"duration_min": 30, "avg_hr": 160}},
                {"kind": "sleep",
                 "recorded_at": (now - timedelta(hours=10)).isoformat(),
                 "value": {"hours": 7}},
                {"kind": "fatigue",
                 "recorded_at": (now - timedelta(hours=3)).isoformat(),
                 "value": {"rpe": 4}},
            ]})
        self.assertEqual(status, 200)
        self.assertEqual(ingested["status"], "accepted")

        # 重复同步只计一次。
        status, dup = self.request("POST", "/users/web_u1/metrics", {
            "sync_id": "sync-1", "tz": "Asia/Shanghai", "samples": []})
        self.assertEqual(dup["status"], "duplicate")

        # 异常心率 → 暂停并等待医疗确认。
        status, rec = self.request("POST", "/users/web_u1/recommendations")
        self.assertEqual(status, 201)
        self.assertEqual(rec["status"], "held")
        self.assertEqual(rec["required_confirmations"], ["medical"])
        rec_id = rec["rec_id"]

        # 未授权者看不到他人建议；本人可见完整解释。
        status, _ = self.request("GET", f"/recommendations/{rec_id}?actor=intruder")
        self.assertEqual(status, 403)
        status, view = self.request("GET", f"/recommendations/{rec_id}?actor=web_u1")
        self.assertEqual(status, 200)
        self.assertIn("explanation", view["recommendation"])

        # 未授权的医疗顾问不能确认；本人授权后可以。
        status, _ = self.request("POST", f"/recommendations/{rec_id}/confirm",
                                 {"actor": "dr_li", "role": "medical"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/users/web_u1/grants",
                                 {"actor": "dr_li", "role": "medical",
                                  "grantee_id": "dr_li"})
        self.assertEqual(status, 403)  # 只能本人授权
        status, _ = self.request("POST", "/users/web_u1/grants",
                                 {"actor": "web_u1", "role": "medical",
                                  "grantee_id": "dr_li"})
        self.assertEqual(status, 201)
        status, confirmed = self.request("POST", f"/recommendations/{rec_id}/confirm",
                                         {"actor": "dr_li", "role": "medical",
                                          "note": "复查无异常"})
        self.assertEqual(status, 200)
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertIsNotNone(confirmed["plan_version_id"])

        # 教练覆盖：无授权 403，缺原因/期限 400，合规则保留原版本。
        status, _ = self.request("POST", "/users/web_u1/overrides", {
            "actor": "coach_wang", "reason": "减量", "sessions": [{"duration_min": 20}],
            "valid_until": (now + timedelta(days=2)).isoformat()})
        self.assertEqual(status, 403)
        self.request("POST", "/users/web_u1/grants",
                     {"actor": "web_u1", "role": "coach", "grantee_id": "coach_wang"})
        status, _ = self.request("POST", "/users/web_u1/overrides", {
            "actor": "coach_wang", "reason": "", "sessions": [{"duration_min": 20}],
            "valid_until": (now + timedelta(days=2)).isoformat()})
        self.assertEqual(status, 400)
        status, version = self.request("POST", "/users/web_u1/overrides", {
            "actor": "coach_wang", "reason": "赛前减量",
            "sessions": [{"weekday": 3, "kind": "超慢跑", "duration_min": 20}],
            "valid_until": (now + timedelta(days=2)).isoformat()})
        self.assertEqual(status, 201)
        self.assertEqual(version["supersedes"], confirmed["plan_version_id"])

        # 周负荷与回放。
        status, week = self.request("GET", "/users/web_u1/week?actor=web_u1")
        self.assertEqual(status, 200)
        self.assertGreater(week["week"]["total_load"], 0)
        status, replay = self.request("GET",
                                      f"/recommendations/{rec_id}/replay?actor=web_u1")
        self.assertEqual(status, 200)
        self.assertTrue(replay["manually_adjusted"])
        self.assertEqual(len(replay["inputs"]["workouts"]), 1)
        self.assertTrue(any(e["type"] == "override_applied"
                            for e in replay["adjustments"]))

    def test_unknown_route_and_bad_input(self):
        status, _ = self.request("GET", "/unknown")
        self.assertEqual(status, 404)
        status, _ = self.request("POST", "/users", {
            "user_id": "web_u2", "group": "不存在的人群", "resting_hr": 60,
            "max_hr": 180, "home_tz": "Asia/Shanghai"})
        self.assertEqual(status, 400)
        status, _ = self.request("GET", "/users/web_u1/week")
        self.assertEqual(status, 400)  # 缺少 actor
        status, _ = self.request("POST", "/users/ghost/recommendations")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
