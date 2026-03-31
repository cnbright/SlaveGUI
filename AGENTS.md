# PMIC AUX GUI Agent Notes

## Scope
This file captures the PMIC and TCON knowledge already extracted from:
- `IC DATASHEET/EDS-NVP2515-230503-0.2.pdf`
- `IC DATASHEET/NT50805_External_Datasheet_V00_20220906.pdf`
- `IC DATASHEET/RT6755-P03.pdf`
- `circuit_project/pylib/OperateCardLib.py`
- `circuit_project/exp/AUX鍔熻兘/EXP_nova.ipynb`
- `circuit_project/exp/VCOM鐑у綍/*.ipynb`
- `circuit_project/exp/VOP娴嬭瘯/*.ipynb`

The purpose is to make follow-up development decision-light.

## Current GUI / Service Model
- Project package: `pmic_aux_gui`
- Core files:
  - `pmic_aux_gui/profiles.py`
  - `pmic_aux_gui/service.py`
  - `pmic_aux_gui/gui.py`
- Runtime entry:
  - `python -m pmic_aux_gui`
- Current architecture:
  - `SessionConfig`: connection parameters
  - `AuxSession`: hardware lifecycle, init, read/write, MTP, VCOM
  - `TCON profile`: ANX / NOVA / Parade
  - `PMIC profile`: NVP2515 / NT50805 / RT6755

## TCON Notes

### ANX
- Adapter class: `ANX_ANX2176`
- IIC over AUX is implemented by password + DPCD path in `OperateCardLib.py`
- Existing code uses:
  - `write_dpcd(0x004F5, b"\x41\x56\x4F\x20\x16")`
  - `write_dpcd(0x004F0, b"\x0E\x00\x00\x00")`
  - `write_dpcd(0x004F3, b"\x01")`
  - `write_dpcd(0x004F0, b"\x0E\x00\x00\x30\x09")`

### NOVA
- Adapter class: `Nova_NT71877`
- Standard AUX enable path:
  - `DPCD 0x00102 <- 0xC0`
  - `IIC 0x60 / 0x02 <- 0x04 0x00`
- Optional NOVA `IIC_EN` path exists in `EXP_nova.ipynb`
- GUI already supports a connect option: `NOVA IIC_EN`
- Current implemented `IIC_EN` sequence:
  - `DPCD 0x00102 <- 0xC0`
  - `DPCD 0x004C1 <- 0x14`
  - `IIC 0x60 / 0x02 <- 0x01`
  - `IIC 0x61 / 0x02 <- 0x04`
  - `IIC 0x61 / 0x02 <- 0x04 0x00`
  - `IIC 0x61 / 0x45 <- 0x44 0x05`
  - `IIC 0x61 / 0x38 <- 0x9E 0x10`
  - `DPCD 0x00102 <- 0x00`
- Important: this path should remain optional, not defaulted for every NOVA panel.

### Parade
- Adapter class: `Parade_TC3410`
- AUX entry is implemented through DPCD command staging in `OperateCardLib.py`

## PMIC Summary

### Shared assumptions
- Communication path for GUI is `IIC over AUX`
- User manually selects `GPU / TCON / PMIC`
- Hardware auto-detect is not implemented
- Session init is lazy:
  - First real register read/write triggers TCON init
- `VCOM` is handled separately from normal register bulk operations

## NVP2515

### Device addressing
- PMIC 8-bit slave address: `0x46`
- D-VCOM 8-bit slave address: `0x9E` write, `0x9F` read
- PMIC 7-bit address in datasheet terms: `0x23`

