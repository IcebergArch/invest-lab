"""Run the independent visual service: ``python -m visual_lab``."""
from __future__ import annotations

import argparse
import os

from visual_lab.server import serve_visual


def main() -> None:
    parser = argparse.ArgumentParser(description="A 股研究台可视化服务")
    parser.add_argument("--host", default=os.getenv("VISUAL_LAB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("VISUAL_LAB_PORT", "8765")))
    parser.add_argument("--quant-api-base", default=os.getenv("QUANT_API_BASE", "http://127.0.0.1:8766"))
    parser.add_argument("--asset-dir", default=os.getenv("VISUAL_LAB_ASSET_DIR", "frontend/dist"))
    args = parser.parse_args()
    serve_visual(args.quant_api_base, args.host, args.port, args.asset_dir)


if __name__ == "__main__":
    main()
