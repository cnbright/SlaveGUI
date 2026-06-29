from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from .profiles import GPU_CARD_IDS, NOVA_IIC_EN_SCHEMES, PMIC_PROFILES, TCON_PROFILES, RegisterDefinition, BitOptionDefinition
from .service import AuxGuiError, SessionConfig, connect, default_i2c_paths, enumerate_gpu_aux_port_choices, parse_hex_input, format_register_display, format_vcom_display


ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


@dataclass
class RegisterRowState:
    definition: RegisterDefinition
    frame: ctk.CTkFrame
    raw_value_var: tk.StringVar
    editor_var: tk.StringVar | None
    slider_var: tk.IntVar
    value_label: ctk.CTkLabel
    slider: ctk.CTkSlider | None
    entry: ctk.CTkEntry | None
    bit_option_vars: dict[str, tk.StringVar]
    options_frame: ctk.CTkFrame | None = None
    toggle_button: ctk.CTkButton | None = None
    collapsed: bool = False
    numeric_mask: int = 0
    value_trace_id: str | None = None
    editor_trace_id: str | None = None


@dataclass
class RegisterPageState:
    pmic_key: str
    frame: ctk.CTkFrame
    rows: dict[str, RegisterRowState]
    current_values: dict[str, int]
    status_var: tk.StringVar
    loading_label: ctk.CTkLabel
    pending_registers: list[RegisterDefinition]
    build_after_id: str | None = None
    build_complete: bool = False


