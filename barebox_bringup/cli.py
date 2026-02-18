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
from labgrid.driver import QEMUDriver
from labgrid.driver import SerialDriver


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
  %(prog)s -c test/arm/am62l-evk.yaml --image tiboot3=/path/to/tiboot3.img --image barebox=/path/to/barebox.img

  # Use known-good image set from config file
  %(prog)s -c test/arm/am62l-evk.yaml --images known_good

  # Use testing image set from config file
  %(prog)s -c test/arm/am62l-evk.yaml --images testing

  # Interactive with auto-created FIFO for programmatic control
  %(prog)s -c test/arm/imx6s-riotboard.yaml -i -o boot.log &
  # Tool prints: Created FIFO: /tmp/barebox-input-12345.fifo
  echo "version" > /tmp/barebox-input-12345.fifo

  # Interactive with specified FIFO
  %(prog)s -c test/arm/imx6s-riotboard.yaml -i /tmp/cmds.fifo -o boot.log &
  echo "help" > /tmp/cmds.fifo

  # Interactive with commands from regular file (watches file for new input)
  %(prog)s -c test/arm/imx6s-riotboard.yaml -f commands.txt -o boot.log &
  # In another terminal: echo "version" >> commands.txt
  # In another terminal: echo "help" >> commands.txt

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
    parser.add_argument('-f', '--file', type=str, metavar='FILE',
                        help='Input file: read commands from regular file, watching for new input (like tail -f)')

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
    parser.add_argument('-s', '--state', type=str, default='on',
                        help='Target state to transition to (default: on)')
    parser.add_argument('--timeout', type=int, default=None,
                        help='Timeout in seconds for operations (default: no timeout)')
    parser.add_argument('--image', action='append', dest='images',
                        help='Override image: --image name=path or --image path (for single image configs)')
    parser.add_argument('--images', type=str, default=None, dest='image_set',
                        help='Select named image set from config (default: auto-detect from environment)')
    parser.add_argument('--no-write', action='store_true',
                        help='Skip writing images to SD card, boot from existing card (SD-MUX only)')

    # Display options
    parser.add_argument('--graphic', '--graphics', action='store_true', dest='graphics',
                        help='Enable QEMU graphics output (default: disabled for headless operation)')

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


def determine_image_set(requested_set=None):
    """Determine which image set to use based on environment and user input

    Args:
        requested_set: Image set explicitly requested by user (or None for auto-detect)

    Returns:
        str: Name of image set to use ('default', 'yocto', 'barebox', or user-specified)

    Priority:
        1. User explicitly specified via --images (highest priority)
        2. BBPATH environment variable set -> use 'yocto'
        3. Inside barebox source tree -> use 'barebox'
        4. Default to 'default'
    """
    if requested_set is not None:
        # User explicitly requested a specific set
        return requested_set

    # Auto-detect based on environment
    if 'BBPATH' in os.environ:
        logging.info("Detected BBPATH environment variable, using 'yocto' image set")
        return 'yocto'

    # Check if we're inside a barebox source tree
    if os.path.exists('commands/barebox-update.c'):
        logging.info("Detected barebox source tree, using 'barebox' image set")
        return 'barebox'

    # Check if we're inside a ptxdist workspace
    if os.path.exists('configs/ptxconfig'):
        logging.info("Detected ptxdist workspace, using 'ptxdist' image set")
        return 'ptxdist'

    # Default
    return 'default'


def normalize_image_config(image_dict):
    """Normalize image configuration to support both formats.

    Old format: image_name: path
    New format: image_name: {image: path, seek: N, ...}

    Args:
        image_dict: Raw image configuration dict from YAML

    Returns:
        tuple: (images_dict, image_config_dict)
            images_dict: {name: path} for labgrid compatibility
            image_config_dict: {name: {image: path, seek: N, ...}} full config
    """
    images = {}
    image_config = {}

    for name, value in image_dict.items():
        if isinstance(value, dict):
            # New format with attributes
            if 'image' not in value:
                raise ValueError(f"Image '{name}' config missing 'image' key")
            images[name] = value['image']
            image_config[name] = value.copy()
        else:
            # Old format: just a path string
            images[name] = value
            image_config[name] = {'image': value}

    return images, image_config


