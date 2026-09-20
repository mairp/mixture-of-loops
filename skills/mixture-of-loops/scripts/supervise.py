#!/usr/bin/env python3
"""Run and supervise a generated mixture-of-loops launcher from the invoking harness.

Every subcommand is "read this file, run this command": nothing here needs a host
specific hook, subagent, scheduler or tool name, so a harness with only `read` and
`bash` can drive the whole execution path.

    supervise.py mode     --request TEXT [--token TOK ...]
    supervise.py resolve  --repo DIR [--feature SLUG] [--launcher PATH]
    supervise.py gate     --launcher PATH [--implement] [--smoke]
    supervise.py launch   --launcher PATH [--mode run|auto] [--implement] [--smoke]
    supervise.py watch    (--launcher PATH | --run-dir DIR) [--poll-seconds N] ...
    supervise.py observe  (--launcher PATH | --run-dir DIR) [--json]
    supervise.py auto     --launcher PATH [--mode run|auto] [--implement] [--smoke]
    supervise.py stop     (--launcher PATH | --run-dir DIR)

`auto` is gate, launch and watch in one call, for a harness that supervises inside its
own turn. `observe` is the same reporting for a harness that schedules its own wake-ups.
Neither ever runs the launcher with --dry-run: that is the generation gate, not a mode.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import supervisor_lib as mol  # noqa: E402
from contract_lib import ContractError  # noqa: E402


def emit(line: str) -> None:
    print(line, flush=True)


def _emitter(run_dir: object) -> "object":
    """Mirror the messages into the run's own report log as soon as it is known."""
    return mol.report_log(Path(str(run_dir))) if run_dir else emit


def _bundle_for(record: dict) -> mol.Bundle | None:
    """The contract behind the launcher, read through the launcher rather than through the
    bundle path recorded at launch. A re-render between launch and now therefore shows up
    as a different digest, which is exactly what stops a relaunch from silently resetting
    the stage map."""
    try:
        return mol.read_bundle(Path(record["launcher"]), check_sources=False)
    except (ContractError, mol.SupervisorError, KeyError, OSError):
        return None


def _locate(args: argparse.Namespace) -> dict:
    if getattr(args, "run_dir", None):
        return mol.load_record(Path(args.run_dir).resolve())
    launcher = Path(args.launcher).resolve()
    bundle = mol.read_bundle(launcher, check_sources=False)
    return mol.load_record(bundle.run_dir)


def command_mode(args: argparse.Namespace) -> int:
    decision = mol.select_mode(args.request, args.token)
    emit(decision.message())
    if decision.mode == "generate":
        emit('[MOL-NEXT] next="generation only; offer `run` once the launcher exists"')
    return 0


def command_resolve(args: argparse.Namespace) -> int:
    resolution = mol.resolve_launcher(Path(args.repo).resolve(), requested=args.launcher,
                                      feature=args.feature)
    emit(resolution.message())
    return 0 if resolution.launcher is not None else mol.EXIT_STATUS["refused"]


def command_gate(args: argparse.Namespace) -> int:
    report = mol.gate(Path(args.launcher).resolve(), implement=args.implement, smoke=args.smoke)
    out = _emitter(report.bundle.run_dir if report.bundle else None)
    for line in report.messages():
        out(line)
    return 0 if report.decision == "launch" else mol.EXIT_STATUS["refused"]


def command_launch(args: argparse.Namespace) -> int:
    report = mol.gate(Path(args.launcher).resolve(), implement=args.implement, smoke=args.smoke)
    out = _emitter(report.bundle.run_dir if report.bundle else None)
    for line in report.messages():
        out(line)
    if report.decision != "launch" or report.bundle is None:
        return mol.EXIT_STATUS["refused"]
    record = mol.launch(report.bundle, mode=args.mode, implement=args.implement, smoke=args.smoke)
    out(mol.launch_message(record))
    return 0


def command_watch(args: argparse.Namespace) -> int:
    record = _locate(args)
    status, _ = mol.supervise(record, _bundle_for(record), emit=_emitter(record["run_dir"]),
                              base=args.poll_seconds, cap=args.max_poll_seconds,
                              allow_relaunch=not args.no_relaunch,
                              max_wait=args.max_wait_seconds)
    return mol.exit_status(status)


def command_observe(args: argparse.Namespace) -> int:
    record = _locate(args)
    bundle = _bundle_for(record)
    out = _emitter(record["run_dir"])
    observation, interval = mol.report_once(record, bundle, emit=out,
                                            base=args.poll_seconds, cap=args.max_poll_seconds)
    if observation.terminal:
        decision = mol.classify_relaunch(observation, record, bundle)
        out(mol.digest_message(observation, record, decision))
        if decision.relaunch and not args.no_relaunch:
            out(mol.relaunch_message(record, observation, decision))
            record = mol.relaunch(record, justification=decision.justification(observation))
            out(mol.launch_message(record))
            out(f"[MOL-NEXT] poll-in={args.poll_seconds:g}s")
            return 0
    else:
        out(f"[MOL-NEXT] poll-in={interval:g}s")
    if args.json:
        emit(json.dumps(observation.__dict__, default=str, sort_keys=True))
    return mol.exit_status(observation.status) if observation.terminal else 0


