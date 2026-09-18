"""慢跑训练负荷服务的运行入口与 HTTP API。

身份通过请求头声明（本地联调用，生产环境应替换为真实认证）：
  X-Actor-Id:   执行者标识（跑者本人、教练或医疗顾问）
  X-Actor-Role: runner | coach | medical | system

保留既有契约：GET /health 返回稳定身份，未知路径返回 404。
"""

import argparse
import json
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from training import access, ingestion, profiles, recommendation
from training.errors import DomainError, ForbiddenError, UnauthorizedError, ValidationError
from training.models import Actor, Role
from training.store import Store

SERVICE_ID = "running-load"
SERVICE_NAME = "慢跑训练负荷服务"

STORE = Store()


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _utcnow():
    return datetime.now(timezone.utc)


class Handler(BaseHTTPRequestHandler):
    """健康检查 + 训练负荷业务接口。"""

    store = STORE  # 类级默认仓储；测试可用 make_handler 注入独立实例

    # ---- 基础 ----
    def log_message(self, *_args):
        return

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    # ---- 路由 ----
    def _dispatch(self, method):
        try:
            body = self._read_json() if method == "POST" else None
            status, payload = self._route(method, self.path, body)
        except DomainError as error:
            status, payload = error.status, _error_body(error.code, error.message)
        except Exception:  # 未预期错误不泄露内部细节
            status, payload = 500, _error_body("internal_error", "服务内部错误")
        self._send_json(status, payload)

    def _route(self, method, path, body):
        if method == "GET" and path == "/health":
            return 200, health_payload()

        match = re.fullmatch(r"/runners", path)
        if match and method == "POST":
            actor = self._actor()
            profile = profiles.create_runner(self.store, body, actor)
            return 201, access.project_runner(profile, access.FULL)

        match = re.fullmatch(r"/runners/([^/]+)", path)
        if match and method == "GET":
            actor = self._actor()
            runner_id = match.group(1)
            profile = self.store.get_runner(runner_id)
            kind = access.view_kind(self.store, actor, runner_id)
            return 200, access.project_runner(profile, kind)

        match = re.fullmatch(r"/runners/([^/]+)/authorizations", path)
        if match and method == "POST":
            actor = self._actor()
            auth = profiles.grant_authorization(self.store, match.group(1), body, actor)
            return 201, auth.to_dict()

        match = re.fullmatch(r"/runners/([^/]+)/events", path)
        if match and method == "GET":
            actor = self._actor()
            runner_id = match.group(1)
            self.store.get_runner(runner_id)
            if not (actor.role == Role.SYSTEM
                    or (actor.role == Role.RUNNER and actor.actor_id == runner_id)):
                raise ForbiddenError("只有本人可以查看完整事件流")
            events = [e.to_dict() for e in self.store.events_for_runner(runner_id)]
            return 200, {"runner_id": runner_id, "events": events}

        match = re.fullmatch(r"/syncs", path)
        if match and method == "POST":
            actor = self._actor()
            runner_id = (body or {}).get("runner_id")
            if not (actor.role == Role.SYSTEM
                    or (actor.role == Role.RUNNER and actor.actor_id == runner_id)):
                raise ForbiddenError("只能同步本人的设备数据")
            return 200, ingestion.ingest_sync(self.store, body, actor)

        match = re.fullmatch(r"/recommendations", path)
        if match and method == "POST":
            actor = self._actor()
            runner_id = (body or {}).get("runner_id")
            if not isinstance(runner_id, str) or not runner_id:
                raise ValidationError("缺少必填字段: runner_id")
            self.store.get_runner(runner_id)
            if actor.role in (Role.COACH, Role.MEDICAL):
                access.view_kind(self.store, actor, runner_id)  # 需有效授权
            elif not (actor.role == Role.SYSTEM
                      or (actor.role == Role.RUNNER and actor.actor_id == runner_id)):
                raise ForbiddenError("无权为该跑者生成建议")
            rec = recommendation.generate(self.store, runner_id, actor)
            return 201, access.project_recommendation(rec, access.FULL)

        match = re.fullmatch(r"/recommendations/([^/]+)", path)
        if match and method == "GET":
            actor = self._actor()
            rec = self.store.get_recommendation(match.group(1))
            kind = access.view_kind(self.store, actor, rec.runner_id)
            return 200, access.project_recommendation(rec, kind, now=_utcnow())

        match = re.fullmatch(r"/recommendations/([^/]+)/replay", path)
        if match and method == "GET":
            actor = self._actor()
            rec = self.store.get_recommendation(match.group(1))
            kind = access.view_kind(self.store, actor, rec.runner_id)
            raw = recommendation.replay(self.store, rec.recommendation_id, now=_utcnow())
            return 200, access.project_replay(raw, kind)

        match = re.fullmatch(r"/recommendations/([^/]+)/confirmations", path)
        if match and method == "POST":
            actor = self._actor()
            note = (body or {}).get("note", "")
            rec = recommendation.confirm(self.store, match.group(1), actor, note=note)
            return 200, access.project_recommendation(rec, access.FULL)

        match = re.fullmatch(r"/recommendations/([^/]+)/overrides", path)
        if match and method == "POST":
            actor = self._actor()
            payload = body or {}
            expires_at = None
            if payload.get("expires_at") is not None:
                expires_at = ingestion.parse_datetime(payload.get("expires_at"), "expires_at")
            rec = recommendation.override(
                self.store, match.group(1), actor,
                reason=payload.get("reason", ""),
                expires_at=expires_at,
                changes=payload.get("changes"),
            )
            return 200, access.project_recommendation(rec, access.FULL, now=_utcnow())

        return 404, _error_body("not_found", "路径不存在")

    # ---- 工具 ----
    def _actor(self) -> Actor:
        actor_id = self.headers.get("X-Actor-Id")
        role_value = self.headers.get("X-Actor-Role")
        if not actor_id or not role_value:
            raise UnauthorizedError("缺少 X-Actor-Id / X-Actor-Role 请求头")
        try:
            role = Role(role_value)
        except ValueError:
            raise UnauthorizedError(f"未知角色: {role_value}")
        return Actor(actor_id=actor_id, role=role)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValidationError("请求体不是合法 JSON")

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_handler(store: Store):
    """返回绑定独立仓储的 Handler 子类，便于测试隔离。"""

    class _BoundHandler(Handler):
        pass

    _BoundHandler.store = store
    return _BoundHandler


def _error_body(code, message):
    return {"error": {"code": code, "message": message}}


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        Store()  # 领域模块可正常装配
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
