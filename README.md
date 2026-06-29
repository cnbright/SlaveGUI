# SlaveGUI

SlaveGUI is a Windows desktop tool for PMIC register, VCOM, and panel power debugging over AUX and direct I2C. It is focused on display bring-up workflows where PMICs are accessed through DP/eDP AUX paths or through a JTool I2C adapter.

The current application code lives in `pmic_aux_gui/`. Direct-I2C runtime files are kept under `drivers/jtool/`.

## Features

- PMIC register read/write with DAC and MTP actions.
- DPCD and I2C-over-AUX access through `gpu-aux`.
- Display-port selection for each GPU backend so the target DP/eDP display can be chosen explicitly.
- Direct I2C mode through `jtoollib.py` and `jtool.dll`.
- Profile-driven register UI with sliders, raw hex editors, interpreted values, and bit radio controls.
- VCOM panel separated from bulk register read/write.
- Worker-process isolation so hardware access does not run directly on the Tk UI thread.

## Supported Backends

GPU/AUX backends:

- Intel eDP
- Intel DP
- AMD eDP
- AMD DP
- NVIDIA DP
- Direct I2C

TCON profiles:

- ANX
- NOVA
- Parade
- Direct I2C

PMIC profiles:

- B602
- B802
- NVP2515
- NT50805
- RT6755
- RTQ6749
- LX52042C

## Requirements

- Windows
- Python 3.9 or newer
- 64-bit Python when using `gpu-aux`
- GPU vendor runtime dependencies required by `gpu-aux`
  - AMD: ADL runtime
  - Intel: IGCL / ControlLib
  - NVIDIA: NVAPI
- Hardware connected through the selected DP/eDP AUX port or direct I2C adapter

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

This project pins `gpu-aux==1.3.1`. Version 1.3.1 includes the NVIDIA I2C write length fix needed for safe PMIC reads on NVIDIA AUX.

## Run

From the project root:

```powershell
python -m pmic_aux_gui
```

or:

```powershell
python run_pmic_aux_gui.py
```

## Typical Workflow

1. Select the GPU/AUX backend.
2. Select the target display from the `Display` dropdown.
3. Select TCON and PMIC.
4. Confirm or edit the PMIC slave address.
5. Connect.
6. Use register read/write buttons or the VCOM panel.

For NOVA panels, `NOVA IIC_EN` is optional and user controlled. Leave it disabled unless that path is required by the hardware setup.

## Packaging

Install Nuitka when packaging is needed, then run:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_nuitka.ps1
```

Use the clean switch to remove the previous package output first:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_nuitka.ps1 -Clean
```

Packaging output is generated under `build/`, which is intentionally ignored by Git.

## Project Layout

```text
pmic_aux_gui/
  gui.py        CustomTkinter application and register UI
  service.py    hardware session lifecycle, worker process, AUX/I2C logic
  profiles.py   PMIC/TCON/GPU profile metadata
  main.py       application entry point

drivers/jtool/
  jtool.dll     direct-I2C runtime library
  jtoollib.py   direct-I2C Python wrapper

IC DATASHEET/   datasheets used to derive PMIC profiles
```

## Notes

- Bulk register read/write intentionally excludes VCOM.
- Command and unlock registers are not treated as ordinary bulk writable rows.
- Reserved and non-numeric register bits are preserved where profile metadata provides masks.
- Hardware validation is required for release confidence; smoke tests here only cover imports and software call paths.

## License

This project is licensed under the GNU General Public License v3.0. See [LICENSE](LICENSE).
