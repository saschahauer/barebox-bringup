#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only

"""
Barebox Hardware Bringup Tool

Brings up barebox on target hardware using labgrid and provides raw console access.
Supports interactive, non-interactive, and listen-only modes.
"""

import sys
import os
import argparse
import logging
import signal
import time
import asyncio

from labgrid import Environment, target_factory
from labgrid.protocol import ConsoleProtocol, PowerProtocol
from labgrid.strategy import Strategy
from labgrid.logging import basicConfig, StepLogger
from labgrid.remote.client import start_session
from labgrid.resource.remote import RemotePlaceManager
from labgrid.util.proxy import proxymanager


def create_argument_parser():
    """Create and configure argument parser"""
    parser = argparse.ArgumentParser(
        description='Bring up barebox on target hardware using labgrid',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Interactive mode (default)
  %(prog)s -c test/arm/imx6s-riotboard.yaml

  # Interactive with output logging
  %(prog)s -c test/arm/imx6s-riotboard.yaml -o session.log
  # In another terminal: tail -f session.log

  # Override single image from config file (positional)
  %(prog)s -c test/arm/imx6s-riotboard.yaml --image /path/to/barebox.img

  # Override multiple images from config file (named)
  %(prog)s -c test/arm/vusion-ugate.yaml --image tiboot3.img=/path/to/tiboot3.img --image barebox-proper.img=/path/to/barebox.img

  # Interactive with auto-created FIFO for programmatic control
  %(prog)s -c test/arm/imx6s-riotboard.yaml -i -o boot.log &
  # Tool prints: Created FIFO: /tmp/barebox-input-12345.fifo
  echo "version" > /tmp/barebox-input-12345.fifo

  # Interactive with specified FIFO
  %(prog)s -c test/arm/imx6s-riotboard.yaml -i /tmp/cmds.fifo -o boot.log &
  echo "help" > /tmp/cmds.fifo

  # Non-interactive (no keyboard input, output only)
  %(prog)s -c test/arm/imx6s-riotboard.yaml -n -o boot.log
''')

    # Required arguments
    parser.add_argument('-c', '--config', required=True,
                        help='Labgrid configuration file (YAML)')

    # Mode selection
    parser.add_argument('-n', '--non-interactive', action='store_true',
                        help='Non-interactive mode: no keyboard input, output to file only')

    # I/O configuration
    parser.add_argument('-o', '--output', type=str,
                        help='Output file for console log (works in all modes)')
    parser.add_argument('-i', '--input', nargs='?', const='', type=str, metavar='FIFO',
                        help='Input FIFO: without arg creates temp FIFO, with arg creates specified FIFO')

    # Target configuration
    parser.add_argument('-r', '--role', type=str, default='main',
                        help='Target role in config file (default: main)')
    parser.add_argument('--coordinator', type=str,
                        help='Labgrid coordinator address (overrides config/env)')
    parser.add_argument('--proxy', type=str,
                        help='Labgrid proxy address (overrides config/env)')

    # Control options
    parser.add_argument('--no-power-cycle', action='store_true',
                        help='Skip power cycle, assume target is already on')
    parser.add_argument('--timeout', type=int, default=None,
                        help='Timeout in seconds for operations (default: no timeout)')
    parser.add_argument('--image', action='append', dest='images',
                        help='Override image: --image name=path or --image path (for single image configs)')

    # Debugging
    parser.add_argument('-v', '--verbose', action='count', default=0,
                        help='Increase verbosity (-v, -vv, -vvv)')

    return parser


def setup_input_fifo(input_arg):
    """Setup input FIFO based on --input argument

    Args:
        input_arg: Value from --input argument
                   None = no FIFO
                   '' = auto-create temp FIFO
                   'path' = create FIFO at path

    Returns:
        tuple: (fifo_path, created_by_us)
               fifo_path is None if no FIFO requested
    """
    import tempfile
    import stat

    if input_arg is None:
        # No --input specified
        return None, False

    if input_arg == '':
        # Auto-create temp FIFO
        fd, fifo_path = tempfile.mkstemp(prefix='barebox-input-', suffix='.fifo')
        os.close(fd)
        os.unlink(fifo_path)  # Remove regular file
        os.mkfifo(fifo_path)
        print(f"Created FIFO: {fifo_path}")
        return fifo_path, True
    else:
        # User specified path
        if os.path.exists(input_arg):
            # Check if it's already a FIFO
            if stat.S_ISFIFO(os.stat(input_arg).st_mode):
                # Already a FIFO, use it
                return input_arg, False
            else:
                raise ValueError(f"Error: {input_arg} exists but is not a FIFO")
        else:
            # Create FIFO at specified path
            os.mkfifo(input_arg)
            print(f"Created FIFO: {input_arg}")
            return input_arg, True


def load_environment(config_file, coordinator=None, proxy=None, image_overrides=None):
    """Load labgrid environment from configuration file

    Args:
        config_file: Path to YAML configuration file
        coordinator: Optional coordinator address override (highest priority)
        proxy: Optional proxy address override (highest priority)
        image_overrides: Optional list of image overrides (overrides config file images)

    Returns:
        Environment object

    Note:
        Coordinator priority (highest to lowest):
        1. --coordinator command line argument (overrides everything)
        2. coordinator_address in YAML config file (options: section)
        3. LG_COORDINATOR environment variable
        4. Default: 127.0.0.1:20408

        Proxy priority (highest to lowest):
        1. --proxy command line argument (overrides everything)
        2. proxy in YAML config file (options: section)
        3. LG_PROXY environment variable

        Image overrides:
        Supports two formats:
        - Named: "name=path" - Override specific image by name
        - Positional: "path" - Override first image (backwards compatibility)

        Examples:
        - --image tiboot3.img=/path/to/tiboot3.img --image barebox-proper.img=/path/to/barebox.img
        - --image /path/to/barebox.img (overrides first image)
    """
    # Set up labgrid logging
    basicConfig(level=logging.WARNING)
    StepLogger.start()

    # Load environment
    env = Environment(config_file=config_file)

    # Override coordinator_address in config if --coordinator was specified
    # This ensures CLI argument has highest priority
    if coordinator:
        env.config.set_option('coordinator_address', coordinator)

    # Override proxy in config if --proxy was specified
    # This ensures CLI argument has highest priority
    if proxy:
        env.config.set_option('proxy', proxy)

    # Override image paths if --image was specified
    if image_overrides:
        images = env.config.get_images()
        if not images:
            logging.warning("No images defined in config, --image option(s) ignored")
        else:
            for override in image_overrides:
                if '=' in override:
                    # Named format: name=path
                    name, path = override.split('=', 1)
                    if name in images:
                        abs_path = os.path.realpath(path)
                        env.config.data['images'][name] = abs_path
                        logging.info(f"Overriding image '{name}' with {abs_path}")
                    else:
                        available = ', '.join(images.keys())
                        logging.warning(f"Image '{name}' not found in config (available: {available}), ignoring")
                else:
                    # Positional format: just path (backwards compatibility)
                    # Override first image
                    first_name = list(images.keys())[0]
                    abs_path = os.path.realpath(override)
                    env.config.data['images'][first_name] = abs_path
                    logging.info(f"Overriding first image '{first_name}' with {abs_path}")

    return env


def find_place_name(env, role='main'):
    """Find the RemotePlace name from environment configuration

    Args:
        env: Environment object
        role: Target role to search (default: 'main')

    Returns:
        Place name (str) or None if no RemotePlace found
    """
    targets = env.config.get_targets()
    if not targets or role not in targets:
        return None

    role_config = targets[role]
    resources, _ = target_factory.normalize_config(role_config)
    remote_places = resources.get('RemotePlace', {})

    # Return the first RemotePlace name found
    for place_name in remote_places:
        return place_name

    return None


def prepare_manager(session, loop):
    """Prepare RemotePlaceManager for use with session

    This must be called before using env.get_target() with remote places.

    Args:
        session: ClientSession object
        loop: Event loop
    """
    manager = RemotePlaceManager.get()
    manager.session = session
    manager.loop = loop


async def acquire_place(session, place_name):
    """Acquire a place via coordinator

    Args:
        session: ClientSession object
        place_name: Name of the place to acquire

    Returns:
        bool: True if we acquired the place, False if it was already acquired
    """
    place = session.get_place(place_name)
    if place.acquired:
        host, user = place.acquired.split("/")
        if session.getuser() == user and session.gethostname() == host:
            print(f"Place {place_name} already acquired by this session")
            return False  # Already acquired, we didn't acquire it
        else:
            raise RuntimeError(
                f"Place {place_name} is already acquired by {place.acquired}"
            )

    # Import the protobuf request type
    from labgrid.remote.generated import labgrid_coordinator_pb2

    request = labgrid_coordinator_pb2.AcquirePlaceRequest(placename=place_name)
    await session.stub.AcquirePlace(request)
    await session.sync_with_coordinator()
    print(f"Acquired place {place_name}")
    return True  # We acquired it


async def release_place(session, place_name):
    """Release a previously acquired place

    Args:
        session: ClientSession object
        place_name: Name of the place to release
    """
    place = session.get_place(place_name)
    if not place.acquired:
        # Already released or never acquired
        return

    # Import the protobuf request type
    from labgrid.remote.generated import labgrid_coordinator_pb2

    request = labgrid_coordinator_pb2.ReleasePlaceRequest(placename=place_name)
    await session.stub.ReleasePlace(request)
    await session.sync_with_coordinator()
    print(f"Released place {place_name}")


def interactive_console(console, input_fifo=None, output_fd=None, timeout=0):
    """Provide interactive console access with manual I/O handling

    Console output goes to stdout/screen (and optionally to file).
    Keyboard input (or FIFO input) goes to console.
    Press Ctrl-] to exit.

    Args:
        console: Active ConsoleProtocol driver
        input_fifo: Optional path to named pipe for command input
        output_fd: Optional open file descriptor for logging (already opened)
        timeout: Timeout in seconds (0 = no timeout)
    """
    import select
    import tty
    import termios

    print("=== Interactive Console ===")
    if input_fifo:
        print(f"Reading commands from FIFO: {input_fifo}")
        print("Press Ctrl-C to exit")
    else:
        print("Press Ctrl-] to exit")
    print("=" * 40)

    # Setup input source
    input_fd = None
    old_settings = None
    start_time = time.time() if timeout > 0 else None

    try:
        if input_fifo:
            # Open FIFO in non-blocking mode
            input_fd = os.open(input_fifo, os.O_RDONLY | os.O_NONBLOCK)
        else:
            # Use stdin
            input_fd = sys.stdin.fileno()
            # Only set raw mode if stdin is actually a TTY
            if os.isatty(input_fd):
                old_settings = termios.tcgetattr(input_fd)
                # Set terminal to raw mode (pass through all keypresses)
                tty.setraw(input_fd)

        while True:
            # Check timeout
            if timeout > 0 and (time.time() - start_time >= timeout):
                print("\nTimeout reached")
                break

            # Check for data from input or console
            readable, _, _ = select.select([input_fd], [], [], 0.01)

            # Read from input source and send to console
            if input_fd in readable:
                try:
                    data = os.read(input_fd, 1024)
                    if not data:
                        # EOF
                        if not input_fifo:
                            # EOF on stdin - exit
                            break
                    else:
                        # Check for Ctrl-] only from keyboard (not FIFO)
                        if not input_fifo and len(data) == 1 and data == b'\x1d':
                            break
                        console.write(data)
                except OSError:
                    # EAGAIN/EWOULDBLOCK on non-blocking read
                    pass

            # Read from console and display
            try:
                data = console.read(timeout=0.05, max_size=4096)
                if data:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
                    if output_fd:
                        os.write(output_fd, data)
            except Exception:
                # Timeout is expected
                pass

    except KeyboardInterrupt:
        pass
    finally:
        # Restore terminal settings if using stdin
        if old_settings:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        # Close FIFO if opened
        if input_fifo and input_fd is not None:
            os.close(input_fd)
        print("\n=== Console closed ===")


def non_interactive_console(console, input_fifo=None, output_fd=None, timeout=60):
    """Provide non-interactive console access (no keyboard input)

    Console output goes to file only (not to screen).
    Optional input from FIFO.

    Args:
        console: Active ConsoleProtocol driver
        input_fifo: Optional named pipe to read commands from
        output_fd: Open file descriptor for output (already opened)
        timeout: Timeout for waiting for all output (0 = no timeout)
    """
    import select

    print("=== Non-Interactive Console (output to file only) ===")
    if input_fifo:
        print(f"Reading from FIFO: {input_fifo}")
    print("Press Ctrl-C to stop")
    print("=" * 40)

    input_fd = None
    stop_requested = False

    def signal_handler(sig, frame):
        nonlocal stop_requested
        stop_requested = True
    signal.signal(signal.SIGINT, signal_handler)

    try:
        # Open input FIFO if provided
        if input_fifo:
            # Open FIFO in non-blocking mode
            input_fd = os.open(input_fifo, os.O_RDONLY | os.O_NONBLOCK)

        # Read all output until timeout or Ctrl-C
        start_time = time.time()
        quiet_time = 0

        while not stop_requested:
            # Check timeout
            if timeout > 0 and (time.time() - start_time >= timeout):
                print("\nTimeout reached")
                break

            # Build list of file descriptors to monitor
            read_fds = []
            if input_fd is not None:
                read_fds.append(input_fd)

            # Check for data from input FIFO
            if read_fds:
                readable, _, _ = select.select(read_fds, [], [], 0.01)
            else:
                readable = []
                time.sleep(0.01)

            # Read from FIFO and send to console
            if input_fd in readable:
                try:
                    data = os.read(input_fd, 1024)
                    if data:
                        console.write(data)
                        quiet_time = 0  # Reset quiet timer on input
                except OSError:
                    # EAGAIN/EWOULDBLOCK
                    pass

            # Read from console and write to file
            try:
                data = console.read(timeout=0.05, max_size=4096)
                if output_fd:
                    os.write(output_fd, data)
                quiet_time = 0
            except Exception:
                # Timeout on read
                quiet_time += 1
                # Exit after 5 seconds of no output (only if timeout > 0)
                if timeout > 0 and quiet_time >= 100:  # 5 seconds at 0.05s intervals
                    break

    except KeyboardInterrupt:
        pass
    finally:
        if input_fd is not None:
            os.close(input_fd)
        print("\n=== Console closed ===")


def main():
    """Main program entry point"""
    parser = create_argument_parser()
    args = parser.parse_args()

    # Validate arguments
    if args.non_interactive and not args.output:
        parser.error("--non-interactive requires --output")

    # Set up logging level based on verbosity
    if args.verbose >= 3:
        logging.basicConfig(level=logging.DEBUG)
    elif args.verbose >= 2:
        logging.basicConfig(level=logging.INFO)
    elif args.verbose >= 1:
        logging.basicConfig(level=logging.WARNING)

    input_fifo = None
    fifo_created = False
    output_fd = None
    session = None
    place_name = None
    place_acquired = False  # Track if we acquired (vs. found already acquired)
    loop = None
    target = None
    console = None
    is_qemu = False

    try:
        # Auto-detect LG_BUILDDIR if not set (same logic as conftest.py)
        if 'LG_BUILDDIR' not in os.environ:
            if 'KBUILD_OUTPUT' in os.environ:
                os.environ['LG_BUILDDIR'] = os.environ['KBUILD_OUTPUT']
            elif os.path.isdir('build'):
                os.environ['LG_BUILDDIR'] = os.path.realpath('build')
            else:
                os.environ['LG_BUILDDIR'] = os.getcwd()
            if args.verbose:
                print(f"Auto-detected LG_BUILDDIR: {os.environ['LG_BUILDDIR']}")

        # Make LG_BUILDDIR absolute
        if os.environ.get('LG_BUILDDIR'):
            os.environ['LG_BUILDDIR'] = os.path.realpath(os.environ['LG_BUILDDIR'])

        # Setup input FIFO if requested
        if args.input is not None:
            input_fifo, fifo_created = setup_input_fifo(args.input)

        # Create output file EARLY (so user can tail -f immediately)
        if args.output:
            output_fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            print(f"Output logging to: {args.output}")

        # Load labgrid environment
        print(f"Loading configuration: {args.config}")
        env = load_environment(args.config, args.coordinator, args.proxy, args.images)

        if args.images:
            for override in args.images:
                if '=' in override:
                    name, path = override.split('=', 1)
                    print(f"Image override: {name} = {path}")
                else:
                    print(f"Image override (first): {override}")

        # Check if this config uses a RemotePlace (requires coordinator)
        place_name = find_place_name(env, args.role)

        if place_name:
            # Get coordinator address
            try:
                coordinator_address = args.coordinator or env.config.get_option('coordinator_address')
            except (AttributeError, KeyError):
                coordinator_address = os.environ.get('LG_COORDINATOR', '127.0.0.1:20408')

            # Get and set proxy if configured
            # Priority: --proxy CLI arg > config file > LG_PROXY env var
            try:
                proxy_address = args.proxy or env.config.get_option('proxy')
            except (AttributeError, KeyError):
                proxy_address = os.environ.get('LG_PROXY')

            if proxy_address:
                proxymanager.force_proxy(proxy_address)
                print(f"Using proxy: {proxy_address}")

            print(f"Connecting to coordinator at {coordinator_address}...")

            # Create event loop and session
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            extra = {
                'args': args,
                'env': env,
                'role': args.role,
                'prog': 'barebox-bringup'
            }

            session = start_session(coordinator_address, extra=extra, loop=loop)
            print(f"Connected to coordinator")

            # Prepare the RemotePlaceManager before acquiring
            prepare_manager(session, loop)

            # Acquire the place
            print(f"Acquiring place {place_name}...")
            place_acquired = loop.run_until_complete(acquire_place(session, place_name))

        # Get target
        target = env.get_target(args.role)
        if not target:
            print(f"Error: Target role '{args.role}' not found in config")
            return 1

        # Get console driver (but don't activate yet)
        console = target.get_driver(ConsoleProtocol, activate=False)

        # Check if this is a QEMU target (special handling needed)
        try:
            from labgrid.driver import QEMUDriver
            is_qemu = isinstance(console, QEMUDriver)
        except ImportError:
            pass  # is_qemu remains False

        if is_qemu:
            # QEMU: add -nographic BEFORE activation
            print("Detected QEMU target")
            if '-nographic' not in console.extra_args:
                console.extra_args += ' -nographic'
                if args.verbose:
                    print("Added -nographic option")

        # CRITICAL: Activate console FIRST (before power cycle)
        # This ensures we capture ALL boot output including bootrom
        print("Activating console...")
        target.activate(console)

        if is_qemu:
            # QEMU: console is also the power control
            if not args.no_power_cycle:
                # Start QEMU execution
                print("Starting QEMU...")
                console.on()
                print("QEMU is running!")
            else:
                print("Skipping QEMU start (--no-power-cycle)")
                if not console.status:
                    print("Warning: QEMU is not running, consider removing --no-power-cycle")
        else:
            # Hardware: use strategy
            # First check if strategy driver exists
            try:
                strategy = target.get_driver(Strategy)
            except Exception as e:
                # No strategy configured - console-only mode
                print("No strategy configured - console ready for manual control")
                if args.verbose:
                    print(f"  (No strategy driver: {e})")
                strategy = None

            # If strategy exists, use it - any errors should bail out
            if strategy:
                if not args.no_power_cycle:
                    print("Bootstrapping target...")
                    try:
                        strategy.transition('barebox')
                    except Exception as e:
                        # Strategy failed - this is a fatal error
                        print(f"Error: Strategy failed: {e}")
                        raise
                    print("Target is ready!")
                else:
                    print("Skipping power cycle (--no-power-cycle)")
                    print("Target should already be running")

        # Enter appropriate console mode
        timeout = args.timeout if args.timeout is not None else 0

        if args.non_interactive:
            non_interactive_console(console, input_fifo, output_fd, timeout)
        else:
            # Interactive mode (default)
            interactive_console(console, input_fifo, output_fd, timeout)

        return 0

    except KeyboardInterrupt:
        print("\nInterrupted by user")
        return 130
    except Exception as e:
        print(f"Error: {e}")
        if args.verbose >= 2:
            import traceback
            traceback.print_exc()
        return 1
    finally:
        # Power off the target before cleanup
        if console:
            try:
                if is_qemu:
                    # QEMU: use console.off() to shut down
                    print("Shutting down QEMU...")
                    console.off()
                else:
                    # Hardware: try strategy first (handles multiple power sources),
                    # then fall back to PowerProtocol for simple cases
                    powered_off = False

                    # Try strategy transition to 'off' state
                    try:
                        strategy = target.get_driver(Strategy, activate=False)
                        if strategy:
                            print("Powering off target via strategy...")
                            strategy.transition('off')
                            powered_off = True
                    except Exception:
                        # Strategy not available or doesn't support 'off' state
                        pass

                    # Fallback: use PowerProtocol directly (for non-strategy configs)
                    if not powered_off:
                        try:
                            power = target.get_driver(PowerProtocol, activate=False)
                            print("Powering off target...")
                            power.off()
                        except Exception as e:
                            # No power driver or power off failed
                            if args.verbose:
                                print(f"  (Could not power off: {e})")
            except Exception as e:
                print(f"Warning: Failed to power off target: {e}")

        # Release place only if we acquired it (not if it was already acquired)
        if session and place_name and place_acquired and loop:
            try:
                print(f"Releasing place {place_name}...")
                loop.run_until_complete(release_place(session, place_name))
            except Exception as e:
                print(f"Warning: Failed to release place: {e}")

        # Stop and close session
        if session and loop:
            try:
                loop.run_until_complete(session.stop())
                loop.run_until_complete(session.close())
            except Exception as e:
                print(f"Warning: Failed to close session: {e}")

        # Close event loop
        if loop:
            try:
                loop.close()
            except Exception:
                pass

        # Cleanup
        if output_fd is not None:
            try:
                os.close(output_fd)
            except Exception:
                pass

        if fifo_created and input_fifo:
            try:
                os.unlink(input_fifo)
                print(f"Removed FIFO: {input_fifo}")
            except Exception:
                pass

        if 'env' in locals():
            try:
                env.cleanup()
            except Exception:
                pass
        try:
            StepLogger.stop()
        except Exception:
            pass


if __name__ == '__main__':
    sys.exit(main())
