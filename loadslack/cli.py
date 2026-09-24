"""
LoadSlack command-line interface.

  loadslack serve --upstream https://api.openai.com   Run the governing proxy
  loadslack waste  [--db loadslack-receipts.db]         Print the waste report
  loadslack verify [--db loadslack-receipts.db]         Check receipt-chain integrity
  loadslack queue  [--db loadslack-queue.db]            Show deferral-queue stats
  loadslack upgrade                                    LoadSlack Pro upgrade flow
"""
import argparse
import json
import os
import sys

from .ledger import Ledger
from .deferq import DeferQueue
from .profiles import License
from .sidechain import Sidechain
from .signal import StaticSource


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loadslack",
                                     description="LoadSlack: grid-aware AI request governor.")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Start the LoadSlack governing proxy")
    serve.add_argument("--upstream", required=True,
                       help="Upstream API base URL, e.g. https://api.openai.com")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--db", default="loadslack-receipts.db",
                       help="Path to the receipts ledger database")

    waste = sub.add_parser("waste", help="Print the waste report from the receipts ledger")
    waste.add_argument("--db", default="loadslack-receipts.db")
    waste.add_argument("--since", type=float, default=0.0,
                       help="Unix timestamp; report events after this time")

    verify = sub.add_parser("verify", help="Check the receipt chain for tampering")
    verify.add_argument("--db", default="loadslack-receipts.db")

    queue = sub.add_parser("queue", help="Show durable deferral-queue stats")
    queue.add_argument("--db", default="loadslack-queue.db")

    sub.add_parser("upgrade", help="LoadSlack Pro upgrade flow")

    return parser


def cmd_serve(args) -> int:
    from .proxy import serve as run_proxy
    source = StaticSource()  # swap for a WebhookSource pointed at your grid feed
    sc = Sidechain(source=source)
    print(f"loadslack: proxying {args.host}:{args.port} -> {args.upstream}")
    print(f"loadslack: receipts -> {args.db}")
    run_proxy(sc, host=args.host, port=args.port, upstream=args.upstream)
    return 0


def cmd_waste(args) -> int:
    if not os.path.exists(args.db):
        print(f"No ledger found at {args.db}. Run 'loadslack serve' first, or pass --db.")
        return 1
    ledger = Ledger(path=args.db)
    try:
        report = ledger.event_report(since=args.since)
        print(json.dumps(report, indent=2, default=str))
    finally:
        ledger.close()
    return 0


def cmd_verify(args) -> int:
    if not os.path.exists(args.db):
        print(f"No ledger found at {args.db}.")
        return 1
    ledger = Ledger(path=args.db)
    try:
        result = ledger.verify()
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("ok", result.get("valid", False)) else 1
    finally:
        ledger.close()


def cmd_queue(args) -> int:
    if not os.path.exists(args.db):
        print(f"No queue database found at {args.db}.")
        return 1
    q = DeferQueue(path=args.db)
    try:
        print(json.dumps(q.stats(), indent=2, default=str))
    finally:
        q.close()
    return 0


def cmd_upgrade(args) -> int:
    lic = License.from_env()
    print("--- LoadSlack Pro ---")
    print(json.dumps(lic.status(), indent=2, default=str))
    if not lic.active:
        print("\nNo active licence found. Set LOADSLACK_LICENSE_KEY in your "
             "environment or .env file. Free-tier features remain fully active.")
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    handlers = {"serve": cmd_serve, "waste": cmd_waste, "verify": cmd_verify,
               "queue": cmd_queue, "upgrade": cmd_upgrade}
    sys.exit(handlers[args.command](args))


if __name__ == "__main__":
    main()
