"""光动力早期试验运行入口。

除原有的 /health 外，通过 trial.api 暴露受控流程命令接口：
POST /api/commands/<命令> 与 GET /api/<资源>。
"""

import argparse
from http.server import ThreadingHTTPServer

from trial.api import Api, make_handler
from trial.coordinator import TrialCoordinator
from trial.store import EventStore

SERVICE_ID = "photodynamic-trial"
SERVICE_NAME = "光动力早期试验"

# 进程级共享存储（ThreadingHTTPServer 下所有请求共用，存储自身带锁）。
api = Api(TrialCoordinator(EventStore()))
Handler = make_handler(api)


def health_payload():
    """返回稳定的服务身份信息。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def build_server(port):
    return ThreadingHTTPServer(("0.0.0.0", port), make_handler(api))


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        assert health_payload()["service"] == SERVICE_ID
        # 领域引擎冒烟：空存储下查询应可用
        assert Api().dispatch_get("subjects", {}) == []
        print("基础检查通过")
        return
    build_server(args.port).serve_forever()


if __name__ == "__main__":
    main()
