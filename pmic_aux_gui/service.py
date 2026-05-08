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


class AuxGuiError(RuntimeError):
    """User-facing hardware/service error."""


@dataclass
class SessionConfig:
    gpu_key: str
    tcon_key: str
    pmic_key: str
    pmic_slave_addr: int
    dll_path: Path
    operate_module_path: Path
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
        self.ensure_ready()
        self._ensure_pmic_unlock()
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
        value = normalize_register_value(register, value)
        validate_register_value(register.min_value, register.max_value, value)
        self.ensure_ready()
        self._ensure_pmic_unlock()
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
        if not exclude_vcom:
            results["vcom"] = self.read_vcom()
        return results

    def write_all_registers(self, values: dict[str, int], target: str = "dac", exclude_vcom: bool = True) -> None:
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

    def read_vcom(self, target: str = "dac") -> int:
        self.ensure_ready()
        self._ensure_pmic_unlock()
        if target == "mtp" and self.pmic_profile.mtp_read_mode == "fallback_to_dac":
            self.logger("MTP VCOM read falls back to DAC path")
        try:
            if self.pmic_profile.key == "lx52042c":
                return self._read_lx_vcom_via_pmic(target)
            if self.pmic_profile.vcom.use_special_accessor and hasattr(self.card_lib, "read_vcom"):
                raw_value = int(self.card_lib.read_vcom())
            elif self.pmic_profile.vcom.no_register_access:
                self._prepare_vcom_access()
                read_addr = self.pmic_profile.vcom.read_device_addr or self.pmic_profile.vcom.device_addr
                raw_value = int(self.card_lib.iic_read_s(read_addr))
            else:
                raw_value = int(
                    self.card_lib.iic_over_aux_read(
                        self.pmic_profile.vcom.device_addr,
                        self.pmic_profile.vcom.register_addr,
                        1,
                    )[0]
                )
            value = raw_value >> self.pmic_profile.vcom.raw_shift
            self.logger(f"Read {self.pmic_profile.vcom.name} raw=0x{raw_value:02X} logical=0x{value:02X}")
            return value
        except Exception as exc:
            raise AuxGuiError(f"Read VCOM failed: {exc}") from exc

    def write_vcom(self, value: int, target: str = "dac") -> None:
        validate_register_value(self.pmic_profile.vcom.min_value, self.pmic_profile.vcom.max_value, value)
        self.ensure_ready()
        self._ensure_pmic_unlock()
        try:
            if self.pmic_profile.key == "lx52042c":
                self._write_lx_vcom_via_pmic(value, target)
                return
            raw_value = value << self.pmic_profile.vcom.raw_shift
            if target == "dac":
                raw_value |= self.pmic_profile.vcom.dac_flag
            else:
                raw_value |= self.pmic_profile.vcom.mtp_flag
            if self.pmic_profile.vcom.use_special_accessor and hasattr(self.card_lib, "write_vcom"):
                self.card_lib.write_vcom(raw_value)
            elif self.pmic_profile.vcom.no_register_access:
                self._prepare_vcom_access()
                self.card_lib.iic_write_s(self.pmic_profile.vcom.device_addr, raw_value)
            else:
                self.card_lib.iic_over_aux_write(
                    self.pmic_profile.vcom.device_addr,
                    self.pmic_profile.vcom.register_addr,
                    [raw_value],
                )
            self.logger(f"Write {self.pmic_profile.vcom.name} logical=0x{value:02X} raw=0x{raw_value:02X}")
            if target == "mtp":
                self.logger(f"{self.pmic_profile.vcom.name} MTP write uses device-specific command bit; no extra PMIC MTP commit applied")
        except Exception as exc:
            raise AuxGuiError(f"Write VCOM failed: {exc}") from exc

    def _prepare_vcom_access(self) -> None:
        for step in self.tcon_profile.ready_sequence:
            step.callback(self.card_lib, self.logger)
        if self.config.tcon_key == "nova" and self.config.nova_use_iic_en:
            _step_nova_iic_en(self.card_lib, self.logger, self.config.nova_iic_en_scheme)

    def _prepare_register_read(self, register, target: str) -> None:
        if self.pmic_profile.key != "b602":
            return
        if register.address in {0xFE, 0xFF}:
            return
        red_value = 0x01 if target == "mtp" else 0x00
        self.logger(
            f"B602 prepare read: target={target.upper()} slave=0x{self.pmic_profile.slave_addr:02X} "
            f"select_reg=0xFF value=0x{red_value:02X} next_reg=0x{register.address:02X}"
        )
        self.card_lib.iic_over_aux_write(self.pmic_profile.slave_addr, 0xFF, [red_value])
        self.logger(
            f"B602 register read source: slave=0x{self.pmic_profile.slave_addr:02X} "
            f"reg=0xFF <- 0x{red_value:02X}"
        )
        time.sleep(0.01)
        self.logger(
            f"B602 register read command: slave=0x{self.pmic_profile.slave_addr:02X} "
            f"reg=0x{register.address:02X} after source select delay=10ms"
        )

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


