from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from .profiles import GPU_CARD_IDS, PMIC_PROFILES, TCON_PROFILES, RegisterDefinition, BitOptionDefinition
from .service import AuxGuiError, SessionConfig, connect, default_paths, parse_hex_input, format_register_display, format_vcom_display


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
    status_var: tk.StringVar
    loading_label: ctk.CTkLabel
    pending_registers: list[RegisterDefinition]
    build_after_id: str | None = None
    build_complete: bool = False


class PmicAuxGuiApp(ctk.CTk):
    COLLAPSIBLE_REGISTER_NAMES = {
        "channel setting 0",
        "channel setting 1",
        "channel discharge setting",
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
        self.default_dll_path, self.default_operate_path = default_paths(base_dir)
        self.title("PMIC AUX Debug GUI")
        self.geometry("1500x920")
        self.minsize(1280, 760)
        self._apply_window_icon()

        self.gpu_var = tk.StringVar(value="amd_dp")
        self.tcon_var = tk.StringVar(value="nova")
        self.pmic_var = tk.StringVar(value="nt50805")
        self.nova_iic_en_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Disconnected")
        self.init_var = tk.StringVar(value="Not initialized")
        self.vcom_var = tk.StringVar(value="0x00")
        self.vcom_slider_var = tk.IntVar(value=0)
        self.vcom_actual_var = tk.StringVar(value="--")

        self._build_layout()
        self.after(1, self._reload_register_rows)
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
        for index in range(10):
            top.grid_columnconfigure(index, weight=1 if index in (1, 3, 5, 7) else 0)

        ctk.CTkLabel(top, text="GPU / CardID").grid(row=0, column=0, padx=8, pady=8, sticky="w")
        ctk.CTkOptionMenu(top, values=list(GPU_CARD_IDS.keys()), variable=self.gpu_var).grid(row=0, column=1, padx=8, pady=8, sticky="ew")
        ctk.CTkLabel(top, text="TCON").grid(row=0, column=2, padx=8, pady=8, sticky="w")
        ctk.CTkOptionMenu(top, values=list(TCON_PROFILES.keys()), variable=self.tcon_var, command=lambda _v: self._reload_register_rows()).grid(row=0, column=3, padx=8, pady=8, sticky="ew")
        ctk.CTkLabel(top, text="PMIC").grid(row=0, column=4, padx=8, pady=8, sticky="w")
        ctk.CTkOptionMenu(top, values=list(PMIC_PROFILES.keys()), variable=self.pmic_var, command=lambda _v: self._reload_register_rows()).grid(row=0, column=5, padx=8, pady=8, sticky="ew")
        self.nova_iic_en_switch = ctk.CTkSwitch(top, text="NOVA IIC_EN", variable=self.nova_iic_en_var, onvalue=True, offvalue=False)
        self.nova_iic_en_switch.grid(row=0, column=6, padx=8, pady=8, sticky="w")
        ctk.CTkButton(top, text="Connect", command=self._connect).grid(row=0, column=7, padx=8, pady=8, sticky="ew")
        ctk.CTkButton(top, text="Disconnect", command=self._disconnect).grid(row=0, column=8, padx=8, pady=8, sticky="ew")

        ctk.CTkLabel(top, text="Status").grid(row=1, column=0, padx=8, pady=(0, 8), sticky="w")
        ctk.CTkLabel(top, textvariable=self.status_var, anchor="w").grid(row=1, column=1, columnspan=3, padx=8, pady=(0, 8), sticky="ew")
        ctk.CTkLabel(top, text="Init").grid(row=1, column=4, padx=8, pady=(0, 8), sticky="w")
        ctk.CTkLabel(top, textvariable=self.init_var, anchor="w").grid(row=1, column=5, columnspan=4, padx=8, pady=(0, 8), sticky="ew")

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

        vcom_frame = ctk.CTkFrame(right)
        vcom_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        vcom_frame.grid_columnconfigure(1, weight=1)
        for index in range(2):
            vcom_frame.grid_columnconfigure(index, weight=1)
        ctk.CTkLabel(vcom_frame, text="VCOM", font=ctk.CTkFont(size=18, weight="bold")).grid(row=0, column=0, sticky="w", padx=8, pady=(8, 2))
        ctk.CTkLabel(vcom_frame, textvariable=self.vcom_actual_var, anchor="e").grid(row=0, column=1, padx=8, pady=(8, 2), sticky="e")
        ctk.CTkLabel(vcom_frame, text="Value").grid(row=1, column=0, padx=8, pady=(4, 6), sticky="w")
        ctk.CTkEntry(vcom_frame, textvariable=self.vcom_var).grid(row=1, column=1, padx=8, pady=(4, 6), sticky="ew")
        self.vcom_slider = ctk.CTkSlider(vcom_frame, from_=0, to=255, number_of_steps=255, variable=self.vcom_slider_var, command=self._on_vcom_slider)
        self.vcom_slider.grid(row=2, column=0, columnspan=2, padx=8, pady=(2, 8), sticky="ew")
        actions = ctk.CTkFrame(vcom_frame, fg_color="transparent")
        actions.grid(row=3, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
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
        ctk.CTkButton(bottom, text="Refresh Register UI", command=self._refresh_current_register_ui).grid(row=0, column=4, padx=8, pady=8, sticky="ew")
        self.vcom_var.trace_add("write", lambda *_args: self._refresh_vcom_display())

    def _reload_register_rows(self) -> None:
        self._sync_nova_toggle()
        profile = PMIC_PROFILES[self.pmic_var.get()]
        self.vcom_slider.configure(from_=profile.vcom.min_value, to=profile.vcom.max_value, number_of_steps=profile.vcom.max_value - profile.vcom.min_value)
        self._show_register_page(self.pmic_var.get())
        self._refresh_vcom_display()

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
        batch_size = 6
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
        for index in range(5):
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
        current_label = ctk.CTkLabel(row, text=display_text, width=180)
        current_label.grid(row=0, column=4, padx=6, pady=6, sticky="w")
        bit_option_vars: dict[str, tk.StringVar] = {}
        options_frame: ctk.CTkFrame | None = None
        toggle_button: ctk.CTkButton | None = None
        collapsed = bool(register.bit_options and self._is_collapsible_register(register))
        if register.bit_options:
            options_frame = ctk.CTkFrame(row, fg_color="transparent")
            options_frame.grid(row=1, column=0, columnspan=5, padx=6, pady=(0, 6), sticky="ew")
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
            toggle_button.grid(row=0, column=4, padx=6, pady=6, sticky="e")
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
            self._refresh_row_display(register.key)

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
            self._refresh_all_row_displays()
        else:
            self._refresh_vcom_display()

    def _is_collapsible_register(self, definition: RegisterDefinition) -> bool:
        return definition.name.strip().lower() in self.COLLAPSIBLE_REGISTER_NAMES

    def log(self, message: str) -> None:
        self.log_box.insert("end", f"{message}\n")
        self.log_box.see("end")
        self.update_idletasks()

    def _current_config(self) -> SessionConfig:
        return SessionConfig(
            gpu_key=self.gpu_var.get(),
            tcon_key=self.tcon_var.get(),
            pmic_key=self.pmic_var.get(),
            dll_path=self.default_dll_path,
            operate_module_path=self.default_operate_path,
            nova_use_iic_en=bool(self.nova_iic_en_var.get()),
        )

    def _sync_nova_toggle(self) -> None:
        if self.tcon_var.get() == "nova":
            self.nova_iic_en_switch.configure(state="normal")
        else:
            self.nova_iic_en_var.set(False)
            self.nova_iic_en_switch.configure(state="disabled")

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
                    self._update_row_value(reg_key, value)
            self._refresh_vcom_display()
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
            self.log(f"Wrote all registers through {target.upper()} path")
            self.init_var.set(session.init_summary)
        except Exception as exc:
            self._show_error(exc)

    def _read_vcom(self, target: str) -> None:
        try:
            session = self._require_session()
            value = session.read_vcom(target=target)
            self.vcom_var.set(f"0x{value:02X}")
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
            value = parse_hex_input(self.vcom_var.get())
            session.write_vcom(value, target=target)
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
        self._refresh_all_row_displays()

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

    def _refresh_row_display(self, reg_key: str) -> None:
        row = self.register_rows[reg_key]
        value = self._try_parse_row_value(row.raw_value_var.get())
        if value is None:
            row.value_label.configure(text="--")
            return
        clamped_value = self._normalize_register_value(row.definition, value)
        numeric_value = clamped_value & row.numeric_mask if row.numeric_mask else None
        if row.slider is not None and numeric_value is not None and row.slider_var.get() != numeric_value:
            row.slider_var.set(numeric_value)
        if row.editor_var is not None and numeric_value is not None:
            editor_text = self._format_numeric_value(numeric_value)
            if row.editor_var.get() != editor_text:
                self._editor_sync_guard.add(reg_key)
                row.editor_var.set(editor_text)
                self._editor_sync_guard.discard(reg_key)
        current_values = self._collect_current_row_values()
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

    def _refresh_all_row_displays(self) -> None:
        for reg_key in self.register_rows:
            self._refresh_row_display(reg_key)
        self._refresh_vcom_display()

    def _collect_current_row_values(self) -> dict[str, int]:
        values: dict[str, int] = {}
        for key, state in self.register_rows.items():
            value = self._try_parse_row_value(state.raw_value_var.get())
            if value is not None:
                values[key] = self._normalize_register_value(state.definition, value)
        return values

    def _try_parse_row_value(self, text: str) -> int | None:
        try:
            return parse_hex_input(text)
        except Exception:
            return None

    def _numeric_mask(self, definition: RegisterDefinition) -> int:
        if not definition.supports_slider:
            return 0
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
        next_raw = (current_raw & ~row.numeric_mask) | (numeric_value & row.numeric_mask)
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
        self.vcom_var.set(f"0x{int(round(value)):02X}")

    def _refresh_vcom_display(self) -> None:
        value = self._try_parse_row_value(self.vcom_var.get())
        if value is None:
            self.vcom_actual_var.set("--")
            return
        profile = PMIC_PROFILES[self.pmic_var.get()]
        self.vcom_actual_var.set(format_vcom_display(profile, value, self._collect_current_row_values()))

    def _allowed_value_mask(self, definition: RegisterDefinition) -> int:
        logic_mask = 0
        for option in definition.bit_options:
            logic_mask |= option.bit_mask
        numeric_mask = self._numeric_mask(definition)
        allowed_mask = logic_mask | numeric_mask
        if not definition.bit_options:
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
        self._disconnect()
        self.destroy()
