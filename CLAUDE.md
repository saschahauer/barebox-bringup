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
- `images`: Named sets of barebox images (e.g., default, known_good, testing)
  - Each set contains image names mapped to paths
  - Supports `$LG_BUILDDIR` template variable
  - Use `--images <name>` CLI option to select a set (defaults to 'default')
- `imports`: Custom strategies (like `strategy-bootstrap.py`)
- `options`: Coordinator address, etc.

### Image Sets
The configuration supports multiple named image sets for different purposes:

```yaml
images:
  default:
    barebox.img: !template "$LG_BUILDDIR/images/barebox-board.img"

  known_good:
    barebox.img: "/validated/barebox-v2024.01.0.img"

  testing:
    barebox.img: "/experimental/barebox-next.img"
```

Usage:
```bash
# Use default image set (implicit)
barebox-bringup -c config.yaml

# Use known-good image set
barebox-bringup -c config.yaml --images known_good

# Use testing image set
barebox-bringup -c config.yaml --images testing
```

#### Auto-Detection of Yocto Builds

When the `BBPATH` environment variable is set (indicating you're inside a Yocto build environment), the tool automatically selects the `yocto` image set instead of `default`:

```yaml
images:
  default:
    barebox.img: !template "$LG_BUILDDIR/images/barebox-board.img"

  yocto:
    barebox.img: !template "$BBPATH/../build/tmp/deploy/images/myboard/barebox.img"
```

Selection priority:
1. **Explicit --images flag** (highest priority): Always used if specified
2. **BBPATH environment variable**: Automatically selects 'yocto' if set
3. **Default**: Uses 'default' image set

Example Yocto workflow:
```bash
# Inside Yocto build environment (BBPATH is set)
cd ~/yocto/build
barebox-bringup -c ~/labgrid-places/arm/myboard.yaml  # Automatically uses 'yocto' image set

# Override to use a different set
barebox-bringup -c ~/labgrid-places/arm/myboard.yaml --images known_good
```

#### Backward Compatibility
The tool supports the old singular `image:` key for backward compatibility:

```yaml
# Old format (still supported, treated as 'default' set)
image:
  barebox.img: !template "$LG_BUILDDIR/images/barebox.img"
```

When using old format configs with `image:`, the `--images` option is ignored with a warning, and the images are used as the default set.

### Example Strategy (`examples/strategy-bootstrap.py`)
Custom labgrid strategy for bootstrap-based targets:
- Supports USB recovery mode (i.MX, Rockchip, etc.)
- Supports JTAG/SWD bootstrap (OpenOCD)
- States: `off` -> `barebox`
- Implements `BootstrapProtocol` for loading images

## Environment Variables

- `LG_BUILDDIR`: Path to barebox build directory (auto-detected from `KBUILD_OUTPUT` or `./build`)
- `LG_COORDINATOR`: Labgrid coordinator address (can be overridden via `--coordinator`)
- `BBPATH`: Yocto build environment indicator (when set, automatically selects 'yocto' image set)

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
