from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass(frozen=True)
class RegisterDefinition:
    key: str
    name: str
    address: int
    min_value: int = 0x00
    max_value: int = 0xFF
    step: int = 1
    supports_slider: bool = True
    description: str = ""
    default_value: int | None = None
    writable: bool = True
    display_formatter: Callable[[int], str] | None = None
    bit_options: tuple["BitOptionDefinition", ...] = ()


@dataclass(frozen=True)
class BitOptionDefinition:
    key: str
    label: str
    bit_mask: int
    enabled_value: int
    disabled_value: int
    enabled_label: str = "Enable"
    disabled_label: str = "Disable"


@dataclass(frozen=True)
class VcomDefinition:
    name: str = "VCOM"
    device_addr: int = 0x4F
    read_device_addr: int | None = None
    register_addr: int = 0x00
    min_value: int = 0x00
    max_value: int = 0x7F
    step: int = 1
    raw_shift: int = 1
    dac_flag: int = 0x01
    mtp_flag: int = 0x00
    use_special_accessor: bool = True
    no_register_access: bool = False
    display_formatter: Callable[[int], str] | None = None


@dataclass(frozen=True)
class MtpAction:
    name: str
    callback: Callable[["HardwareProxy", "PmicProfile", Callable[[str], None]], str]


@dataclass(frozen=True)
class PmicProfile:
    key: str
    name: str
    slave_addr: int
    registers: tuple[RegisterDefinition, ...]
    vcom: VcomDefinition
    unlock_before_access: bool = False
    unlock_register: int | None = None
    unlock_data: tuple[int, ...] = ()
    mtp_action: MtpAction | None = None
    mtp_read_mode: str = "fallback_to_dac"


@dataclass(frozen=True)
class TconProfile:
    key: str
    name: str
    adapter_class_name: str
    needs_ready_sequence: bool = False
    ready_sequence: tuple["InitStep", ...] = ()
    include_tcon_en: bool = False


@dataclass(frozen=True)
class InitStep:
    name: str
    callback: Callable[["HardwareProxy", Callable[[str], None]], None]


class HardwareProxy:
    """Protocol-like helper for step callbacks."""

    def write_dpcd(self, addr: int, data: bytes) -> None:
        raise NotImplementedError

    def iic_write(self, addr: int, offset: int, data: bytes) -> None:
        raise NotImplementedError


def reg(
    address: int,
    name: str,
    *,
    max_value: int = 0xFF,
    min_value: int = 0x00,
    default_value: int | None = None,
    description: str = "",
    writable: bool = True,
    supports_slider: bool = True,
    display_formatter: Callable[[int], str] | None = None,
    bit_options: tuple[BitOptionDefinition, ...] = (),
) -> RegisterDefinition:
    return RegisterDefinition(
        key=f"reg_{address:02X}",
        name=name,
        address=address,
        min_value=min_value,
        max_value=max_value,
        default_value=default_value,
        description=description or name,
        writable=writable,
        supports_slider=supports_slider,
        display_formatter=display_formatter,
        bit_options=bit_options,
    )


def bitopt(
    key: str,
    label: str,
    bit_mask: int,
    *,
    enabled_value: int | None = None,
    disabled_value: int = 0x00,
    enabled_label: str = "Enable",
    disabled_label: str = "Disable",
) -> BitOptionDefinition:
    return BitOptionDefinition(
        key=key,
        label=label,
        bit_mask=bit_mask,
        enabled_value=bit_mask if enabled_value is None else enabled_value,
        disabled_value=disabled_value,
        enabled_label=enabled_label,
        disabled_label=disabled_label,
    )


def fmt_volt_linear(base: float, step: float, unit: str = "V", precision: int = 2) -> Callable[[int], str]:
    def _formatter(value: int) -> str:
        actual = base + step * value
        return f"{actual:.{precision}f}{unit}"
    return _formatter


def fmt_negative_linear(base: float, step: float, unit: str = "V", precision: int = 2) -> Callable[[int], str]:
    def _formatter(value: int) -> str:
        actual = base - step * value
        return f"{actual:.{precision}f}{unit}"
    return _formatter


def fmt_vcom_min_nvp(value: int) -> str:
    if value <= 0x02:
        actual = -3.6
    else:
        actual = -3.6 + 0.15 * (value - 2)
    return f"{actual:.2f}V"