### Key register map currently captured
- `0x00`: Channel Setting 0
- `0x01`: Channel Setting 1
- `0x02`: Channel Discharge Setting
- `0x03`: AVDD
- `0x04`: AVEE
- `0x05`: VGH
- `0x06`: VGL
- `0x07`: VCORE
- `0x08`: VIO
- `0x09`: LDO
- `0x0A`: VCOM DAC register
- `0x0B`: RESET threshold
- `0x0C`: GMA1
- `0x0D`: GMA2
- `0x0E`: AVDD boost config
- `0x0F`: AVDD delay / soft-start
- `0x10`: AVEE config
- `0x11`: VGH/VGL config
- `0x12`: VGH delay / soft-start
- `0x13`: VGL delay / soft-start
- `0x14`: VCORE config
- `0x15`: VCORE delay / soft-start
- `0x16`: VIO config
- `0x17`: VIO delay / soft-start
- `0x18`: RESET delay
- `0x19`: LDO delay
- `0x1A`: VCOM config
- `0x1B`: AVEE advanced config
- `0x1C`: VCOM_MIN
- `0xFE`: WED_VCOM
- `0xFF`: Control register

### Voltage formulas already implemented
- `AVDD = 4.0 + 0.05 * code`
- `AVEE = -4.0 - 0.1 * code`
- `VGH`
  - high resolution disabled: `6.0 + 0.2 * code`, clamped to `12.0`
  - high resolution enabled: `12.5 + 0.5 * code`
- `VGL = -5.4 - 0.2 * code`
- `VCORE = 0.8 + 0.02 * code`
- `VIO = 1.0 + 0.05 * code`
- `LDO = 1.7 + 0.1 * code`, clamped to `2.8`
- `RESET = 2.0 + 0.1 * code`
- `GMA1 = AVDD - 0.02 * code`
- `GMA2 = AVEE + 0.02 * code`
- `VCOM_MIN`
  - code `0x00..0x02` maps to `-3.6V`
  - code `>= 0x03` maps to `-3.6 + 0.15 * (code - 2)`
- `VCOM = VCOM_MIN + 0.01 * logical_code`

### VCOM behavior
- Uses D-VCOM command format, not ordinary PMIC register write flow
- Existing notebook / library behavior assumes:
  - DAC write: low bit = `1`
  - MTP write: low bit = `0`
  - logical range is `0x00..0x7F`
  - raw transfer is `(logical << 1) | flag`
- `FE` is special: write current VCOM DAC into EEPROM
- `FF` control meanings:
  - `0x00`: read DAC
  - `0x01`: read EEPROM
  - `0x80`: write all DAC into EEPROM

### Boolean controls currently surfaced
- `0x00`: all output enable bits
- `0x01`:
  - `VGH High Resolution`
  - `VIO PWM`
  - `VCORE PWM`
  - `PRE_AVDD`
  - `CTRL`
  - `RESET`
  - `GMA2`
  - `GMA1`
- `0x02`: all discharge enable bits
- `0x1A`:
  - `VCOM Power-Off`: follow `RESET` vs follow `UVLO`

## NT50805

### Device addressing
- PMIC 8-bit slave address in datasheet: `0x46`
- Existing AUX notebooks often access PMIC as `0x47`
- Existing codebase behavior must be preserved for current target hardware
- In GUI profiles, current active slave address is `0x47`
- This difference likely comes from board/address strap usage; do not "normalize" casually

### Key register map currently captured
- `0x00`: Channel Setting 0
- `0x01`: Channel Setting 1
- `0x02`: Channel Discharge Setting
- `0x03`: AVDD
- `0x04`: AVEE
- `0x05`: VGH
- `0x06`: VGL
- `0x07`: VCORE
- `0x08`: VIO
- `0x09`: LDO
- `0x0A`: VCOM DAC register
- `0x0B`: VDET
- `0x0C`: GMA1
- `0x0D`: GMA2
- `0x0E`: AVDD boost config
- `0x0F`: AVDD delay / soft-start
- `0x10`: AVEE delay / soft-start
- `0x11`: VGH/VGL SIBO config
- `0x12`: VGH delay / soft-start
- `0x13`: VGL delay / soft-start
- `0x14`: VCORE config
- `0x15`: VCORE delay / soft-start
- `0x16`: VIO config
- `0x17`: VIO delay / soft-start
- `0x18`: RESET delay
- `0x19`: LDO delay
- `0x1A`: VCOM config
- `0x1B`: AVEE advanced config
- `0x1C`: VCOM_MIN
- `0x1D`: Boost PWM control
- `0x20`: WP
- `0x21`: UBRR WP
- `0x22`: UBRR
- `0xFE`: WED_VCOM
- `0xFF`: Control register

