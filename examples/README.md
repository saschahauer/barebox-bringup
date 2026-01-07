# barebox-bringup Examples

This directory contains example configurations and strategies for bringing up barebox on various platforms.

## Directory Structure

```
examples/
├── qemu/                   # QEMU emulation examples
│   ├── arm64-virt.yaml    # ARM64 virt machine
│   ├── arm-virt.yaml      # ARM virt machine (32-bit)
│   ├── x86-pc.yaml        # x86-64 PC with UEFI
│   └── README.md          # Detailed QEMU documentation
├── arm/                    # Real hardware examples
│   ├── imx6s-riotboard.yaml
│   └── sdmux-example.yaml
├── strategy-qemu.py       # QEMU strategy implementation
├── strategy-bootstrap.py  # USB/JTAG bootstrap strategy
└── strategy-sdmux.py      # SD card mux strategy
```

## Quick Start

### QEMU Testing (No Hardware Required)

Perfect for development and testing without real hardware:

```bash
# Build barebox for ARM64
cd /path/to/barebox
make multi_v8_defconfig
make

# Run in QEMU
barebox-bringup -c /path/to/barebox-bringup/examples/qemu/arm64-virt.yaml
```

See [qemu/README.md](qemu/README.md) for more QEMU examples and options.

### Hardware Testing

For real hardware with labgrid infrastructure:

```bash
# Using USB bootstrap (i.MX, Rockchip, etc.)
barebox-bringup -c examples/arm/imx6s-riotboard.yaml

# Using SD card mux
barebox-bringup -c examples/arm/sdmux-example.yaml
```

## Available Strategies

### QEMUStrategy (`strategy-qemu.py`)
For QEMU emulation targets. Manages QEMU lifecycle and provides simple state transitions.

**Use cases:**
- Development testing
- CI/CD pipelines
- Quick iteration without hardware

**States:** `off` → `barebox`

### BootstrapStrategy (`strategy-bootstrap.py`)
For hardware with USB recovery or JTAG bootstrap. Supports fast RAM-only bootstrap without SD card manipulation.

**Supported bootstrap drivers:**
- IMXUSBDriver (i.MX USB recovery)
- RKUSBDriver (Rockchip)
- MXSUSBDriver (i.MX23/28)
- OpenOCDDriver (JTAG/SWD)
- UUUDriver (Universal Update Utility)
- Any BootstrapProtocol driver

**States:** `off` → `barebox`

### SDMuxStrategy (`strategy-sdmux.py`)
For hardware using SD card multiplexers. Writes images to SD card and boots.

**States:** `off` → `barebox`

## Configuration Patterns

### Image Sets
All configurations support multiple named image sets:

```yaml
image-sets:
  default:
    barebox.img: !template "$LG_BUILDDIR/images/barebox.img"

  known_good:
    barebox.img: "/validated/barebox-v2024.01.0.img"

  yocto:
    barebox.img: !template "$BBPATH/../build/tmp/deploy/images/board/barebox.img"
```

Usage:
```bash
# Use default (or auto-detected)
barebox-bringup -c config.yaml

# Use specific set
barebox-bringup -c config.yaml --images known_good
```

### Auto-Detection
The tool automatically detects your build environment:

- **BBPATH set**: Selects 'yocto' image set
- **LG_BUILDDIR**: Auto-detected from KBUILD_OUTPUT or ./build
- **Override**: Use `--images` flag to force a specific set

## Creating Custom Configurations

1. Choose the appropriate strategy for your target
2. Copy the closest example configuration
3. Customize the configuration:
   - Update drivers for your hardware
   - Adjust image paths
   - Set coordinator address (if using RemotePlace)
4. Test and iterate

Example minimal QEMU config:
```yaml
targets:
  main:
    drivers:
      QEMUDriver:
        qemu_bin: qemu-system-aarch64
        machine: virt
        cpu: cortex-a57
        memory: 1024M
        kernel: barebox.img
        display: qemu-default
      QEMUStrategy: {}

image-sets:
  default:
    barebox.img: !template "$LG_BUILDDIR/images/barebox.img"

imports:
  - /path/to/strategy-qemu.py
```

## See Also

- [Main Documentation](../CLAUDE.md) - Complete project overview
- [qemu/README.md](qemu/README.md) - QEMU-specific documentation
- [barebox documentation](https://barebox.org)
- [labgrid documentation](https://labgrid.readthedocs.io/)
