# IC_GUI Agent Guide

## Scope
This repository is a Windows desktop GUI for PMIC register and VCOM debugging over `IIC over AUX`.

The current application code lives in `pmic_aux_gui/`. Direct-I2C runtime files live in `drivers/jtool/`. Upstream experiment notebooks and large reference trees are not published in this repository.

This file is for future agents and developers who need to modify the project with minimal rediscovery.

## Project Layout
- `pmic_aux_gui/`
  - `profiles.py`: declarative hardware model for PMICs and TCONs
  - `service.py`: hardware session lifecycle, worker subprocess bridge, read/write/VCOM/MTP logic
  - `gui.py`: CustomTkinter UI, register pages, row state, VCOM panel, log panel
  - `main.py`, `__main__.py`: app entry and worker-process entry
- `run_pmic_aux_gui.py`: thin top-level launcher used by Nuitka
- `build_nuitka.ps1`: one-file Windows packaging script
- `requirements.txt`: minimal Python runtime dependency list
- `drivers/jtool/`
  - `jtoollib.py`, `jtool.dll`: direct I2C hardware access dependency
- `IC DATASHEET/`: PMIC datasheets used to derive register maps and formulas
- `build/`, `pmic_aux_gui/__pycache__/`: generated artifacts, not primary edit targets

## Run And Build
- Dev run:
  - `python -m pmic_aux_gui`
  - or `python run_pmic_aux_gui.py`
- Dependency install:
  - `pip install -r requirements.txt`
- Packaging:
  - `powershell -ExecutionPolicy Bypass -File .\build_nuitka.ps1`

`build_nuitka.ps1` bundles:
- `app_icon.ico`
- `drivers/jtool/jtoollib.py`
- `drivers/jtool/jtool.dll`

If runtime behavior depends on a new local asset, packaging must usually be updated too.

## Core Architecture

### High-level model
- `SessionConfig`: user-selected GPU/TCON/PMIC and optional direct-I2C library paths
- `PmicProfile`: PMIC register map, VCOM definition, unlock requirements, MTP behavior
- `TconProfile`: TCON identity and AUX-ready sequence
- `GpuAuxCard`: `gpu_aux.AuxPort` compatibility adapter for DPCD and I2C-over-AUX
- `LocalAuxSession`: real hardware session
- `_WorkerBackedAuxSession`: subprocess wrapper used by GUI to isolate hardware work from the UI thread
- `PmicAuxGuiApp`: Tk/CTk application

### Process model
- The GUI does not talk to hardware directly.
- `connect()` in `service.py` starts a worker process by invoking:
  - source mode: `python -m pmic_aux_gui --aux-worker <payload>`
  - compiled mode: current executable with `--aux-worker`
- Worker I/O is JSON over stdio.
- Keep worker commands request/response shaped. Do not introduce UI-thread blocking DLL access in `gui.py`.

### UI model
- Register pages are cached per PMIC in `register_page_cache`.
- Rows are built incrementally in batches to avoid a slow initial render.
- Display refresh is dependency-aware and debounced through `DISPLAY_DEPENDENCIES`.
- The right-side display column is for interpreted values or boolean state, not raw hex.
- Raw editable values remain in the entry field.
- Boolean bit controls should be described in profiles through `bit_options`, not hardcoded in GUI branches.

## Current Supported Hardware

### TCON
- `anx` -> `ANX_ANX2176`
- `nova` -> `Nova_NT71877`
- `parade` -> `Parade_TC3410`

### PMIC
- `nvp2515`
- `nt50805`
- `nt51950`
- `rt6755`
- `lx52042c`

The codebase currently reflects all five PMICs above. Older notes that mention fewer PMICs are outdated.

## Current Behavior Contracts

### Connection and init
- User manually selects `GPU / TCON / PMIC`.
- No hardware autodetect is implemented.
- TCON init is lazy. First meaningful read/write triggers initialization.
- `NOVA IIC_EN` is optional and user-controlled. Do not make it unconditional.
- AUX access uses the `gpu-aux` package (`gpu_aux.AuxPort`) instead of local `OperateCardLib` files.

### Read/write semantics
- Standard PMIC register operations go through `GpuAuxCard.iic_over_aux_read` / `GpuAuxCard.iic_over_aux_write`.
- `target="mtp"` on normal register writes triggers the profile MTP action after the write.
- VCOM is handled separately and may not follow ordinary register semantics.
- `Read All` / `Write All` intentionally exclude VCOM in the bulk path.

### Safety constraints already encoded
- Command-like registers such as `0xFE` and `0xFF` are marked `writable=False` where needed.
- `RT6755` unlock registers `0x00` and `0x01` are not generic writable fields.
- Preserve these constraints. Do not turn command/unlock registers into normal bulk-edit rows.

### Display/value normalization
- `normalize_register_value()` masks out unsupported bits.
- Slider/editable numeric fields only control non-boolean bits.
- Displayed voltages often depend on sibling registers, not just the current raw byte.
- Voltage/state rendering should stay in `service.py` formatting helpers, not in widget code.

## Editing Guidance

### When changing hardware support
- Start in `profiles.py` for register maps, bit options, VCOM definitions, unlock rules, and TCON startup sequences.
- Use `service.py` for actual-value formulas, special read/write accessors, MTP actions, and worker-facing behavior.
- Touch `gui.py` only when the profile-driven model is insufficient.

