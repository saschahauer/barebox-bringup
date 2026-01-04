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

from labgrid import Environment
from labgrid.protocol import ConsoleProtocol
from labgrid.strategy import Strategy
from labgrid.logging import basicConfig, StepLogger


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

  # Interactive with auto-created FIFO for programmatic control
  %(prog)s -c test/arm/imx6s-riotboard.yaml -i -o boot.log &
  # Tool prints: Created FIFO: /tmp/barebox-input-12345.fifo
  echo "version" > /tmp/barebox-input-12345.fifo

  # Interactive with specified FIFO
  %(prog)s -c test/arm/imx6s-riotboard.yaml -i /tmp/cmds.fifo -o boot.log &
  echo "help" > /tmp/cmds.fifo

  # Non-interactive (no keyboard input, output only)
  %(prog)s -c test/arm/imx6s-riotboard.yaml -n -o boot.log --timeout 0
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

    # Control options
    parser.add_argument('--no-power-cycle', action='store_true',
                        help='Skip power cycle, assume target is already on')
    parser.add_argument('--timeout', type=int, default=60,
                        help='Timeout in seconds for operations (default: 60, 0 = no timeout)')

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


def load_environment(config_file, coordinator=None):
    """Load labgrid environment from configuration file

    Args:
        config_file: Path to YAML configuration file
        coordinator: Optional coordinator address override (highest priority)

    Returns:
        Environment object

    Note:
        Coordinator priority (highest to lowest):
        1. --coordinator command line argument (overrides everything)
        2. coordinator_address in YAML config file (options: section)
        3. LG_COORDINATOR environment variable
        4. Default: 127.0.0.1:20408
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

    return env


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
        env = load_environment(args.config, args.coordinator)

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
            is_qemu = False

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
            try:
                strategy = target.get_driver(Strategy)

                if not args.no_power_cycle:
                    print("Bootstrapping target...")
                    strategy.transition('barebox')
                    print("Target is ready!")
                else:
                    print("Skipping power cycle (--no-power-cycle)")
                    print("Target should already be running")

            except Exception as e:
                # No strategy configured - console-only mode
                print("No strategy configured - console ready for manual control")
                if args.verbose:
                    print(f"  (Strategy error: {e})")

        # Enter appropriate console mode
        timeout = args.timeout if args.timeout > 0 else 0

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
