# SlaveGUI

SlaveGUI 是一个 Windows 桌面调试工具，用于通过 AUX 或直连 I2C 调试 PMIC 寄存器、VCOM 和屏电源相关参数。它面向显示模组 bring-up 场景，支持通过 DP/eDP AUX 访问面板侧 PMIC，也支持通过 JTool 适配器走直连 I2C。

当前主程序代码位于 `pmic_aux_gui/`。直连 I2C 所需的运行文件单独保存在 `drivers/jtool/`。

## 功能

- PMIC 寄存器 DAC/MTP 读写。
- 通过 `gpu-aux` 实现 DPCD 与 I2C-over-AUX 访问。
- 按 GPU 后端显式选择目标 DP/eDP 显示器，避免访问到错误面板。
- 支持通过 `jtoollib.py` 与 `jtool.dll` 进行直连 I2C 调试。
- 基于 profile 的寄存器界面，支持滑条、原始十六进制编辑、解释值显示和 bit 单选控制。
- VCOM 面板与批量寄存器读写路径分离。
- 硬件访问运行在 worker 子进程中，不直接阻塞 Tk UI 线程。

## 支持范围

GPU/AUX 后端：

- Intel eDP
- Intel DP
- AMD eDP
- AMD DP
- NVIDIA DP
- Direct I2C

TCON 配置：

- ANX
- NOVA
- Parade
- Direct I2C

PMIC 配置：

- B602
- B802
- NVP2515
- NT50805
- NT51950
- RT6755
- RTQ6749
- LX52042C

## 环境要求

- Windows
- Python 3.9 或更新版本
- 使用 `gpu-aux` 时建议使用 64 位 Python
- `gpu-aux` 对应 GPU 后端所需的厂商运行环境：
  - AMD：ADL runtime
  - Intel：IGCL / ControlLib
  - NVIDIA：NVAPI
- 硬件已连接到所选 DP/eDP AUX 端口，或已连接直连 I2C 适配器

安装 Python 依赖：

```powershell
python -m pip install -r requirements.txt
```

当前项目固定使用 `gpu-aux==1.3.1`。该版本包含 NVIDIA I2C 写入长度修复，用于避免 NVIDIA AUX 后端读取 PMIC 时出现异常访问。

## 运行

在项目根目录执行：

```powershell
python -m pmic_aux_gui
```

或：

```powershell
python run_pmic_aux_gui.py
```

## 典型使用流程

1. 选择 GPU/AUX 后端。
2. 在 `Display` 下拉框中选择目标显示器。
3. 选择 TCON 与 PMIC。
4. 确认或修改 PMIC slave address。
5. 点击 Connect 建立连接。
6. 使用寄存器读写按钮或右侧 VCOM 面板进行调试。

NOVA 面板的 `NOVA IIC_EN` 是可选路径，由用户手动控制。除非当前硬件链路确实需要该路径，否则保持关闭。

## 打包

需要打包时先安装 Nuitka，然后执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_nuitka.ps1
```

如果需要先清理旧产物：

```powershell
powershell -ExecutionPolicy Bypass -File .\build_nuitka.ps1 -Clean
```

打包产物会生成到 `build/` 下，该目录不会提交到 Git。

## 项目结构

```text
pmic_aux_gui/
  gui.py        CustomTkinter 界面与寄存器 UI
  service.py    硬件会话生命周期、worker 子进程、AUX/I2C 逻辑
  profiles.py   PMIC/TCON/GPU profile 元数据
  main.py       程序入口

drivers/jtool/
  jtool.dll     直连 I2C 运行库
  jtoollib.py   直连 I2C Python 封装

IC DATASHEET/   用于建立 PMIC profile 的数据手册
```

## 注意事项

- 批量寄存器读写不包含 VCOM。
- 命令寄存器与解锁寄存器不会作为普通批量可写行处理。
- profile 提供 mask 元数据时，会保留保留位和非数值位。
- 软件 smoke 测试只能覆盖导入和基础调用路径，最终 release 可靠性仍需要硬件验证。

## 许可证

本项目使用 GNU General Public License v3.0 许可证，详见 [LICENSE](LICENSE)。