def load_environment(config_file, coordinator=None, proxy=None, image_overrides=None, image_set='default', no_write=False):
    """Load labgrid environment from configuration file

    Args:
        config_file: Path to YAML configuration file
        coordinator: Optional coordinator address override (highest priority)
        proxy: Optional proxy address override (highest priority)
        image_overrides: Optional list of image overrides (overrides config file images)
        image_set: Name of image set to use from config (default: 'default')
        no_write: If True, skip writing images to SD card (SD-MUX only)

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

        Image set selection:
        Priority (highest to lowest):
        1. User explicitly specified via --images
        2. BBPATH environment variable set -> use 'yocto'
        3. Default to 'default'

        Configuration format:
        New format uses 'image-sets:' with nested named sets:
        - image-sets: { default: {...}, known_good: {...} }
        Old format uses 'images:' with flat dict (backwards compatible)

        Image overrides:
        Supports two formats:
        - Named: "name=path" - Override specific image by name
        - Positional: "path" - Override first image (backwards compatibility)

        Examples:
        - --image tiboot3=/path/to/tiboot3.img --image barebox=/path/to/barebox.img
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

    # Handle --images: select named image set from image-sets
    # This must happen BEFORE image overrides so overrides can modify the selected set
    image_sets = env.config.data.get('image-sets')
    images = env.config.data.get('images')  # Old format fallback

    if image_sets:
        # New format with image sets
        # Expected: image-sets: { default: { img1: path1 }, known_good: { img1: path1 } }
        # Or with attributes: image-sets: { default: { img1: { image: path1, seek: 64 } } }
        if image_set not in image_sets:
            available = ', '.join(sorted(image_sets.keys()))
            print(f"Error: Image set '{image_set}' not found in config")
            print(f"Available image sets: {available}")
            sys.exit(1)

        # Select the requested image set
        selected_images = image_sets[image_set]

        if not selected_images:
            print(f"Error: Image set '{image_set}' exists but contains no images")
            sys.exit(1)

        # Normalize image config to support both old and new formats
        try:
            images_paths, image_config = normalize_image_config(selected_images)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)

        # Debug: show what was normalized
        logging.debug(f"Normalized images_paths: {images_paths}")
        logging.debug(f"Normalized image_config: {image_config}")

        # Place normalized paths into 'images' section for labgrid compatibility
        env.config.data['images'] = images_paths
        # Store full config (with seek, etc.) for strategies
        env.config.data['image-config'] = image_config
        logging.info(f"Using image set '{image_set}' with images: {', '.join(images_paths.keys())}")
    elif images:
        # Fallback to old 'images:' key (flat dict, no sets)
        if image_set != 'default':
            print(f"Warning: Config uses old 'images:' format, ignoring --images '{image_set}'")
            print("To use image sets, update config to: image-sets: { default: {...}, known_good: {...} }")

        # Normalize image config
        try:
            images_paths, image_config = normalize_image_config(images)
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)

        logging.info(f"Using images from 'images:' section (old format): {', '.join(images_paths.keys())}")
        env.config.data['images'] = images_paths
        env.config.data['image-config'] = image_config
    else:
        print("Error: No 'image-sets:' or 'images:' section found in config")
        print("Expected format: image-sets: { default: {...}, known_good: {...}, ... }")
        sys.exit(1)

    # Override image paths if --image was specified
    # This happens AFTER image set selection so overrides are applied to the selected set
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

    # Set no_write option if --no-write was specified
    if no_write:
        env.config.set_option('no_write', True)
        logging.info("--no-write: Skipping image writing to SD card")

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

    request = labgrid_coordinator_pb2.AcquirePlaceRequest(placename=place.name)
    await session.stub.AcquirePlace(request)
    await session.sync_with_coordinator()
    print(f"Acquired place {place.name}")
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

    request = labgrid_coordinator_pb2.ReleasePlaceRequest(placename=place.name)
    await session.stub.ReleasePlace(request)
    await session.sync_with_coordinator()
    print(f"Released place {place.name}")


def _open_input_source(input_fifo, input_file):
    """Open input source (FIFO, file, or stdin) and setup terminal

    Args:
        input_fifo: Optional path to FIFO, or None
        input_file: Optional path to regular file, or None

    Returns:
        tuple: (input_fd, old_settings, is_file)
               old_settings is None if stdin is not a TTY or using FIFO/file
               is_file is True if using a regular file (not FIFO)
    """
    import tty
    import termios

    if input_file:
        # Open regular file in blocking mode
        input_fd = os.open(input_file, os.O_RDONLY)
        old_settings = None
        is_file = True
    elif input_fifo:
        # Open FIFO in non-blocking mode
        input_fd = os.open(input_fifo, os.O_RDONLY | os.O_NONBLOCK)
        old_settings = None
        is_file = False
    else:
        # Use stdin
        input_fd = sys.stdin.fileno()
        old_settings = None
        is_file = False
        # Only set raw mode if stdin is actually a TTY
        if os.isatty(input_fd):
            old_settings = termios.tcgetattr(input_fd)
            tty.setraw(input_fd)

    return input_fd, old_settings, is_file


def _close_input_source(input_fd, old_settings, input_fifo, input_file):
    """Close input source and restore terminal settings

    Args:
        input_fd: File descriptor to close (or None)
        old_settings: Terminal settings to restore (or None)
        input_fifo: FIFO path (for determining whether to close fd)
        input_file: File path (for determining whether to close fd)
    """
    import termios

    # Restore terminal settings if using stdin
    if old_settings:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)

    # Close FIFO or file if opened
    if (input_fifo or input_file) and input_fd is not None:
        os.close(input_fd)


def _read_from_input(input_fd, input_fifo, input_file, is_file, check_ctrl_bracket=True):
    """Read data from input source (FIFO, file, or stdin)

    Args:
        input_fd: File descriptor to read from
        input_fifo: FIFO path (or None)
        input_file: File path (or None)
        is_file: True if reading from regular file
        check_ctrl_bracket: If True, check for Ctrl-] exit key

    Returns:
        tuple: (data, should_exit)
               data is bytes or None, should_exit is boolean
    """
    try:
        data = os.read(input_fd, 1024)
        if not data:
            # EOF
            if not input_fifo and not input_file:
                # EOF on stdin - exit
                return None, True
            # EOF on FIFO or regular file - keep running (wait for more input)
            return None, False

        # Check for Ctrl-] only from keyboard (not FIFO or file)
        if check_ctrl_bracket and not input_fifo and not input_file and len(data) == 1 and data == b'\x1d':
            return None, True

        return data, False
    except OSError:
        # EAGAIN/EWOULDBLOCK on non-blocking read (FIFO only)
        return None, False


def _read_from_console(console, verbose=False):
    """Read data from console with timeout handling

    Args:
        console: Active ConsoleProtocol driver
        verbose: If True, log all exceptions for debugging

    Returns:
        tuple: (data, console_alive)
               data is bytes or None
               console_alive is False if console has closed/failed
    """
    import logging
    try:
        data = console.read(timeout=0.05, max_size=4096)
        return (data if data else None, True)
    except TimeoutError:
        # Normal timeout, console still alive
        return (None, True)
    except (BrokenPipeError, ConnectionError, EOFError):
        # Connection definitively closed
        return (None, False)
    except OSError as e:
        # Check if it's a "bad file descriptor" or similar critical error
        import errno
        if e.errno in (errno.EBADF, errno.EPIPE, errno.ECONNRESET):
            return (None, False)
        # For all other OSErrors, treat as timeout (be conservative)
        if verbose:
            logging.info(f"OSError from console.read() treated as timeout: {type(e).__name__}: {e}")
        return (None, True)
    except Exception as e:
        # Be very conservative for non-QEMU consoles
        # Only treat as dead for very specific exception types
        # Most labgrid exceptions for timeouts should be treated as "alive"
        if verbose:
            logging.info(f"Exception from console.read() treated as timeout: {type(e).__name__}: {e}")
        # Otherwise treat as normal timeout
        return (None, True)


def _check_console_alive(console):
    """Check if console/target is still alive

    Args:
        console: Console driver

    Returns:
        bool: True if alive, False if dead
    """
    from labgrid.driver import QEMUDriver

    # For QEMU, check if the process is still running
    if isinstance(console, QEMUDriver):
        try:
            # QEMUDriver stores the subprocess in _child
            if hasattr(console, '_child') and console._child:
                # poll() returns None if process is still running
                return console._child.poll() is None
        except Exception:
            pass

    # For other console types, assume alive (we'll detect on read/write)
    return True


def interactive_console(console, input_fifo=None, input_file=None, output_fd=None, timeout=0):
    """Provide interactive console access with manual I/O handling

    Console output goes to stdout/screen (and optionally to file).
    Keyboard input (or FIFO/file input) goes to console.
    Press Ctrl-] to exit.

    Args:
        console: Active ConsoleProtocol driver
        input_fifo: Optional path to named pipe for command input
        input_file: Optional path to regular file for command input
        output_fd: Optional open file descriptor for logging (already opened)
        timeout: Timeout in seconds (0 = no timeout)
    """
    import select

    print("=== Interactive Console ===")
    if input_file:
        print(f"Reading commands from file: {input_file}")
        print("Watching file for new input. Press Ctrl-C to exit")
    elif input_fifo:
        print(f"Reading commands from FIFO: {input_fifo}")
        print("Press Ctrl-C to exit")
    else:
        print("Press Ctrl-] to exit")
    print("=" * 40)

    input_fd = None
    old_settings = None
    is_file = False
    start_time = time.time() if timeout > 0 else None

    try:
        input_fd, old_settings, is_file = _open_input_source(input_fifo, input_file)

        # Check if we should monitor input_fd or not
        # Only monitor stdin if it's a TTY, or always monitor FIFO/file
        monitor_input = input_fifo is not None or input_file is not None or (input_fd is not None and os.isatty(input_fd))

        while True:
            # Check if console/target is still alive
            if not _check_console_alive(console):
                print("\nConsole closed (target terminated)")
                break
            # Check timeout
            if timeout > 0 and (time.time() - start_time >= timeout):
                print("\nTimeout reached")
                break

            # Check for data from input or console
            # Only include input_fd in select if we should monitor it
            if monitor_input:
                readable, _, _ = select.select([input_fd], [], [], 0.01)
            else:
                readable = []
                time.sleep(0.01)

            # Read from input source and send to console
            if input_fd in readable:
                data, should_exit = _read_from_input(input_fd, input_fifo, input_file, is_file, check_ctrl_bracket=True)
                if should_exit:
                    break
                if data:
                    try:
                        console.write(data)
                    except Exception:
                        # Console write failed - console is closed
                        print("\nConsole closed (target terminated)")
                        break

            # Read from console and display
            data, console_alive = _read_from_console(console)
            if not console_alive:
                print("\nConsole closed (target terminated)")
                break
            if data:
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                if output_fd:
                    os.write(output_fd, data)

    except KeyboardInterrupt:
        pass
    finally:
        _close_input_source(input_fd, old_settings, input_fifo, input_file)
        print("\n=== Console closed ===")


def non_interactive_console(console, input_fifo=None, input_file=None, output_fd=None, timeout=60):
    """Provide non-interactive console access (no keyboard input)

    Console output goes to file only (not to screen).
    Optional input from FIFO or file.

    Args:
        console: Active ConsoleProtocol driver
        input_fifo: Optional named pipe to read commands from
        input_file: Optional regular file to read commands from
        output_fd: Open file descriptor for output (already opened)
        timeout: Timeout for waiting for all output (0 = no timeout)
    """
    import select

    print("=== Non-Interactive Console (output to file only) ===")
    if input_file:
        print(f"Reading from file: {input_file}")
    elif input_fifo:
        print(f"Reading from FIFO: {input_fifo}")
    print("Press Ctrl-C to stop")
    print("=" * 40)

    input_fd = None
    is_file = False
    stop_requested = False

    def signal_handler(sig, frame):
        nonlocal stop_requested
        stop_requested = True
    signal.signal(signal.SIGINT, signal_handler)

    try:
        # Open input FIFO or file if provided (no terminal setup for non-interactive)
        if input_file:
            input_fd = os.open(input_file, os.O_RDONLY)
            is_file = True
        elif input_fifo:
            input_fd = os.open(input_fifo, os.O_RDONLY | os.O_NONBLOCK)
            is_file = False

        # Read all output until timeout or Ctrl-C
        start_time = time.time()
        quiet_time = 0

        while not stop_requested:
            # Check if console/target is still alive
            if not _check_console_alive(console):
                print("\nConsole closed (target terminated)")
                break

            # Check timeout
            if timeout > 0 and (time.time() - start_time >= timeout):
                print("\nTimeout reached")
                break

            # Build list of file descriptors to monitor
            read_fds = [input_fd] if input_fd is not None else []

            # Check for data from input FIFO or file
            if read_fds:
                readable, _, _ = select.select(read_fds, [], [], 0.01)
            else:
                readable = []
                time.sleep(0.01)

            # Read from FIFO/file and send to console
            if input_fd in readable:
                data, should_exit = _read_from_input(input_fd, input_fifo, input_file, is_file, check_ctrl_bracket=False)
                if should_exit:
                    # Should not happen for FIFO/file in non-interactive mode
                    break
                if data:
                    try:
                        console.write(data)
                        quiet_time = 0  # Reset quiet timer on input
                    except Exception:
                        # Console write failed - console is closed
                        print("\nConsole closed (target terminated)")
                        break

            # Read from console and write to file
            data, console_alive = _read_from_console(console)
            if not console_alive:
                print("\nConsole closed (target terminated)")
                break
            if data:
                if output_fd:
                    os.write(output_fd, data)
                quiet_time = 0
            else:
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


def setup_build_directory(verbose=False):
    """Auto-detect and set LG_BUILDDIR if not already set

    Args:
        verbose: If True, print the detected directory
    """
    if 'LG_BUILDDIR' not in os.environ:
        if 'KBUILD_OUTPUT' in os.environ:
            os.environ['LG_BUILDDIR'] = os.environ['KBUILD_OUTPUT']
        elif os.path.isdir('build'):
            os.environ['LG_BUILDDIR'] = os.path.realpath('build')
        else:
            os.environ['LG_BUILDDIR'] = os.getcwd()
        if verbose:
            print(f"Auto-detected LG_BUILDDIR: {os.environ['LG_BUILDDIR']}")

    # Make LG_BUILDDIR absolute
    if os.environ.get('LG_BUILDDIR'):
        os.environ['LG_BUILDDIR'] = os.path.realpath(os.environ['LG_BUILDDIR'])


def setup_coordinator_session(env, coordinator_address, proxy_address, place_name, args):
    """Create event loop and connect to coordinator

    Args:
        env: Environment object
        coordinator_address: Coordinator address string
        proxy_address: Proxy address string (or None)
        place_name: Place name to display in messages
        args: Parsed command-line arguments

    Returns:
        tuple: (loop, session) - Event loop and session objects
    """
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

    return loop, session, place_acquired


def bootstrap_target(target, console, args, state='on'):
    """Bootstrap target hardware

    Args:
        target: Target object
        console: Console driver
        args: Parsed command-line arguments
        state: Target state to transition to (default: 'on')
    """
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
            print(f"Bootstrapping target to '{state}' state...")
            try:
                strategy.transition(state)
            except Exception as e:
                # Strategy failed - this is a fatal error
                print(f"Error: Strategy failed: {e}")
                raise
            print("Target is ready!")
        else:
            print("Skipping power cycle (--no-power-cycle)")
            print("Target should already be running")


def cleanup_resources(console, target, session, loop, place_name,
                      place_acquired, output_fd, input_fifo, fifo_created,
                      env, verbose=False):
    """Cleanup all resources on exit

    Args:
        console: Console driver (or None)
        target: Target object (or None)
        session: ClientSession object (or None)
        loop: Event loop (or None)
        place_name: Place name (or None)
        place_acquired: True if we acquired the place
        output_fd: Output file descriptor (or None)
        input_fifo: Input FIFO path (or None)
        fifo_created: True if we created the FIFO
        env: Environment object (or None)
        verbose: If True, print additional error details
    """
    import atexit

    # Unregister labgrid's atexit handler to prevent duplicate cleanup attempts
    # We'll handle cleanup ourselves to avoid errors when QEMU has already exited
    if target:
        try:
            atexit.unregister(target._atexit_cleanup)
        except Exception:
            # atexit.unregister might not work with bound methods in older Python
            pass

    # Deactivate console first to prevent issues if target already terminated
    if console and target:
        try:
            target.deactivate(console)
        except Exception as e:
            # Console may already be closed (e.g., QEMU exited)
            if verbose:
                print(f"  (Console deactivation: {e})")

    # Power off the target before cleanup
    if target:
        try:
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
            except Exception as e:
                # Strategy not available or doesn't support 'off' state
                # If QEMU already exited, this will fail - that's okay
                if verbose:
                    print(f"  (Strategy transition to off: {e})")

            # Fallback: use PowerProtocol directly (for non-strategy configs)
            if not powered_off:
                try:
                    power = target.get_driver(PowerProtocol, activate=False)
                    print("Powering off target...")
                    power.off()
                except Exception as e:
                    # No power driver or power off failed
                    if verbose:
                        print(f"  (Could not power off: {e})")

            # Deactivate all remaining drivers to prevent labgrid atexit from trying
            # This is especially important for QEMU where the process may have already exited
            try:
                target.deactivate_all_drivers()
            except Exception as e:
                # Deactivation may fail if target already terminated - that's expected
                if verbose:
                    print(f"  (Deactivate all drivers: {e})")
        except Exception as e:
            if verbose:
                print(f"  (Power off error: {e})")

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

    # Cleanup files
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

    # Call target.cleanup() since we unregistered labgrid's atexit handler
    # We've already deactivated drivers above, so this should mostly be cleanup of other resources
    if target:
        try:
            target.cleanup()
        except Exception as e:
            # Cleanup may fail if QEMU already exited - suppress common errors
            if verbose:
                print(f"  (Target cleanup: {e})")

    # Cleanup environment
    if env:
        try:
            env.cleanup()
        except Exception:
            pass

    try:
        StepLogger.stop()
    except Exception:
        pass


def main():
    """Main program entry point"""
    parser = create_argument_parser()
    args = parser.parse_args()

    # Validate arguments
    if args.non_interactive and not args.output:
        parser.error("--non-interactive requires --output")

    if args.input is not None and args.file:
        parser.error("--input and --file are mutually exclusive")

    # Set up logging level based on verbosity
    if args.verbose >= 3:
        logging.basicConfig(level=logging.DEBUG)
    elif args.verbose >= 2:
        logging.basicConfig(level=logging.INFO)
    elif args.verbose >= 1:
        logging.basicConfig(level=logging.WARNING)

    input_fifo = None
    fifo_created = False
    input_file = None
    output_fd = None
    session = None
    place_name = None
    place_acquired = False  # Track if we acquired (vs. found already acquired)
    loop = None
    target = None
    console = None

    try:
        # Auto-detect LG_BUILDDIR if not set
        setup_build_directory(verbose=args.verbose)

        # Setup input FIFO if requested
        if args.input is not None:
            input_fifo, fifo_created = setup_input_fifo(args.input)

        # Setup input file if requested
        if args.file:
            if not os.path.exists(args.file):
                print(f"Error: Input file does not exist: {args.file}")
                return 1
            if not os.path.isfile(args.file):
                print(f"Error: Input path is not a regular file: {args.file}")
                return 1
            input_file = args.file
            print(f"Reading commands from file: {input_file}")

        # Create output file EARLY (so user can tail -f immediately)
        if args.output:
            output_fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            print(f"Output logging to: {args.output}")

        # Determine which image set to use (respects --images or auto-detects from environment)
        image_set = determine_image_set(args.image_set)
        if args.image_set is None and image_set != 'default':
            print(f"Auto-detected image set: '{image_set}' (from environment)")

        # Load labgrid environment
        print(f"Loading configuration: {args.config}")
        env = load_environment(args.config, args.coordinator, args.proxy, args.images, image_set, args.no_write)

        if args.images:
            for override in args.images:
                if '=' in override:
                    name, path = override.split('=', 1)
                    print(f"Image override: {name} = {path}")
                else:
                    print(f"Image override (first): {override}")

        if image_set != 'default':
            images = env.config.get_images()
            print(f"Using image set '{image_set}':")
            for name, path in images.items():
                print(f"  - {name}: {path}")

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

            # Connect to coordinator and acquire place
            loop, session, place_acquired = setup_coordinator_session(
                env, coordinator_address, proxy_address, place_name, args
            )

        # Get target
        target = env.get_target(args.role)
        if not target:
            print(f"Error: Target role '{args.role}' not found in config")
            return 1

        # Get console driver (but don't activate yet)
        console = target.get_driver(ConsoleProtocol, activate=False)

        # Increase SerialDriver timeout for slow hardware (e.g. rfc2217 connections)
        if isinstance(console, SerialDriver):
            console.timeout = 10.0

        # Override QEMU display setting based on --graphics flag
        # This prevents QEMU from trying to open a graphics window without X11/Wayland
        if isinstance(console, QEMUDriver):
            if not args.graphics:
                # Default: disable graphics for headless operation
                if console.display != "none":
                    original_display = console.display
                    console.display = "none"
                    if args.verbose:
                        print(f"Overriding QEMU display setting from '{original_display}' to 'none' (use --graphics to enable)")
                    logging.info(f"QEMU display override: {console.display} -> none (use --graphics to enable)")
            else:
                # User explicitly requested graphics, keep the configured display setting
                if args.verbose:
                    print(f"Graphics enabled: using display setting '{console.display}' from config")

        # Bootstrap target hardware
        bootstrap_target(target, console, args, state=args.state)

        # Enter appropriate console mode
        timeout = args.timeout if args.timeout is not None else 0

        if args.non_interactive:
            non_interactive_console(console, input_fifo, input_file, output_fd, timeout)
        else:
            # Interactive mode (default)
            interactive_console(console, input_fifo, input_file, output_fd, timeout)

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
        cleanup_resources(
            console=console,
            target=target,
            session=session,
            loop=loop,
            place_name=place_name,
            place_acquired=place_acquired,
            output_fd=output_fd,
            input_fifo=input_fifo,
            fifo_created=fifo_created,
            env=env if 'env' in locals() else None,
            verbose=args.verbose if 'args' in locals() else False
        )


if __name__ == '__main__':
    sys.exit(main())
