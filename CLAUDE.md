# barebox-bringup Project Overview

## What This Tool Does

`barebox-bringup` is a Python CLI utility for bringing up [barebox](https://barebox.org) bootloader on both emulated (QEMU) and real hardware platforms. It leverages [labgrid](https://labgrid.readthedocs.io/) for hardware control and automation.

## Key Components

### Main CLI (`barebox_bringup/cli.py`)
The main entry point provides:
- Interactive console access to barebox (keyboard input + output)
- Non-interactive mode (output-only, for automation)
- FIFO-based command injection for programmatic control
- Console output logging
- Support for QEMU and real hardware targets
- Power cycling and bootstrap strategies via labgrid

### Architecture

1. **Configuration Loading**: Reads labgrid YAML configs describing target hardware
2. **Console Activation**: Activates console driver (serial for hardware, stdio for QEMU)
3. **Target Bootstrap**:
   - Hardware: Uses labgrid strategies to power cycle and reach barebox
   - QEMU: Starts emulator with appropriate options
4. **Console Modes**: Provides interactive or automated console access

## Typical Workflows

### QEMU Testing (Development)
```bash
# From barebox source directory with built images
barebox-bringup -c test/arm/virt@multi_v8_defconfig.yaml
```
- Uses labgrid configs from barebox's `test/` directory
- Auto-detects build output via `LG_BUILDDIR` (or `KBUILD_OUTPUT`)
- Starts QEMU with `-nographic` for console access

### Hardware Testing (Lab)
```bash
barebox-bringup -c examples/arm/imx6s-riotboard.yaml
```
- Connects to hardware via labgrid (serial console, power control)
- Uses bootstrap strategies to reach barebox
- Requires labgrid infrastructure (coordinator, exporters)

### Automation/CI
```bash
# Non-interactive with logging and FIFO control
barebox-bringup -c config.yaml -n -o boot.log -i /tmp/commands.fifo &
echo "version" > /tmp/commands.fifo
echo "help" > /tmp/commands.fifo
```

## Configuration Files

### Labgrid YAML Format
See `examples/arm/imx6s-riotboard.yaml` for reference structure:
- `targets`: Hardware resources and drivers
- `drivers`: Console, power, bootstrap drivers
- `images`: Barebox images to load (uses `$LG_BUILDDIR` template)
- `imports`: Custom strategies (like `strategy-bootstrap.py`)
- `options`: Coordinator address, etc.

### Example Strategy (`examples/strategy-bootstrap.py`)
Custom labgrid strategy for bootstrap-based targets:
- Supports USB recovery mode (i.MX, Rockchip, etc.)
- Supports JTAG/SWD bootstrap (OpenOCD)
- States: `off` -> `barebox`
- Implements `BootstrapProtocol` for loading images

## Environment Variables

- `LG_BUILDDIR`: Path to barebox build directory (auto-detected from `KBUILD_OUTPUT` or `./build`)
- `LG_COORDINATOR`: Labgrid coordinator address (can be overridden via `--coordinator`)

## Important Code Patterns

### Console Activation Order
CRITICAL: Console must be activated BEFORE power cycling to capture all boot output:
```python
target.activate(console)  # Activate first
power.cycle()            # Then power cycle
```

### QEMU Handling
QEMU targets require special handling:
- Add `-nographic` to `extra_args` before activation
- Console driver doubles as power control (`.on()` starts QEMU)

### TTY Detection
The tool properly handles both TTY and non-TTY stdin:
```python
if os.isatty(input_fd):
    tty.setraw(input_fd)  # Only set raw mode if TTY
```

## Dependencies

- Python 3.7+
- labgrid (hardware control framework)
- QEMU (for emulation testing)
- For hardware: labgrid coordinator + exporters with appropriate hardware

## Testing Philosophy

This tool is designed for **early bootloader bringup** and **hardware validation**:
- Get console access quickly
- Test bootstrap mechanisms
- Validate hardware configurations
- Automate boot testing in CI

It complements barebox's pytest-based test infrastructure by providing direct console access for debugging and interactive development.

## Related Projects

- **barebox**: https://github.com/barebox/barebox.git
- **labgrid**: https://github.com/labgrid-project/labgrid
- Barebox test configs: `test/` directory in barebox source tree

## License

GPL-2.0-only
