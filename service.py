"""慢跑训练负荷服务的运行入口与 JSON 接口。"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from domain import TrainingService
from domain.access import plan_version_dict
from domain.errors import DomainError, ValidationError

SERVICE_ID = "running-load"
SERVICE_NAME = "慢跑训练负荷服务"


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def make_handler(service):
    """把领域门面包装成 HTTP 处理器，便于测试注入独立实例。"""

    class Handler(BaseHTTPRequestHandler):
        """健康检查与业务接口共用同一入口，未知路径一律 404。"""

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def log_message(self, *_args):
            return

        # ---------- 基础设施 ----------

        def _dispatch(self, method):
            parsed = urlparse(self.path)
            try:
                status, payload = self._route(method, parsed.path,
                                              parse_qs(parsed.query))
            except DomainError as error:
                status, payload = error.status, {"error": str(error)}
            except (json.JSONDecodeError, TypeError, ValueError):
                status, payload = 400, {"error": "请求参数不合法"}
            self._send_json(status, payload)

        def _send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        @staticmethod
        def _first(query, key):
            values = query.get(key)
            return values[0] if values else None

        def _actor(self, query):
            actor = self._first(query, "actor")
            if not actor:
                raise ValidationError("缺少 actor 参数")
            return actor

        # ---------- 路由 ----------

        def _route(self, method, path, query):
            if method == "GET" and path == "/health":
                return 200, health_payload()

            if method == "POST" and path == "/users":
                user = service.directory.create_user(**self._read_json())
                return 201, {"user_id": user.user_id, "group": user.group,
                             "weekly_capacity": user.weekly_capacity}

            m = re.fullmatch(r"/users/([^/]+)/metrics", path)
            if method == "POST" and m:
                return 200, service.ingest.ingest(m.group(1), **self._read_json())

            m = re.fullmatch(r"/users/([^/]+)/injuries/clear", path)
            if method == "POST" and m:
                body = self._read_json()
                tag = service.directory.clear_injury(
                    m.group(1), body.get("label"), body.get("actor"))
                return 200, {"label": tag.label, "cleared": True}

            m = re.fullmatch(r"/users/([^/]+)/injuries", path)
            if method == "POST" and m:
                body = self._read_json()
                tag = service.directory.add_injury(
                    m.group(1), body.get("label"), body.get("actor"),
                    body.get("note", ""))
                return 201, {"label": tag.label, "created_by": tag.created_by}

            m = re.fullmatch(r"/users/([^/]+)/grants", path)
            if method == "POST" and m:
                grant = service.directory.grant(m.group(1), **self._read_json())
                return 201, {"role": grant.role, "grantee_id": grant.grantee_id}

            m = re.fullmatch(r"/users/([^/]+)/recommendations", path)
            if method == "POST" and m:
                rec = service.recommend.generate(m.group(1))
                return 201, service.access.recommendation_full(rec)

            m = re.fullmatch(r"/users/([^/]+)/overrides", path)
            if method == "POST" and m:
                version = service.recommend.apply_override(
                    m.group(1), **self._read_json())
                return 201, plan_version_dict(version)

            m = re.fullmatch(r"/users/([^/]+)/week", path)
            if method == "GET" and m:
                return 200, service.access.view_week(
                    m.group(1), self._actor(query),
                    week=self._first(query, "week"))

            m = re.fullmatch(r"/recommendations/([^/]+)/confirm", path)
            if method == "POST" and m:
                body = self._read_json()
                rec = service.recommend.confirm(
                    m.group(1), body.get("actor"), body.get("role"),
                    body.get("note", ""))
                return 200, service.access.recommendation_full(rec)

            m = re.fullmatch(r"/recommendations/([^/]+)/replay", path)
            if method == "GET" and m:
                return 200, service.access.replay(m.group(1), self._actor(query))

            m = re.fullmatch(r"/recommendations/([^/]+)", path)
            if method == "GET" and m:
                return 200, service.access.view_recommendation(
                    m.group(1), self._actor(query))

            return 404, {"error": "未知路径"}

    return Handler


Handler = make_handler(TrainingService())


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        assert Handler is not None
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
