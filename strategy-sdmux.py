# SPDX-License-Identifier: GPL-2.0-only

import enum

import attr

from labgrid import target_factory, step
from labgrid.strategy import Strategy, StrategyError

# Try to import never_retry, but provide a fallback if not available
try:
    from labgrid.strategy import never_retry
except ImportError:
    try:
        from labgrid.strategy.common import never_retry
    except ImportError:
        # Fallback: define a no-op decorator if never_retry is not available
        def never_retry(func):
            return func


class SDMuxStatus(enum.Enum):
    unknown = 0
    off = 1
    on = 2
    barebox = 3


@target_factory.reg_driver
@attr.s(eq=False)
class SDMuxStrategy(Strategy):
    """SDMuxStrategy - Boot from SD card via USB-SD-MUX

    This strategy supports boards that boot from SD cards controlled
    by USB-SD-MUX hardware. It writes barebox images to the SD card
    via the host in 'host' mode, then switches to 'dut' mode for booting.

    Supported hardware:
    - USB-SD-MUX (https://linux-automation.com)
    - Any SD-card based boot device

    Features:
    - Single image writing (dd-based, no bmaptool)
    - --no-write CLI option to skip writing and boot from existing SD
    - Images written only once per session (bootstrap_done flag)
    - No health checks (initial implementation)

    States:
        off: Board powered off, SD card in DUT mode
        barebox: Barebox shell active, booted from SD card

    Configuration:
        Requires 'no_write' option in config to skip image writing.
        Set via CLI: barebox-bringup --no-write ...
    """
    bindings = {
        "power": "PowerProtocol",
        "console": "ConsoleProtocol",
        "sdmux": "USBSDMuxDriver",      # SD card mux control
        "storage": "USBStorageDriver",   # SD card writing
    }

    status = attr.ib(default=SDMuxStatus.unknown)
    bootstrap_done = attr.ib(default=False, init=False)

    def __attrs_post_init__(self):
        super().__attrs_post_init__()

    @never_retry
    @step(args=['status'])
    def transition(self, status, *, step):
        """Transition between states

        State transitions:
        - unknown: Invalid (raises error)
        - off: Power off board, ensure SD in DUT mode
        - barebox: Write SD (if needed), boot from SD

        Args:
            status: Target SDMuxStatus enum value or string
            step: Labgrid step context (injected)
        """
        if not isinstance(status, SDMuxStatus):
            status = SDMuxStatus[status]

        if status == SDMuxStatus.unknown:
            raise StrategyError(f"cannot transition to {status}")

        elif status == self.status:
            step.skip("nothing to do")
            return

        elif status == SDMuxStatus.off:
            # Power off sequence
            # Ensure SD is in DUT mode before powering off
            self.target.activate(self.sdmux)
            self.sdmux.set_mode("dut")

            self.target.deactivate(self.console)
            self.target.activate(self.power)
            self.power.off()

        elif status == SDMuxStatus.on:
            # Main boot sequence: Write SD (if needed) and boot

            # First ensure we're powered off
            if self.status != SDMuxStatus.off:
                self.transition(SDMuxStatus.off)  # pylint: disable=missing-kwoa

            # Re-activate console after going to off
            self.target.activate(self.console)

            # Check if we should skip writing
            no_write = False
            try:
                no_write = self.target.env.config.get_option('no_write')
            except (AttributeError, KeyError):
                pass  # no_write not set, default to False

            # Write image to SD card if needed
            if not self.bootstrap_done and not no_write:
                # Get image from config (single image only)
                images = self.target.env.config.get_images()
                if not images:
                    raise StrategyError("No images defined in config")

                image_name = list(images.keys())[0]
                image_path = self.target.env.config.get_image_path(image_name)

                # Get image attributes (seek, etc.) from config
                image_config = self.target.env.config.data.get('image-config', {})
                image_params = image_config.get(image_name, {})
                seek = image_params.get('seek')
                skip = image_params.get('skip')

                # Activate SD-MUX and storage drivers
                self.target.activate(self.sdmux)
                self.target.activate(self.storage)

                # Switch to host mode for writing
                self.sdmux.set_mode("host")

                # Write image using dd (USBStorageDriver default)
                # Build kwargs for optional parameters
                write_kwargs = {'filename': image_path}
                if seek is not None:
                    write_kwargs['seek'] = seek
                if skip is not None:
                    write_kwargs['skip'] = skip
                self.storage.write_image(**write_kwargs)

                # Switch back to DUT mode
                self.sdmux.set_mode("dut")

                # Mark bootstrap as complete
                self.bootstrap_done = True
            else:
                # Just ensure SD is in DUT mode
                # (either --no-write or already bootstrapped)
                self.target.activate(self.sdmux)
                self.sdmux.set_mode("dut")

                if no_write and not self.bootstrap_done:
                    # Mark as bootstrapped to prevent future writes
                    self.bootstrap_done = True

            # Power cycle to boot from SD
            self.power.cycle()

        elif status == SDMuxStatus.barebox:
            self.transition(SDMuxStatus.on)  # pylint: disable=missing-kwoa
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
        - Skipping image writing: strategy.force('barebox')

        Args:
            status: Target SDMuxStatus enum value or string
        """
        if not isinstance(status, SDMuxStatus):
            status = SDMuxStatus[status]

        if status == SDMuxStatus.barebox:
            # Force into barebox state without writing
            # Activate console and mark bootstrap as done
            self.target.activate(self.console)
            self.bootstrap_done = True
        else:
            raise StrategyError(f"cannot force state {status}")

        self.status = status
