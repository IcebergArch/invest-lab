#!/usr/bin/env python3
"""Run the quant API, visual bridge, and Vite dev server without Docker."""

from __future__ import annotations

import argparse
import errno
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
DEFAULT_PORTS = (8765, 8766, 8767)


def free_port(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return False
            raise
    return True


def wait_for_http(url: str, children: list[subprocess.Popen[bytes]], timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for child in children:
            if child.poll() is not None:
                raise RuntimeError(f"本地服务提前退出，退出码 {child.returncode}。")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            pass
        time.sleep(0.25)
    raise RuntimeError(f"本地服务启动超时：{url}")


def stop(children: list[subprocess.Popen[bytes]]) -> None:
    for child in reversed(children):
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 5
    for child in reversed(children):
        if child.poll() is None:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=DEFAULT_PORTS[0], help="可视页面端口，默认 8765")
    parser.add_argument("--quant-port", type=int, default=DEFAULT_PORTS[1], help="量化 API 端口，默认 8766")
    parser.add_argument("--bridge-port", type=int, default=DEFAULT_PORTS[2], help="可视化桥接端口，默认 8767")
    args = parser.parse_args()
    ports = (args.port, args.quant_port, args.bridge_port)
    if any(port < 1 or port > 65535 for port in ports) or len(set(ports)) != 3:
        parser.error("三个端口必须是不同的有效 TCP 端口")
    busy = [port for port in ports if not free_port(port)]
    if busy:
        parser.error(f"端口已被占用：{', '.join(map(str, busy))}；请停止旧服务或指定其他端口")
    npm = shutil.which("npm")
    if npm is None:
        parser.error("没有找到 npm；请先安装 Node.js 与 npm")

    python_env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUNBUFFERED": "1"}
    vite_env = {**os.environ, "LOCAL_VISUAL_PROXY_TARGET": f"http://127.0.0.1:{args.bridge_port}"}
    children: list[subprocess.Popen[bytes]] = []
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    try:
        if not (FRONTEND / "node_modules" / ".bin" / "vite").exists():
            print("首次启动：安装前端锁定依赖…", flush=True)
            subprocess.run([npm, "ci"], cwd=FRONTEND, check=True)
        print("构建可视化桥接所需的静态资源…", flush=True)
        subprocess.run([npm, "run", "build"], cwd=FRONTEND, check=True)

        children.append(subprocess.Popen(
            [sys.executable, "-m", "quant_lab", "api", "--host", "127.0.0.1",
             "--port", str(args.quant_port)],
            cwd=ROOT, env=python_env, start_new_session=True,
        ))
        wait_for_http(f"http://127.0.0.1:{args.quant_port}/api/dashboard", children)
        children.append(subprocess.Popen(
            [sys.executable, "-m", "visual_lab", "--host", "127.0.0.1",
             "--port", str(args.bridge_port), "--quant-api-base",
             f"http://127.0.0.1:{args.quant_port}"],
            cwd=ROOT, env=python_env, start_new_session=True,
        ))
        wait_for_http(f"http://127.0.0.1:{args.bridge_port}/healthz", children)
        children.append(subprocess.Popen(
            [npm, "run", "dev", "--", "--port", str(args.port), "--strictPort"],
            cwd=FRONTEND, env=vite_env, start_new_session=True,
        ))
        wait_for_http(f"http://127.0.0.1:{args.port}/api/dashboard", children)
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, request_stop)
        print(f"\n本地开发页面：http://127.0.0.1:{args.port}/#quant-overview", flush=True)
        print("前端修改自动刷新；后端修改后按 Ctrl+C，再运行此命令。", flush=True)
        print("按 Ctrl+C 同时停止三个本地服务。\n", flush=True)
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    raise RuntimeError(f"本地服务退出，退出码 {child.returncode}。")
            time.sleep(0.25)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        stop(children)


if __name__ == "__main__":
    raise SystemExit(main())