class PmicAuxGuiApp(ctk.CTk):
    REFRESH_DEBOUNCE_MS = 24
    DISPLAY_DEPENDENCIES = {
        "nvp2515": {
            "reg_01": ("reg_05",),
            "reg_03": ("reg_0C",),
            "reg_04": ("reg_0D",),
            "reg_1C": ("__vcom__",),
        },
        "b602": {
            "reg_01": ("reg_05",),
            "reg_03": ("reg_0C",),
            "reg_04": ("reg_0D",),
            "reg_1C": ("__vcom__",),
        },
        "nt50805": {
            "reg_01": ("reg_05",),
            "reg_03": ("reg_0C", "__vcom__"),
            "reg_04": ("reg_0D",),
            "reg_1C": ("__vcom__",),
        },
        "rt6755": {
            "reg_03": ("reg_07",),
            "reg_05": ("reg_0E",),
            "reg_06": ("reg_0F",),
            "reg_0C": ("__vcom__",),
        },
        "rtq6749": {
            "reg_04": ("__vcom__",),
        },
        "lx52042c": {
            "reg_01": ("reg_05",),
            "reg_03": ("reg_0D",),
            "reg_04": ("reg_0E",),
            "reg_1D": ("__vcom__",),
        },
    }

    def __init__(self, base_dir: Path):
        super().__init__()
        self.base_dir = base_dir
        self.default_icon_path = self.base_dir / "app_icon.ico"
        self.session = None
        self.register_rows: dict[str, RegisterRowState] = {}
        self.register_page_cache: dict[str, RegisterPageState] = {}
        self.active_register_page_key: str | None = None
        self._editor_sync_guard: set[str] = set()
        self._pending_row_refreshes: set[str] = set()
        self._pending_vcom_refresh = False
        self._refresh_after_id: str | None = None
        self.default_jtool_dll_path, self.default_jtool_path = default_i2c_paths(base_dir)
        self.title("PMIC AUX Debug GUI")
        self.geometry("1500x920")
        self.minsize(1280, 760)
        self._apply_window_icon()

        self.gpu_var = tk.StringVar(value="amd_dp")
        self.tcon_var = tk.StringVar(value="nova")
        self.pmic_var = tk.StringVar(value="nt50805")
        self.pmic_addr_var = tk.StringVar(value=f"{PMIC_PROFILES['nt50805'].slave_addr:02X}")
        self.nova_iic_en_var = tk.BooleanVar(value=False)
        self.nova_iic_en_scheme_var = tk.StringVar(value="IO1")
        self.display_port_var = tk.StringVar(value="Auto first display")
        self.display_port_choices = {}
        self.status_var = tk.StringVar(value="Disconnected")
        self.init_var = tk.StringVar(value="Not initialized")
        self.vcom_title_var = tk.StringVar(value="VCOM")
        self.vcom_var = tk.StringVar(value="0x00")
        self.vcom_slider_var = tk.IntVar(value=0)
        self.vcom_actual_var = tk.StringVar(value="--")
        self.vcom_coarse_var = tk.StringVar(value="--")
        self.vcom_lsb_var = tk.StringVar(value="--")

        self._build_layout()
        self.after(1, self._reload_register_rows)
        self.after(10, self._refresh_display_ports)
        self._sync_nova_toggle()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _apply_window_icon(self) -> None:
        if not self.default_icon_path.exists():
            return
        try:
            self.iconbitmap(str(self.default_icon_path))
        except Exception:
            pass

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)

        top = ctk.CTkFrame(self, corner_radius=14)
        top.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        for index in range(12):
            top.grid_columnconfigure(index, weight=1 if index in (1, 3, 5, 8) else 0)

        ctk.CTkLabel(top, text="GPU / CardID").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        self.gpu_menu = ctk.CTkOptionMenu(top, values=list(GPU_CARD_IDS.keys()), variable=self.gpu_var, command=lambda _v: self._on_gpu_change())
        self.gpu_menu.grid(row=0, column=1, padx=8, pady=8, sticky="ew")
        ctk.CTkLabel(top, text="TCON").grid(row=0, column=2, padx=8, pady=8, sticky="w")
        self.tcon_menu = ctk.CTkOptionMenu(top, values=[key for key in TCON_PROFILES if key != "i2c"], variable=self.tcon_var, command=lambda _v: self._reload_register_rows())
        self.tcon_menu.grid(row=0, column=3, padx=8, pady=8, sticky="ew")
        ctk.CTkLabel(top, text="PMIC").grid(row=0, column=4, padx=8, pady=8, sticky="w")
        ctk.CTkOptionMenu(top, values=list(PMIC_PROFILES.keys()), variable=self.pmic_var, command=self._on_pmic_change).grid(row=0, column=5, padx=8, pady=8, sticky="ew")
        ctk.CTkLabel(top, text="PMIC Addr").grid(row=0, column=6, padx=(8, 4), pady=8, sticky="w")
        pmic_addr_frame = ctk.CTkFrame(top, fg_color="transparent")
        pmic_addr_frame.grid(row=0, column=7, padx=(0, 8), pady=8, sticky="ew")
        pmic_addr_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(pmic_addr_frame, text="0x").grid(row=0, column=0, padx=(0, 4), sticky="w")
        ctk.CTkEntry(pmic_addr_frame, textvariable=self.pmic_addr_var, width=72).grid(row=0, column=1, sticky="ew")
        nova_iic_en_frame = ctk.CTkFrame(top, fg_color="transparent")
        nova_iic_en_frame.grid(row=0, column=8, padx=8, pady=8, sticky="ew")
        nova_iic_en_frame.grid_columnconfigure(1, weight=1)
        self.nova_iic_en_switch = ctk.CTkSwitch(nova_iic_en_frame, text="NOVA IIC_EN", variable=self.nova_iic_en_var, onvalue=True, offvalue=False)
        self.nova_iic_en_switch.grid(row=0, column=0, padx=(0, 8), sticky="w")
        self.nova_iic_en_scheme_menu = ctk.CTkOptionMenu(
            nova_iic_en_frame,
            values=[str(scheme["name"]) for scheme in NOVA_IIC_EN_SCHEMES.values()],
            variable=self.nova_iic_en_scheme_var,
            width=96,
        )
        self.nova_iic_en_scheme_menu.grid(row=0, column=1, sticky="e")
        ctk.CTkButton(top, text="Connect", command=self._connect).grid(row=0, column=9, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(top, text="Disconnect", command=self._disconnect).grid(row=0, column=10, padx=8, pady=8, sticky="ew")

        ctk.CTkLabel(top, text="Display").grid(row=1, column=0, padx=8, pady=(0, 8), sticky="w")
        self.display_port_menu = ctk.CTkOptionMenu(top, values=[self.display_port_var.get()], variable=self.display_port_var)
        self.display_port_menu.grid(row=1, column=1, columnspan=5, padx=8, pady=(0, 8), sticky="ew")
        ctk.CTkButton(top, text="Refresh", command=self._refresh_display_ports).grid(row=1, column=6, padx=8, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(top, text="Status").grid(row=1, column=7, padx=8, pady=(0, 8), sticky="w")
        ctk.CTkLabel(top, textvariable=self.status_var, anchor="w").grid(row=1, column=8, padx=8, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(top, text="Init").grid(row=1, column=9, padx=8, pady=(0, 8), sticky="w")
        ctk.CTkLabel(top, textvariable=self.init_var, anchor="w").grid(row=1, column=10, columnspan=2, padx=8, pady=(0, 8), sticky="ew")

        middle = ctk.CTkFrame(self, corner_radius=14)
        middle.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        middle.grid_rowconfigure(0, weight=1)
        middle.grid_columnconfigure(0, weight=7)
        middle.grid_columnconfigure(1, weight=3, minsize=380)

        self.register_panel = ctk.CTkScrollableFrame(middle, label_text="Registers")
        self.register_panel.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        self.register_panel.grid_columnconfigure(0, weight=1)

        right = ctk.CTkFrame(middle, corner_radius=12, width=400)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        right.grid_propagate(False)

        self.vcom_frame = ctk.CTkFrame(right)
        self.vcom_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        self.vcom_frame.grid_columnconfigure(1, weight=1)
        for index in range(2):
            self.vcom_frame.grid_columnconfigure(index, weight=1)
        ctk.CTkLabel(self.vcom_frame, textvariable=self.vcom_title_var, font=ctk.CTkFont(size=18, weight="bold")).grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        ctk.CTkLabel(self.vcom_frame, textvariable=self.vcom_actual_var, anchor="e").grid(row=0, column=1, padx=8, pady=(8, 2), sticky="e")
        ctk.CTkLabel(self.vcom_frame, text="Value").grid(row=1, column=0, padx=8, pady=(4, 6), sticky="w")
        ctk.CTkEntry(self.vcom_frame, textvariable=self.vcom_var).grid(row=1, column=1, padx=8, pady=(4, 6), sticky="ew")
        self.vcom_split_frame = ctk.CTkFrame(self.vcom_frame, fg_color="transparent")
        self.vcom_split_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 2))
        self.vcom_split_frame.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(self.vcom_split_frame, text="VCOM coarse").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(2, 0))
        ctk.CTkLabel(self.vcom_split_frame, textvariable=self.vcom_coarse_var, anchor="e").grid(row=0, column=1, sticky="e", pady=(2, 0))
        ctk.CTkLabel(self.vcom_split_frame, text="VCOM_LSB").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(2, 0))
        ctk.CTkLabel(self.vcom_split_frame, textvariable=self.vcom_lsb_var, anchor="e").grid(row=1, column=1, sticky="e", pady=(2, 0))
        self.vcom_slider = ctk.CTkSlider(self.vcom_frame, from_=0, to=255, number_of_steps=255, variable=self.vcom_slider_var, command=self._on_vcom_slider)
        self.vcom_slider.grid(row=3, column=0, columnspan=2, padx=8, pady=(2, 8), sticky="ew")
        actions = ctk.CTkFrame(self.vcom_frame, fg_color="transparent")
        actions.grid(row=4, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        for index in range(4):
            actions.grid_columnconfigure(index % 2, weight=1)
        ctk.CTkButton(actions, text="Read DAC", height=30, command=lambda: self._read_vcom("dac")).grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        ctk.CTkButton(actions, text="Write DAC", height=30, command=lambda: self._write_vcom("dac")).grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        ctk.CTkButton(actions, text="Read MTP", height=30, command=lambda: self._read_vcom("mtp")).grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        ctk.CTkButton(actions, text="Write MTP", height=30, command=lambda: self._write_vcom("mtp")).grid(row=1, column=1, padx=4, pady=4, sticky="ew")

        log_frame = ctk.CTkFrame(right)
        log_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        log_frame.grid_rowconfigure(1, weight=1)
        log_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(log_frame, text="Operation Log", font=ctk.CTkFont(size=16, weight="bold")).grid(row=0, column=0, sticky="nw", padx=8, pady=(8, 4))
        self.log_box = ctk.CTkTextbox(log_frame, wrap="word")
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))

        bottom = ctk.CTkFrame(self, corner_radius=14)
        bottom.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        for index in range(5):
            bottom.grid_columnconfigure(index, weight=1)
        ctk.CTkButton(bottom, text="Read All DAC", command=lambda: self._read_all_registers("dac")).grid(row=0, column=0, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(bottom, text="Write All DAC", command=lambda: self._write_all_registers("dac")).grid(row=0, column=1, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(bottom, text="Read All MTP", command=lambda: self._read_all_registers("mtp")).grid(row=0, column=2, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(bottom, text="Write All MTP", command=lambda: self._write_all_registers("mtp")).grid(row=0, column=3, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(bottom, text="Clear Log", command=self._clear_log).grid(row=0, column=4, padx=8, pady=8, sticky="ew")
        self.vcom_var.trace_add("write", lambda *_args: self._refresh_vcom_display())

    def _reload_register_rows(self) -> None:
        self._sync_i2c_mode()
        profile = PMIC_PROFILES[self.pmic_var.get()]
        if profile.supports_vcom:
            self.vcom_frame.grid()
            self.vcom_slider.configure(from_=profile.vcom.min_value, to=profile.vcom.max_value, number_of_steps=max(1, profile.vcom.max_value - profile.vcom.min_value))
        else:
            self.vcom_frame.grid_remove()
        self._sync_vcom_split_visibility(profile)
        self._sync_vcom_title(profile)
        self._show_register_page(self.pmic_var.get())
        self._refresh_vcom_display()

    def _on_pmic_change(self, pmic_key: str) -> None:
        self.pmic_var.set(pmic_key)
        self.pmic_addr_var.set(f"{PMIC_PROFILES[pmic_key].slave_addr:02X}")
        self._reload_register_rows()

    def _create_register_page(self, pmic_key: str) -> RegisterPageState:
        profile = PMIC_PROFILES[pmic_key]
        page_frame = ctk.CTkFrame(self.register_panel, fg_color="transparent")
        page_frame.grid_columnconfigure(0, weight=1)
        status_var = tk.StringVar(value=f"Loading {len(profile.registers)} registers...")
        loading_label = ctk.CTkLabel(page_frame, textvariable=status_var, anchor="w")
        loading_label.grid(row=0, column=0, padx=8, pady=8, sticky="ew")
        page = RegisterPageState(
            pmic_key=pmic_key,
            frame=page_frame,
            rows={},
            current_values={
                register.key: register.default_value if register.default_value is not None else register.min_value
                for register in profile.registers
            },
            status_var=status_var,
            loading_label=loading_label,
            pending_registers=list(profile.registers),
        )
        self.register_page_cache[pmic_key] = page
        self._schedule_row_build(page)
        return page

    def _show_register_page(self, pmic_key: str) -> None:
        if self.active_register_page_key == pmic_key:
            page = self.register_page_cache.get(pmic_key)
            if page is not None:
                self.register_rows = page.rows
            return
        self._cancel_pending_refreshes()
        if self.active_register_page_key is not None:
            current_page = self.register_page_cache.get(self.active_register_page_key)
            if current_page is not None:
                current_page.frame.grid_remove()
        page = self.register_page_cache.get(pmic_key)
        if page is None:
            page = self._create_register_page(pmic_key)
        page.frame.grid(row=0, column=0, padx=0, pady=0, sticky="nsew")
        self.active_register_page_key = pmic_key
        self.register_rows = page.rows
        self._refresh_current_register_ui()

    def _schedule_row_build(self, page: RegisterPageState) -> None:
        page.build_after_id = self.after(1, lambda: self._build_register_row_batch(page))

    def _build_register_row_batch(self, page: RegisterPageState) -> None:
        if page.pmic_key not in self.register_page_cache:
            return
        profile = PMIC_PROFILES[page.pmic_key]
        if not page.pending_registers:
            page.status_var.set(f"Loaded {len(page.rows)} registers")
            if page.loading_label.winfo_exists():
                page.loading_label.destroy()
            page.build_after_id = None
            page.build_complete = True
            if self.active_register_page_key == page.pmic_key:
                self.register_rows = page.rows
                self._refresh_vcom_display()
            return
        batch_size = 12
        start_index = len(page.rows) + 1
        for row_offset in range(batch_size):
            if not page.pending_registers:
                break
            register = page.pending_registers.pop(0)
            self._create_register_row(page, profile, register, start_index + row_offset)
        if page.pending_registers:
            page.status_var.set(f"Loading registers... {len(page.rows)} / {len(profile.registers)}")
            self._schedule_row_build(page)
        else:
            page.status_var.set(f"Loaded {len(page.rows)} registers")
            if page.loading_label.winfo_exists():
                page.loading_label.destroy()
            page.build_after_id = None
            page.build_complete = True
            if self.active_register_page_key == page.pmic_key:
                self.register_rows = page.rows
                self._refresh_vcom_display()

    def _create_register_row(self, page: RegisterPageState, profile, register: RegisterDefinition, grid_row: int) -> None:
        row = ctk.CTkFrame(page.frame)
        row.grid(row=grid_row, column=0, sticky="ew", padx=6, pady=4)
        for index in range(6):
            row.grid_columnconfigure(index, weight=1 if index in (2, 3) else 0)
        initial_value = register.default_value if register.default_value is not None else register.min_value
        raw_value_var = tk.StringVar(value=f"0x{initial_value:02X}")
        numeric_mask = self._numeric_mask(register)
        editor_var = tk.StringVar(value=self._format_numeric_value(initial_value & numeric_mask)) if numeric_mask else None
        slider_var = tk.IntVar(value=0)
        ctk.CTkLabel(row, text=register.name, width=140, anchor="w").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ctk.CTkLabel(row, text=f"0x{register.address:02X}", width=64).grid(row=0, column=1, padx=6, pady=6, sticky="w")
        entry: ctk.CTkEntry | None = None
        slider: ctk.CTkSlider | None = None
        if numeric_mask:
            entry = ctk.CTkEntry(row, textvariable=editor_var, width=100)
            entry.grid(row=0, column=2, padx=6, pady=6, sticky="ew")
            entry.bind("<Up>", lambda event, key=register.key: self._adjust_register_value_from_key(key, 1))
            entry.bind("<Down>", lambda event, key=register.key: self._adjust_register_value_from_key(key, -1))
            slider = ctk.CTkSlider(
                row,
                from_=0,
                to=numeric_mask,
                number_of_steps=max(1, numeric_mask),
                variable=slider_var,
                command=lambda value, key=register.key: self._on_register_slider(key, value),
            )
            slider.grid(row=0, column=3, padx=6, pady=6, sticky="ew")
            slider_var.set(initial_value & numeric_mask)
        display_text = format_register_display(profile, register.key, initial_value, {register.key: initial_value})
        if register.bit_options and not numeric_mask:
            display_text = ""
        current_label = ctk.CTkLabel(row, text=display_text, width=110, anchor="e")
        current_label.grid(row=0, column=4, padx=6, pady=6, sticky="e")
        bit_option_vars: dict[str, tk.StringVar] = {}
        options_frame: ctk.CTkFrame | None = None
        toggle_button: ctk.CTkButton | None = None
        collapsed = bool(register.bit_options)
        if register.bit_options:
            options_frame = ctk.CTkFrame(row, fg_color="transparent")
            options_frame.grid(row=1, column=0, columnspan=6, padx=6, pady=(0, 6), sticky="ew")
            option_columns = 2 if len(register.bit_options) <= 4 else 3
            for column in range(option_columns):
                options_frame.grid_columnconfigure(column, weight=1, uniform=f"{register.key}_opts")
            for index, opt in enumerate(register.bit_options):
                opt_row = index // option_columns
                opt_col = index % option_columns
                var = tk.StringVar(value=opt.enabled_label if self._bit_is_enabled(initial_value, opt) else opt.disabled_label)
                bit_option_vars[opt.key] = var
                option_frame = ctk.CTkFrame(options_frame, fg_color="transparent")
                option_frame.grid(row=opt_row, column=opt_col, padx=6, pady=4, sticky="ew")
                option_frame.grid_columnconfigure(0, weight=1)
                ctk.CTkLabel(option_frame, text=opt.label, anchor="w").grid(row=0, column=0, columnspan=2, sticky="w")
                ctk.CTkRadioButton(
                    option_frame,
                    text=opt.enabled_label,
                    variable=var,
                    value=opt.enabled_label,
                    command=lambda key=register.key, option=opt: self._apply_bit_option(key, option),
                ).grid(row=1, column=0, padx=(0, 8), pady=(2, 0), sticky="w")
                ctk.CTkRadioButton(
                    option_frame,
                    text=opt.disabled_label,
                    variable=var,
                    value=opt.disabled_label,
                    command=lambda key=register.key, option=opt: self._apply_bit_option(key, option),
                ).grid(row=1, column=1, padx=(0, 8), pady=(2, 0), sticky="w")
            if collapsed:
                options_frame.grid_remove()
            toggle_button = ctk.CTkButton(
                row,
                text="Expand" if collapsed else "Collapse",
                width=88,
                height=28,
                command=lambda key=register.key: self._toggle_register_options(key),
            )
            toggle_button.grid(row=0, column=5, padx=6, pady=6, sticky="e")
        if not register.writable:
            if entry is not None:
                entry.configure(state="disabled")
            if slider is not None:
                slider.configure(state="disabled")
        state = RegisterRowState(
            definition=register,
            frame=row,
            raw_value_var=raw_value_var,
            editor_var=editor_var,
            slider_var=slider_var,
            value_label=current_label,
            slider=slider,
            entry=entry,
            bit_option_vars=bit_option_vars,
            options_frame=options_frame,
            toggle_button=toggle_button,
            collapsed=collapsed,
            numeric_mask=numeric_mask,
        )
        state.value_trace_id = raw_value_var.trace_add("write", lambda *_args, key=register.key: self._on_register_value_change(key))
        if editor_var is not None:
            state.editor_trace_id = editor_var.trace_add("write", lambda *_args, key=register.key: self._on_editor_value_change(key))
        page.rows[register.key] = state
        if self.active_register_page_key == page.pmic_key:
            self.register_rows = page.rows
            self._refresh_row_display(register.key, page.current_values.copy())

    def _refresh_current_register_ui(self) -> None:
        if self.active_register_page_key is None:
            self.register_rows = {}
            self._refresh_vcom_display()
            return
        page = self.register_page_cache.get(self.active_register_page_key)
        if page is None:
            self.register_rows = {}
            self._refresh_vcom_display()
            return
        self.register_rows = page.rows
        if page.rows:
            self._refresh_all_row_displays(page.current_values.copy())
        else:
            self._refresh_vcom_display(page.current_values.copy())

    def log(self, message: str) -> None:
        self.log_box.insert("end", f"{message}\n")
        self.log_box.see("end")

    def _clear_log(self) -> None:
        self.log_box.delete("1.0", "end")

    def _on_gpu_change(self) -> None:
        self._sync_i2c_mode()
        self._refresh_display_ports()

    def _refresh_display_ports(self) -> None:
        gpu_key = self.gpu_var.get()
        if gpu_key == "i2c":
            label = "Direct I2C"
            self.display_port_choices = {}
            self.display_port_var.set(label)
            self.display_port_menu.configure(values=[label], state="disabled")
            return
        try:
            choices = enumerate_gpu_aux_port_choices(gpu_key)
        except Exception as exc:
            label = f"Enumeration failed: {exc}"
            self.display_port_choices = {}
            self.display_port_var.set(label)
            self.display_port_menu.configure(values=[label], state="disabled")
            self.log(label)
            return
        if not choices:
            label = "No matching display"
            self.display_port_choices = {}
            self.display_port_var.set(label)
            self.display_port_menu.configure(values=[label], state="disabled")
            self.log(f"No {gpu_key} display found")
            return
        previous = self.display_port_var.get()
        self.display_port_choices = {choice.label: choice for choice in choices}
        labels = list(self.display_port_choices)
        self.display_port_var.set(previous if previous in self.display_port_choices else labels[0])
        self.display_port_menu.configure(values=labels, state="normal")
        self.log(f"Found {len(labels)} display(s) for {gpu_key}")

    def _selected_display_indices(self) -> tuple[int, int]:
        if self.gpu_var.get() == "i2c":
            return 0, 0
        choice = self.display_port_choices.get(self.display_port_var.get())
        if choice is None:
            self._refresh_display_ports()
            choice = self.display_port_choices.get(self.display_port_var.get())
        if choice is None:
            raise AuxGuiError("Please select a valid display port")
        return choice.gpu_index, choice.port_index

    def _current_config(self) -> SessionConfig:
        pmic_addr = parse_hex_input(f"0x{self.pmic_addr_var.get().strip()}")
        if not 0x00 <= pmic_addr <= 0xFF:
            raise AuxGuiError("PMIC address must be in range 0x00 to 0xFF")
        gpu_index, port_index = self._selected_display_indices()
        return SessionConfig(
            gpu_key=self.gpu_var.get(),
            tcon_key="i2c" if self.gpu_var.get() == "i2c" else self.tcon_var.get(),
            pmic_key=self.pmic_var.get(),
            pmic_slave_addr=pmic_addr,
            gpu_index=gpu_index,
            port_index=port_index,
            jtool_dll_path=self.default_jtool_dll_path,
            jtool_module_path=self.default_jtool_path,
            nova_use_iic_en=bool(self.nova_iic_en_var.get()),
            nova_iic_en_scheme=self._nova_iic_en_scheme_key(),
        )

    def _sync_i2c_mode(self) -> None:
        if self.gpu_var.get() == "i2c":
            self.tcon_menu.configure(state="disabled")
            self.nova_iic_en_var.set(False)
            self.nova_iic_en_switch.configure(state="disabled")
            self.nova_iic_en_scheme_menu.configure(state="disabled")
            self.display_port_menu.configure(state="disabled")
        else:
            self.tcon_menu.configure(state="normal")
            self.display_port_menu.configure(state="normal")
            self._sync_nova_toggle()

    def _sync_nova_toggle(self) -> None:
        if self.gpu_var.get() == "i2c":
            return
        if self.tcon_var.get() == "nova":
            self.nova_iic_en_switch.configure(state="normal")
            self.nova_iic_en_scheme_menu.configure(state="normal")
        else:
            self.nova_iic_en_var.set(False)
            self.nova_iic_en_switch.configure(state="disabled")
            self.nova_iic_en_scheme_menu.configure(state="disabled")

    def _nova_iic_en_scheme_key(self) -> str:
        selected_name = self.nova_iic_en_scheme_var.get()
        for key, scheme in NOVA_IIC_EN_SCHEMES.items():
            if selected_name == scheme["name"]:
                return key
        return "io1"

    def _connect(self) -> None:
        self._disconnect()
        try:
            self.session = connect(self._current_config(), self.log)
            self.status_var.set("Connected")
            self.init_var.set(self.session.init_summary)
        except Exception as exc:
            self.status_var.set("Connect failed")
            self._show_error(exc)

    def _disconnect(self) -> None:
        if self.session is None:
            return
        self.session.close()
        self.session = None
        self.status_var.set("Disconnected")
        self.init_var.set("Not initialized")

    def _require_session(self):
        if self.session is None:
            raise AuxGuiError("Connect first")
        return self.session

    def _read_all_registers(self, target: str) -> None:
        try:
            session = self._require_session()
            results = session.read_all_registers(target=target, exclude_vcom=True)
            for reg_key, value in results.items():
                if isinstance(value, int) and reg_key in self.register_rows:
                    try:
                        self._update_row_value(reg_key, value)
                    except Exception as exc:
                        raise AuxGuiError(f"UI update failed at {reg_key}: {exc}") from exc
            try:
                self._refresh_all_row_displays()
            except Exception as exc:
                raise AuxGuiError(f"VCOM display refresh failed: {exc}") from exc
            self.log(f"Read all registers through {target.upper()} path")
            self.init_var.set(session.init_summary)
        except Exception as exc:
            self._show_error(exc)

    def _write_all_registers(self, target: str) -> None:
        try:
            session = self._require_session()
            values: dict[str, int] = {}
            for reg_key, row in self.register_rows.items():
                raw_value = parse_hex_input(row.raw_value_var.get())
                values[reg_key] = self._normalize_register_value(row.definition, raw_value)
            session.write_all_registers(values, target=target, exclude_vcom=True)
            for reg_key, value in values.items():
                self._update_row_value(reg_key, value)
            self._refresh_all_row_displays()
            self.log(f"Wrote all registers through {target.upper()} path")
            self.init_var.set(session.init_summary)
        except Exception as exc:
            self._show_error(exc)

    def _read_vcom(self, target: str) -> None:
        try:
            session = self._require_session()
            if not session.pmic_profile.supports_vcom:
                raise AuxGuiError(f"{session.pmic_profile.name} does not support VCOM")
            value = session.read_vcom(target=target, device_addr=self._vcom_device_addr_for_profile(session.pmic_profile))
            width = 3 if session.pmic_profile.key == "lx52042c" else 2
            self.vcom_var.set(f"0x{value:0{width}X}")
            self.vcom_slider_var.set(value)
            actual = format_vcom_display(session.pmic_profile, value, self._collect_current_row_values())
            self.vcom_actual_var.set(actual)
            self.log(f"VCOM actual: {actual}")
            self.init_var.set(session.init_summary)
        except Exception as exc:
            self._show_error(exc)

    def _write_vcom(self, target: str) -> None:
        try:
            session = self._require_session()
            if not session.pmic_profile.supports_vcom:
                raise AuxGuiError(f"{session.pmic_profile.name} does not support VCOM")
            value = parse_hex_input(self.vcom_var.get())
            session.write_vcom(value, target=target, device_addr=self._vcom_device_addr_for_profile(session.pmic_profile))
            self.vcom_slider_var.set(value)
            self._refresh_vcom_display()
            self.init_var.set(session.init_summary)
        except Exception as exc:
            self._show_error(exc)

    def _run_mtp_commit(self) -> None:
        try:
            session = self._require_session()
            session.run_mtp_commit()
            self.init_var.set(session.init_summary)
        except Exception as exc:
            self._show_error(exc)

    def _update_row_value(self, reg_key: str, value: int) -> None:
        row = self.register_rows[reg_key]
        normalized = self._normalize_register_value(row.definition, value)
        page = self._current_register_page()
        if page is not None:
            page.current_values[reg_key] = normalized
        row.raw_value_var.set(f"0x{normalized:02X}")

    def _on_register_slider(self, reg_key: str, value: float) -> None:
        self._set_row_numeric_value(reg_key, int(round(value)))

    def _adjust_register_value_from_key(self, reg_key: str, delta: int) -> str:
        row = self.register_rows[reg_key]
        current = self._current_numeric_value(row)
        if current is None:
            current = 0
        next_value = max(0, min(row.numeric_mask, current + delta))
        self._set_row_numeric_value(reg_key, next_value)
        return "break"

    def _on_register_value_change(self, reg_key: str) -> None:
        if reg_key not in self.register_rows:
            return
        self._schedule_impacted_refresh(reg_key)

    def _on_editor_value_change(self, reg_key: str) -> None:
        if reg_key in self._editor_sync_guard or reg_key not in self.register_rows:
            return
        row = self.register_rows[reg_key]
        if row.editor_var is None:
            return
        numeric_value = self._try_parse_row_value(row.editor_var.get())
        if numeric_value is None:
            row.value_label.configure(text="--")
            return
        self._set_row_numeric_value(reg_key, numeric_value)

    def _refresh_row_display(self, reg_key: str, current_values: dict[str, int] | None = None) -> None:
        row = self.register_rows[reg_key]
        value = self._try_parse_row_value(row.raw_value_var.get())
        if value is None:
            row.value_label.configure(text="--")
            return
        clamped_value = self._normalize_register_value(row.definition, value)
        numeric_value = self._numeric_value_from_raw(row.definition, clamped_value, row.numeric_mask)
        if row.slider is not None and numeric_value is not None and row.slider_var.get() != numeric_value:
            row.slider_var.set(numeric_value)
        if row.editor_var is not None and numeric_value is not None:
            editor_text = self._format_numeric_value(numeric_value)
            if row.editor_var.get() != editor_text:
                self._editor_sync_guard.add(reg_key)
                row.editor_var.set(editor_text)
                self._editor_sync_guard.discard(reg_key)
        if current_values is None:
            current_values = self._current_values_snapshot()
        current_values[reg_key] = clamped_value
        display_text = format_register_display(PMIC_PROFILES[self.pmic_var.get()], reg_key, clamped_value, current_values)
        if row.definition.bit_options and not row.numeric_mask:
            display_text = ""
        row.value_label.configure(text=display_text)
        for option in row.definition.bit_options:
            if option.key in row.bit_option_vars:
                row.bit_option_vars[option.key].set(option.enabled_label if self._bit_is_enabled(clamped_value, option) else option.disabled_label)
        if row.toggle_button is not None:
            row.toggle_button.configure(text="Expand" if row.collapsed else "Collapse")

    def _refresh_all_row_displays(self, current_values: dict[str, int] | None = None) -> None:
        self._cancel_pending_refreshes()
        snapshot = current_values or self._current_values_snapshot()
        for reg_key in self.register_rows:
            self._refresh_row_display(reg_key, snapshot)
        self._refresh_vcom_display(snapshot)

    def _collect_current_row_values(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for key, state in self.register_rows.items():
            value = self._try_parse_row_value(state.raw_value_var.get())
            if value is not None:
                values[key] = self._normalize_register_value(state.definition, value)
        return values

    def _current_register_page(self) -> RegisterPageState | None:
        if self.active_register_page_key is None:
            return None
        return self.register_page_cache.get(self.active_register_page_key)

    def _current_values_snapshot(self) -> dict[str, int]:
        page = self._current_register_page()
        if page is not None:
            return page.current_values.copy()
        return self._collect_current_row_values()

    def _impacted_targets(self, reg_key: str) -> tuple[set[str], bool]:
        impacted_rows = {reg_key}
        refresh_vcom = False
        profile_key = self.pmic_var.get()
        dependencies = self.DISPLAY_DEPENDENCIES.get(profile_key, {})
        for target in dependencies.get(reg_key, ()):
            if target == "__vcom__":
                refresh_vcom = True
            else:
                impacted_rows.add(target)
        return impacted_rows, refresh_vcom

    def _schedule_impacted_refresh(self, reg_key: str) -> None:
        impacted_rows, refresh_vcom = self._impacted_targets(reg_key)
        self._pending_row_refreshes.update(impacted_rows)
        self._pending_vcom_refresh = self._pending_vcom_refresh or refresh_vcom
        if self._refresh_after_id is not None:
            self.after_cancel(self._refresh_after_id)
        self._refresh_after_id = self.after(self.REFRESH_DEBOUNCE_MS, self._flush_scheduled_refreshes)

    def _flush_scheduled_refreshes(self) -> None:
        self._refresh_after_id = None
        if not self.register_rows:
            self._pending_row_refreshes.clear()
            self._pending_vcom_refresh = False
            return
        snapshot = self._current_values_snapshot()
        pending_rows = [reg_key for reg_key in self.register_rows if reg_key in self._pending_row_refreshes]
        refresh_vcom = self._pending_vcom_refresh
        self._pending_row_refreshes.clear()
        self._pending_vcom_refresh = False
        for reg_key in pending_rows:
            self._refresh_row_display(reg_key, snapshot)
        if refresh_vcom:
            self._refresh_vcom_display(snapshot)

    def _cancel_pending_refreshes(self) -> None:
        if self._refresh_after_id is not None:
            self.after_cancel(self._refresh_after_id)
            self._refresh_after_id = None
        self._pending_row_refreshes.clear()
        self._pending_vcom_refresh = False

    def _try_parse_row_value(self, text: str) -> int | None:
        try:
            return parse_hex_input(text)
        except Exception:
            return None

    def _numeric_mask(self, definition: RegisterDefinition) -> int:
        if not definition.supports_slider:
            return 0
        if definition.numeric_mask is not None:
            return definition.numeric_mask
        logic_mask = 0
        for option in definition.bit_options:
            logic_mask |= option.bit_mask
        numeric_mask = definition.max_value & ~logic_mask
        if not definition.bit_options:
            numeric_mask = definition.max_value
        if numeric_mask < 0:
            numeric_mask = 0
        return numeric_mask

    def _format_numeric_value(self, value: int) -> str:
        return f"0x{value:02X}"

    def _numeric_value_from_raw(self, definition: RegisterDefinition, value: int, numeric_mask: int) -> int | None:
        if not numeric_mask:
            return None
        if definition.value_mask is not None and definition.numeric_mask is None and not definition.bit_options:
            return value
        return value & numeric_mask

    def _current_numeric_value(self, row: RegisterRowState) -> int | None:
        if row.editor_var is None:
            return None
        return self._try_parse_row_value(row.editor_var.get())

    def _set_row_numeric_value(self, reg_key: str, numeric_value: int) -> None:
        row = self.register_rows[reg_key]
        if not row.numeric_mask:
            return
        numeric_value = max(0, min(row.numeric_mask, numeric_value))
        current_raw = self._try_parse_row_value(row.raw_value_var.get())
        if current_raw is None:
            current_raw = row.definition.default_value if row.definition.default_value is not None else row.definition.min_value
        current_raw = self._normalize_register_value(row.definition, current_raw)
        if row.definition.value_mask is not None and row.definition.numeric_mask is None and not row.definition.bit_options:
            next_raw = numeric_value
        else:
            next_raw = (current_raw & ~row.numeric_mask) | (numeric_value & row.numeric_mask)
        page = self._current_register_page()
        if page is not None:
            page.current_values[reg_key] = next_raw
        row.raw_value_var.set(f"0x{next_raw:02X}")

    def _bit_is_enabled(self, value: int, option: BitOptionDefinition) -> bool:
        return (value & option.bit_mask) == option.enabled_value

    def _apply_bit_option(self, reg_key: str, option: BitOptionDefinition) -> None:
        row = self.register_rows[reg_key]
        current = self._normalize_register_value(row.definition, parse_hex_input(row.raw_value_var.get()))
        enabled = row.bit_option_vars[option.key].get() == option.enabled_label
        current &= ~option.bit_mask
        current |= option.enabled_value if enabled else option.disabled_value
        self._update_row_value(reg_key, current)

    def _toggle_register_options(self, reg_key: str) -> None:
        row = self.register_rows[reg_key]
        if row.options_frame is None:
            return
        row.collapsed = not row.collapsed
        if row.collapsed:
            row.options_frame.grid_remove()
        else:
            row.options_frame.grid()
        if row.toggle_button is not None:
            row.toggle_button.configure(text="Expand" if row.collapsed else "Collapse")

    def _on_vcom_slider(self, value: float) -> None:
        profile = PMIC_PROFILES[self.pmic_var.get()]
        width = 3 if profile.key == "lx52042c" else 2
        self.vcom_var.set(f"0x{int(round(value)):0{width}X}")

    def _refresh_vcom_display(self, current_values: dict[str, int] | None = None) -> None:
        profile = PMIC_PROFILES[self.pmic_var.get()]
        current_values = current_values or self._current_values_snapshot()
        self._sync_vcom_title(profile, current_values)
        if not profile.supports_vcom:
            self.vcom_actual_var.set("Unsupported")
            self.vcom_coarse_var.set("--")
            self.vcom_lsb_var.set("--")
            return
        value = self._try_parse_row_value(self.vcom_var.get())
        if value is None:
            self.vcom_actual_var.set("--")
            self.vcom_coarse_var.set("--")
            self.vcom_lsb_var.set("--")
            return
        self.vcom_actual_var.set(format_vcom_display(profile, value, current_values))
        if profile.key == "lx52042c":
            coarse = (value >> 3) & 0xFF
            lsb = value & 0x07
            self.vcom_coarse_var.set(f"0x{coarse:02X} ({coarse * 10:.1f}mV)")
            self.vcom_lsb_var.set(f"0x{lsb:02X} ({lsb * 1.25:.2f}mV)")
        else:
            self.vcom_coarse_var.set("--")
            self.vcom_lsb_var.set("--")

    def _sync_vcom_split_visibility(self, profile) -> None:
        if profile.supports_vcom and profile.key == "lx52042c":
            self.vcom_split_frame.grid()
        else:
            self.vcom_split_frame.grid_remove()

    def _sync_vcom_title(self, profile, current_values: dict[str, int] | None = None) -> None:
        if profile.key == "nt50805":
            _, write_addr = self._nt50805_vcom_addresses(current_values)
            self.vcom_title_var.set(f"{profile.vcom.name} (D-VCOM 0x{write_addr:02X})")
            return
        if profile.key == "rtq6749":
            self.vcom_title_var.set(f"{profile.vcom.name} (0x{profile.vcom.device_addr:02X})")
            return
        self.vcom_title_var.set(profile.vcom.name)

    def _nt50805_vcom_addresses(self, current_values: dict[str, int] | None = None) -> tuple[int, int]:
        current_values = current_values or self._current_values_snapshot()
        reg03 = current_values.get("reg_03", 0x00)
        write_addr = 0x9A if reg03 & 0x80 else 0x9E
        return write_addr >> 1, write_addr

    def _vcom_device_addr_for_profile(self, profile) -> int | None:
        if profile.key == "nt50805":
            _, write_addr = self._nt50805_vcom_addresses()
            return write_addr
        return None

    def _allowed_value_mask(self, definition: RegisterDefinition) -> int:
        logic_mask = 0
        for option in definition.bit_options:
            logic_mask |= option.bit_mask
        numeric_mask = self._numeric_mask(definition)
        if definition.value_mask is not None:
            allowed_mask = definition.value_mask
        elif definition.bit_options:
            allowed_mask = logic_mask | numeric_mask
        else:
            allowed_mask = definition.max_value
        return allowed_mask

    def _normalize_register_value(self, definition: RegisterDefinition, value: int) -> int:
        allowed_mask = self._allowed_value_mask(definition)
        normalized = value & allowed_mask
        return max(definition.min_value, min(definition.max_value, normalized))

    def _show_error(self, exc: Exception) -> None:
        self.log(f"ERROR: {exc}")
        messagebox.showerror("PMIC AUX GUI", str(exc))

    def _on_close(self) -> None:
        for page in self.register_page_cache.values():
            if page.build_after_id is not None:
                self.after_cancel(page.build_after_id)
                page.build_after_id = None
        self._cancel_pending_refreshes()
        self._disconnect()
        self.destroy()
