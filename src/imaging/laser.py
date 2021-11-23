import time
from concurrent.futures import Future
from dataclasses import dataclass
from logging import getLogger
from typing import Callable, Literal

from returns.result import Failure, ResultE, Success
from src.instruments import UsesSerial
from src.utils.com import COM, CmdVerify, is_between

logger = getLogger("laser")


class LaserCmd:
    @staticmethod
    def v_get_status(s: str) -> ResultE[bool]:
        try:
            return Success({"DISABLED": False, "ENABLED": True}[s])
        except KeyError:
            return Failure(Exception("Invalid laser response"))

    ON = "ON"
    OFF = "OFF"
    GET_POWER = "POWER?"
    SET_POWER: Callable[[int], str] = lambda x: f"POWER={x}"
    GET_STATUS = CmdVerify("STAT?", v_get_status)


class Laser(UsesSerial):
    POWER_RANGE = (0, 500)

    def __init__(self, port_tx: str) -> None:
        self.com = COM("laser_r", port_tx=port_tx, logger=logger)  # Doesn't matter if laser_r or g.

    def initialize(self) -> Future[str]:
        self.com.repl(LaserCmd.ON)
        return self.com.repl(LaserCmd.SET_POWER(1))

    def set_power(self, power: int) -> Future[int]:
        def worker() -> None:
            self.com.repl(is_between(LaserCmd.SET_POWER, *self.POWER_RANGE)(power))
            while self.power.result() - power > 3:
                time.sleep(1)

        return self.com.put(worker)

    @property
    def power(self) -> Future[int]:
        return self.com.repl(LaserCmd.GET_POWER)

    @property
    def status(self) -> Future[bool]:
        return self.com.repl_verify(LaserCmd.GET_STATUS)


@dataclass
class Lasers:
    g: Laser
    r: Laser

    def initialize(self) -> None:
        self.g.initialize()
        self.r.initialize()
