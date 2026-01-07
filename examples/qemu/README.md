# QEMU Examples for barebox-bringup

This directory contains example configurations for bringing up barebox in QEMU emulation using barebox-bringup.

## Available Configurations

### ARM64
- **arm64-virt.yaml**: ARM64 virt machine with Cortex-A57 CPU
  - Requires: qemu-system-aarch64
  - Barebox config: multi_v8_defconfig

### ARM (32-bit)
- **arm-virt.yaml**: ARM virt machine with Cortex-A15 CPU
  - Requires: qemu-system-arm
  - Barebox config: multi_v7_defconfig

### x86
- **x86-pc.yaml**: x86-64 PC with UEFI firmware
  - Requires: qemu-system-x86_64, OVMF firmware
  - Barebox config: efi_defconfig

## Quick Start

1. **Build barebox** for your target architecture:
   ```bash
   cd /path/to/barebox
   make multi_v8_defconfig
   make
   ```

2. **Run barebox-bringup**:
   ```bash
   # From barebox source directory
   barebox-bringup -c /path/to/barebox-bringup/examples/qemu/arm64-virt.yaml
   ```

3. **Interactive console**: Type commands directly, press Ctrl-] to exit

## Features

### Image Sets
All configurations support multiple image sets:
```bash
# Use default image set
barebox-bringup -c arm64-virt.yaml

# Use known-good image set (if configured)
barebox-bringup -c arm64-virt.yaml --images known_good

# Auto-detect Yocto builds (when BBPATH is set)
barebox-bringup -c arm64-virt.yaml  # Automatically uses 'yocto' set
```

### Image Overrides
Override image paths from command line:
```bash
# Override with custom image
barebox-bringup -c arm64-virt.yaml --image /path/to/my-barebox.img
```

### Output Logging
Log console output to file:
```bash
# Interactive with logging
barebox-bringup -c arm64-virt.yaml -o boot.log

# In another terminal
tail -f boot.log
```

### Non-Interactive Mode
Run without keyboard input (for automation):
```bash
barebox-bringup -c arm64-virt.yaml -n -o boot.log
```

### FIFO Control
Programmatic control via named pipes:
```bash
# Auto-create FIFO
barebox-bringup -c arm64-virt.yaml -i -o boot.log &

# Tool prints: Created FIFO: /tmp/barebox-input-12345.fifo
echo "version" > /tmp/barebox-input-12345.fifo
echo "help" > /tmp/barebox-input-12345.fifo
```

## Environment Variables

- **LG_BUILDDIR**: Path to barebox build directory
  - Auto-detected from `KBUILD_OUTPUT` or `./build`
  - Used by `!template "$LG_BUILDDIR/..."` in configs

- **BBPATH**: Yocto build indicator
  - When set, automatically selects 'yocto' image set
  - Can be overridden with `--images` flag

## Strategy

All configurations use the `QEMUStrategy` (defined in `../strategy-qemu.py`) which provides:

- **State management**: `off` ↔ `barebox`
- **Automatic QEMU lifecycle**: Start/stop handling
- **Console integration**: Captures all boot output
- **Simple interface**: Just works for most use cases

The strategy automatically detects QEMUDriver and manages the emulator appropriately.

## Customization

To create your own QEMU configuration:

1. Copy an example that's closest to your target
2. Adjust QEMU parameters (machine, cpu, memory, etc.)
3. Update image paths in the `images:` section
4. Adjust `imports:` path to strategy-qemu.py if needed

Example customization:
```yaml
QEMUDriver:
  qemu_bin: qemu-system-riscv64
  machine: virt
  cpu: rv64
  memory: 2048M
  kernel: barebox-riscv.img
  display: qemu-default
```

## Differences from Hardware Configs

QEMU configurations differ from hardware configs (like `examples/arm/imx6s-riotboard.yaml`) in several ways:

- No `RemotePlace` needed (local execution)
- No coordinator required (can run standalone)
- Console driver is also the power control
- Simpler strategy (no bootstrap protocols needed)
- Faster iteration (no real hardware delays)

## Troubleshooting

### QEMU not found
```
Error: qemu-system-aarch64 not found
```
Install QEMU for your platform:
```bash
# Debian/Ubuntu
apt install qemu-system-arm qemu-system-x86

# Fedora
dnf install qemu-system-aarch64 qemu-system-x86
```

### Image not found
```
Error: /path/to/images/barebox-dt-2nd.img not found
```
Build barebox first, or set LG_BUILDDIR:
```bash
export LG_BUILDDIR=/path/to/barebox/build
```

### OVMF not found (x86 only)
```
Error: Could not open '/usr/share/ovmf/OVMF.fd'
```
Install OVMF firmware or adjust the `bios:` path in x86-pc.yaml:
```bash
# Debian/Ubuntu
apt install ovmf

# Fedora
dnf install edk2-ovmf
# Adjust bios path to: /usr/share/edk2/ovmf/OVMF_CODE.fd
```

## See Also

- [barebox documentation](https://barebox.org)
- [labgrid documentation](https://labgrid.readthedocs.io/)
- [../strategy-qemu.py](../strategy-qemu.py) - Strategy implementation
- [../arm/](../arm/) - Hardware examples