### Preferred extension path
- New boolean toggle:
  - add `bit_options` in the relevant `RegisterDefinition`
- New displayed voltage or interpreted text:
  - implement in `format_register_display()` or `format_vcom_display()`
- New PMIC:
  - add a `PmicProfile`
  - define register set, VCOM behavior, unlock/MTP behavior
  - extend display logic if formulas depend on other registers
  - update `DISPLAY_DEPENDENCIES` in `gui.py` if derived values depend on other rows
- New TCON init path:
  - add `InitStep` callbacks in `profiles.py`
  - keep them callable from `LocalAuxSession.ensure_ready()`

### What not to do
- Do not bypass the worker and call GPU AUX functions directly from GUI code.
- Do not hardcode PMIC-specific widget behavior if the profile model can express it.
- Do not make `NOVA IIC_EN` the default path for every NOVA panel.
- Do not bulk-write special command registers or unlock bytes.
- Do not edit Nuitka-generated `build/` outputs by hand unless the task is explicitly about compiled artifacts.

## Reference Assets

### Datasheets currently relevant
- `IC DATASHEET/EDS-NVP2515-230503-0.2.pdf`
- `IC DATASHEET/NT50805_External_Datasheet_V00_20220906.pdf`
- `IC DATASHEET/RT6755-P03.pdf`
- `IC DATASHEET/LX52042C_datasheet_preliminary_20231117.pdf`

### Behavior references
- `drivers/jtool/jtoollib.py`
- Historical notebooks and upstream experiment trees may be used locally as private references, but they are not part of the published repository.

Notebook and upstream library behavior should guide compatibility decisions when datasheet wording and existing field behavior conflict.

## TCON Notes

### ANX
- Existing AUX entry relies on DPCD password/staging behavior mirrored in `GpuAuxCard`.

### NOVA
- Standard AUX enable path is implemented in `_step_nova_enable_aux()`
- Optional `NOVA IIC_EN` path is implemented in `_step_nova_iic_en()`
- This path must remain optional

### Parade
- AUX entry is driven through `GpuAuxCard` DPCD staging.

## PMIC Notes

### Shared assumptions
- GUI communication path is `IIC over AUX`
- VCOM is not treated as a normal bulk register field
- MTP handling may be device-specific even when the UI presents a common DAC/MTP choice

### NVP2515
- PMIC slave address: `0x46`
- D-VCOM write/read: `0x9E` / `0x9F`
- VCOM uses logical `0x00..0x7F`
- Raw transfer uses `(logical << 1) | flag`
- `0xFE` and `0xFF` are command registers

### NT50805
- Current active PMIC slave address in code: `0x47`
- Datasheet may show `0x46`; preserve current code behavior unless hardware evidence justifies a change
- VCOM handling mirrors NVP2515 logic in the current GUI model

### NT51950
- I2C write address: `0xDE`
- Direct I2C only; do not expose or route this profile through GPU AUX
- Supported register scope is CMD1 page 0 (`0x10`), addresses `0x00..0x0B`, `0x0D`, and `0x0E`
- Registers `0x00` and `0x01` are read-only
- Access follows the unlock, reload-off, protection-disable, operation, and relock flow from the reference notebook and datasheet
- VCOM remains in ordinary registers `0x02`/`0x03`; there is no separate VCOM panel
- VCOM display combines `0x03[1:0]` and `0x02[7:0]` into a 10-bit code before applying the datasheet voltage table
- Voltage interpretations for `0x02..0x0B`, `0x0D`, and `0x0E` live in `format_register_display()`; keep the `0x02`/`0x03` dependency bidirectional in `gui.py`
- Online read/write only; MTP controls and MTP service targets are disabled

### RT6755
- PMIC slave address: `0x47`
- Access requires unlock before normal PMIC operations
- Current unlock sequence:
  - `0x00 <- 0x65`
  - `0x01 <- 0x9A`
- Generic PMIC MTP commit is still `0xFF <- 0x80`
- No dedicated `VCOM_MIN` register is defined in the datasheet
- VCOM uses coarse `0x0C` plus fine-tune D-VCOM on slave `0x9E`

### LX52042C
- PMIC slave address: `0x46`
- Supported in current code
- Uses dedicated PMIC-side VCOM read/write handling in `service.py`
- VCOM is split into coarse plus 3-bit LSB in the GUI
- `0xFE` and `0xFF` are command-like and not generic writable rows

## Known Gaps
- Many multi-bit config registers are still byte-level hex editors instead of enums/segmented controls.
- Actual-value formulas are partly dependency-based and not modeled as a fully normalized graph.
- No automated test suite is present in this repository.
- Hardware validation is the main verification path; code-only changes should at least preserve source run and import integrity.

## Practical Rules For Future Agents
- Prefer editing source files under `pmic_aux_gui/`.
- Keep published runtime assets small; do not add upstream experiment trees or generated build outputs.
- Preserve worker-process isolation.
- Preserve lazy init.
- Preserve optional `NOVA IIC_EN`.
- Keep profile-driven UI behavior whenever possible.
- When adding hardware formulas, keep current register context in mind.
- If a behavior differs between datasheet and existing notebooks/library code, document the decision and bias toward compatibility unless the task explicitly asks for a correction.
