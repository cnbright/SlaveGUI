from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
import base64
import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

from .profiles import GPU_CARD_IDS, NOVA_IIC_EN_SCHEMES, PMIC_PROFILES, TCON_PROFILES, PmicProfile, TconProfile, _step_nova_iic_en


GPU_AUX_TARGETS: dict[str, tuple[str, str]] = {
    "intel_edp": ("INTEL", "eDP"),
    "intel_dp": ("INTEL", "DP"),
    "amd_edp": ("AMD", "eDP"),
    "amd_dp": ("AMD", "DP"),
    "nvidia": ("NVIDIA", "DP"),
}


class AuxGuiError(RuntimeError):
    """User-facing hardware/service error."""


@dataclass(frozen=True)
class GpuAuxPortChoice:
    label: str
    backend: str
    kind: str
    gpu_index: int
    port_index: int
    identity: str


@dataclass
class SessionConfig:
    gpu_key: str
    tcon_key: str
    pmic_key: str
    pmic_slave_addr: int
    gpu_index: int = 0
    port_index: int = 0
    jtool_dll_path: Path | None = None
    jtool_module_path: Path | None = None
    nova_use_iic_en: bool = False
    nova_iic_en_scheme: str = "io1"


@dataclass
class LocalAuxSession:
    config: SessionConfig
    logger: Callable[[str], None]
    card_lib: object
    tcon_profile: TconProfile
    pmic_profile: PmicProfile
    initialized: bool = False
    unlocked: bool = False
    closed: bool = False
    init_summary: str = "Not initialized"

    def ensure_ready(self) -> None:
        if self.closed:
            raise AuxGuiError("Session is closed")
        if self.initialized:
            return
        try:
            self.logger("Running first-access initialization")
            for step in self.tcon_profile.ready_sequence:
                self.logger(f"Init step: {step.name}")
                step.callback(self.card_lib, self.logger)
            if self.config.tcon_key == "nova" and self.config.nova_use_iic_en:
                self.logger(f"Init step: NOVA IIC_EN {self._nova_iic_en_scheme_name()}")
                _step_nova_iic_en(self.card_lib, self.logger, self.config.nova_iic_en_scheme)
            self.initialized = True
            suffix = f" with IIC_EN {self._nova_iic_en_scheme_name()}" if self.config.tcon_key == "nova" and self.config.nova_use_iic_en else ""
            self.init_summary = f"Initialized {self.tcon_profile.name}{suffix} for {self.pmic_profile.name}"
            self.logger(self.init_summary)
        except Exception as exc:
            raise AuxGuiError(f"Initialization failed: {exc}") from exc

    def _nova_iic_en_scheme_name(self) -> str:
        return str(NOVA_IIC_EN_SCHEMES.get(self.config.nova_iic_en_scheme, NOVA_IIC_EN_SCHEMES["io1"])["name"])

    def _ensure_pmic_unlock(self) -> None:
        if self.unlocked or not self.pmic_profile.unlock_before_access:
            return
        if self.pmic_profile.unlock_register is None or not self.pmic_profile.unlock_data:
            return
        try:
            self.ensure_ready()
            unlock_slave = self.pmic_profile.unlock_slave_addr or self.pmic_profile.slave_addr
            data = bytes(self.pmic_profile.unlock_data)
            self.logger(
                f"PMIC unlock: slave=0x{unlock_slave:02X} "
                f"reg=0x{self.pmic_profile.unlock_register:02X} data={data.hex(' ').upper()}"
            )
            self.card_lib.iic_over_aux_write(
                unlock_slave,
                self.pmic_profile.unlock_register,
                list(self.pmic_profile.unlock_data),
            )
            self.unlocked = True
        except Exception as exc:
            raise AuxGuiError(f"PMIC unlock failed: {exc}") from exc

    def read_register(self, reg_key: str, target: str = "dac") -> int:
        register = _find_register(self.pmic_profile, reg_key)
        self._ensure_target_supported(target)
        self.ensure_ready()
        self._ensure_pmic_unlock()
        if self.pmic_profile.key == "nt51950":
            return self._read_nt51950_register(register)
        self._prepare_register_read(register, target)
        if target == "mtp" and self.pmic_profile.mtp_read_mode == "fallback_to_dac":
            self.logger(f"MTP read for {register.name} falls back to DAC path")
        try:
            raw = self.card_lib.iic_over_aux_read(self.pmic_profile.slave_addr, register.address, 1)
            value = int(raw[0])
            self.logger(
                f"Read {register.name}: slave=0x{self.pmic_profile.slave_addr:02X} "
                f"reg=0x{register.address:02X} -> 0x{value:02X}"
            )
            return value
        except Exception as exc:
            raise AuxGuiError(f"Read register failed: {exc}") from exc

    def write_register(self, reg_key: str, value: int, target: str = "dac") -> None:
        register = _find_register(self.pmic_profile, reg_key)
        self._ensure_target_supported(target)
        if not register.writable:
            raise AuxGuiError(f"{register.name} is read-only")
        value = normalize_register_value(register, value)
        validate_register_value(register.min_value, register.max_value, value)
        self.ensure_ready()
        self._ensure_pmic_unlock()
        if self.pmic_profile.key == "nt51950":
            self._write_nt51950_register(register, value)
            return
        try:
            self.card_lib.iic_over_aux_write(self.pmic_profile.slave_addr, register.address, [value])
            self.logger(
                f"Write {register.name}: slave=0x{self.pmic_profile.slave_addr:02X} "
                f"reg=0x{register.address:02X} <- 0x{value:02X}"
            )
            if target == "mtp":
                self.run_mtp_commit()
        except Exception as exc:
            raise AuxGuiError(f"Write register failed: {exc}") from exc

    def read_all_registers(self, target: str = "dac", exclude_vcom: bool = True) -> dict[str, int | str]:
        self._ensure_target_supported(target)
        results: dict[str, int | str] = {}
        for register in self.pmic_profile.registers:
            if exclude_vcom and _is_bulk_excluded_vcom_register(self.pmic_profile, register):
                continue
            if register.address in {0xFE, 0xFF}:
                continue
            try:
                results[register.key] = self.read_register(register.key, target=target)
            except Exception as exc:
                results[register.key] = f"ERROR: {exc}"
                self.logger(f"Read all failed at {register.name}: {exc}")
        if not exclude_vcom and self.pmic_profile.supports_vcom:
            results["vcom"] = self.read_vcom()
        return results

    def write_all_registers(self, values: dict[str, int], target: str = "dac", exclude_vcom: bool = True) -> None:
        self._ensure_target_supported(target)
        write_target = "dac" if target == "mtp" else target
        wrote_any = False
        if target == "mtp":
            self.logger("Write all MTP: staging registers through DAC before one MTP commit")
        for register in self.pmic_profile.registers:
            if exclude_vcom and _is_bulk_excluded_vcom_register(self.pmic_profile, register):
                continue
            if not register.writable:
                continue
            if register.key not in values:
                continue
            self.write_register(register.key, values[register.key], target=write_target)
            wrote_any = True
        if target == "mtp" and wrote_any:
            self.run_mtp_commit()

    def read_vcom(self, target: str = "dac", device_addr: int | None = None) -> int:
        if not self.pmic_profile.supports_vcom:
            raise AuxGuiError(f"{self.pmic_profile.name} does not support VCOM")
        self.ensure_ready()
        self._ensure_pmic_unlock()
        if target == "mtp" and self.pmic_profile.mtp_read_mode == "fallback_to_dac":
            self.logger("MTP VCOM read falls back to DAC path")
        try:
            vcom_device_addr = self._resolve_vcom_device_addr(device_addr)
            if self.pmic_profile.key == "lx52042c":
                return self._read_lx_vcom_via_pmic(target)
            if self.pmic_profile.key == "rtq6749":
                return self._read_rtq6749_vcom(target, vcom_device_addr)
            if self.pmic_profile.key == "nt50805" and device_addr is not None and hasattr(self.card_lib, "read_vcom"):
                raw_value = self._read_vcom_with_addr(vcom_device_addr)
            elif device_addr is None and self.pmic_profile.vcom.use_special_accessor and hasattr(self.card_lib, "read_vcom"):
                raw_value = int(self.card_lib.read_vcom())
            elif self.pmic_profile.vcom.no_register_access:
                self._prepare_vcom_access()
                read_addr = self.pmic_profile.vcom.read_device_addr or vcom_device_addr
                raw_value = int(self.card_lib.iic_read_s(read_addr))
            else:
                raw_value = int(
                    self.card_lib.iic_over_aux_read(
                        vcom_device_addr,
                        self.pmic_profile.vcom.register_addr,
                        1,
                    )[0]
                )
            value = raw_value >> self.pmic_profile.vcom.raw_shift
            self.logger(f"Read {self.pmic_profile.vcom.name}: addr=0x{vcom_device_addr:02X} raw=0x{raw_value:02X} logical=0x{value:02X}")
            return value
        except Exception as exc:
            raise AuxGuiError(f"Read VCOM failed: {exc}") from exc

    def write_vcom(self, value: int, target: str = "dac", device_addr: int | None = None) -> None:
        if not self.pmic_profile.supports_vcom:
            raise AuxGuiError(f"{self.pmic_profile.name} does not support VCOM")
        validate_register_value(self.pmic_profile.vcom.min_value, self.pmic_profile.vcom.max_value, value)
        self.ensure_ready()
        self._ensure_pmic_unlock()
        try:
            vcom_device_addr = self._resolve_vcom_device_addr(device_addr)
            if self.pmic_profile.key == "lx52042c":
                self._write_lx_vcom_via_pmic(value, target)
                return
            if self.pmic_profile.key == "rtq6749":
                self._write_rtq6749_vcom(value, target, vcom_device_addr)
                return
            raw_value = value << self.pmic_profile.vcom.raw_shift
            if target == "dac":
                raw_value |= self.pmic_profile.vcom.dac_flag
            else:
                raw_value |= self.pmic_profile.vcom.mtp_flag
            if self.pmic_profile.key == "nt50805" and device_addr is not None and hasattr(self.card_lib, "write_vcom"):
                self._write_vcom_with_addr(raw_value, vcom_device_addr)
            elif device_addr is None and self.pmic_profile.vcom.use_special_accessor and hasattr(self.card_lib, "write_vcom"):
                self.card_lib.write_vcom(raw_value)
            elif self.pmic_profile.vcom.no_register_access:
                self._prepare_vcom_access()
                self.card_lib.iic_write_s(vcom_device_addr, raw_value)
            else:
                self.card_lib.iic_over_aux_write(
                    vcom_device_addr,
                    self.pmic_profile.vcom.register_addr,
                    [raw_value],
                )
            self.logger(f"Write {self.pmic_profile.vcom.name}: addr=0x{vcom_device_addr:02X} logical=0x{value:02X} raw=0x{raw_value:02X}")
            if target == "mtp":
                self.logger(f"{self.pmic_profile.vcom.name} MTP write uses device-specific command bit; no extra PMIC MTP commit applied")
        except Exception as exc:
            raise AuxGuiError(f"Write VCOM failed: {exc}") from exc

    def _resolve_vcom_device_addr(self, device_addr: int | None = None) -> int:
        if device_addr is None:
            return self.pmic_profile.vcom.device_addr
        validate_register_value(0x00, 0xFF, device_addr)
        return device_addr

    def _read_vcom_with_addr(self, vcom_addr: int) -> int:
        try:
            return int(self.card_lib.read_vcom(vcom_addr))
        except TypeError as exc:
            if vcom_addr == self.pmic_profile.vcom.device_addr:
                return int(self.card_lib.read_vcom())
            raise AuxGuiError("Current AUX adapter read_vcom() does not accept a VCOM address parameter") from exc

    def _write_vcom_with_addr(self, raw_value: int, vcom_addr: int) -> None:
        try:
            self.card_lib.write_vcom(raw_value, vcom_addr)
        except TypeError as exc:
            if vcom_addr == self.pmic_profile.vcom.device_addr:
                self.card_lib.write_vcom(raw_value)
                return
            raise AuxGuiError("Current AUX adapter write_vcom() does not accept a VCOM address parameter") from exc

    def _prepare_vcom_access(self) -> None:
        for step in self.tcon_profile.ready_sequence:
            step.callback(self.card_lib, self.logger)
        if self.config.tcon_key == "nova" and self.config.nova_use_iic_en:
            _step_nova_iic_en(self.card_lib, self.logger, self.config.nova_iic_en_scheme)

    def _prepare_register_read(self, register, target: str) -> None:
        if self.pmic_profile.key not in {"b602", "b802", "rtq6749"}:
            return
        if register.address in {0xFE, 0xFF}:
            return
        red_value = 0x01 if target == "mtp" else 0x00
        label = self.pmic_profile.name
        self.logger(
            f"{label} prepare read: target={target.upper()} slave=0x{self.pmic_profile.slave_addr:02X} "
            f"select_reg=0xFF value=0x{red_value:02X} next_reg=0x{register.address:02X}"
        )
        self.card_lib.iic_over_aux_write(self.pmic_profile.slave_addr, 0xFF, [red_value])
        self.logger(
            f"{label} register read source: slave=0x{self.pmic_profile.slave_addr:02X} "
            f"reg=0xFF <- 0x{red_value:02X}"
        )
        time.sleep(0.01)
        self.logger(
            f"{label} register read command: slave=0x{self.pmic_profile.slave_addr:02X} "
            f"reg=0x{register.address:02X} after source select delay=10ms"
        )

    def _ensure_target_supported(self, target: str) -> None:
        if target not in {"dac", "mtp"}:
            raise AuxGuiError(f"Unsupported register target: {target}")
        if target == "mtp" and not self.pmic_profile.supports_mtp:
            raise AuxGuiError(f"{self.pmic_profile.name} supports online read/write only")

    def _read_nt51950_register(self, register) -> int:
        self._begin_nt51950_access()
        try:
            value = self._nt51950_read(0x10, register.address)
            self.logger(
                f"Read {register.name}: slave=0x{self.pmic_profile.slave_addr:02X} "
                f"page=0x10 reg=0x{register.address:02X} -> 0x{value:02X}"
            )
            return value
        except Exception as exc:
            raise AuxGuiError(f"Read register failed: {exc}") from exc
        finally:
            self._end_nt51950_access()

    def _write_nt51950_register(self, register, value: int) -> None:
        self._begin_nt51950_access()
        try:
            self.card_lib.iic_over_aux_write(
                self.pmic_profile.slave_addr,
                0x10,
                [register.address, value],
            )
            self.logger(
                f"Write {register.name}: slave=0x{self.pmic_profile.slave_addr:02X} "
                f"page=0x10 reg=0x{register.address:02X} <- 0x{value:02X}"
            )
        except Exception as exc:
            raise AuxGuiError(f"Write register failed: {exc}") from exc
        finally:
            self._end_nt51950_access()

    def _begin_nt51950_access(self) -> None:
        slave = self.pmic_profile.slave_addr
        try:
            for _attempt in range(10):
                self.card_lib.iic_over_aux_write(slave, 0xD0, [0x0D, 0x5A])
                self.card_lib.iic_over_aux_write(slave, 0xD0, [0x0E, 0x28])
                if self._nt51950_read(0xD0, 0x0F) == 0x01:
                    break
            else:
                raise AuxGuiError("NT51950 unlock flag did not become 0x01 after 10 attempts")

            self.card_lib.iic_over_aux_write(slave, 0x21, [0x09, 0xA5])
            for page in (0x10, 0x11, 0x17, 0x20, 0x30, 0x40):
                self.card_lib.iic_over_aux_write(slave, page, [0x1D, 0x03])
            self.logger("NT51950 access unlocked, CMD1 reload disabled, register protection disabled")
        except Exception:
            self._end_nt51950_access()
            raise

    def _end_nt51950_access(self) -> None:
        slave = self.pmic_profile.slave_addr
        try:
            self.card_lib.iic_over_aux_write(slave, 0xD0, [0x0D, 0x00])
            self.card_lib.iic_over_aux_write(slave, 0xD0, [0x0E, 0x00])
            self.logger("NT51950 access relocked")
        except Exception as exc:
            self.logger(f"NT51950 relock failed: {exc}")

    def _nt51950_read(self, page: int, address: int) -> int:
        raw = self.card_lib.iic_over_aux_write_then_read(
            self.pmic_profile.slave_addr,
            [page, address],
            1,
        )
        return int(raw[0] if isinstance(raw, (list, tuple, bytes, bytearray)) else raw)

    def _read_rtq6749_vcom(self, target: str, vcom_addr: int) -> int:
        select_value = 0x00 if target == "mtp" else 0x80
        if self.config.tcon_key == "i2c":
            self.card_lib.iic_write(vcom_addr, 0x02, bytes([select_value]))
            self.logger(f"RTQ6749 VCOM_F I2C read source: slave=0x{vcom_addr:02X} reg=0x02 <- 0x{select_value:02X}")
            value = int(self.card_lib.iic_read(vcom_addr, 0x00, 1)[0])
            self.logger(f"Read VCOM_F I2C ({target.upper()}): addr=0x{vcom_addr:02X} reg=0x00 -> 0x{value:02X}")
            return value
        self.card_lib.iic_over_aux_write(vcom_addr, 0x02, [select_value])
        self.logger(f"RTQ6749 VCOM_F AUX read source: slave=0x{vcom_addr:02X} reg=0x02 <- 0x{select_value:02X}")
        value = int(self.card_lib.iic_over_aux_read(vcom_addr, 0x00, 1)[0])
        self.logger(f"Read VCOM_F AUX ({target.upper()}): addr=0x{vcom_addr:02X} reg=0x00 -> 0x{value:02X}")
        return value

    def _write_rtq6749_vcom(self, value: int, target: str, vcom_addr: int) -> None:
        select_value = 0x00 if target == "mtp" else 0x80
        if self.config.tcon_key == "i2c":
            self.card_lib.iic_write(vcom_addr, 0x02, bytes([select_value]))
            self.logger(f"RTQ6749 VCOM_F I2C write source: slave=0x{vcom_addr:02X} reg=0x02 <- 0x{select_value:02X}")
            self.card_lib.iic_write(vcom_addr, 0x00, bytes([value]))
            self.logger(f"Write VCOM_F I2C ({target.upper()}): addr=0x{vcom_addr:02X} reg=0x00 <- 0x{value:02X}")
            return
        self.card_lib.iic_over_aux_write(vcom_addr, 0x02, [select_value])
        self.logger(f"RTQ6749 VCOM_F AUX write source: slave=0x{vcom_addr:02X} reg=0x02 <- 0x{select_value:02X}")
        self.card_lib.iic_over_aux_write(vcom_addr, 0x00, [value])
        self.logger(f"Write VCOM_F AUX ({target.upper()}): addr=0x{vcom_addr:02X} reg=0x00 <- 0x{value:02X}")

    def _read_lx_vcom_via_pmic(self, target: str) -> int:
        slave = self.pmic_profile.slave_addr
        red_value = 0x01 if target == "mtp" else 0x00
        if target == "mtp":
            self.card_lib.iic_over_aux_write(slave, 0xFF, [red_value])
            self.logger(f"LX52042C VCOM read source: slave=0x{slave:02X} reg=0xFF <- 0x{red_value:02X}")
        try:
            coarse = int(self.card_lib.iic_over_aux_read(slave, 0x0A, 1)[0])
            lsb = int(self.card_lib.iic_over_aux_read(slave, 0x0B, 1)[0]) & 0x07
            value = (coarse << 3) | lsb
            self.logger(
                f"Read VCOM ({target.upper()}): slave=0x{slave:02X} "
                f"coarse=0x{coarse:02X} lsb=0x{lsb:02X}"
            )
            return value
        finally:
            if target == "mtp":
                self.card_lib.iic_over_aux_write(slave, 0xFF, [0x00])
                self.logger(f"LX52042C VCOM read source restore: slave=0x{slave:02X} reg=0xFF <- 0x00")

    def _write_lx_vcom_via_pmic(self, value: int, target: str) -> None:
        coarse = (value >> 3) & 0xFF
        lsb = value & 0x07
        slave = self.pmic_profile.slave_addr
        self.card_lib.iic_over_aux_write(slave, 0x0A, [coarse, lsb])
        self.logger(
            f"Write VCOM ({target.upper()}): slave=0x{slave:02X} "
            f"coarse=0x{coarse:02X} lsb=0x{lsb:02X}"
        )
        if target == "mtp":
            self.card_lib.iic_over_aux_write(slave, 0xFE, [0x08])
            self.logger(f"LX52042C VCOM MTP commit: slave=0x{slave:02X} reg=0xFE <- 0x08")

    def run_mtp_commit(self) -> str:
        self.ensure_ready()
        self._ensure_pmic_unlock()
        if self.pmic_profile.mtp_action is None:
            raise AuxGuiError(f"{self.pmic_profile.name} does not define MTP commit")
        try:
            result = self.pmic_profile.mtp_action.callback(self.card_lib, self.pmic_profile, self.logger)
            self.logger(result)
            return result
        except Exception as exc:
            raise AuxGuiError(f"MTP commit failed: {exc}") from exc

    def close(self) -> None:
        if self.closed:
            return
        try:
            if hasattr(self.card_lib, "free_lib"):
                self.card_lib.free_lib()
        finally:
            self.closed = True
            self.logger("Disconnected")