def fmt_vcom_min_standard(value: int) -> str:
    code = value & 0x1F
    if code <= 0x02:
        actual = -3.6
    else:
        actual = -3.6 + 0.15 * (code - 2)
    return f"{actual:.2f}V"


def fmt_nt_vgh(value: int) -> str:
    code = value & 0x1F
    high_res = bool(value & 0x20)
    vgh_30 = bool(value & 0x04)
    if high_res:
        actual = min(12.5 + 0.5 * code, 28.0)
        if vgh_30 and actual >= 28.0:
            actual = 30.0
    else:
        actual = min(6.0 + 0.2 * code, 12.0)
    return f"{actual:.2f}V"


def fmt_nvp_vgh_from_mode_supplier(mode_getter: Callable[[], bool]) -> Callable[[int], str]:
    def _formatter(value: int) -> str:
        if mode_getter():
            actual = 12.5 + 0.5 * min(value, 0x1F)
        else:
            actual = min(6.0 + 0.2 * min(value, 0x1F), 12.0)
        return f"{actual:.2f}V"
    return _formatter


def fmt_rt_vgh_from_mode_supplier(mode_getter: Callable[[], bool]) -> Callable[[int], str]:
    def _formatter(value: int) -> str:
        code = min(value & 0x1F, 0x1F)
        if mode_getter():
            actual = 10.0 + 1.0 * code
        else:
            actual = 6.0 + 0.2 * code
        return f"{actual:.2f}V"
    return _formatter


def fmt_vcom_offset_from_min(min_getter: Callable[[], float], step: float = 0.01) -> Callable[[int], str]:
    def _formatter(value: int) -> str:
        actual = min_getter() + step * value
        return f"{actual:.2f}V"
    return _formatter


_nvp_high_res = {"enabled": False}
_rt_high_res = {"enabled": True}
_nvp_vcom_min = {"value": -0.6}
_nt_vcom_min = {"value": -0.6}


def _get_nvp_high_res() -> bool:
    return _nvp_high_res["enabled"]


def _get_rt_high_res() -> bool:
    return _rt_high_res["enabled"]


def _get_nvp_vcom_min() -> float:
    return _nvp_vcom_min["value"]


def _get_nt_vcom_min() -> float:
    return _nt_vcom_min["value"]


def _mtp_standard_commit(hw: HardwareProxy, profile: PmicProfile, logger: Callable[[str], None]) -> str:
    hw.iic_write(profile.slave_addr >> 1, 0xFF, b"\x80")
    logger(f"MTP commit: slave=0x{profile.slave_addr:02X} reg=0xFF data=0x80")
    return "MTP commit finished via 0xFF <- 0x80"


def _step_nova_enable_aux(hw: HardwareProxy, logger: Callable[[str], None]) -> None:
    hw.write_dpcd(0x00102, b"\xC0")
    logger("NOVA AUX enabled: DPCD[0x00102] <- 0xC0")
    hw.iic_write(0x60, 0x02, b"\x04\x00")
    logger("NOVA IIC-over-AUX enabled via 0x60/0x02 <- 0x04 0x00")


def _step_nova_iic_en(hw: HardwareProxy, logger: Callable[[str], None]) -> None:
    hw.write_dpcd(0x00102, b"\xC0")
    hw.write_dpcd(0x004C1, b"\x14")
    hw.iic_write(0x60, 0x02, b"\x01")
    hw.iic_write(0x61, 0x02, b"\x04")
    hw.iic_write(0x61, 0x02, b"\x04\x00")
    hw.iic_write(0x61, 0x45, b"\x44\x05")
    hw.iic_write(0x61, 0x38, b"\x9E\x10")
    hw.write_dpcd(0x00102, b"\x00")
    logger("NOVA IIC_EN startup sequence applied")


