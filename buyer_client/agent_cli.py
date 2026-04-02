from __future__ import annotations

import argparse
import json
from pathlib import Path

from buyer_client.runtime import (
    bootstrap_runtime_session_wireguard,
    create_runtime_session,
    disconnect_runtime_session_wireguard,
    exec_runtime_command_locally,
    find_local_service_container,
    handshake_runtime_gateway,
    login_or_register,
    read_runtime_session,
    redeem_connect_code,
    redeem_order_license,
    renew_runtime_session,
    run_archive,
    run_code,
    run_github_repo,
    start_licensed_shell_session,
    start_order_runtime_session,
    start_shell_session,
    stop_runtime_session,
    stop_session,
    wait_for_runtime_completion,
)

__all__ = [
    "bootstrap_runtime_session_wireguard",
    "create_runtime_session",
    "disconnect_runtime_session_wireguard",
    "exec_runtime_command_locally",
    "find_local_service_container",
    "handshake_runtime_gateway",
    "login_or_register",
    "read_runtime_session",
    "redeem_connect_code",
    "redeem_order_license",
    "renew_runtime_session",
    "run_archive",
    "run_code",
    "run_github_repo",
    "start_licensed_shell_session",
    "start_order_runtime_session",
    "start_shell_session",
    "stop_runtime_session",
    "stop_session",
    "wait_for_runtime_completion",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal buyer-agent CLI for relay-style runtime sessions.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run-code", help="Upload one code file and run it on a seller node.")
    run_parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    run_parser.add_argument("--email", required=True)
    run_parser.add_argument("--password", required=True)
    run_parser.add_argument("--display-name", default="")
    run_parser.add_argument("--seller-node-key", required=True)
    run_parser.add_argument("--code", required=True)
    run_parser.add_argument("--runtime-image", default="python:3.12-alpine")
    run_parser.add_argument("--poll-seconds", type=int, default=2)
    run_parser.add_argument("--minutes", type=int, default=30)

    archive_parser = subparsers.add_parser("run-archive", help="Upload a zip file or directory and run it on a seller node.")
    archive_parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    archive_parser.add_argument("--email", required=True)
    archive_parser.add_argument("--password", required=True)
    archive_parser.add_argument("--display-name", default="")
    archive_parser.add_argument("--seller-node-key", required=True)
    archive_parser.add_argument("--source", required=True)
    archive_parser.add_argument("--runtime-image", default="python:3.12-alpine")
    archive_parser.add_argument("--poll-seconds", type=int, default=2)
    archive_parser.add_argument("--working-dir", default="")
    archive_parser.add_argument("--run-command", default="")
    archive_parser.add_argument("--minutes", type=int, default=30)

    github_parser = subparsers.add_parser("run-github", help="Download a public GitHub repo archive locally and run it on a seller node.")
    github_parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    github_parser.add_argument("--email", required=True)
    github_parser.add_argument("--password", required=True)
    github_parser.add_argument("--display-name", default="")
    github_parser.add_argument("--seller-node-key", required=True)
    github_parser.add_argument("--repo-url", required=True)
    github_parser.add_argument("--ref", default="main")
    github_parser.add_argument("--runtime-image", default="python:3.12-alpine")
    github_parser.add_argument("--poll-seconds", type=int, default=2)
    github_parser.add_argument("--working-dir", default="")
    github_parser.add_argument("--run-command", default="")
    github_parser.add_argument("--minutes", type=int, default=30)

    shell_parser = subparsers.add_parser("start-shell", help="Start a long-lived runtime shell session.")
    shell_parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    shell_parser.add_argument("--email", required=True)
    shell_parser.add_argument("--password", required=True)
    shell_parser.add_argument("--display-name", default="")
    shell_parser.add_argument("--seller-node-key", required=True)
    shell_parser.add_argument("--runtime-image", default="python:3.12-alpine")
    shell_parser.add_argument("--minutes", type=int, default=30)

    exec_parser = subparsers.add_parser("exec", help="Run one command inside a local runtime container.")
    exec_parser.add_argument("--service-name", required=True)
    exec_parser.add_argument("--command", required=True)

    stop_parser = subparsers.add_parser("stop", help="Stop a runtime session.")
    stop_parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    stop_parser.add_argument("--email", required=True)
    stop_parser.add_argument("--password", required=True)
    stop_parser.add_argument("--display-name", default="")
    stop_parser.add_argument("--session-id", required=True, type=int)

    renew_parser = subparsers.add_parser("renew", help="Extend a runtime session lease.")
    renew_parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    renew_parser.add_argument("--email", required=True)
    renew_parser.add_argument("--password", required=True)
    renew_parser.add_argument("--display-name", default="")
    renew_parser.add_argument("--session-id", required=True, type=int)
    renew_parser.add_argument("--minutes", type=int, default=30)

    wg_bootstrap_parser = subparsers.add_parser(
        "wireguard-bootstrap",
        help="Request buyer lease WireGuard credentials and bring up local wg-buyer.",
    )
    wg_bootstrap_parser.add_argument("--backend-url", default="http://127.0.0.1:8000")
    wg_bootstrap_parser.add_argument("--email", required=True)
    wg_bootstrap_parser.add_argument("--password", required=True)
    wg_bootstrap_parser.add_argument("--display-name", default="")
    wg_bootstrap_parser.add_argument("--session-id", required=True, type=int)
    wg_bootstrap_parser.add_argument("--state-dir", default="")

    wg_disconnect_parser = subparsers.add_parser(
        "wireguard-disconnect",
        help="Tear down the local buyer WireGuard interface.",
    )
    wg_disconnect_parser.add_argument("--state-dir", default="")
    wg_disconnect_parser.add_argument("--interface-name", default="wg-buyer")

    args = parser.parse_args()
    if args.command == "run-code":
        result = run_code(
            backend_url=args.backend_url,
            email=args.email,
            password=args.password,
            display_name=args.display_name or None,
            seller_node_key=args.seller_node_key,
            code_path=Path(args.code),
            runtime_image=args.runtime_image,
            poll_seconds=args.poll_seconds,
            requested_duration_minutes=args.minutes,
        )
    elif args.command == "run-archive":
        result = run_archive(
            backend_url=args.backend_url,
            email=args.email,
            password=args.password,
            display_name=args.display_name or None,
            seller_node_key=args.seller_node_key,
            source_path=Path(args.source),
            runtime_image=args.runtime_image,
            poll_seconds=args.poll_seconds,
            working_dir=args.working_dir or None,
            run_command=["sh", "-lc", args.run_command] if args.run_command else None,
            requested_duration_minutes=args.minutes,
        )
    elif args.command == "run-github":
        result = run_github_repo(
            backend_url=args.backend_url,
            email=args.email,
            password=args.password,
            display_name=args.display_name or None,
            seller_node_key=args.seller_node_key,
            repo_url=args.repo_url,
            repo_ref=args.ref,
            runtime_image=args.runtime_image,
            poll_seconds=args.poll_seconds,
            working_dir=args.working_dir or None,
            run_command=["sh", "-lc", args.run_command] if args.run_command else None,
            requested_duration_minutes=args.minutes,
        )
    elif args.command == "start-shell":
        result = start_shell_session(
            backend_url=args.backend_url,
            email=args.email,
            password=args.password,
            display_name=args.display_name or None,
            seller_node_key=args.seller_node_key,
            runtime_image=args.runtime_image,
            requested_duration_minutes=args.minutes,
        )
    elif args.command == "exec":
        result = exec_runtime_command_locally(args.service_name, args.command)
    elif args.command == "stop":
        result = stop_session(
            backend_url=args.backend_url,
            email=args.email,
            password=args.password,
            session_id=args.session_id,
            display_name=args.display_name or None,
        )
    elif args.command == "renew":
        auth = login_or_register(args.backend_url, args.email, args.password, display_name=args.display_name or None)
        result = renew_runtime_session(
            backend_url=args.backend_url,
            buyer_token=auth["access_token"],
            session_id=args.session_id,
            additional_minutes=args.minutes,
        )
    elif args.command == "wireguard-bootstrap":
        auth = login_or_register(args.backend_url, args.email, args.password, display_name=args.display_name or None)
        result = bootstrap_runtime_session_wireguard(
            backend_url=args.backend_url,
            buyer_token=auth["access_token"],
            session_id=args.session_id,
            state_dir=args.state_dir or str(Path.cwd() / ".cache" / "buyer-cli"),
        )
    else:
        result = disconnect_runtime_session_wireguard(
            state_dir=args.state_dir or str(Path.cwd() / ".cache" / "buyer-cli"),
            interface_name=args.interface_name,
        )

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
