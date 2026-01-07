# barebox-bringup

A Python tool that makes it easy to bring up [barebox](https://barebox.org) on real hardware using [labgrid](https://labgrid.readthedocs.io/).

## Overview

`barebox-bringup` simplifies the process of testing barebox on physical hardware:

- **Real Hardware**: Brings up barebox on physical boards using labgrid for hardware control, power cycling, and serial console access

The tool provides both interactive console access and automated testing modes, with support for logging, programmatic control via FIFOs, and flexible configuration.

## Features

- Interactive and non-interactive console modes
- Support for real hardware targets
- Console output logging
- FIFO-based command injection for automation
- Automatic target bootstrapping via labgrid strategies
- Works with labgrid configuration files

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

### Basic interactive mode

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

### Override barebox image from command line

```bash
barebox-bringup -c test/arm/imx6s-riotboard.yaml --image /path/to/custom-barebox.img
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
--timeout SECS          Timeout in seconds (default: no timeout)
--image PATH            Override image path from config file
-v, --verbose           Increase verbosity (-v, -vv, -vvv)
```

## Requirements

- Python 3.7+
- [labgrid](https://github.com/labgrid-project/labgrid) - Hardware control and testing framework
- Appropriate labgrid exporter and hardware setup

## How It Works

1. Loads a labgrid configuration file (YAML) that describes the target hardware
2. Activates the console driver (serial port)
3. Optionally power-cycles the target and uses labgrid strategies to reach barebox
4. Provides interactive console access or automated testing mode

## Contributing

Contributions are welcome! This tool was created to simplify barebox development and testing workflows.

## License

GPL-2.0-only

## Author

Written with Claude Code by Sascha Hauer <s.hauer@pengutronix.de>