PMIC_PROFILES: dict[str, PmicProfile] = {
    "nvp2515": PmicProfile(
        key="nvp2515",
        name="NVP2515",
        slave_addr=0x46,
        registers=(
            reg(
                0x00,
                "Channel Setting 0",
                default_value=0xFF,
                description="Enable bits for VCOM/LDO/VCORE/VIO/VGL/VGH/AVEE/AVDD",
                supports_slider=False,
                bit_options=(
                    bitopt("en_vcom", "VCOM", 0x80),
                    bitopt("en_ldo", "LDO", 0x40),
                    bitopt("en_vcore", "VCORE", 0x20),
                    bitopt("en_vio", "VIO", 0x10),
                    bitopt("en_vgl", "VGL", 0x08),
                    bitopt("en_vgh", "VGH", 0x04),
                    bitopt("en_avee", "AVEE", 0x02),
                    bitopt("en_avdd", "AVDD", 0x01),
                ),
            ),
            reg(
                0x02,
                "Channel Discharge Setting",
                default_value=0x00,
                description="Discharge enable bits for all outputs",
                supports_slider=False,
                bit_options=(
                    bitopt("dis_vcom", "VCOM Discharge", 0x80),
                    bitopt("dis_ldo", "LDO Discharge", 0x40),
                    bitopt("dis_vio", "VIO Discharge", 0x20),
                    bitopt("dis_vcore", "VCORE Discharge", 0x10),
                    bitopt("dis_vgl", "VGL Discharge", 0x08),
                    bitopt("dis_vgh", "VGH Discharge", 0x04),
                    bitopt("dis_avee", "AVEE Discharge", 0x02),
                    bitopt("dis_avdd", "AVDD Discharge", 0x01),
                ),
            ),
            reg(0x03, "AVDD Voltage", max_value=0x3F, default_value=0x14, description="4.0V to 7.0V, 0.05V/step", display_formatter=fmt_volt_linear(4.0, 0.05)),
            reg(0x04, "AVEE Voltage", max_value=0x1F, default_value=0x05, description="-4.0V to -7.0V, 0.1V/step", display_formatter=fmt_negative_linear(-4.0, 0.1)),
            reg(
                0x01,
                "Channel Setting 1",
                default_value=0x07,
                description="VGH resolution, PWM and GMA/RESET/CTRL enable",
                supports_slider=False,
                bit_options=(
                    bitopt("vgh_high_resolution", "VGH High Resolution", 0x80),
                    bitopt("en_vio_pwm", "VIO PWM", 0x40),
                    bitopt("en_vcore_pwm", "VCORE PWM", 0x20),
                    bitopt("pre_avdd", "PRE_AVDD", 0x10),
                    bitopt("en_ctrl", "CTRL", 0x08),
                    bitopt("en_reset", "RESET", 0x04),
                    bitopt("en_gma2", "GMA2", 0x02),
                    bitopt("en_gma1", "GMA1", 0x01),
                ),
            ),
            reg(0x05, "VGH Voltage", max_value=0x1F, default_value=0x0B, description="6V-12V or 12.5V-28V depending on high-resolution bit", display_formatter=fmt_nvp_vgh_from_mode_supplier(_get_nvp_high_res)),
            reg(0x06, "VGL Voltage", max_value=0x3F, default_value=0x08, description="-5.4V to -18V, 0.2V/step", display_formatter=fmt_negative_linear(-5.4, 0.2)),
            reg(0x07, "VCORE Voltage", max_value=0x3F, default_value=0x14, description="0.8V to 2.06V, 0.02V/step", display_formatter=fmt_volt_linear(0.8, 0.02)),
            reg(0x08, "VIO Voltage", max_value=0x1F, default_value=0x10, description="1.0V to 2.55V, 0.05V/step", display_formatter=fmt_volt_linear(1.0, 0.05)),
            reg(0x09, "LDO Voltage", max_value=0x0F, default_value=0x08, description="1.7V to 2.8V, 0.1V/step", display_formatter=fmt_volt_linear(1.7, 0.1)),
            reg(0x0B, "RESET Voltage", max_value=0x07, default_value=0x02, description="2.0V to 2.7V, 0.1V/step", display_formatter=fmt_volt_linear(2.0, 0.1)),
            reg(0x0C, "GMA1 Voltage", max_value=0x3F, default_value=0x0A, description="AVDD to AVDD-1.26V, 0.02V/step"),
            reg(0x0D, "GMA2 Voltage", max_value=0x3F, default_value=0x0A, description="AVEE to AVEE+1.26V, 0.02V/step"),
            reg(0x0E, "AVDD Boost Config", default_value=0x61, description="AVDD current limit, slew rate and switching frequency"),
            reg(0x0F, "AVDD Delay/Soft-Start", default_value=0x12, description="AVDD delay and soft-start"),
            reg(0x10, "AVEE Config", default_value=0x92, description="AVEE frequency, delay and soft-start"),
            reg(0x11, "VGH/VGL SIBO Config", default_value=0x12, description="LXH/LXN slew rate and frequency"),
            reg(0x12, "VGH Delay/Soft-Start", default_value=0x05, description="VGH delay and soft-start"),
            reg(0x13, "VGL Delay/Soft-Start", default_value=0x03, description="VGL delay and soft-start"),
            reg(0x14, "VCORE Buck Config", default_value=0x15, description="LXB1 frequency and slew rate"),
            reg(0x15, "VCORE Delay/Soft-Start", default_value=0x11, description="VCORE delay and soft-start"),
            reg(0x16, "VIO Buck Config", default_value=0x15, description="LXB2 frequency and slew rate"),
            reg(0x17, "VIO Delay/Soft-Start", default_value=0x12, description="VIO power-off select, delay and soft-start"),
            reg(0x18, "RESET Delay", max_value=0x0F, default_value=0x03, description="RESET delay 0ms to 75ms, 5ms/step"),
            reg(0x19, "LDO Delay", max_value=0x0F, default_value=0x02, description="LDO delay 0ms to 45ms, 5ms/step"),
            reg(
                0x1A,
                "VCOM Config",
                max_value=0x3F,
                default_value=0x05,
                description="VCOM delay and power-off selection",
                bit_options=(
                    bitopt("vcom_poweroff_reset", "VCOM Power-Off", 0x20, enabled_label="Follow RESET", disabled_label="Follow UVLO"),
                ),
            ),
            reg(0x1B, "AVEE Advanced Config", default_value=0x48, description="LXI slew rate and current limit"),
            reg(0x1C, "VCOM_MIN", max_value=0x1F, default_value=0x16, description="VCOM minimum voltage -3.6V to 0.75V, 0.15V/step", display_formatter=fmt_vcom_min_nvp),
            reg(0xFE, "WED_VCOM", max_value=0x01, default_value=0x00, description="Write VCOM DAC to EEPROM command", writable=False),
            reg(0xFF, "Control Register", max_value=0xFF, default_value=0x00, description="00 read DAC, 01 read EEPROM, 80 write all DAC into EEPROM", writable=False),
        ),
        vcom=VcomDefinition(display_formatter=fmt_vcom_offset_from_min(_get_nvp_vcom_min)),
        mtp_action=MtpAction(name="Standard MTP Commit", callback=_mtp_standard_commit),
    ),
    "nt50805": PmicProfile(
        key="nt50805",
        name="NT50805",
        slave_addr=0x47,
        registers=(
            reg(
                0x00,
                "Channel Setting 0",
                default_value=0xFF,
                description="Enable bits for VCOM/LDO/VCORE/VIO/VGL/VGH/AVEE/AVDD",
                supports_slider=False,
                bit_options=(
                    bitopt("en_vcom", "VCOM", 0x80),
                    bitopt("en_ldo", "LDO", 0x40),
                    bitopt("en_vcore", "VCORE", 0x20),
                    bitopt("en_vio", "VIO", 0x10),
                    bitopt("en_vgl", "VGL", 0x08),
                    bitopt("en_vgh", "VGH", 0x04),
                    bitopt("en_avee", "AVEE", 0x02),
                    bitopt("en_avdd", "AVDD", 0x01),
                ),
            ),
            reg(
                0x01,
                "Channel Setting 1",
                default_value=0x07,
                description="VGH resolution, PWM and GMA/RESET enable",
                supports_slider=False,
                bit_options=(
                    bitopt("vgh_high_resolution", "VGH High Resolution", 0x80),
                    bitopt("en_vio_pwm", "VIO PWM", 0x40),
                    bitopt("en_vcore_pwm", "VCORE PWM", 0x20),
                    bitopt("pre_avdd", "PRE_AVDD", 0x10),
                    bitopt("en_reset", "RESET", 0x04),
                    bitopt("en_gma2", "GMA2", 0x02),
                    bitopt("en_gma1", "GMA1", 0x01),
                ),
            ),
            reg(
                0x02,
                "Channel Discharge Setting",
                default_value=0x00,
                description="Discharge enable bits for all outputs",
                supports_slider=False,
                bit_options=(
                    bitopt("dis_vcom", "VCOM Discharge", 0x80),
                    bitopt("dis_ldo", "LDO Discharge", 0x40),
                    bitopt("dis_vio", "VIO Discharge", 0x20),
                    bitopt("dis_vcore", "VCORE Discharge", 0x10),
                    bitopt("dis_vgl", "VGL Discharge", 0x08),
                    bitopt("dis_vgh", "VGH Discharge", 0x04),
                    bitopt("dis_avee", "AVEE Discharge", 0x02),
                    bitopt("dis_avdd", "AVDD Discharge", 0x01),
                ),
            ),
            reg(0x03, "AVDD Voltage", max_value=0x7F, default_value=0x14, description="A0 plus AVDD[5:0], AVDD range 4.0V to 7.0V"),
            reg(0x04, "AVEE Voltage", max_value=0xFF, default_value=0x05, description="PIN26 select plus AVEE[4:0], AVEE range -4.0V to -7.0V"),
            reg(0x05, "VGH Voltage", max_value=0xFF, default_value=0x0B, description="RESET controls, VGH_30 and VGH[4:0]"),
            reg(0x06, "VGL Voltage", max_value=0xFF, default_value=0x08, description="VIN_DISC and VGL[5:0], VGL range -5.4V to -18V"),
            reg(0x07, "VCORE Voltage", max_value=0xFF, default_value=0x14, description="Inductor selection plus VCORE[5:0]"),
            reg(0x08, "VIO Voltage", max_value=0xFF, default_value=0x10, description="Inductor selection, UBRR LDO enable and VIO[4:0]"),
            reg(0x09, "LDO Voltage", max_value=0xFF, default_value=0x08, description="UBRR routing plus LDO[3:0]"),
            reg(0x0B, "VDET Voltage", max_value=0x7F, default_value=0x02, description="EN_RESET2, VDET2[6:4] and VDET[2:0]"),
            reg(0x0C, "GMA1 Voltage", max_value=0x3F, default_value=0x0A, description="GMA1 range AVDD to AVDD-1.26V"),
            reg(0x0D, "GMA2 Voltage", max_value=0x3F, default_value=0x0A, description="GMA2 range AVEE to AVEE+1.26V"),
            reg(0x0E, "AVDD Boost Config", default_value=0x61, description="LXA current limit, slew rate and frequency"),
            reg(0x0F, "AVDD Delay/Soft-Start", default_value=0x12, description="AVDD delay and soft-start"),
            reg(0x10, "AVEE Delay/Soft-Start", default_value=0x92, description="AVEE delay and soft-start"),
            reg(0x11, "VGH/VGL SIBO Config", default_value=0x12, description="SIBO_ENH, LXN/LXH slew rate and frequency"),
            reg(0x12, "VGH Delay/Soft-Start", default_value=0x05, description="SIBO_FREQH, VGH delay and soft-start"),
            reg(0x13, "VGL Delay/Soft-Start", default_value=0x03, description="VGL delay and soft-start"),
            reg(0x14, "VCORE Buck Config", default_value=0x15, description="LXB1 slew rate and frequency"),
            reg(0x15, "VCORE Delay/Soft-Start", default_value=0x11, description="VCORE delay and soft-start"),
            reg(0x16, "VIO Buck Config", default_value=0x15, description="LXB2 slew rate and frequency"),
            reg(0x17, "VIO Delay/Soft-Start", default_value=0x12, description="VIO delay and soft-start"),
            reg(0x18, "RESET Delay", default_value=0x03, description="RESET2_DLY[7:4] and RESET_DLY[3:0]"),
            reg(0x19, "LDO Delay", max_value=0x0F, default_value=0x02, description="LDO delay"),
            reg(
                0x1A,
                "VCOM Config",
                default_value=0x05,
                description="Fast discharge, power-off selection and VCOM delay",
                bit_options=(
                    bitopt("vcom_fast_disc", "VCOM Fast Discharge", 0x80),
                    bitopt("vcom_poweroff_reset", "VCOM Power-Off", 0x40, enabled_label="Follow RESET", disabled_label="Follow UVLO"),
                ),
            ),
            reg(0x1B, "AVEE Advanced Config", default_value=0x48, description="LXI slew rate, LXI current limit and version code"),
            reg(0x1C, "VCOM_MIN", default_value=0x16, description="VCORE_FREQ_H plus VCOM minimum voltage", display_formatter=fmt_vcom_min_standard),
            reg(0x1D, "Boost PWM Control", max_value=0x03, default_value=0x00, description="AVEE PWM and AVDD PWM"),
            reg(0x20, "WP", default_value=0x00, description="Write protect"),
            reg(0x21, "UBRR WP", default_value=0x00, description="UBRR write protect"),
            reg(0x22, "UBRR", default_value=0x00, description="UBRR register"),
            reg(0xFE, "WED_VCOM", max_value=0x01, default_value=0x00, description="Write VCOM command", writable=False),
            reg(0xFF, "Control Register", max_value=0xFF, default_value=0x00, description="00 read DAC, 01 read EEPROM, 80 write all DAC into EEPROM", writable=False),
        ),
        vcom=VcomDefinition(display_formatter=fmt_vcom_offset_from_min(_get_nt_vcom_min)),
        mtp_action=MtpAction(name="Standard MTP Commit", callback=_mtp_standard_commit),
    ),
    "rt6755": PmicProfile(
        key="rt6755",
        name="RT6755",
        slave_addr=0x46,
        registers=(
            reg(0x00, "Unlock Code 1", default_value=0x00, description="Unlock code 1, write 0x65 before access", writable=False),
            reg(0x01, "Unlock Code 2", default_value=0x00, description="Unlock code 2, write 0x9A before access", writable=False),
            reg(
                0x02,
                "Channel ON/OFF Setting",
                default_value=0xFF,
                description="Enable bits for VCOM/LDO/VIO/VCORE/VGL/VGH/NAVDD/PAVDD",
                supports_slider=False,
                bit_options=(
                    bitopt("en_vcom", "VCOM", 0x80),
                    bitopt("en_ldo", "LDO", 0x40),
                    bitopt("en_vio", "VIO", 0x20),
                    bitopt("en_vcore", "VCORE", 0x10),
                    bitopt("en_vgl", "VGL", 0x08),
                    bitopt("en_vgh", "VGH", 0x04),
                    bitopt("en_navdd", "NAVDD", 0x02),
                    bitopt("en_pavdd", "PAVDD", 0x01),
                ),
            ),
            reg(
                0x03,
                "Channel Mode Setting",
                default_value=0x07,
                description="VGH high resolution, FCCM, EXT_EN, RESET and GMA enables",
                supports_slider=False,
                bit_options=(
                    bitopt("vgh_high_resolution", "VGH High Resolution", 0x80, enabled_value=0x00, disabled_value=0x80),
                    bitopt("en_vio_fccm", "VIO FCCM", 0x40),
                    bitopt("en_vcore_fccm", "VCORE FCCM", 0x20),
                    bitopt("en_reset", "RESET", 0x04),
                    bitopt("en_gma2", "GMA2", 0x02),
                    bitopt("en_gma1", "GMA1", 0x01),
                ),
            ),
            reg(
                0x04,
                "Channel Discharge Setting",
                default_value=0x00,
                description="Discharge enable bits for all channels",
                supports_slider=False,
                bit_options=(
                    bitopt("dis_vcom", "VCOM Discharge", 0x80),
                    bitopt("dis_ldo", "LDO Discharge", 0x40),
                    bitopt("dis_vio", "VIO Discharge", 0x20),
                    bitopt("dis_vcore", "VCORE Discharge", 0x10),
                    bitopt("dis_vgl", "VGL Discharge", 0x08),
                    bitopt("dis_vgh", "VGH Discharge", 0x04),
                    bitopt("dis_navdd", "NAVDD Discharge", 0x02),
                    bitopt("dis_pavdd", "PAVDD Discharge", 0x01),
                ),
            ),
            reg(0x05, "PAVDD Voltage", max_value=0x3F, default_value=0x28, description="4.0V to 7.0V, 0.05V/step", display_formatter=fmt_volt_linear(4.0, 0.05)),
            reg(0x06, "NAVDD Voltage", max_value=0x1F, default_value=0x14, description="-4.0V to -7.0V, 0.1V/step", display_formatter=fmt_negative_linear(-4.0, 0.1)),
            reg(0x07, "VGH Voltage", max_value=0x1F, default_value=0x08, description="6V-10V or 10V-30V depending on high-resolution bit", display_formatter=fmt_rt_vgh_from_mode_supplier(_get_rt_high_res)),
            reg(0x08, "VGL Voltage", max_value=0x3F, default_value=0x2B, description="-5.4V to -18V, 0.2V/step", display_formatter=fmt_negative_linear(-5.4, 0.2)),
            reg(0x09, "VCORE Voltage", max_value=0x3F, default_value=0x0F, description="0.8V to 2.0V, 0.02V/step", display_formatter=fmt_volt_linear(0.8, 0.02)),
            reg(0x0A, "VIO Voltage", max_value=0x1F, default_value=0x10, description="1.0V to 2.5V, 0.05V/step", display_formatter=fmt_volt_linear(1.0, 0.05)),
            reg(0x0B, "LDO Voltage/Delay", default_value=0x27, description="LDO delay and LDO[3:0] voltage"),
            reg(0x0D, "VCOM Delay/RESET Voltage", default_value=0x2A, description="VCOM delay and RESET threshold"),
            reg(0x0E, "GMA1 Voltage", max_value=0x3F, default_value=0x0A, description="PAVDD to PAVDD-1.26V, 0.02V/step"),
            reg(0x0F, "GMA2 Voltage", max_value=0x3F, default_value=0x0A, description="NAVDD to NAVDD+1.26V, 0.02V/step"),
            reg(0x10, "PAVDD Config", default_value=0x2D, description="FCCM, current limit, slew rate and frequency"),
            reg(0x11, "PAVDD Delay/Soft-Start", default_value=0x12, description="PAVDD delay and soft-start"),
            reg(0x12, "NAVDD Delay/Soft-Start", default_value=0x12, description="NAVDD delay and soft-start"),
            reg(0x13, "NAVDD Config", default_value=0x0D, description="FCCM, current limit, slew rate and frequency"),
            reg(0x14, "VGH/VGL Config", default_value=0x15, description="LXH/N slew rate, VINP source and frequency"),
            reg(0x15, "VGH Delay/Soft-Start", default_value=0x05, description="VGH delay and soft-start"),
            reg(0x16, "VGL Delay/Soft-Start", default_value=0x03, description="VGL delay and soft-start"),
            reg(0x17, "VCORE Config", default_value=0x0D, description="LXB1 slew rate and frequency"),
            reg(0x18, "VCORE Delay/Soft-Start", default_value=0x11, description="VCORE delay and soft-start"),
            reg(0x19, "VIO Config", default_value=0x0D, description="LXB2 slew rate and frequency"),
            reg(0x1A, "VIO Delay/Soft-Start", default_value=0x12, description="VIO delay and soft-start"),
            reg(
                0x1B,
                "VCOM/RESET Config",
                default_value=0x83,
                description="VCOM pull-down, power-off select, RESET off control and RESET delay",
                bit_options=(
                    bitopt("vcom_pull_down", "VCOM Pull-Down", 0x80),
                    bitopt("vcom_poweroff_reset", "VCOM Power-Off", 0x40, enabled_label="Follow RESET", disabled_label="Follow UVLO"),
                ),
            ),
            reg(0xFF, "Control Register", max_value=0xFF, default_value=0x00, description="00 read DAC, 01 read EEPROM, 80 write all DAC into EEPROM", writable=False),
        ),
        vcom=VcomDefinition(
            device_addr=0x4F,
            min_value=0x00,
            max_value=0x7F,
            raw_shift=1,
            dac_flag=0x01,
            mtp_flag=0x00,
            use_special_accessor=True,
            no_register_access=False,
            display_formatter=None,
        ),
        unlock_before_access=True,
        unlock_register=0x00,
        unlock_data=(0x65, 0x9A),
        mtp_action=MtpAction(name="Standard MTP Commit", callback=_mtp_standard_commit),
    ),
}


TCON_PROFILES: dict[str, TconProfile] = {
    "anx": TconProfile(
        key="anx",
        name="ANX",
        adapter_class_name="ANX_ANX2176",
    ),
    "nova": TconProfile(
        key="nova",
        name="NOVA",
        adapter_class_name="Nova_NT71877",
        needs_ready_sequence=True,
        ready_sequence=(
            InitStep(name="Enable AUX", callback=_step_nova_enable_aux),
        ),
    ),
    "parade": TconProfile(
        key="parade",
        name="Parade",
        adapter_class_name="Parade_TC3410",
    ),
}


GPU_CARD_IDS: dict[str, int] = {
    "intel_edp": 0,
    "intel_dp": 1,
    "amd_edp": 2,
    "nvidia": 3,
    "qualcomm": 5,
    "amd_dp": 6,
}
