from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
import importlib.util
import traceback

from .profiles import GPU_CARD_IDS, PMIC_PROFILES, TCON_PROFILES, PmicProfile, TconProfile, _step_nova_iic_en


class AuxGuiError(RuntimeError):
    """User-facing hardware/service error."""


@dataclass
class SessionConfig:
    gpu_key: str
    tcon_key: str
    pmic_key: str
    dll_path: Path
    operate_module_path: Path
    nova_use_iic_en: bool = False


@dataclass
class AuxSession:
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
                self.logger("Init step: NOVA IIC_EN")
                _step_nova_iic_en(self.card_lib, self.logger)
            self.initialized = True
            suffix = " with IIC_EN" if self.config.tcon_key == "nova" and self.config.nova_use_iic_en else ""
            self.init_summary = f"Initialized {self.tcon_profile.name}{suffix} for {self.pmic_profile.name}"
            self.logger(self.init_summary)
        except Exception as exc:
            raise AuxGuiError(f"Initialization failed: {exc}") from exc

    def _ensure_pmic_unlock(self) -> None:
        if self.unlocked or not self.pmic_profile.unlock_before_access:
            return
        if self.pmic_profile.unlock_register is None or not self.pmic_profile.unlock_data:
            return
        try:
            self.ensure_ready()
            data = bytes(self.pmic_profile.unlock_data)
            self.logger(
                f"PMIC unlock: slave=0x{self.pmic_profile.slave_addr:02X} "
                f"reg=0x{self.pmic_profile.unlock_register:02X} data={data.hex(' ').upper()}"
            )
            self.card_lib.iic_over_aux_write(
                self.pmic_profile.slave_addr,
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
            if exclude_vcom and register.address == 0x0A and "VCOM Voltage" in register.name:
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
        for register in self.pmic_profile.registers:
            if exclude_vcom and register.address == 0x0A and "VCOM Voltage" in register.name:
                continue
            if not register.writable:
                continue
            if register.key not in values:
                continue
            self.write_register(register.key, values[register.key], target=target)

    def read_vcom(self, target: str = "dac") -> int:
        self.ensure_ready()
        self._ensure_pmic_unlock()
        if target == "mtp" and self.pmic_profile.mtp_read_mode == "fallback_to_dac":
            self.logger("MTP VCOM read falls back to DAC path")
        try:
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
            self.logger(f"Read VCOM raw=0x{raw_value:02X} logical=0x{value:02X}")
            return value
        except Exception as exc:
            raise AuxGuiError(f"Read VCOM failed: {exc}") from exc

    def write_vcom(self, value: int, target: str = "dac") -> None:
        validate_register_value(self.pmic_profile.vcom.min_value, self.pmic_profile.vcom.max_value, value)
        self.ensure_ready()
        self._ensure_pmic_unlock()
        try:
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
            self.logger(f"Write VCOM logical=0x{value:02X} raw=0x{raw_value:02X}")
            if target == "mtp":
                self.logger("VCOM MTP write uses device-specific command bit; no extra PMIC MTP commit applied")
        except Exception as exc:
            raise AuxGuiError(f"Write VCOM failed: {exc}") from exc

    def _prepare_vcom_access(self) -> None:
        for step in self.tcon_profile.ready_sequence:
            step.callback(self.card_lib, self.logger)
        if self.config.tcon_key == "nova" and self.config.nova_use_iic_en:
            _step_nova_iic_en(self.card_lib, self.logger)

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


def connect(config: SessionConfig, logger: Callable[[str], None]) -> AuxSession:
    if config.gpu_key not in GPU_CARD_IDS:
        raise AuxGuiError(f"Unknown GPU card id key: {config.gpu_key}")
    tcon_profile = TCON_PROFILES[config.tcon_key]
    pmic_profile = PMIC_PROFILES[config.pmic_key]
    module = _load_operate_module(config.operate_module_path)
    try:
        card_class = getattr(module, tcon_profile.adapter_class_name)
        card_lib = card_class(str(config.dll_path))
        card_id = GPU_CARD_IDS[config.gpu_key]
        logger(
            f"Connecting: GPU={config.gpu_key} CardID={card_id} "
            f"TCON={tcon_profile.name} PMIC={pmic_profile.name} NOVA_IIC_EN={config.nova_use_iic_en}"
        )
        card_lib.init(card_id)
        logger("DLL init succeeded")
    except Exception as exc:
        raise AuxGuiError(f"Connection failed: {exc}") from exc
    return AuxSession(
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

    if profile.key == "nvp2515":
        if register.address == 0x01:
            from . import profiles as _profiles
            _profiles._nvp_high_res["enabled"] = bool(value & 0x80)
        elif register.address == 0x05:
            high_res = bool(current_values.get("reg_01", 0x00) & 0x80)
            code = value & 0x1F
            actual = 12.5 + 0.5 * code if high_res else min(6.0 + 0.2 * code, 12.0)
            return f"{actual:.2f}V"
        elif register.address == 0x1C:
            from . import profiles as _profiles
            code = value & 0x1F
            _profiles._nvp_vcom_min["value"] = -3.6 if code <= 0x02 else -3.6 + 0.15 * (code - 2)
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
        elif register.address == 0x0E:
            pavdd = current_values.get("reg_05")
            if pavdd is not None:
                return f"{4.0 + 0.05 * (pavdd & 0x3F) - 0.02 * (value & 0x3F):.2f}V"
        elif register.address == 0x0F:
            navdd = current_values.get("reg_06")
            if navdd is not None:
                return f"{-4.0 - 0.1 * (navdd & 0x1F) + 0.02 * (value & 0x3F):.2f}V"

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
    if profile.key == "nvp2515":
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
