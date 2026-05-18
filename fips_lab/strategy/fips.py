import enum
import time

import attr
from labgrid import target_factory
from labgrid.strategy.common import Strategy, StrategyError


class FipsStatus(enum.Enum):
    unknown = 0
    off = 1
    deployed = 2
    connected = 3
    ready = 4


@target_factory.reg_driver
@attr.s(eq=False)
class FipsStrategy(Strategy):
    bindings = {"service": "FipsServiceDriver", "fipsctl": "FipsctlDriver"}
    status = attr.ib(default=FipsStatus.unknown)

    def transition(self, status):
        if not isinstance(status, FipsStatus):
            status = FipsStatus[status]
        if status == self.status:
            return

        if status == FipsStatus.off:
            self.service.stop()

        elif status == FipsStatus.deployed:
            self.transition(FipsStatus.off)
            self.service.start()
            time.sleep(5)

        elif status == FipsStatus.connected:
            self.transition(FipsStatus.deployed)
            peers = self.fipsctl.show_peers()
            if not peers:
                raise StrategyError("No peers connected")

        elif status == FipsStatus.ready:
            self.transition(FipsStatus.connected)

        self.status = status

    def force(self, status):
        if not isinstance(status, FipsStatus):
            status = FipsStatus[status]
        self.status = status