### Voltage formulas already implemented
- `AVDD = 4.0 + 0.05 * (reg03 & 0x3F)`
- `AVEE = -4.0 - 0.1 * (reg04 & 0x1F)`
- `VGH`
  - high resolution bit is currently interpreted from `0x01[7]`
  - value source is `reg05`
  - standard mode: `6.0 + 0.2 * code`, clamped to `12.0`
  - high resolution mode: `12.5 + 0.5 * code`, clamped to `28.0`
  - if `VGH_30` indicates 30V extension and computed value reaches 28V, map to `30.0V`
- `VGL = -5.4 - 0.2 * (reg06 & 0x3F)`
- `VCORE = 0.8 + 0.02 * (reg07 & 0x3F)`
- `VIO = 1.0 + 0.05 * (reg08 & 0x1F)`
- `LDO = min(1.7 + 0.1 * (reg09 & 0x0F), 2.8)`
- `VDET = 2.0 + 0.1 * (reg0B & 0x07)`
- `GMA1 = AVDD - 0.02 * (reg0C & 0x3F)`
- `GMA2 = AVEE + 0.02 * (reg0D & 0x3F)`
- `VCOM_MIN = -3.6 + 0.15 * (reg1C & 0x1F)`
- `VCOM = VCOM_MIN + 0.01 * logical_code`

### VCOM behavior
- Same logical handling strategy as NVP2515 in current GUI
- `logical_code` range is `0x00..0x7F`
- `raw = logical << 1 | flag`
- Current code treats:
  - DAC write flag = `1`
  - MTP write flag = `0`
- `FE`: write VCOM command
- `FF`:
  - `0x00`: read DAC
  - `0x01`: read EEPROM
  - `0x80`: write all DAC into EEPROM

### MTP behavior
- Existing code and docs use:
  - `slave 0x46, reg 0xFF, data 0x80`
- GUI currently uses generic PMIC MTP commit for bulk non-VCOM register MTP path

### Boolean controls currently surfaced
- `0x00`: all output enable bits
- `0x01`:
  - `VGH High Resolution`
  - `VIO PWM`
  - `VCORE PWM`
  - `PRE_AVDD`
  - `RESET`
  - `GMA2`
  - `GMA1`
- `0x02`: all discharge enable bits
- `0x1A`:
  - `VCOM Fast Discharge`
  - `VCOM Power-Off`

## RT6755

### Device addressing
- PMIC 8-bit slave address: `0x46`
- PMIC read address: `0x47`
- D-VCOM write: `0x9E`
- D-VCOM read: `0x9F`

### Unlock
- Required before PMIC accesses
- Existing verified sequence:
  - write `0x65` to `0x00`
  - write `0x9A` to `0x01`
- GUI currently auto-runs unlock once before first real PMIC access

### Key register map currently captured
- `0x00`: Unlock Code 1
- `0x01`: Unlock Code 2
- `0x02`: Channel ON/OFF
- `0x03`: Channel Mode
- `0x04`: Channel Discharge
- `0x05`: PAVDD
- `0x06`: NAVDD
- `0x07`: VGH
- `0x08`: VGL
- `0x09`: VCORE
- `0x0A`: VIO
- `0x0B`: LDO voltage / delay combo
- `0x0C`: VCOM DAC register
- `0x0D`: VCOM delay / RESET threshold combo
- `0x0E`: GMA1
- `0x0F`: GMA2
- `0x10`: PAVDD config
- `0x11`: PAVDD delay / soft-start
- `0x12`: NAVDD delay / soft-start
- `0x13`: NAVDD config
- `0x14`: VGH/VGL config
- `0x15`: VGH delay / soft-start
- `0x16`: VGL delay / soft-start
- `0x17`: VCORE config
- `0x18`: VCORE delay / soft-start
- `0x19`: VIO config
- `0x1A`: VIO delay / soft-start
- `0x1B`: VCOM / RESET config
- `0xFF`: control register

