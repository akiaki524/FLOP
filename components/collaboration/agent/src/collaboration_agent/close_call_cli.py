"""Operator CLI for Close Call registration and first-trade planning.

No command signs, posts, or mutates external state.
"""
import argparse
import json

from .close_call import (
    CloseCallError,
    build_first_trade_plan,
    collect_registration_state,
    load_launch_pin,
    load_maker_packet,
    registration_sign_request,
)


def emit(value):
    print(json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Close Call close-1 deterministic read-only planner"
    )
    parser.add_argument(
        "--launch-pin", required=True,
        help="Human-approved signed close-1 launch seed pin JSON",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("register-plan")
    first_trade = sub.add_parser("first-trade-plan")
    first_trade.add_argument(
        "--maker-packet", required=True,
        help="Human-selected signed maker terms packet JSON",
    )
    args = parser.parse_args(argv)

    try:
        pin = load_launch_pin(args.launch_pin)
        if args.command == "first-trade-plan":
            emit(build_first_trade_plan(
                maker_packet=load_maker_packet(args.maker_packet),
                launch_pin=pin,
            ))
            return 0

        observed = collect_registration_state(launch_pin=pin)

        if args.command == "status":
            emit(observed)
            return 0

        ownership = observed.get("refereeRoomOwnership", {})
        if ownership.get("verified") is not True:
            emit({
                "status": "HUMAN_REVIEW",
                "reason": "REFEREE_ROOM_OWNERSHIP_NOT_VERIFIED",
                "refereeRoomOwnership": ownership,
                "retry": False,
            })
            return 2

        status = observed["registration"]["status"]
        if status == "READY_MINTED":
            emit({
                "status": "NO_ACTION",
                "reason": "READY_MINTED",
                "registration": observed["registration"],
            })
            return 0

        if status == "REGISTRATION_OBSERVED":
            emit({
                "status": "HUMAN_REVIEW",
                "reason": "REGISTRATION_ALREADY_OBSERVED_MINT_NOT_PROVEN",
                "registration": observed["registration"],
                "retry": False,
            })
            return 2

        emit({
            "status": "PLAN",
            "observation": observed["registration"],
            "signRequest": registration_sign_request(),
            "next": "SIGNER_SUPPORT_THEN_HUMAN_DECISION_BEFORE_ONE_REAL_WRITE",
            "warning": "current public history cannot prove that this DID was never registered",
        })
        return 0
    except (CloseCallError, OSError, ValueError, KeyError) as exc:
        emit({"status": "HUMAN_REVIEW", "reason": str(exc), "retry": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
