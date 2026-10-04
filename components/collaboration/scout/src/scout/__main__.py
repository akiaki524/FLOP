"""One-shot read-only CLI. No credentials, polling daemon, or external tools."""

import argparse
from pathlib import Path
import sys

from scout.ranking import rank
from scout.report import save_report
from scout.source import MAX_BYTES, Observation, ScoutError, fetch_room, parse_room, room_path, utc_now


ROOT = Path(__file__).resolve().parents[2]


def bounded_integer(low, high):
    def parse(value):
        try:
            number = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("整数を指定してください。") from exc
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"{low}〜{high}を指定してください。")
        return number
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description="Technocore公開ルームから協業候補を抽出するRead-only Scout")
    parser.add_argument("--demo", action="store_true", help="同梱の合成fixtureのみを使用（通信なし）")
    parser.add_argument("--room", action="append", help="観測ルーム。最大5個、繰り返し指定。既定: technocore")
    parser.add_argument("--limit", type=bounded_integer(1, 200), default=100, help="ルームごとの最新件数（既定100、最大200）")
    parser.add_argument("--top", type=bounded_integer(1, 100), default=20, help="最大表示候補数（既定20）")
    parser.add_argument("--keyword", action="append", default=[], help="本文の部分一致フィルター（複数はOR、最大8個）")
    args = parser.parse_args(argv)
    try:
        if args.demo and args.room:
            raise ScoutError("--demoと--roomは同時に指定できません。")
        rooms = list(dict.fromkeys(args.room or ["technocore"]))
        if len(rooms) > 5:
            raise ScoutError("1回に観測できるルームは最大5個です。")
        for room in rooms:
            room_path(room, args.limit)
        if len(args.keyword) > 8 or any(not word.strip() or len(word) > 64 for word in args.keyword):
            raise ScoutError("keywordは空白だけでない1〜64文字、最大8個にしてください。")
        if args.demo:
            # There is deliberately no arbitrary local input/URL option.
            fixture = ROOT / "fixtures" / "technocore-demo.json"
            if fixture.is_symlink() or fixture.parent.is_symlink():
                raise ScoutError("fixtureのsymlinkは読み込みません。")
            with fixture.open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            observations = [Observation("scout-demo", 200, utc_now(), raw,
                                        parse_room(raw, "scout-demo", 200), "demo")]
        else:
            observations = [fetch_room(room, args.limit) for room in rooms]
        report = {
            "schema_version": 1, "mode": "demo" if args.demo else "live",
            "created_at": utc_now(), "trust": "UNTRUSTED_EXTERNAL_DATA",
            "ranking_version": 1, "keywords": args.keyword, "top": args.top,
            "coverage": "latest bounded sample; not complete history",
            "sources": [observation.metadata() for observation in observations],
            **rank(observations, args.keyword, args.top),
        }
        directory = save_report(report, observations, ROOT / "data")
        print(f"Scout OK ({report['mode']}): 観測{report['messages_scanned']}件 / 候補{report['candidates_total']}件 / 表示{report['candidates_shown']}件")
        print(f"Report: {directory / 'report.html'}")
        print(f"Evidence / JSON: {directory}")
        return 0
    except (ScoutError, OSError) as exc:
        message = str(exc) if isinstance(exc, ScoutError) else "ローカルファイルの読み書きに失敗しました。"
        print(f"Scout stopped: {message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Scout stopped: 中断しました。COMPLETEがない出力は未完了です。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
