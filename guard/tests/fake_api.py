#!/usr/bin/env python3
"""假的 RunPod API v2，只用于离线测试。数据从 $FAKE_DIR 下的 JSON 读取，所有写操作记到 calls.log。

用法: fake_api.py <port>
  $FAKE_DIR/api_pods.json     GET /v2/pods 的 pods 数组
  $FAKE_DIR/api_gv.json       GET /v2/global-volumes 的 globalVolumes 数组
  $FAKE_DIR/api_catalog.json  {gpuId: {"availability": "LOW", "price": 1.79}}
  $FAKE_DIR/api_fail          存在时所有请求返回 503（模拟 API 故障）
  $FAKE_DIR/create_fail       每行一个 gpuId，创建这些 GPU 的 pod 返回 409（模拟抢不到卡）
"""
import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

D = Path(os.environ["FAKE_DIR"])


def read(name, default):
    p = D / name
    return json.loads(p.read_text()) if p.exists() else default


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_any(self, method):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        with (D / "calls.log").open("a") as f:
            f.write(f"API {method} {path} {body}\n")
        if self.headers.get("Authorization") != "Bearer testkey":
            return self.send(401, {"detail": "missing bearer token"})
        if (D / "api_fail").exists():
            return self.send(503, {"detail": "unavailable"})
        if method == "GET" and path == "/v2/pods":
            return self.send(200, {"pods": read("api_pods.json", [])})
        if method == "GET" and path.startswith("/v2/pods/"):
            pid = path.split("/")[3]
            for p in read("api_pods.json", []) + read("api_created.json", []):
                if p["id"] == pid:
                    return self.send(200, p)
            return self.send(404, {"detail": "not found"})
        if method == "GET" and path.startswith("/v2/global-volumes/"):
            vid = path.split("/")[3]
            for v in read("api_gv.json", []):
                if v["id"] == vid:
                    return self.send(200, v)
            return self.send(404, {"detail": "global volume not found"})
        if method == "GET" and path == "/v2/global-volumes":
            return self.send(200, {"globalVolumes": read("api_gv.json", [])})
        if method == "GET" and path.startswith("/v2/catalog/gpus/"):
            gid = path[len("/v2/catalog/gpus/"):]
            c = read("api_catalog.json", {}).get(gid, {"availability": "NONE", "price": None})
            return self.send(200, {"id": gid, "price": {"secure": c["price"]}, "availability": c["availability"],
                                   "dataCenters": [{"id": "US-XX-1", "availability": c["availability"]}],
                                   "cudaVersions": [{"version": "12.8", "available": True}]})
        if method == "POST" and path == "/v2/pods":
            b = json.loads(body)
            fails = (D / "create_fail").read_text().split("\n") if (D / "create_fail").exists() else []
            if b["gpu"]["id"] in fails:
                return self.send(409, {"detail": "no capacity"})
            pod = {"id": "fakepod123", "name": b["name"], "status": "RUNNING", "gpu": {"id": b["gpu"]["id"]},
                   "cost": 1.79, "dataCenterId": "US-XX-1", "startedAt": None,
                   "ssh": {"direct": {"command": "ssh root@pod.example.invalid -p 22022"}}}
            (D / "api_created.json").write_text(json.dumps([pod]))
            return self.send(201, pod)
        if method == "POST" and path.endswith("/action"):
            return self.send(200, {"ok": True})
        if method == "DELETE" and path.startswith("/v2/pods/"):
            return self.send(200, {})
        return self.send(404, {"detail": "fake: not implemented"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_DELETE(self):
        self.handle_any("DELETE")


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
