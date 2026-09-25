"""HTTP/页面入口（仅标准库）。

资料、判定、保存在 waterright 包内分层；这里只负责路由、身份头与静态页面。
"""
from __future__ import annotations

import argparse
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from waterright import DeskService, DomainError, seed_demo

# 兼容旧原型：Database 即生态预留台服务。
Database = DeskService

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "water_rights.db"


class Handler(BaseHTTPRequestHandler):
    db: DeskService
    server_version = "EcoReservation/1.0"

    def _send(self, payload: Any, status: int = 200) -> None:
        import json
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _html(self) -> None:
        data = (ROOT / "static" / "index.html").read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        import json
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DomainError("请求体不是合法 JSON") from exc

    def _auth(self) -> tuple[str, str]:
        return self.headers.get("X-User", "anonymous"), self.headers.get("X-Role", "viewer")

    # ---------------- GET ----------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query)
        try:
            path = parsed.path
            if path in {"/", "/index.html"}:
                return self._html()
            if path == "/api/health":
                return self._send({"ok": True})
            if path == "/api/accounts":
                return self._send({"accounts": self.db.list_accounts()})
            if path == "/api/transfers":
                return self._send({"transfers": self.db.list_transfers()})
            if path == "/api/reservations":
                account_id = q.get("account_id", [None])[0]
                return self._send({"reservations": self.db.list_reservations(
                    int(account_id) if account_id else None)})
            if path == "/api/reservations/conflicts":
                status = q.get("status", [None])[0]
                return self._send({"conflicts": self.db.list_conflicts(status)})
            if path == "/api/audit":
                return self._send({"audit": self.db.audit()})
            if path.startswith("/api/accounts/") and path.endswith("/available"):
                account_id = int(path.split("/")[3])
                as_of = q.get("as_of", [None])[0]
                return self._send(self.db.available(account_id, as_of))
            if path == "/api/drought/simulate":
                return self._send(self.db.simulate_drought(
                    float(q.get("supply", ["0"])[0]),
                    float(q.get("reduction", ["0"])[0]),
                    q.get("as_of", [None])[0],
                ))
            raise DomainError("接口不存在", 404)
        except (ValueError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    # ---------------- POST ----------------
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            actor, role = self._auth()
            body = self._body()
            parts = [p for p in parsed.path.split("/") if p]
            if parts == ["api", "accounts"]:
                return self._send(self.db.create_account(actor, body, role), 201)
            if parts == ["api", "rules", "season"]:
                return self._send(self.db.set_season_rule(
                    actor, str(body.get("region", "")), int(body.get("month", 0)),
                    body.get("max_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "rules", "impact"]:
                return self._send(self.db.set_impact_rule(
                    actor, str(body.get("source_region", "")), str(body.get("target_region", "")),
                    body.get("min_source_fraction"), str(body.get("note", "")), role), 201)
            if parts == ["api", "transfers"]:
                return self._send(self.db.create_transfer(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "approve":
                return self._send(self.db.approve_transfer(int(parts[2]), actor, role))
            if len(parts) == 4 and parts[:2] == ["api", "transfers"] and parts[3] == "reject":
                return self._send(self.db.reject_transfer(int(parts[2]), actor, role))
            if parts == ["api", "usage"]:
                return self._send(self.db.record_usage(actor, body, role), 201)
            if parts == ["api", "reservations", "preview"]:
                return self._send(self.db.preview_reservation(actor, body, role))
            if parts == ["api", "reservations", "confirm"]:
                return self._send(self.db.confirm_reservation(actor, body, role), 201)
            if len(parts) == 4 and parts[:2] == ["api", "reservations"] and parts[3] == "release":
                return self._send(self.db.release_reservation(actor, body | {"reservation_id": int(parts[2])}, role))
            if len(parts) == 5 and parts[:3] == ["api", "reservations", "conflicts"] and parts[4] == "occupy":
                return self._send(self.db.occupy_conflict(int(parts[3]), actor, role))
            if len(parts) == 5 and parts[:3] == ["api", "reservations", "conflicts"] and parts[4] == "dismiss":
                return self._send(self.db.dismiss_conflict(int(parts[3]), actor, role))
            raise DomainError("接口不存在", 404)
        except (ValueError, TypeError, DomainError) as exc:
            self._send({"error": str(exc)}, getattr(exc, "status", 400))

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[water] {self.address_string()} - {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="水权账户与生态预留台服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8007")))
    parser.add_argument("--db", default=os.getenv("WATER_DB", str(DEFAULT_DB)))
    parser.add_argument("--init", action="store_true", help="创建数据库并写入示例账户和夏季生态预留")
    args = parser.parse_args()
    db = DeskService(args.db)
    if args.init:
        seed_demo(db)
        print(f"initialized database at {args.db}")
        return
    Handler.db = db
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"eco-reservation desk listening on http://127.0.0.1:{args.port} (db={args.db})")
    server.serve_forever()


if __name__ == "__main__":
    main()