### Voltage formulas already implemented
- `PAVDD = 4.0 + 0.05 * code`
- `NAVDD = -4.0 - 0.1 * code`
- `VGH`
  - current GUI logic uses `0x03[7]`
  - if high-resolution enabled: `10.0 + 1.0 * code`
  - else: `6.0 + 0.2 * code`
- `VGL = -5.4 - 0.2 * code`
- `VCORE = 0.8 + 0.02 * code`
- `VIO = 1.0 + 0.05 * code`
- `GMA1 = PAVDD - 0.02 * code`
- `GMA2 = NAVDD + 0.02 * code`
- `VCOM = -2.56 + 0.02 * code`

### VCOM behavior
- Different from NVP2515 / NT50805
- RT6755 datasheet shows dedicated D-VCOM command semantics and PMIC register `0x0C`
- Current GUI profile uses:
  - `use_special_accessor = False`
  - direct logical range `0x00..0xFF`
  - display formula `-2.56 + 0.02 * code`
- Existing `OperateCardLib.py` `read_vcom/write_vcom` still targets the old `0x4F` style accessor for some notebook flows; for RT6755 this should be treated carefully in future refactors

### MTP behavior
- Datasheet control register `0xFF` controls read source / write-to-EEPROM mode
- Existing notebook usage also uses:
  - unlock first
  - then `reg 0xFF <- 0x80` for MTP write path
- GUI currently retains generic PMIC MTP commit for non-VCOM bulk MTP flow

### Boolean controls currently surfaced
- `0x02`: all output enable bits
- `0x03`:
  - `VGH High Resolution`
  - `VIO FCCM`
  - `VCORE FCCM`
  - `RESET`
  - `GMA2`
  - `GMA1`
- `0x04`: all discharge enable bits
- `0x1B`:
  - `VCOM Pull-Down`
  - `VCOM Power-Off`

## Bulk Write Safety Rules
- The following should not participate in generic bulk write:
  - `RT6755 0x00 / 0x01` unlock codes
  - PMIC command registers like `0xFE / 0xFF`
- This is already enforced in current profiles via `writable=False`

## GUI Behavior Already Implemented
- Right-side per-register display column shows:
  - actual voltage for voltage-like registers
  - `Enable` / `Disable` style status for boolean-only cases
  - no longer shows raw hex in that column
- Raw hex still exists in editable entry field
- Boolean bits are exposed as radio-button controls under the parent register row
- Changing a radio-button updates the backing register byte
- `Read All` refreshes both:
  - entry value
  - displayed voltage / state
  - boolean radio selections

## Known Gaps / Future Work
- Many multi-bit config fields still remain byte-level only:
  - current limits
  - slew rates
  - soft-start
  - delay values
  - frequency selections
- These should eventually become enums or segmented controls instead of raw hex/slider
- Some formulas are based on currently selected mode bits and current GUI state, not a fully normalized dependency graph
- `NT50805` address strap ambiguity (`0x46` vs `0x47`) should stay configurable if more panels are added
- `RT6755` VCOM path likely deserves a dedicated UX review because PMIC register VCOM and D-VCOM command path both exist in docs

## Development Rules For Future Edits
- Preserve lazy init behavior
- Keep NOVA `IIC_EN` optional
- Do not convert command / unlock registers into normal bulk-writable config rows
- When adding a new boolean UI item, prefer `bit_options` in profile rather than hardcoding GUI logic
- When adding a new voltage display, prefer computing actual values in `service.py` using current register context
