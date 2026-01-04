# barebox-bringup

A Python tool that makes it easy to bring up [barebox](https://barebox.org) on both emulated and real hardware using [labgrid](https://labgrid.readthedocs.io/).

## Overview

`barebox-bringup` simplifies the process of testing barebox on various platforms:

- **Emulated Hardware**: Works seamlessly with QEMU using the barebox-provided labgrid configuration files found in the `test/` directory of the [barebox repository](https://git.pengutronix.de/cgit/barebox)
- **Real Hardware**: Brings up barebox on physical boards using labgrid for hardware control, power cycling, and serial console access

The tool provides both interactive console access and automated testing modes, with support for logging, programmatic control via FIFOs, and flexible configuration.

## Features

- Interactive and non-interactive console modes
- Support for both QEMU emulation and real hardware targets
- Console output logging
- FIFO-based command injection for automation
- Automatic target bootstrapping via labgrid strategies
- Works with existing barebox labgrid test configurations

## Installation

### From source

```bash
pip3 install .
```

### Development installation

```bash
pip3 install -e .
```

## Quick Start

### Using with barebox QEMU configurations

The barebox repository includes ready-to-use labgrid configuration files in the `test/` directory. To test barebox on a QEMU-emulated ARM board:

```bash
# From your barebox source directory
barebox-bringup -c test/arm/virt@multi_v8_defconfig.yaml
```

### Basic interactive mode (real hardware)

```bash
barebox-bringup -c test/arm/imx6s-riotboard.yaml
```

### Interactive with output logging

```bash
barebox-bringup -c test/arm/imx6s-riotboard.yaml -o session.log
# In another terminal:
tail -f session.log
```

### Interactive with auto-created FIFO for programmatic control

```bash
barebox-bringup -c test/arm/imx6s-riotboard.yaml -i -o boot.log &
# Tool prints: Created FIFO: /tmp/barebox-input-12345.fifo
echo "version" > /tmp/barebox-input-12345.fifo
```

### Non-interactive mode (output only)

```bash
barebox-bringup -c test/arm/imx6s-riotboard.yaml -n -o boot.log --timeout 0
```

## Command-line options

```
-c, --config FILE       Labgrid configuration file (required)
-n, --non-interactive   Non-interactive mode: no keyboard input
-o, --output FILE       Output file for console log
-i, --input [FIFO]      Input FIFO (creates temp FIFO if no path given)
-r, --role ROLE         Target role in config file (default: main)
--coordinator ADDR      Labgrid coordinator address
--no-power-cycle        Skip power cycle, assume target is on
--timeout SECS          Timeout in seconds (default: 60, 0 = no timeout)
-v, --verbose           Increase verbosity (-v, -vv, -vvv)
```

## Requirements

- Python 3.7+
- [labgrid](https://github.com/labgrid-project/labgrid) - Hardware control and testing framework
- For QEMU targets: QEMU (typically `qemu-system-arm`, `qemu-system-aarch64`, etc.)
- For hardware targets: Appropriate labgrid exporter and hardware setup

## How It Works

1. Loads a labgrid configuration file (YAML) that describes the target hardware or QEMU setup
2. Activates the console driver (serial port for hardware, QEMU stdio for emulation)
3. For hardware: Optionally power-cycles the target and uses labgrid strategies to reach barebox
4. For QEMU: Starts the emulator with the configured options
5. Provides interactive console access or automated testing mode

## Contributing

Contributions are welcome! This tool was created to simplify barebox development and testing workflows.

## License

GPL-2.0-only

## Author

Written with Claude Code by Sascha Hauer <s.hauer@pengutronix.de>