def command_auto(args: argparse.Namespace) -> int:
    status = command_launch(args)
    if status != 0:
        return status
    return command_watch(args)


def command_stop(args: argparse.Namespace) -> int:
    record = _locate(args)
    out = _emitter(record["run_dir"])
    sent, detail = mol.request_stop(record)
    out(f"[MOL-STOP] pipeline={record['pipeline']} sent={str(sent).lower()} "
        f"detail={json.dumps(detail)}")
    if not sent:
        return 0
    status, _ = mol.supervise(record, _bundle_for(record), emit=out,
                              base=min(args.poll_seconds, 5), cap=args.max_poll_seconds,
                              allow_relaunch=False, max_wait=args.max_wait_seconds or 120)
    return mol.exit_status(status)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def with_target(sub: argparse.ArgumentParser, *, run_dir: bool = True) -> None:
        group = sub.add_mutually_exclusive_group(required=True)
        group.add_argument("--launcher", help="the generated launcher")
        if run_dir:
            group.add_argument("--run-dir", help=".mixture-of-loops/runs/<pipeline>")

    def with_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--implement", action="store_true",
                         help="pass --implement through to the launcher")
        sub.add_argument("--smoke", action="store_true",
                         help="pass --smoke through to the launcher")

    def with_cadence(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--poll-seconds", type=float, default=mol.BASE_POLL_SECONDS,
                         help="first poll interval (default: %(default)s)")
        sub.add_argument("--max-poll-seconds", type=float, default=mol.MAX_POLL_SECONDS,
                         help="the interval the backoff stops at (default: %(default)s)")
        sub.add_argument("--max-wait-seconds", type=float, default=None,
                         help="stop watching after this long; the run is left alone "
                              "(default: the contract's declared supervision ceiling)")
        sub.add_argument("--no-relaunch", action="store_true",
                         help="report a classified transient instead of relaunching it")

    mode = subparsers.add_parser("mode", help="read the execution mode from a request")
    mode.add_argument("--request", default="", help="the request text, verbatim")
    mode.add_argument("--token", action="append", default=[],
                      help="an explicit invocation token; repeatable")
    mode.set_defaults(handler=command_mode)

    resolve = subparsers.add_parser("resolve", help="find the launcher a run would use")
    resolve.add_argument("--repo", required=True)
    resolve.add_argument("--feature", help="feature slug or path, for the default launcher name")
    resolve.add_argument("--launcher", help="an explicitly requested launcher path")
    resolve.set_defaults(handler=command_resolve)

    gate = subparsers.add_parser("gate", help="report every launch check without launching")
    with_target(gate, run_dir=False)
    with_flags(gate)
    gate.set_defaults(handler=command_gate)

    launch = subparsers.add_parser("launch", help="gate, then start the launcher detached")
    with_target(launch, run_dir=False)
    with_flags(launch)
    launch.add_argument("--mode", default="run", choices=("run", "auto"))
    launch.set_defaults(handler=command_launch)

    watch = subparsers.add_parser("watch", help="report an already launched run until it ends")
    with_target(watch)
    with_cadence(watch)
    watch.set_defaults(handler=command_watch)

    observe = subparsers.add_parser("observe", help="one scheduled reading and the next delay")
    with_target(observe)
    with_cadence(observe)
    observe.add_argument("--json", action="store_true", help="also print the raw observation")
    observe.set_defaults(handler=command_observe)

    auto = subparsers.add_parser("auto", help="gate, launch and watch in one call")
    with_target(auto, run_dir=False)
    with_flags(auto)
    with_cadence(auto)
    auto.add_argument("--mode", default="auto", choices=("run", "auto"))
    auto.set_defaults(handler=command_auto)

    stop = subparsers.add_parser("stop", help="ask the run to stop, then report it")
    with_target(stop)
    with_cadence(stop)
    stop.set_defaults(handler=command_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except mol.SupervisorError as exc:
        emit(mol.refuse_message(args.command, exc.reason, exc.detail))
        return mol.EXIT_STATUS["refused"]
    except ContractError as exc:
        emit(mol.refuse_message(args.command, "invalid-contract", str(exc)))
        return mol.EXIT_STATUS["refused"]
    except KeyboardInterrupt:
        emit(mol.refuse_message(args.command, "interrupted",
                                "supervision was interrupted; the run was left alone"))
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
