from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from . import frames
from .client import MiMeshDevice, scan
from .curtain import Curtain
from .frames import Prop


def _onoff(s: str) -> bool:
    if s.lower() in ("on", "1", "true", "yes"):
        return True
    if s.lower() in ("off", "0", "false", "no"):
        return False
    raise argparse.ArgumentTypeError("expected on/off")


def _sp(s: str) -> tuple[int, int]:
    a, b = s.split(".")
    return int(a), int(b)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="curtain", description="Direct BLE control for lumi.curtain.hmcn02")
    ap.add_argument("-a", "--address", default=os.environ.get("CURTAIN_ADDRESS"),
                    help="BLE address (CoreBluetooth UUID on macOS); env CURTAIN_ADDRESS")
    ap.add_argument("-k", "--ltmk", default=os.environ.get("CURTAIN_LTMK"),
                    help="device gatt_ltmk from Mi cloud (hex); env CURTAIN_LTMK")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="list nearby Xiaomi BLE devices")
    s.add_argument("-t", "--timeout", type=float, default=8.0)
    sub.add_parser("status")
    sub.add_parser("open")
    sub.add_parser("close")
    sub.add_parser("stop")
    s = sub.add_parser("pos", help="move to position 0..100")
    s.add_argument("percent", type=int)
    s = sub.add_parser("reverse", help="motor direction")
    s.add_argument("state", type=_onoff)
    s = sub.add_parser("handpull", help="hand-pull start")
    s.add_argument("state", type=_onoff)
    s = sub.add_parser("limit", help="set stroke end at current position")
    s.add_argument("which", choices=["open", "close", "clear"])
    sub.add_parser("calibrate", help="run motor_calibrate action")
    sub.add_parser("identify", help="make the device beep/blink")
    sub.add_parser("synctime")
    sub.add_parser("watch", help="print property changes and events")
    s = sub.add_parser("get", help="raw get, e.g. get 2.6 5.1")
    s.add_argument("props", nargs="+", type=_sp)
    s = sub.add_parser("set", help="raw set, e.g. set 2.2 uint8 50")
    s.add_argument("prop", type=_sp)
    s.add_argument("type", choices=list(frames.TYPE_BY_NAME))
    s.add_argument("value")
    s = sub.add_parser("action", help="raw action, e.g. action 2.1")
    s.add_argument("ida", type=_sp)
    return ap


async def run(args: argparse.Namespace) -> int:
    if args.cmd == "scan":
        for a in await scan(args.timeout):
            pid = f"pid={a.product_id}" if a.product_id is not None else ""
            print(f"{a.device.address}  rssi={a.rssi:4d}  mac={a.mac or '?':17}  {pid}  {a.device.name or ''}")
        return 0
    if not args.address or not args.ltmk:
        print("need --address and --ltmk (or CURTAIN_ADDRESS / CURTAIN_LTMK)", file=sys.stderr)
        return 2
    async with MiMeshDevice(args.address, bytes.fromhex(args.ltmk)) as dev:
        c = Curtain(dev)
        cmd = args.cmd
        if cmd == "status":
            print(json.dumps(await c.status(), ensure_ascii=False, indent=2))
        elif cmd == "open":
            await c.open()
        elif cmd == "close":
            await c.close()
        elif cmd == "stop":
            await c.stop()
        elif cmd == "pos":
            await c.set_position(args.percent)
        elif cmd == "reverse":
            await c.set_reverse(args.state)
        elif cmd == "handpull":
            await c.set_hand_pull(args.state)
        elif cmd == "limit":
            await c.set_limit_point(args.which)
        elif cmd == "calibrate":
            await c.calibrate()
        elif cmd == "identify":
            await c.identify()
        elif cmd == "synctime":
            await c.sync_time()
        elif cmd == "get":
            print(json.dumps(await dev.get(args.props), ensure_ascii=False))
        elif cmd == "set":
            vtype = frames.TYPE_BY_NAME[args.type]
            print(json.dumps(await dev.set([Prop(*args.prop, vtype, args.value)]), ensure_ascii=False))
        elif cmd == "action":
            f = await dev.action(*args.ida)
            print(json.dumps(f.items, ensure_ascii=False))
        elif cmd == "watch":
            dev.on_props_changed = lambda items: print("props", json.dumps(items, ensure_ascii=False), flush=True)
            dev.on_event = lambda f: print(f"event {f.siid}.{f.eiid}", json.dumps(f.items), flush=True)
            print("watching, Ctrl-C to quit", flush=True)
            await asyncio.Event().wait()
    return 0


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(level=[logging.WARNING, logging.INFO, logging.DEBUG][min(args.verbose, 2)],
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
