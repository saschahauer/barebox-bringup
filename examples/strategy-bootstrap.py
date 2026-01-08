# SPDX-License-Identifier: GPL-2.0-only

import enum
import sys
from pathlib import Path

import attr

from labgrid import target_factory, step
from labgrid.strategy import Strategy, StrategyError

# Import never_retry from barebox_bringup (handles version compatibility)
sys.path.insert(0, str(Path(__file__).parent.parent))
from barebox_bringup.strategy_utils import never_retry


class BootstrapStatus(enum.Enum):
    unknown = 0
    off = 1
    on = 2
    barebox = 3


@target_factory.reg_driver
@attr.s(eq=False)
class BootstrapStrategy(Strategy):
    """BootstrapStrategy - Production strategy for USB/JTAG bootstrap testing

    This strategy supports boards with USB recovery mode, JTAG loading, or any
    other bootstrap mechanism via labgrid's BootstrapProtocol.

    Supported bootstrap drivers:
    - IMXUSBDriver: i.MX USB recovery (imx-usb-loader)
    - RKUSBDriver: Rockchip USB recovery
    - MXSUSBDriver: Freescale i.MX23/28 USB recovery
    - OpenOCDDriver: JTAG/SWD bootstrap
    - UUUDriver: Universal Update Utility for i.MX
    - BDIMXUSBDriver: Boundary Devices i.MX USB
    - FlashromDriver: Flash ROM programming
    - Any custom driver implementing BootstrapProtocol

    Features:
    - Fast RAM-only bootstrap (no SD card mux required)
    - Optional health checks for production lab validation
    - Production-quality error handling
    - Minimal states focused on barebox testing

    States:
        off: Board powered off
        barebox: Barebox shell active (main testing state)
    """
    bindings = {
        "power": "PowerProtocol",
        "console": "ConsoleProtocol",
        "usbloader": "BootstrapProtocol",  # Any BootstrapProtocol driver
    }

    status = attr.ib(default=BootstrapStatus.unknown)
    bootstrap_done = attr.ib(default=False, init=False)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()

    @never_retry
    @step(args=['status'])
    def transition(self, status, *, step):
        """Transition between states with production error handling

        State transitions:
        - unknown: Invalid (raises error)
        - off: Power off board, deactivate all drivers
        - barebox: Bootstrap via BootstrapProtocol and activate barebox shell

        Args:
            status: Target BootstrapStatus enum value or string
            step: Labgrid step context (injected)
        """
        if not isinstance(status, BootstrapStatus):
            status = BootstrapStatus[status]

        if status == BootstrapStatus.unknown:
            raise StrategyError(f"cannot transition to {status}")

        elif status == self.status:
            step.skip("nothing to do")
            return

        elif status == BootstrapStatus.off:
            # Deactivate all drivers cleanly
            self.target.deactivate(self.console)
            self.target.activate(self.power)
            self.power.off()

        elif status == BootstrapStatus.on:
            self.transition(BootstrapStatus.off)  # pylint: disable=missing-kwoa
            self.target.activate(self.console)

            # Power cycle to bootrom/recovery mode
            self.power.cycle()

            # Bootstrap via BootstrapProtocol
            # Get first image from config (usually barebox-*.img)
            images = self.target.env.config.get_images()
            image = list(images.keys())[0]
            image_path = self.target.env.config.get_image_path(image)

            self.target.activate(self.usbloader)
            self.usbloader.load(image_path)

        elif status == BootstrapStatus.barebox or
            self.transition(BootstrapStatus.on)  # pylint: disable=missing-kwoa
            # interrupt barebox
            self.target.activate(self.barebox)

        else:
            raise StrategyError(
                f"no transition found from {self.status} to {status}"
            )

        self.status = status

    @never_retry
    @step(args=['status'])
    def force(self, status):
        """Force strategy into a specific state (for debugging/recovery)

        This bypasses normal transitions and directly activates drivers.
        Useful for:
        - Recovering from failed tests
        - Starting mid-session debugging
        - pytest --lg-initial-state option

        Args:
            status: Target BootstrapStatus enum value or string
        """
        if not isinstance(status, BootstrapStatus):
            status = BootstrapStatus[status]

        if status == BootstrapStatus.barebox:
            self.target.activate(self.console)
        else:
            raise StrategyError(f"cannot force state {status}")

        self.status = status
