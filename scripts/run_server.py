#!/usr/bin/env python3
"""Start CHILI server with automatic port fallback if 8000 is busy (WinError 10048)."""
import os
import socket
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)


def find_free_port(start: int = 8000, end: int = 8010) -> int:
    for port in range(start, end):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("0.0.0.0", port))
                return port
        except OSError:
            continue
    return None


def main():
    import argparse
    p = argparse.ArgumentParser(description="Start CHILI server with port fallback")
    p.add_argument("--ssl-certfile", help="SSL certificate path")
    p.add_argument("--ssl-keyfile", help="SSL key path")
    p.add_argument("--reload", action="store_true", help="Enable uvicorn --reload")
    args = p.parse_args()

    port = find_free_port()
    if port is None:
        print("ERROR: No free port in 8000-8010. Stop existing processes first.")
        sys.exit(1)

    ssl_args = []
    if args.ssl_certfile and args.ssl_keyfile and Path(args.ssl_certfile).exists() and Path(args.ssl_keyfile).exists():
        ssl_args = ["--ssl-certfile", args.ssl_certfile, "--ssl-keyfile", args.ssl_keyfile]
    else:
        certs = [
            (Path("certs/localhost.pem"), Path("certs/localhost.key")),
            (Path("localhost+2.pem"), Path("localhost+2-key.pem")),
        ]
        for cert, key in certs:
            if cert.exists() and key.exists():
                ssl_args = ["--ssl-certfile", str(cert), "--ssl-keyfile", str(key)]
                break

    print(f"Starting CHILI on port {port}...")
    print(f"  https://localhost:{port}/chat")
    print()

    cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "0.0.0.0", "--port", str(port),
        *ssl_args,
    ]
    subprocess.run(cmd, cwd=PROJECT_ROOT)


if __name__ == "__main__":
    main()