def _load_operate_module(module_path: Path):
    spec = importlib.util.spec_from_file_location("pmic_aux_operate_card", module_path)
    if spec is None or spec.loader is None:
        raise AuxGuiError(f"Unable to load OperateCardLib module from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def connect(config: SessionConfig, logger: Callable[[str], None]) -> "_WorkerBackedAuxSession":
    worker = _WorkerBackedAuxSession.start(config, logger)
    return worker


def _connect_local(config: SessionConfig, logger: Callable[[str], None]) -> LocalAuxSession:
    if config.gpu_key not in GPU_CARD_IDS:
        raise AuxGuiError(f"Unknown GPU card id key: {config.gpu_key}")
    tcon_profile = TCON_PROFILES[config.tcon_key]
    base_pmic_profile = PMIC_PROFILES[config.pmic_key]
    pmic_profile = replace(
        base_pmic_profile,
        slave_addr=config.pmic_slave_addr,
        unlock_slave_addr=config.pmic_slave_addr if base_pmic_profile.unlock_before_access else base_pmic_profile.unlock_slave_addr,
    )
    module = _load_operate_module(config.operate_module_path)
    try:
        card_class = getattr(module, tcon_profile.adapter_class_name)
        card_lib = card_class(str(config.dll_path))
        card_id = GPU_CARD_IDS[config.gpu_key]
        logger(
            f"Connecting: GPU={config.gpu_key} CardID={card_id} "
            f"TCON={tcon_profile.name} PMIC={pmic_profile.name} "
            f"PMIC_ADDR=0x{pmic_profile.slave_addr:02X} "
            f"NOVA_IIC_EN={config.nova_use_iic_en} NOVA_IIC_EN_SCHEME={config.nova_iic_en_scheme}"
        )
        init_ok = bool(card_lib.init(card_id))
        if not init_ok:
            raise AuxGuiError(f"DLL init returned failure for CardID {card_id}")
        logger("DLL init succeeded")
    except Exception as exc:
        raise AuxGuiError(f"Connection failed: {exc}") from exc
    return LocalAuxSession(
        config=config,
        logger=logger,
        card_lib=card_lib,
        tcon_profile=tcon_profile,
        pmic_profile=pmic_profile,
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
    if not getattr(register, "supports_slider", True):
        numeric_mask = 0
    elif logic_mask:
        numeric_mask = register.max_value & ~logic_mask
    else:
        numeric_mask = register.max_value
    allowed_mask = (logic_mask | numeric_mask) if logic_mask else register.max_value
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


def default_paths(base_dir: Path) -> tuple[Path, Path]:
    pylib = base_dir / "circuit_project" / "pylib"
    return pylib / "OperateCardLib.dll", pylib / "OperateCardLib.py"


def format_exception(exc: Exception) -> str:
    return "".join(traceback.format_exception_only(type(exc), exc)).strip()


def format_register_display(profile: PmicProfile, register_key: str, value: int, current_values: dict[str, int] | None = None) -> str:
    register = _find_register(profile, register_key)
    current_values = current_values or {}

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
    vgh_30 = bool(value & 0x04)
    if high_res:
        actual = min(12.5 + 0.5 * code, 28.0)
        if vgh_30 and actual >= 28.0:
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

    def read_vcom(self, target: str = "dac") -> int:
        return int(self._call("read_vcom", target=target))

    def write_vcom(self, value: int, target: str = "dac") -> None:
        self._call("write_vcom", value=value, target=target)

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
        "dll_path": str(config.dll_path),
        "operate_module_path": str(config.operate_module_path),
        "nova_use_iic_en": config.nova_use_iic_en,
        "nova_iic_en_scheme": config.nova_iic_en_scheme,
        "use_local_bundle_paths": bool(getattr(sys, "frozen", False)),
    }
    return base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


def _decode_config(payload: str) -> SessionConfig:
    data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    dll_path = Path(data["dll_path"])
    operate_module_path = Path(data["operate_module_path"])
    if data.get("use_local_bundle_paths"):
        bundle_base = Path(__file__).resolve().parents[1]
        dll_path, operate_module_path = default_paths(bundle_base)
    return SessionConfig(
        gpu_key=data["gpu_key"],
        tcon_key=data["tcon_key"],
        pmic_key=data["pmic_key"],
        pmic_slave_addr=int(data["pmic_slave_addr"]),
        dll_path=dll_path,
        operate_module_path=operate_module_path,
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