def _load_jtool_module(module_path: Path):
    spec = importlib.util.spec_from_file_location("pmic_aux_jtool", module_path)
    if spec is None or spec.loader is None:
        raise AuxGuiError(f"Unable to load jtoollib module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    old_cwd = Path.cwd()
    try:
        os.chdir(module_path.parent)
        spec.loader.exec_module(module)
    finally:
        os.chdir(old_cwd)
    return module


class DirectJtoolI2c:
    def __init__(self, module, handle, logger: Callable[[str], None]) -> None:
        self.module = module
        self.handle = handle
        self.logger = logger
        self.closed = False

    def iic_over_aux_write(self, addr: int, offset: int, data: list[int] | bytes) -> None:
        self.module.i2c_write(self.handle, addr=addr, reg=offset, data=data)

    def iic_over_aux_read(self, addr: int, offset: int, length: int) -> list[int]:
        return list(self.module.i2c_read(self.handle, addr=addr, reg=offset, length=length))

    def iic_over_aux_write_then_read(self, addr: int, prefix: list[int] | bytes, length: int) -> list[int]:
        self.module.i2c_write_no_reg(self.handle, addr=addr, data=list(prefix))
        result = self.iic_read_s(addr, length)
        return [int(result)] if length == 1 else list(result)

    def iic_write(self, addr: int, offset: int, data: list[int] | bytes) -> None:
        actual_addr = addr << 1 if addr < 0x40 else addr
        self.iic_over_aux_write(actual_addr, offset, data)

    def iic_read(self, addr: int, offset: int, length: int) -> list[int]:
        actual_addr = addr << 1 if addr < 0x40 else addr
        return self.iic_over_aux_read(actual_addr, offset, length)

    def iic_write_s(self, addr: int, value: int) -> None:
        self.module.i2c_write_no_reg(self.handle, addr=addr, data=[value])

    def iic_read_s(self, addr: int, length: int = 1) -> int | list[int]:
        buf = (self.module.c_uint8 * length)()
        err = self.module.jtool.I2CRead(
            self.handle,
            self.module.c_uint8(addr),
            self.module.c_int(self.module.REGADDR_TYPE.REGADDR_NONE),
            self.module.c_uint32(0),
            self.module.c_uint16(length),
            buf,
        )
        if err != 0:
            raise RuntimeError(f"I2CRead failed, error code: {err}")
        if length == 1:
            return int(buf[0])
        return [int(buf[i]) for i in range(length)]

    def write_vcom(self, raw_value: int, vcom_addr: int = 0x9E) -> None:
        self.iic_write_s(vcom_addr, raw_value)

    def read_vcom(self, vcom_addr: int = 0x9E) -> int:
        return int(self.iic_read_s(vcom_addr | 0x01))

    def free_lib(self) -> None:
        if self.closed:
            return
        try:
            self.module.jtool.DevClose(self.handle)
        finally:
            self.closed = True


class GpuAuxCard:
    def __init__(self, port, tcon_key: str, backend: str, logger: Callable[[str], None]) -> None:
        self.port = port
        self.tcon_key = tcon_key
        self.backend = backend.upper()
        self.logger = logger
        self.closed = False

    def write_dpcd(self, addr: int, data: bytes) -> None:
        payload = bytes(data)
        if not payload:
            raise ValueError("DPCD write payload cannot be empty")
        self.port.write_dpcd(addr, payload)

    def read_dpcd(self, addr: int, length: int) -> bytes:
        return bytes(self.port.read_dpcd(addr, length))

    def iic_over_aux_write(self, addr: int, offset: int, data: list[int] | bytes) -> None:
        self._prepare_iic_over_aux(addr)
        try:
            self.iic_write(_write_addr_to_7bit(addr), offset, data)
        finally:
            self._finish_iic_over_aux()

    def iic_over_aux_read(self, addr: int, offset: int, length: int) -> list[int]:
        self._prepare_iic_over_aux(addr)
        try:
            return self.iic_read(_write_addr_to_7bit(addr), offset, length)
        finally:
            self._finish_iic_over_aux()

    def iic_over_aux_write_then_read(self, addr: int, prefix: list[int] | bytes, length: int) -> list[int]:
        self._prepare_iic_over_aux(addr)
        try:
            device = _addr7_to_write_addr(_write_addr_to_7bit(addr))
            self.port.i2c_write(device, bytes(prefix))
            return list(self.port.i2c_read(device, length))
        finally:
            self._finish_iic_over_aux()

    def iic_write(self, addr: int, offset: int, data: list[int] | bytes) -> None:
        payload = bytes([offset & 0xFF]) + bytes(data)
        self.port.i2c_write(_addr7_to_write_addr(addr), payload)

    def iic_read(self, addr: int, offset: int, length: int) -> list[int]:
        device = _addr7_to_write_addr(addr)
        self.port.i2c_write(device, bytes([offset & 0xFF]))
        return list(self.port.i2c_read(device, length))

    def iic_write_s(self, addr: int, value: int) -> None:
        self.port.i2c_write(_addr7_to_write_addr(addr), bytes([value & 0xFF]))

    def iic_read_s(self, addr: int, length: int = 1) -> int | list[int]:
        data = list(self.port.i2c_read(_addr7_to_write_addr(addr), length))
        if length == 1:
            return int(data[0])
        return data

    def write_vcom(self, raw_value: int, vcom_addr: int = 0x9E) -> None:
        self._prepare_iic_over_aux(vcom_addr)
        try:
            self.iic_write_s(_write_addr_to_7bit(vcom_addr), raw_value)
        finally:
            self._finish_iic_over_aux()

    def read_vcom(self, vcom_addr: int = 0x9E) -> int:
        self._prepare_iic_over_aux(vcom_addr)
        try:
            return int(self.iic_read_s(_write_addr_to_7bit(vcom_addr)))
        finally:
            self._finish_iic_over_aux()

    def free_lib(self) -> None:
        if self.closed:
            return
        try:
            self.port.close()
        finally:
            self.closed = True

    def _prepare_iic_over_aux(self, slave_addr: int) -> None:
        if self.tcon_key == "nova":
            self.write_dpcd(0x00102, b"\xC0")
            self.iic_write(0x60, 0x02, b"\x04\x00")
        elif self.tcon_key == "anx":
            self.write_dpcd(0x004F5, b"\x41\x56\x4F\x20\x16")
            self.write_dpcd(0x004F0, b"\x0E\x00\x00\x00")
            self.write_dpcd(0x004F3, b"\x01")
            self.write_dpcd(0x004F0, b"\x0E\x00\x00\x30\x09")
        elif self.tcon_key == "parade":
            for value in b"PARADE-FW-DP\x00\x06\x03\x03":
                self.write_dpcd(0x00480, bytes([value]))
            self.read_dpcd(0x00480, 1)
            self.write_dpcd(0x00482, b"\x80")
            self.write_dpcd(0x0048B, b"\x90")
            self.write_dpcd(0x0048E, bytes([slave_addr & 0xFF]))

    def _finish_iic_over_aux(self) -> None:
        if self.tcon_key == "parade":
            self.write_dpcd(0x00480, b"\x00")
            self.write_dpcd(0x00480, b"\x00")


def _addr7_to_write_addr(addr: int) -> int:
    if not 0 <= addr <= 0xFF:
        raise ValueError(f"I2C address out of range: 0x{addr:X}")
    if addr < 0x80:
        return (addr << 1) & 0xFE
    return addr & 0xFE


def _write_addr_to_7bit(addr: int) -> int:
    if not 0 <= addr <= 0xFF:
        raise ValueError(f"I2C address out of range: 0x{addr:X}")
    return (addr & 0xFE) >> 1


def enumerate_gpu_aux_port_choices(gpu_key: str) -> list[GpuAuxPortChoice]:
    if gpu_key == "i2c":
        return []
    if gpu_key not in GPU_AUX_TARGETS:
        raise AuxGuiError(f"GPU backend {gpu_key} is not supported by gpu-aux")
    try:
        from gpu_aux import enumerate_gpus, enumerate_ports

        backend, kind = GPU_AUX_TARGETS[gpu_key]
        choices: list[GpuAuxPortChoice] = []
        for gpu_index, _gpu in enumerate(enumerate_gpus(backend)):
            matching_ports = [port for port in enumerate_ports(backend, gpu_index) if port.kind == kind]
            for port_index, port in enumerate(matching_ports):
                name = getattr(port, "name", "") or "Unknown display"
                identity = getattr(port, "identity", f"{backend}:{gpu_index}:{port_index}")
                label = f"GPU {gpu_index} {kind} {port_index}  {identity}  {name}"
                choices.append(
                    GpuAuxPortChoice(
                        label=label,
                        backend=backend,
                        kind=kind,
                        gpu_index=gpu_index,
                        port_index=port_index,
                        identity=identity,
                    )
                )
        return choices
    except AuxGuiError:
        raise
    except Exception as exc:
        raise AuxGuiError(f"Display enumeration failed: {exc}") from exc


def connect(config: SessionConfig, logger: Callable[[str], None]) -> "_WorkerBackedAuxSession":
    worker = _WorkerBackedAuxSession.start(config, logger)
    return worker


def _connect_local(config: SessionConfig, logger: Callable[[str], None]) -> LocalAuxSession:
    base_pmic_profile = PMIC_PROFILES[config.pmic_key]
    if base_pmic_profile.direct_i2c_only and config.gpu_key != "i2c":
        raise AuxGuiError(f"{base_pmic_profile.name} supports Direct I2C only; AUX access is disabled")
    if config.gpu_key == "i2c":
        return _connect_local_i2c(config, logger)
    if config.gpu_key not in GPU_CARD_IDS:
        raise AuxGuiError(f"Unknown GPU card id key: {config.gpu_key}")
    if config.gpu_key not in GPU_AUX_TARGETS:
        raise AuxGuiError(f"GPU backend {config.gpu_key} is not supported by gpu-aux")
    tcon_profile = TCON_PROFILES[config.tcon_key]
    pmic_profile = replace(
        base_pmic_profile,
        slave_addr=config.pmic_slave_addr,
        unlock_slave_addr=config.pmic_slave_addr if base_pmic_profile.unlock_before_access else base_pmic_profile.unlock_slave_addr,
    )
    try:
        from gpu_aux import AuxPort

        backend, kind = GPU_AUX_TARGETS[config.gpu_key]
        logger(
            f"Connecting via gpu-aux: GPU={config.gpu_key} backend={backend} port={kind} "
            f"gpu_index={config.gpu_index} port_index={config.port_index} "
            f"TCON={tcon_profile.name} PMIC={pmic_profile.name} "
            f"PMIC_ADDR=0x{pmic_profile.slave_addr:02X} "
            f"NOVA_IIC_EN={config.nova_use_iic_en} NOVA_IIC_EN_SCHEME={config.nova_iic_en_scheme}"
        )
        port = AuxPort(kind, index=config.port_index, gpu_index=config.gpu_index, backend=backend)
        card_lib = GpuAuxCard(port, config.tcon_key, backend, logger)
        logger(f"gpu-aux port opened: {port.identity}")
    except Exception as exc:
        raise AuxGuiError(f"Connection failed: {exc}") from exc
    return LocalAuxSession(
        config=config,
        logger=logger,
        card_lib=card_lib,
        tcon_profile=tcon_profile,
        pmic_profile=pmic_profile,
    )


def _connect_local_i2c(config: SessionConfig, logger: Callable[[str], None]) -> LocalAuxSession:
    default_jtool_dll_path, default_jtool_module_path = default_i2c_paths(Path(__file__).resolve().parents[1])
    jtool_dll_path = config.jtool_dll_path or default_jtool_dll_path
    jtool_module_path = config.jtool_module_path or default_jtool_module_path
    if not jtool_dll_path.exists():
        raise AuxGuiError(f"jtool.dll not found: {jtool_dll_path}")
    if not jtool_module_path.exists():
        raise AuxGuiError(f"jtoollib.py not found: {jtool_module_path}")
    base_pmic_profile = PMIC_PROFILES[config.pmic_key]
    pmic_profile = replace(
        base_pmic_profile,
        slave_addr=config.pmic_slave_addr,
        unlock_slave_addr=config.pmic_slave_addr if base_pmic_profile.unlock_before_access else base_pmic_profile.unlock_slave_addr,
    )
    module = _load_jtool_module(jtool_module_path)
    try:
        devices = module.scan_devices_sn()
        sn = devices[0] if devices else None
        handle = module.open_device(sn) if sn else module.open_device(idx=0)
        card_lib = DirectJtoolI2c(module, handle, logger)
        logger(
            f"Connecting direct I2C: SN={sn or 'default'} "
            f"PMIC={pmic_profile.name} PMIC_ADDR=0x{pmic_profile.slave_addr:02X}"
        )
        logger("jtool I2C device opened")
    except Exception as exc:
        raise AuxGuiError(f"Connection failed: {exc}") from exc
    direct_config = replace(config, tcon_key="i2c", nova_use_iic_en=False)
    return LocalAuxSession(
        config=direct_config,
        logger=logger,
        card_lib=card_lib,
        tcon_profile=TCON_PROFILES["i2c"],
        pmic_profile=pmic_profile,
        init_summary="Direct I2C ready",
    )


def _find_register(profile: PmicProfile, reg_key: str):
    for register in profile.registers:
        if register.key == reg_key:
            return register
    raise AuxGuiError(f"Unknown register key: {reg_key}")


def normalize_register_value(register, value: int) -> int:
    logic_mask = 0
    for option in getattr(register, "bit_options", ()):
        logic_mask |= option.bit_mask
    explicit_mask = getattr(register, "value_mask", None)
    explicit_numeric_mask = getattr(register, "numeric_mask", None)
    if not getattr(register, "supports_slider", True):
        numeric_mask = 0
    elif explicit_numeric_mask is not None:
        numeric_mask = explicit_numeric_mask
    elif logic_mask:
        numeric_mask = register.max_value & ~logic_mask
    elif explicit_mask is not None:
        numeric_mask = explicit_mask
    else:
        numeric_mask = register.max_value
    if explicit_mask is not None:
        allowed_mask = explicit_mask
    elif logic_mask:
        allowed_mask = logic_mask | numeric_mask
    else:
        allowed_mask = register.max_value
    normalized = value & allowed_mask
    return max(register.min_value, min(register.max_value, normalized))


def validate_register_value(min_value: int, max_value: int, value: int) -> None:
    if not min_value <= value <= max_value:
        raise AuxGuiError(f"Value 0x{value:02X} out of range 0x{min_value:02X}-0x{max_value:02X}")


def parse_hex_input(text: str) -> int:
    cleaned = text.strip().lower()
    if cleaned.startswith("0x"):
        cleaned = cleaned[2:]
    if not cleaned:
        raise AuxGuiError("Empty value")
    try:
        return int(cleaned, 16)
    except ValueError as exc:
        raise AuxGuiError(f"Invalid hex value: {text}") from exc


def default_i2c_paths(base_dir: Path) -> tuple[Path, Path]:
    pylib = base_dir / "drivers" / "jtool"
    return pylib / "jtool.dll", pylib / "jtoollib.py"


def format_exception(exc: Exception) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()


def format_register_display(profile: PmicProfile, register_key: str, value: int, current_values: dict[str, int] | None = None) -> str:
    register = _find_register(profile, register_key)
    current_values = current_values or {}

    if profile.key == "nt51950":
        if register.address in {0x00, 0x01} and register.display_formatter is not None:
            return register.display_formatter(value)
        return _format_nt51950_register_voltage(register.address, value, current_values)

    if profile.key == "b802" and register.address == 0x04:
        freq_code = current_values.get("reg_03", 0x0C) & 0x0F
        freq_values = (100, 150, 200, 250, 300, 400, 500, 600, 700, 800, 900, 1000, 1225, 1335, 1450, 1600)
        fsw = freq_values[freq_code]
        dac = (value >> 2) & 0x3F
        low_freq = 16000 / ((16000 / fsw) + (8 * dac) + 7)
        rates = ("0.5V/ns", "1V/ns", "2V/ns", "4V/ns")
        return f"LX {rates[value & 0x03]} / PFM {low_freq:.2f}kHz"

    if profile.key in {"nvp2515", "b602"}:
        if register.address == 0x01:
            from . import profiles as _profiles
            if profile.key == "nvp2515":
                _profiles._nvp_high_res["enabled"] = bool(value & 0x80)
            else:
                _profiles._b602_high_res["enabled"] = bool(value & 0x80)
        elif register.address == 0x05:
            high_res = bool(current_values.get("reg_01", 0x00) & 0x80)
            code = value & 0x1F
            actual = 12.5 + 0.5 * code if high_res else min(6.0 + 0.2 * code, 12.0)
            return f"{actual:.2f}V"
        elif register.address == 0x1C:
            from . import profiles as _profiles
            code = value & 0x1F
            min_value = -3.6 if code <= 0x02 else -3.6 + 0.15 * (code - 2)
            if profile.key == "nvp2515":
                _profiles._nvp_vcom_min["value"] = min_value
            else:
                _profiles._b602_vcom_min["value"] = min_value
        elif register.address == 0x0C:
            avdd = current_values.get("reg_03")
            if avdd is not None:
                return f"{4.0 + 0.05 * avdd - 0.02 * value:.2f}V"
        elif register.address == 0x0D:
            avee = current_values.get("reg_04")
            if avee is not None:
                return f"{-4.0 - 0.1 * avee + 0.02 * value:.2f}V"

    if profile.key == "nt50805":
        if register.address == 0x1C:
            from . import profiles as _profiles
            code = value & 0x1F
            _profiles._nt_vcom_min["value"] = -3.6 if code <= 0x02 else -3.6 + 0.15 * (code - 2)
        elif register.address == 0x03:
            return f"{4.0 + 0.05 * (value & 0x3F):.2f}V"
        elif register.address == 0x04:
            return f"{-4.0 - 0.1 * (value & 0x1F):.2f}V"
        elif register.address == 0x05:
            return _format_nt_vgh(value, current_values.get("reg_01", 0x00))
        elif register.address == 0x06:
            return f"{-5.4 - 0.2 * (value & 0x3F):.2f}V"
        elif register.address == 0x07:
            return f"{0.8 + 0.02 * (value & 0x3F):.2f}V"
        elif register.address == 0x08:
            return f"{1.0 + 0.05 * (value & 0x1F):.2f}V"
        elif register.address == 0x09:
            return f"{min(1.7 + 0.1 * (value & 0x0F), 2.8):.2f}V"
        elif register.address == 0x0B:
            return f"{2.0 + 0.1 * (value & 0x07):.2f}V"
        elif register.address == 0x0C:
            avdd = current_values.get("reg_03")
            if avdd is not None:
                return f"{4.0 + 0.05 * (avdd & 0x3F) - 0.02 * (value & 0x3F):.2f}V"
        elif register.address == 0x0D:
            avee = current_values.get("reg_04")
            if avee is not None:
                return f"{-4.0 - 0.1 * (avee & 0x1F) + 0.02 * (value & 0x3F):.2f}V"

    if profile.key == "rt6755":
        if register.address == 0x03:
            from . import profiles as _profiles
            _profiles._rt_high_res["enabled"] = not bool(value & 0x80)
        elif register.address == 0x05:
            return f"{4.0 + 0.05 * (value & 0x3F):.2f}V"
        elif register.address == 0x06:
            return f"{-4.0 - 0.1 * (value & 0x1F):.2f}V"
        elif register.address == 0x07:
            high_res = not bool(current_values.get('reg_03', 0x07) & 0x80)
            actual = 10.0 + 1.0 * (value & 0x1F) if high_res else 6.0 + 0.2 * (value & 0x1F)
            return f"{actual:.2f}V"
        elif register.address == 0x08:
            return f"{-5.4 - 0.2 * (value & 0x3F):.2f}V"
        elif register.address == 0x09:
            return f"{0.8 + 0.02 * (value & 0x3F):.2f}V"
        elif register.address == 0x0A:
            return f"{1.0 + 0.05 * (value & 0x1F):.2f}V"
        elif register.address == 0x0C:
            return f"{-2.56 + 0.02 * (value & 0xFF):.2f}V"
        elif register.address == 0x0E:
            pavdd = current_values.get("reg_05")
            if pavdd is not None:
                return f"{4.0 + 0.05 * (pavdd & 0x3F) - 0.02 * (value & 0x3F):.2f}V"
        elif register.address == 0x0F:
            navdd = current_values.get("reg_06")
            if navdd is not None:
                return f"{-4.0 - 0.1 * (navdd & 0x1F) + 0.02 * (value & 0x3F):.2f}V"

    if profile.key == "rtq6749":
        if register.address == 0x00:
            return f"{5.0 + 0.05 * min(value & 0x3F, 0x2E):.2f}V"
        elif register.address == 0x01:
            return f"{-5.0 - 0.05 * min(value & 0x3F, 0x2E):.2f}V"
        elif register.address == 0x02:
            return f"{min(7.0 + 0.5 * (value & 0x3F), 30.0):.2f}V"
        elif register.address == 0x03:
            return f"{-6.0 - 0.25 * min(value & 0x3F, 0x30):.2f}V"
        elif register.address == 0x04:
            return f"{-3.0 + 0.02 * min(value & 0xFF, 0xFA):.2f}V"
        elif register.address == 0x05:
            return f"{2.0 + 2.0 * (value & 0x03):.1f}V"
        elif register.address == 0x06:
            return ("600kHz", "800kHz", "1MHz", "2.2MHz")[value & 0x03]
        elif register.address in {0x07, 0x09, 0x0B, 0x0D, 0x0F, 0x10}:
            return f"{5 * (value & 0x0F)}ms"
        elif register.address in {0x08, 0x0E}:
            return f"{5 * ((value & 0x07) + 1)}ms"
        elif register.address == 0x0A:
            return f"{3 * ((value & 0x07) + 1)}ms"
        elif register.address == 0x0C:
            return f"{5 * ((value & 0x03) + 1)}ms"
        elif register.address == 0x11:
            return f"{3 * (value & 0x0F)}ms"
        elif register.address in {0x18, 0x19, 0x1A, 0x1B}:
            return f"{2 * (value & 0x07)}ms"
        elif register.address == 0x1C:
            return f"VCOM off {2 * (value & 0x07)}ms"

    if profile.key == "lx52042c":
        if register.address == 0x03:
            return f"{4.0 + 0.05 * (value & 0x3F):.2f}V"
        elif register.address == 0x04:
            return f"{-4.0 - 0.05 * (value & 0x3F):.2f}V"
        elif register.address == 0x05:
            high_res = bool(current_values.get("reg_01", 0x00) & 0x20)
            code = value & 0x1F
            actual = 12.5 + 0.5 * code if high_res else min(6.0 + 0.2 * code, 12.0)
            return f"{actual:.2f}V"
        elif register.address == 0x06:
            return f"{-5.4 - 0.2 * (value & 0x3F):.2f}V"
        elif register.address == 0x07:
            return f"{0.8 + 0.02 * (value & 0x3F):.2f}V"
        elif register.address == 0x08:
            return f"{1.0 + 0.05 * (value & 0x1F):.2f}V"
        elif register.address == 0x09:
            return f"{-5.4 - 0.2 * (value & 0x3F):.2f}V"
        elif register.address == 0x0C:
            reset1 = 2.0 + 0.1 * (value & 0x07)
            reset2 = 2.0 + 0.1 * ((value >> 4) & 0x07)
            return f"R1 {reset1:.1f}V / R2 {reset2:.1f}V"
        elif register.address == 0x0D:
            pvdd = current_values.get("reg_03")
            if pvdd is not None:
                return f"{4.0 + 0.05 * (pvdd & 0x3F) - 0.02 * (value & 0x3F):.2f}V"
        elif register.address == 0x0E:
            nvdd = current_values.get("reg_04")
            if nvdd is not None:
                return f"{-4.0 - 0.05 * (nvdd & 0x3F) + 0.02 * (value & 0x3F):.2f}V"
        elif register.address == 0x1D:
            from . import profiles as _profiles
            code = value & 0x1F
            _profiles._lx_vcom_min["value"] = -3.6 if code <= 0x02 else -3.6 + 0.15 * (code - 2)
            return f"{_profiles._lx_vcom_min['value']:.2f}V"

    if register.display_formatter is not None:
        try:
            return register.display_formatter(value)
        except Exception:
            return "--"
    if register.bit_options:
        return "Enable" if any((value & opt.bit_mask) == opt.enabled_value for opt in register.bit_options) else "Disable"
    return "--"


def _format_nt51950_register_voltage(address: int, value: int, current_values: dict[str, int]) -> str:
    if address in {0x00, 0x01}:
        return "--"

    if address in {0x02, 0x03}:
        low = current_values.get("reg_02")
        high = current_values.get("reg_03")
        if low is None or high is None:
            return "--"
        code = ((high & 0x03) << 8) | (low & 0xFF)
        if code > 0x2FF:
            return f"Reserved / 0x{code:03X}"
        voltage = min(1.0, 1.28 - 0.005 * code)
        return f"{voltage:.3f}V / 0x{code:03X}"

    code = value & 0xFF
    if address == 0x04:
        return "Reserved" if code > 0x3E else f"{4.75 + 0.05 * code:.2f}V"
    if address == 0x05:
        return "Reserved" if code > 0x3E else f"{-4.75 - 0.05 * code:.2f}V"
    if address == 0x06:
        return "Reserved" if not 0x03 <= code <= 0x1F else f"{0.05 * (code + 1):.2f}V"
    if address == 0x07:
        return "Reserved" if not 0x03 <= code <= 0x1F else f"{-0.05 * (code + 1):.2f}V"
    if address == 0x08:
        voltage = 7.0 + 0.05 * code
        return "Reserved" if code & 0x03 not in {0x00, 0x02} or voltage > 19.0 else f"{voltage:.2f}V"
    if address == 0x09:
        voltage = -5.3 - 0.05 * code
        return "Reserved" if code & 0x03 not in {0x00, 0x02} or not -18.0 <= voltage <= -6.5 else f"{voltage:.2f}V"
    if address == 0x0A:
        return "Reserved" if code > 0x8C else f"{8.0 + 0.1 * code:.2f}V"
    if address == 0x0B:
        voltage = -6.5 - 0.05 * code
        return "Reserved" if code & 0x03 not in {0x00, 0x02} or voltage < -16.0 else f"{voltage:.2f}V"
    if address == 0x0D:
        return "Reserved" if not 0x07 <= code <= 0x1B else f"{5.3 + 0.1 * code:.2f}V"
    if address == 0x0E:
        return "Reserved" if not 0x07 <= code <= 0x1B else f"{-5.3 - 0.1 * code:.2f}V"
    return "--"


def format_vcom_display(profile: PmicProfile, value: int, current_values: dict[str, int] | None = None) -> str:
    current_values = current_values or {}
    if profile.key in {"nvp2515", "b602"}:
        min_code = current_values.get("reg_1C")
        if min_code is None:
            return "unknown vcom_min"
        code = min_code & 0x1F
        min_value = -3.6 if code <= 0x02 else -3.6 + 0.15 * (code - 2)
        return f"{min_value + 0.01 * value:.2f}V"
    if profile.key == "nt50805":
        min_code = current_values.get("reg_1C")
        if min_code is None:
            return "unknown vcom_min"
        code = min_code & 0x1F
        min_value = -3.6 if code <= 0x02 else -3.6 + 0.15 * (code - 2)
        return f"{min_value + 0.01 * value:.2f}V"
    if profile.key == "rt6755":
        coarse_code = current_values.get("reg_0C", 0x4E)
        coarse_value = -2.56 + 0.02 * coarse_code
        return f"{coarse_value + 0.01 * (value - 0x40):.2f}V"
    if profile.key == "rtq6749":
        coarse_code = current_values.get("reg_04")
        if coarse_code is None:
            return "unknown vcom_c"
        vcom_c = -3.0 + 0.02 * min(coarse_code & 0xFF, 0xFA)
        return f"{vcom_c + 0.01 * (value - 0x7F):.2f}V"
    if profile.key == "lx52042c":
        min_code = current_values.get("reg_1D")
        if min_code is None:
            return "unknown vcom_min"
        code = min_code & 0x1F
        min_value = -3.6 if code <= 0x02 else -3.6 + 0.15 * (code - 2)
        coarse = (value >> 3) & 0xFF
        lsb = value & 0x07
        return f"{min_value + 0.01 * coarse + 0.00125 * lsb:.4f}V"
    if profile.vcom.display_formatter is not None:
        try:
            return profile.vcom.display_formatter(value)
        except Exception:
            return "--"
    return "--"


def _format_nt_vgh(value: int, mode_value: int = 0x00) -> str:
    code = value & 0x1F
    high_res = bool(mode_value & 0x80)
    vgh_30 = bool(value & 0x20)
    if high_res:
        actual = min(12.5 + 0.5 * code, 28.0)
        if vgh_30 and code == 0x1F:
            actual = 30.0
    else:
        actual = min(6.0 + 0.2 * code, 12.0)
    return f"{actual:.2f}V"


def _is_bulk_excluded_vcom_register(profile: PmicProfile, register) -> bool:
    if profile.key == "rt6755":
        return False
    return "VCOM Voltage" in register.name


class _WorkerBackedAuxSession:
    def __init__(
        self,
        config: SessionConfig,
        logger: Callable[[str], None],
        process: subprocess.Popen[str],
        init_summary: str,
    ) -> None:
        self.config = config
        self.logger = logger
        self.process = process
        self.init_summary = init_summary
        self.closed = False
        self._request_lock = threading.Lock()
        self.tcon_profile = TCON_PROFILES[config.tcon_key]
        self.pmic_profile = PMIC_PROFILES[config.pmic_key]
        self._event_queue: queue.Queue[dict] = queue.Queue()
        self._stdout_thread = threading.Thread(target=self._read_stdout, name="pmic-aux-worker-stdout", daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, name="pmic-aux-worker-stderr", daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    @classmethod
    def start(cls, config: SessionConfig, logger: Callable[[str], None]):
        payload = _encode_config(config)
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            _build_worker_command(payload),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        session = cls(
            config=config,
            logger=logger,
            process=process,
            init_summary="Not initialized",
        )
        try:
            message = session._wait_for_startup_message(timeout=15.0)
        except queue.Empty as exc:
            stderr_output = session._drain_startup_stderr()
            process.kill()
            process.wait(timeout=2)
            raise AuxGuiError(f"Connection failed: worker startup timed out{stderr_output}") from exc
        if message.get("type") != "ready":
            stderr_output = session._drain_startup_stderr()
            process.kill()
            process.wait(timeout=2)
            error_text = message.get("error", "worker failed to start")
            if stderr_output:
                error_text = f"{error_text}{stderr_output}"
            raise AuxGuiError(error_text)
        session.init_summary = message.get("init_summary", "Not initialized")
        return session

    def _read_stdout(self) -> None:
        if self.process.stdout is None:
            return
        for line in self.process.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                message = {"type": "stderr", "message": f"[worker-stdout] {text}"}
            self._event_queue.put(message)
        self._event_queue.put({"type": "eof"})

    def _read_stderr(self) -> None:
        if self.process.stderr is None:
            return
        for line in self.process.stderr:
            text = line.rstrip()
            if text:
                self._event_queue.put({"type": "stderr", "message": text})

    def _wait_for_message(self, timeout: float | None = None) -> dict:
        return self._event_queue.get(timeout=timeout)

    def _wait_for_startup_message(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise queue.Empty
            message = self._wait_for_message(timeout=remaining)
            message_type = message.get("type")
            if message_type in {"log", "stderr"}:
                self.logger(str(message.get("message", "")))
                continue
            if message_type == "eof":
                raise AuxGuiError("Connection failed: worker exited during startup")
            return message

    def _drain_startup_stderr(self) -> str:
        lines: list[str] = []
        while True:
            try:
                message = self._event_queue.get_nowait()
            except queue.Empty:
                break
            if message.get("type") == "stderr":
                lines.append(str(message.get("message", "")))
        if not lines:
            return ""
        return "\n" + "\n".join(lines[-8:])

    def _call(self, command: str, **payload):
        if self.closed:
            raise AuxGuiError("Session is closed")
        if self.process.poll() is not None:
            self.closed = True
            raise AuxGuiError("Hardware worker exited unexpectedly")
        with self._request_lock:
            try:
                if self.process.stdin is None:
                    raise BrokenPipeError("worker stdin is unavailable")
                self.process.stdin.write(json.dumps({"type": "call", "command": command, "payload": payload}) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                self.closed = True
                raise AuxGuiError("Hardware worker is unavailable") from exc
            while True:
                try:
                    message = self._wait_for_message(timeout=30.0)
                except queue.Empty as exc:
                    raise AuxGuiError("Hardware worker did not respond in time") from exc
                if message.get("type") == "eof":
                    self.closed = True
                    raise AuxGuiError("Hardware worker closed unexpectedly")
                message_type = message.get("type")
                if message_type in {"log", "stderr"}:
                    self.logger(str(message.get("message", "")))
                    continue
                if message_type != "response":
                    raise AuxGuiError(f"Unexpected worker response: {message!r}")
                if message.get("status") == "ok":
                    self.init_summary = message.get("init_summary", self.init_summary)
                    return message.get("result")
                error_text = message.get("error", "Unknown hardware worker error")
                if message.get("traceback"):
                    self.logger(message["traceback"])
                raise AuxGuiError(error_text)

    def ensure_ready(self) -> None:
        self._call("ensure_ready")

    def read_register(self, reg_key: str, target: str = "dac") -> int:
        return int(self._call("read_register", reg_key=reg_key, target=target))

    def write_register(self, reg_key: str, value: int, target: str = "dac") -> None:
        self._call("write_register", reg_key=reg_key, value=value, target=target)

    def read_all_registers(self, target: str = "dac", exclude_vcom: bool = True) -> dict[str, int | str]:
        return dict(self._call("read_all_registers", target=target, exclude_vcom=exclude_vcom))

    def write_all_registers(self, values: dict[str, int], target: str = "dac", exclude_vcom: bool = True) -> None:
        self._call("write_all_registers", values=values, target=target, exclude_vcom=exclude_vcom)

    def read_vcom(self, target: str = "dac", device_addr: int | None = None) -> int:
        return int(self._call("read_vcom", target=target, device_addr=device_addr))

    def write_vcom(self, value: int, target: str = "dac", device_addr: int | None = None) -> None:
        self._call("write_vcom", value=value, target=target, device_addr=device_addr)

    def run_mtp_commit(self) -> str:
        return str(self._call("run_mtp_commit"))

    def close(self) -> None:
        if self.closed:
            return
        try:
            try:
                self._call("close")
            except AuxGuiError as exc:
                self.logger(f"Worker close fallback: {exc}")
        finally:
            self.closed = True
            try:
                if self.process.stdin is not None:
                    self.process.stdin.close()
            except OSError:
                pass
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
            self.logger("Disconnected")


def worker_main_from_payload(payload: str) -> int:
    config = _decode_config(payload)

    def worker_emit(message: dict) -> None:
        try:
            print(json.dumps(message, ensure_ascii=True), flush=True)
        except Exception:
            return

    def worker_log(message: str) -> None:
        worker_emit({"type": "log", "message": message})

    try:
        session = _connect_local(config, worker_log)
        worker_emit({"type": "ready", "init_summary": session.init_summary})
    except Exception as exc:
        worker_emit(
            {
                "type": "error",
                "error": format_exception(exc),
                "traceback": traceback.format_exc(),
            }
        )
        return 1

    try:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                worker_emit({"type": "error", "error": f"Invalid worker command: {line}"})
                break
            if message.get("type") != "call":
                continue
            command = message.get("command")
            payload = message.get("payload", {})
            try:
                if command == "close":
                    session.close()
                    worker_emit(
                        {
                            "type": "response",
                            "status": "ok",
                            "result": None,
                            "init_summary": session.init_summary,
                        }
                    )
                    break
                method = getattr(session, command)
                result = method(**payload)
                worker_emit(
                    {
                        "type": "response",
                        "status": "ok",
                        "result": result,
                        "init_summary": session.init_summary,
                    }
                )
            except Exception as exc:
                worker_emit(
                    {
                        "type": "response",
                        "status": "error",
                        "error": format_exception(exc),
                        "traceback": traceback.format_exc(),
                        "init_summary": session.init_summary,
                    }
                )
    finally:
        try:
            session.close()
        except Exception:
            pass
    return 0


def _encode_config(config: SessionConfig) -> str:
    payload = {
        "gpu_key": config.gpu_key,
        "tcon_key": config.tcon_key,
        "pmic_key": config.pmic_key,
        "pmic_slave_addr": config.pmic_slave_addr,
        "gpu_index": config.gpu_index,
        "port_index": config.port_index,
        "jtool_dll_path": str(config.jtool_dll_path) if config.jtool_dll_path is not None else "",
        "jtool_module_path": str(config.jtool_module_path) if config.jtool_module_path is not None else "",
        "nova_use_iic_en": config.nova_use_iic_en,
        "nova_iic_en_scheme": config.nova_iic_en_scheme,
        "use_local_bundle_paths": bool(getattr(sys, "frozen", False)),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _decode_config(payload: str) -> SessionConfig:
    data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    jtool_dll_path = Path(data["jtool_dll_path"]) if data.get("jtool_dll_path") else None
    jtool_module_path = Path(data["jtool_module_path"]) if data.get("jtool_module_path") else None
    if data.get("use_local_bundle_paths"):
        bundle_base = Path(__file__).resolve().parents[1]
        jtool_dll_path, jtool_module_path = default_i2c_paths(bundle_base)
    return SessionConfig(
        gpu_key=data["gpu_key"],
        tcon_key=data["tcon_key"],
        pmic_key=data["pmic_key"],
        pmic_slave_addr=int(data["pmic_slave_addr"]),
        gpu_index=int(data.get("gpu_index", 0)),
        port_index=int(data.get("port_index", 0)),
        jtool_dll_path=jtool_dll_path,
        jtool_module_path=jtool_module_path,
        nova_use_iic_en=bool(data.get("nova_use_iic_en", False)),
        nova_iic_en_scheme=str(data.get("nova_iic_en_scheme", "io1")),
    )


def _build_worker_command(payload: str) -> list[str]:
    if _is_compiled_binary():
        worker_executable = sys.executable or sys.argv[0]
        return [worker_executable, "--aux-worker", payload]
    return [sys.executable, "-m", "pmic_aux_gui", "--aux-worker", payload]


def _is_compiled_binary() -> bool:
    return bool(
        getattr(sys, "frozen", False)
        or "__compiled__" in globals()
        or hasattr(sys, "nuitka_binary_dir")
    )
