"""Run the gateway from a YAML configuration."""

import argparse
from pathlib import Path

import uvicorn

from .app import create_app
from .config import load_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("gateway.yaml"))
    args = parser.parse_args()
    settings = load_settings(args.config)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port, access_log=False)


if __name__ == "__main__":
    main()
