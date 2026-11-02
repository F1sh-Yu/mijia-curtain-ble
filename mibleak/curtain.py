"""lumi.curtain.hmcn02 MIoT spec (from the RN plugin's Spec/lumi_curtain_hmcn02.json)."""
from __future__ import annotations

import time

from .client import MiMeshDevice
from .frames import T_BOOL, T_UINT8, T_UINT32, Prop

# (siid, piid, type)
MOTOR_CONTROL = (2, 1, T_UINT8)       # 0 pause, 1 open, 2 close, 3 auto (write only)
TARGET_POSITION = (2, 2, T_UINT8)     # 0..100
STATUS = (2, 3, T_UINT8)              # 0 stopped, 1 opening, 2 closing
FAULT = (2, 4, T_UINT8)               # 0 none, 1 charging error, 2 battery temp high
MOTOR_REVERSE = (2, 5, T_UINT8)       # 0 forward, 1 reverse
CURRENT_POSITION = (2, 6, T_UINT8)
WAKE_UP_MODE = (2, 7, T_BOOL)
SET_LIMIT_POINT = (3, 2, T_UINT8)     # 0 clear, 1 set open end, 2 set close end
LIMIT_POINT_NUM = (3, 3, T_UINT8)
CURTAIN_MANUAL = (3, 4, T_UINT8)      # hand-pull start: 0 enable, 1 disable
RANGE_FLAG = (3, 5, T_UINT8)          # 1 = stroke calibrated
RUN_TIME = (3, 8, T_UINT8)            # full stroke seconds (0/255 unknown)
RAIL_TYPE = (3, 19, T_UINT8)          # 1 I rail, 2 U rail, 3 Roman pole
SYNC_UTC = (3, 20, T_UINT32)
BATTERY_LEVEL = (5, 1, T_UINT8)
CHARGING_STATE = (5, 2, T_UINT8)      # 1 charging, 2 not charging, 3 not chargeable

ACTION_CALIBRATE = (2, 1)
ACTION_IDENTIFY = (4, 1)

STATUS_NAMES = {0: "stopped", 1: "opening", 2: "closing"}
FAULT_NAMES = {0: "none", 1: "charging error", 2: "battery temp high"}
CHARGING_NAMES = {1: "charging", 2: "not charging", 3: "not chargeable"}
RAIL_NAMES = {1: "I rail", 2: "U rail", 3: "Roman pole"}

STATUS_FIELDS = {
    "position": CURRENT_POSITION, "target": TARGET_POSITION, "status": STATUS, "fault": FAULT,
    "battery": BATTERY_LEVEL, "charging": CHARGING_STATE, "calibrated": RANGE_FLAG,
    "limit_points": LIMIT_POINT_NUM, "run_time_s": RUN_TIME, "reverse": MOTOR_REVERSE,
    "hand_pull": CURTAIN_MANUAL, "rail": RAIL_TYPE,
}


def _p(spec: tuple[int, int, int], value: object) -> Prop:
    return Prop(spec[0], spec[1], spec[2], value)


class Curtain:
    def __init__(self, dev: MiMeshDevice):
        self.dev = dev

    async def _set(self, spec: tuple[int, int, int], value: object) -> None:
        res = await self.dev.set([_p(spec, value)])
        if res and res[0].get("code", 0) != 0:
            raise RuntimeError(f"set {spec[0]}.{spec[1]} failed: code {res[0]['code']}")

    async def open(self) -> None:
        await self._set(MOTOR_CONTROL, 1)

    async def close(self) -> None:
        await self._set(MOTOR_CONTROL, 2)

    async def stop(self) -> None:
        await self._set(MOTOR_CONTROL, 0)

    async def set_position(self, pct: int) -> None:
        if not 0 <= pct <= 100:
            raise ValueError("position must be 0..100")
        await self._set(TARGET_POSITION, pct)

    async def set_reverse(self, on: bool) -> None:
        await self._set(MOTOR_REVERSE, 1 if on else 0)

    async def set_hand_pull(self, on: bool) -> None:
        await self._set(CURTAIN_MANUAL, 0 if on else 1)

    async def set_limit_point(self, which: str) -> None:
        await self._set(SET_LIMIT_POINT, {"clear": 0, "open": 1, "close": 2}[which])

    async def sync_time(self) -> None:
        await self._set(SYNC_UTC, int(time.time()))

    async def calibrate(self) -> None:
        await self.dev.action(*ACTION_CALIBRATE)

    async def identify(self) -> None:
        await self.dev.action(*ACTION_IDENTIFY)

    async def status(self) -> dict:
        names = list(STATUS_FIELDS)
        items = await self.dev.get([STATUS_FIELDS[n][:2] for n in names])
        by_key = {(i["siid"], i["piid"]): i for i in items}
        out: dict = {}
        for n in names:
            it = by_key.get(STATUS_FIELDS[n][:2])
            if it is None or it.get("code", 0) != 0:
                out[n] = None if it is None else f"error {it['code']}"
                continue
            v = it["value"]
            out[n] = {
                "status": STATUS_NAMES, "fault": FAULT_NAMES, "charging": CHARGING_NAMES, "rail": RAIL_NAMES,
            }.get(n, {}).get(v, v)
            if n == "calibrated":
                out[n] = bool(v)
            elif n == "reverse":
                out[n] = bool(v)
            elif n == "hand_pull":
                out[n] = v == 0
        return out
