import argparse
import hashlib
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, font, messagebox, ttk

try:
    import serial
except ImportError:  # pragma: no cover - depends on local environment
    serial = None


BUFFER_SIZE = 0x10000
BYTES_PER_ROW = 16
SERIAL_BAUDRATE = 115200
SERIAL_TIMEOUT = 1.0
BSND_CHUNK_SIZE = 256
HEX_COLUMN_WIDTH = (BYTES_PER_ROW * 3) - 1
SAVE_START_CHOICES = ("0000", "4000", "8000", "C000")
SAVE_END_CHOICES = ("3FFF", "7FFF", "BFFF", "FFFF")
SAVE_BANK_CHOICES = tuple(f"{bank:02X}" for bank in range(0x100))
DEFAULT_SLOT = 1
TOOLBAR_JUMP_ADDRESSES = (
    ("00", 0x0000),
    ("40", 0x4000),
    ("80", 0x8000),
    ("C0", 0xC000),
    ("FF", 0xFFFF),
)
FLAT_MODE = "flat"
MAPPER_MODE = "mapper"
FLAT_HEADER_PREFIX = "ADDR"
MAPPER_HEADER_PREFIX = "FulAdr:Bk:Ofst"
WINDOW_CHOICES = (
    "4000-5FFF(8k)",
    "4000-7FFF(16k)",
    "8000-9FFF(8k)",
    "8000-BFFF(16k)",
)
WINDOW_CONFIGS = {
    "4000-5FFF(8k)": (0x4000, 0x2000),
    "4000-7FFF(16k)": (0x4000, 0x4000),
    "8000-9FFF(8k)": (0x8000, 0x2000),
    "8000-BFFF(16k)": (0x8000, 0x4000),
}
SWITCH_ADDR_CHOICES = (
    "6000",
    "6800",
    "7000",
    "7800",
    "8000",
    "8800",
    "9000",
    "9800",
)
MAPPER_TYPE_CHOICES = (
    "ASCII 8K",
    "ASCII 16K",
    "Konami",
    "Custom",
)
MAPPER_PRESETS = {
    "ASCII 8K": ("4000-5FFF(8k)", "6000"),
    "ASCII 16K": ("4000-7FFF(16k)", "6000"),
    "Konami": ("4000-5FFF(8k)", "5000"),
    "Custom": ("4000-5FFF(8k)", "6000"),
}
DEFAULT_MAPPER_TYPE = "ASCII 8K"
DEFAULT_WINDOW = MAPPER_PRESETS[DEFAULT_MAPPER_TYPE][0]
DEFAULT_SWITCH_ADDR = MAPPER_PRESETS[DEFAULT_MAPPER_TYPE][1]
APP_VERSION = "v0.2"
APP_TITLE = f"MSX Game Cartidge Adapter GUI {APP_VERSION}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MSX Game Cartidge Adapter GUI"
    )
    parser.add_argument(
        "--device",
        default="",
        help="serial device path to preselect in the serial dialog",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print device I/O to stderr",
    )
    return parser.parse_args()


def to_printable(byte_value: int) -> str:
    if 0x20 <= byte_value <= 0x7E:
        return chr(byte_value)
    return "."


def parse_hex_address(text: str) -> int:
    normalized = text.strip().upper()
    if not normalized:
        raise ValueError("empty")
    if len(normalized) > 4:
        raise ValueError("range")
    if any(ch not in "0123456789ABCDEF" for ch in normalized):
        raise ValueError("format")
    return int(normalized, 16)


def parse_hex_byte(text: str) -> int:
    value = parse_hex_address(text)
    if value > 0xFF:
        raise ValueError("range")
    return value


def discover_serial_devices() -> list[tuple[str, str]]:
    devices: list[tuple[str, str]] = []
    seen: set[str] = set()

    if serial is None:
        return devices

    try:
        from serial.tools import list_ports

        for port in list_ports.comports():
            device = port.device
            if device in seen:
                continue
            seen.add(device)
            description = port.description or "serial device"
            devices.append((device, f"{device}  {description}"))
    except Exception:
        return []

    devices.sort(key=lambda item: item[0])
    return devices


class AdapterProtocolError(RuntimeError):
    pass


class AdapterTransport:
    def __init__(self, device: str, debug: bool = False, log_callback=None) -> None:
        if serial is None:
            raise AdapterProtocolError("pyserial が見つかりません。`pip install pyserial` を実行してください。")
        self._debug = debug
        self._log_callback = log_callback
        started_at = time.monotonic()
        self._debug_log("APP", f"serial open start {device}")
        self._serial = serial.Serial(
            port=device,
            baudrate=SERIAL_BAUDRATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=SERIAL_TIMEOUT,
        )
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()
        elapsed = time.monotonic() - started_at
        self._debug_log("APP", f"serial open done {device} ({elapsed:.3f}s)")

    def close(self) -> None:
        self._serial.close()

    def execute_command(self, text: str) -> list[str]:
        self._write_command(text)
        return self._read_until_result()

    def send_command_without_result(self, text: str) -> None:
        self._write_command(text)

    def read_binary(self, size: int, progress_callback) -> bytes:
        return self._read_exact(size, progress_callback)

    def receive_command_result(self) -> list[str]:
        return self._read_until_result()

    def _read_until_result(self) -> list[str]:
        lines: list[str] = []
        while True:
            line = self._read_line()
            if line == "OK":
                return lines
            if line == "FAIL":
                detail = "\n".join(lines) if lines else "device returned FAIL"
                raise AdapterProtocolError(detail)
            lines.append(line)

    def _read_exact(self, size: int, progress_callback) -> bytes:
        data = bytearray()
        while len(data) < size:
            remaining = min(BSND_CHUNK_SIZE, size - len(data))
            chunk = self._serial.read(remaining)
            if not chunk:
                raise AdapterProtocolError("バイナリ受信がタイムアウトしました。")
            self._debug_binary_chunk("RXB", len(data), chunk)
            data.extend(chunk)
            progress_callback(len(data) - 1)
        return bytes(data)

    def _read_line(self) -> str:
        raw = self._serial.readline()
        if not raw:
            self._debug_log("APP", f"readline timeout ({SERIAL_TIMEOUT:.3f}s)")
            raise AdapterProtocolError("応答待ちがタイムアウトしました。")
        line = raw.decode("utf-8", errors="replace").strip()
        self._debug_log("RX", line)
        return line

    def _write_command(self, text: str) -> None:
        self._debug_log("TX", text)
        self._serial.write((text + "\n").encode("ascii"))

    def _debug_log(self, direction: str, payload: str) -> None:
        if self._log_callback is not None:
            self._log_callback(direction, payload)
        if self._debug:
            print(f"{direction}: {payload}", file=sys.stderr, flush=True)

    def _debug_binary_chunk(self, direction: str, offset: int, chunk: bytes) -> None:
        summary = (
            f"{offset:04X}-{offset + len(chunk) - 1:04X} "
            f"{' '.join(f'{value:02X}' for value in chunk)}"
        )
        if self._log_callback is not None:
            self._log_callback(direction, summary)
        if self._debug:
            print(
                f"{direction}: {summary}",
                file=sys.stderr,
                flush=True,
            )


class CartridgeAdapter:
    def __init__(self, device: str, debug: bool = False, log_callback=None) -> None:
        self.device = device
        self._slot_power_on = False
        self._transport = AdapterTransport(
            device=device,
            debug=debug,
            log_callback=log_callback,
        )

    def close(self) -> None:
        if self._slot_power_on:
            try:
                self._transport.execute_command("SPOFF")
            except Exception:
                pass
        self._transport.close()

    def get_version_info(self) -> list[str]:
        response = self.run_command("HVER")
        if not response:
            raise AdapterProtocolError("HVER の応答が空です。")
        return response

    def run_command(self, text: str) -> list[str]:
        return self._transport.execute_command(text)

    def read_cartridge_64kb(self, progress_callback) -> bytes:
        return self._read_full_buffer(progress_callback)

    def read_slot_range(
        self,
        address: int,
        length: int,
        progress_callback=None,
    ) -> bytes:
        self._ensure_power_on()
        self._transport.execute_command(
            f"SMTR,{address:04X},{length:04X},0000,{DEFAULT_SLOT}"
        )
        self._transport.send_command_without_result(f"BSND,0000,{length:04X}")
        payload = self._transport.read_binary(length, progress_callback or (lambda _offset: None))
        self._transport.receive_command_result()
        return payload

    def read_mapper_bank(
        self,
        bank: int,
        switch_addr: int,
        window_start: int,
        window_length: int,
        progress_callback=None,
    ) -> bytes:
        self._ensure_power_on()
        self._transport.execute_command(
            f"SMWR,{switch_addr:04X},{bank:02X},{DEFAULT_SLOT}"
        )
        return self.read_slot_range(window_start, window_length, progress_callback)

    def write_mapper_byte_and_read_bank(
        self,
        bank: int,
        switch_addr: int,
        window_start: int,
        window_length: int,
        offset: int,
        value: int,
    ) -> bytes:
        self._ensure_power_on()
        self._transport.execute_command(
            f"SMWR,{switch_addr:04X},{bank:02X},{DEFAULT_SLOT}"
        )
        self._transport.execute_command(
            f"SMWR,{window_start + offset:04X},{value:02X},{DEFAULT_SLOT}"
        )
        return self.read_slot_range(window_start, window_length)

    def write_byte(self, address: int, value: int) -> None:
        self._ensure_power_on()
        self._transport.execute_command(
            f"SMWR,{address:04X},{value:02X},{DEFAULT_SLOT}"
        )

    def write_byte_and_read_cartridge_64kb(
        self,
        address: int,
        value: int,
        progress_callback,
    ) -> bytes:
        self._ensure_power_on()
        self._transport.execute_command(
            f"SMWR,{address:04X},{value:02X},{DEFAULT_SLOT}"
        )
        self._transport.execute_command(
            f"SMTR,0000,10000,0,{DEFAULT_SLOT}"
        )
        self._transport.send_command_without_result("BSND,")
        payload = self._transport.read_binary(BUFFER_SIZE, progress_callback)
        self._transport.receive_command_result()
        return payload

    def _read_full_buffer(self, progress_callback) -> bytes:
        self._ensure_power_on()
        self._transport.execute_command(
            f"SMTR,0000,10000,0,{DEFAULT_SLOT}"
        )
        self._transport.send_command_without_result("BSND,")
        payload = self._transport.read_binary(BUFFER_SIZE, progress_callback)
        self._transport.receive_command_result()
        return payload

    def _ensure_power_on(self) -> None:
        if self._slot_power_on:
            return
        self._transport.execute_command("SPON")
        time.sleep(1.0)
        self._slot_power_on = True


class SaveBufferDialog(tk.Toplevel):
    def __init__(
        self,
        master: tk.Misc,
        initial_mode: str,
        mapper_defaults: dict[str, str],
        analyzed_end_bank: str,
    ) -> None:
        super().__init__(master)
        self.result: dict[str, object] | None = None

        self.title("バッファ保存")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.path_var = tk.StringVar(value="msx_adapter_dump.rom")
        self.mode_var = tk.StringVar(value=initial_mode)
        self.start_var = tk.StringVar(value=SAVE_START_CHOICES[0])
        self.end_var = tk.StringVar(value=SAVE_END_CHOICES[-1])
        self.mapper_type_var = tk.StringVar(value=mapper_defaults["mapper_type"])
        self.window_var = tk.StringVar(value=mapper_defaults["window_label"])
        self.switch_var = tk.StringVar(value=mapper_defaults["switch_addr"])
        self.start_bank_var = tk.StringVar(value="00")
        self.end_bank_var = tk.StringVar(value=analyzed_end_bank)
        self.error_var = tk.StringVar(value="")
        self._updating_mapper_controls = False

        self.columnconfigure(0, weight=1)
        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="ファイル名").grid(row=0, column=0, sticky="w")
        path_entry = ttk.Entry(frame, textvariable=self.path_var, width=40)
        path_entry.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(frame, text="参照", command=self._browse).grid(row=0, column=2)

        mode_frame = ttk.Frame(frame)
        mode_frame.grid(row=1, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Radiobutton(mode_frame, text="Flat", variable=self.mode_var, value="flat", command=self._update_mode_state).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(mode_frame, text="Mapper", variable=self.mode_var, value="mapper", command=self._update_mode_state).grid(row=0, column=1, sticky="w", padx=(12, 0))

        self.flat_frame = ttk.LabelFrame(frame, text="Flat", padding=8)
        self.flat_frame.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.flat_frame.columnconfigure(1, weight=1)

        ttk.Label(self.flat_frame, text="開始アドレス").grid(row=0, column=0, sticky="w")
        self.start_combo = ttk.Combobox(
            self.flat_frame,
            textvariable=self.start_var,
            values=SAVE_START_CHOICES,
            width=10,
        )
        self.start_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))

        ttk.Label(self.flat_frame, text="終了アドレス").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.end_combo = ttk.Combobox(
            self.flat_frame,
            textvariable=self.end_var,
            values=SAVE_END_CHOICES,
            width=10,
        )
        self.end_combo.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        self.mapper_frame = ttk.LabelFrame(frame, text="Mapper", padding=8)
        self.mapper_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.mapper_frame.columnconfigure(1, weight=1)

        ttk.Label(self.mapper_frame, text="Type").grid(row=0, column=0, sticky="w")
        self.mapper_type_combo = ttk.Combobox(
            self.mapper_frame,
            textvariable=self.mapper_type_var,
            values=MAPPER_TYPE_CHOICES,
            state="readonly",
            width=12,
        )
        self.mapper_type_combo.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.mapper_type_combo.bind("<<ComboboxSelected>>", self._on_mapper_type_selected)

        ttk.Label(self.mapper_frame, text="Window").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.window_combo = ttk.Combobox(
            self.mapper_frame,
            textvariable=self.window_var,
            values=WINDOW_CHOICES,
            state="readonly",
            width=16,
        )
        self.window_combo.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        self.window_combo.bind("<<ComboboxSelected>>", self._on_mapper_setting_changed)

        ttk.Label(self.mapper_frame, text="Switch").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.switch_combo = ttk.Combobox(
            self.mapper_frame,
            textvariable=self.switch_var,
            values=SWITCH_ADDR_CHOICES,
            width=8,
        )
        self.switch_combo.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        self.switch_combo.bind("<<ComboboxSelected>>", self._on_mapper_setting_changed)
        self.switch_combo.bind("<FocusOut>", self._on_mapper_setting_changed)
        self.switch_combo.bind("<Return>", self._on_mapper_setting_changed)

        ttk.Label(self.mapper_frame, text="StartBank").grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.start_bank_combo = ttk.Combobox(
            self.mapper_frame,
            textvariable=self.start_bank_var,
            values=SAVE_BANK_CHOICES,
            width=6,
        )
        self.start_bank_combo.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        ttk.Label(self.mapper_frame, text="EndBank").grid(row=4, column=0, sticky="w", pady=(10, 0))
        self.end_bank_combo = ttk.Combobox(
            self.mapper_frame,
            textvariable=self.end_bank_var,
            values=SAVE_BANK_CHOICES,
            width=6,
        )
        self.end_bank_combo.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        error_label = ttk.Label(frame, textvariable=self.error_var, foreground="#c62828")
        error_label.grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=5, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(button_frame, text="キャンセル", command=self._cancel).grid(row=0, column=0)
        ttk.Button(button_frame, text="保存", command=self._submit).grid(row=0, column=1, padx=(8, 0))

        self.bind("<Return>", lambda _event: self._submit())
        self.bind("<Escape>", lambda _event: self._cancel())
        self._update_mode_state()
        path_entry.focus_set()

    def show(self) -> dict[str, object] | None:
        self.wait_window()
        return self.result

    def _browse(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self,
            title="保存先選択",
            defaultextension=".rom",
            filetypes=[
                ("ROM", "*.rom"),
                ("Binary", "*.bin"),
                ("All files", "*"),
            ],
            initialfile=self.path_var.get() or "msx_adapter_dump.rom",
        )
        if path:
            self.path_var.set(path)

    def _submit(self) -> None:
        path = self.path_var.get().strip()
        mode = self.mode_var.get()

        if not path:
            self.error_var.set("ファイル名を指定してください。")
            return

        if mode == "flat":
            start_text = self.start_var.get().strip().upper()
            end_text = self.end_var.get().strip().upper()
            try:
                start_address = parse_hex_address(start_text)
                end_address = parse_hex_address(end_text)
            except ValueError:
                self.error_var.set("開始/終了アドレスは16進数で入力してください。")
                return
            if start_address > 0xFFFF or end_address > 0xFFFF:
                self.error_var.set("開始/終了アドレスは0000-FFFFで入力してください。")
                return
            if start_address > end_address:
                self.error_var.set("開始アドレスが終了アドレスを超えています。")
                return
            self.result = {
                "mode": "flat",
                "path": path,
                "start_address": start_address,
                "end_address": end_address,
            }
        else:
            switch_text = self.switch_var.get().strip().upper()
            start_bank_text = self.start_bank_var.get().strip().upper()
            end_bank_text = self.end_bank_var.get().strip().upper()
            try:
                switch_addr = parse_hex_address(switch_text)
                start_bank = parse_hex_byte(start_bank_text)
                end_bank = parse_hex_byte(end_bank_text)
            except ValueError:
                self.error_var.set("Switch, StartBank, EndBank は16進数で入力してください。")
                return
            if start_bank > end_bank:
                self.error_var.set("StartBank が EndBank を超えています。")
                return
            self.result = {
                "mode": "mapper",
                "path": path,
                "mapper_type": self.mapper_type_var.get(),
                "window_label": self.window_var.get(),
                "switch_addr": switch_addr,
                "start_bank": start_bank,
                "end_bank": end_bank,
            }
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()

    def _update_mode_state(self) -> None:
        flat_enabled = self.mode_var.get() == "flat"
        mapper_enabled = not flat_enabled
        self.start_combo.configure(state="normal" if flat_enabled else "disabled")
        self.end_combo.configure(state="normal" if flat_enabled else "disabled")
        self.mapper_type_combo.configure(state="readonly" if mapper_enabled else "disabled")
        self.window_combo.configure(state="readonly" if mapper_enabled else "disabled")
        self.switch_combo.configure(state="normal" if mapper_enabled else "disabled")
        self.start_bank_combo.configure(state="normal" if mapper_enabled else "disabled")
        self.end_bank_combo.configure(state="normal" if mapper_enabled else "disabled")

    def _on_mapper_type_selected(self, _event=None) -> None:
        mapper_type = self.mapper_type_var.get()
        preset = MAPPER_PRESETS.get(mapper_type)
        if preset is None:
            return
        self._updating_mapper_controls = True
        try:
            self.window_var.set(preset[0])
            self.switch_var.set(preset[1])
        finally:
            self._updating_mapper_controls = False

    def _on_mapper_setting_changed(self, _event=None) -> None:
        if self._updating_mapper_controls:
            return None
        current = (self.window_var.get(), self.switch_var.get().strip().upper())
        self.switch_var.set(current[1])
        preset = MAPPER_PRESETS.get(self.mapper_type_var.get())
        if preset is None or current != preset:
            self.mapper_type_var.set("Custom")
        return None


class SerialDeviceDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, current_device: str) -> None:
        super().__init__(master)
        self.result: str | None = None
        self._device_by_label: dict[str, str] = {}
        self._current_device = current_device
        self.device_var = tk.StringVar()

        self.title("シリアル選択")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        ttk.Label(frame, text="シリアルデバイス").grid(row=0, column=0, sticky="w")
        self.device_combo = ttk.Combobox(
            frame,
            textvariable=self.device_var,
            state="readonly",
            width=60,
        )
        self.device_combo.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.device_combo.bind("<<ComboboxSelected>>", lambda _event: None)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(button_frame, text="更新", command=self._refresh_devices).pack(side="left")
        ttk.Button(button_frame, text="キャンセル", command=self._cancel).pack(side="right")
        ttk.Button(button_frame, text="開く", command=self._submit).pack(
            side="right",
            padx=(0, 8),
        )

        self.bind("<Return>", self._submit_event)
        self.bind("<Escape>", lambda _event: self._cancel())
        self._refresh_devices()

    def show(self) -> str | None:
        self.wait_window()
        return self.result

    def _refresh_devices(self) -> None:
        values: list[str] = []
        self._device_by_label = {}
        selected_label = None

        for device, label in discover_serial_devices():
            values.append(label)
            self._device_by_label[label] = device
            if device == self._current_device:
                selected_label = label

        self.device_combo.configure(values=values)
        if not values:
            self.device_combo.configure(state="disabled")
            self.device_var.set("シリアルデバイスが見つかりません")
            return

        self.device_combo.configure(state="readonly")
        if selected_label is None:
            selected_label = values[0]
        self.device_var.set(selected_label)
        self.device_combo.focus_set()

    def _submit_event(self, _event) -> None:
        self._submit()

    def _submit(self) -> None:
        label = self.device_var.get().strip()
        if not label:
            return
        device = self._device_by_label.get(label)
        if device is None:
            return
        self.result = device
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class AccessLogDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, lines: list[str]) -> None:
        super().__init__(master)
        self.title("アクセスログ")
        self.geometry("820x320")

        frame = ttk.Frame(self, padding=8)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.text = tk.Text(
            frame,
            wrap="none",
            state="disabled",
            takefocus=0,
            borderwidth=0,
            highlightthickness=0,
            font=font.nametofont("TkFixedFont"),
        )
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=y_scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")

        self.set_lines(lines)

    def set_lines(self, lines: list[str]) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", "\n".join(lines))
        self.text.see(tk.END)
        self.text.configure(state="disabled")


class ManualCommandDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, submit_callback) -> None:
        super().__init__(master)
        self._submit_callback = submit_callback
        self.title("手動実行")
        self.geometry("820x360")

        self.command_var = tk.StringVar()

        frame = ttk.Frame(self, padding=8)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        command_frame = ttk.LabelFrame(frame, text="Command", padding=(8, 6, 8, 6))
        command_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        command_frame.columnconfigure(0, weight=1)

        self.command_entry = ttk.Entry(command_frame, textvariable=self.command_var)
        self.command_entry.grid(row=0, column=0, sticky="ew")
        self.command_entry.bind("<Return>", self._submit_event)

        self.send_button = ttk.Button(command_frame, text="送信", command=self._submit)
        self.send_button.grid(row=0, column=1, padx=(8, 0))

        log_frame = ttk.LabelFrame(frame, text="Log", padding=(6, 6, 6, 6))
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame,
            wrap="none",
            state="disabled",
            takefocus=0,
            borderwidth=0,
            highlightthickness=0,
            font=font.nametofont("TkFixedFont"),
        )
        y_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=y_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")

        self.command_entry.focus_set()

    def append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, line + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.command_entry.configure(state=state)
        self.send_button.configure(state=state)

    def focus_entry(self) -> None:
        self.command_entry.focus_set()

    def _submit_event(self, _event) -> None:
        self._submit()

    def _submit(self) -> None:
        command = self.command_var.get().strip()
        if not command:
            return
        self.command_var.set("")
        self._submit_callback(command)


class MapperMapDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, analyze_callback, select_bank_callback) -> None:
        super().__init__(master)
        self._analyze_callback = analyze_callback
        self._select_bank_callback = select_bank_callback
        self._cell_text_ids: dict[int, int] = {}
        self.image_size_var = tk.StringVar(value="")
        self.withdraw()

        self.title("Mapper Map")
        self.resizable(False, False)
        self.transient(master)
        self.attributes("-alpha", 0.0)

        frame = ttk.Frame(self, padding=8)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        mono = font.nametofont("TkFixedFont")
        self._header_size = 40
        self._cell_width = 56
        self._cell_height = 28
        self._grid_width = self._header_size + (16 * self._cell_width)
        self._grid_height = self._header_size + (16 * self._cell_height)
        self.canvas = tk.Canvas(
            frame,
            width=self._grid_width,
            height=self._grid_height,
            background="#ffffff",
            highlightthickness=0,
            borderwidth=0,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")

        self._draw_grid(mono)

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        button_frame.columnconfigure(0, weight=1)
        ttk.Label(button_frame, textvariable=self.image_size_var).grid(
            row=0,
            column=0,
            sticky="w",
            padx=(0, 12),
        )
        self.analyze_button = ttk.Button(
            button_frame,
            text="Analyze",
            command=self._analyze_callback,
        )
        self.analyze_button.grid(row=0, column=1)

        self.update_idletasks()
        self.after_idle(self._show_ready)

    def _show_ready(self) -> None:
        self.deiconify()
        self.lift()
        self.update_idletasks()
        self.attributes("-alpha", 1.0)

    def _draw_grid(self, mono) -> None:
        self.canvas.delete("all")
        line_color = "#808080"
        text_color = "#000000"

        self.canvas.create_rectangle(
            0,
            0,
            self._grid_width,
            self._grid_height,
            outline=line_color,
            fill="#ffffff",
        )

        for column in range(16):
            x1 = self._header_size + (column * self._cell_width)
            x2 = x1 + self._cell_width
            self.canvas.create_rectangle(
                x1,
                0,
                x2,
                self._header_size,
                outline=line_color,
                fill="#f5f5f5",
            )
            self.canvas.create_text(
                x1 + (self._cell_width / 2),
                self._header_size / 2,
                text=f"+{column:X}",
                font=mono,
                fill=text_color,
            )

        for row in range(16):
            y1 = self._header_size + (row * self._cell_height)
            y2 = y1 + self._cell_height
            self.canvas.create_rectangle(
                0,
                y1,
                self._header_size,
                y2,
                outline=line_color,
                fill="#f5f5f5",
            )
            self.canvas.create_text(
                self._header_size / 2,
                y1 + (self._cell_height / 2),
                text=f"+{row * 0x10:02X}",
                font=mono,
                fill=text_color,
            )
            for column in range(16):
                bank = (row * 16) + column
                x1 = self._header_size + (column * self._cell_width)
                x2 = x1 + self._cell_width
                tag = f"cell_{bank:02X}"
                self.canvas.create_rectangle(
                    x1,
                    y1,
                    x2,
                    y2,
                    outline=line_color,
                    fill="#ffffff",
                    tags=(tag,),
                )
                text_id = self.canvas.create_text(
                    x1 + (self._cell_width / 2),
                    y1 + (self._cell_height / 2),
                    text=f"{bank:02X}",
                    font=mono,
                    fill=text_color,
                    tags=(tag,),
                )
                self._cell_text_ids[bank] = text_id
                self.canvas.tag_bind(tag, "<Button-1>", lambda _event, b=bank: self._on_cell_click(b))

    def reset_cells(self) -> None:
        self.image_size_var.set("")
        for bank, text_id in self._cell_text_ids.items():
            self.canvas.itemconfigure(
                text_id,
                text=f"{bank:02X}",
                fill="#000000",
            )

    def set_image_size(self, text: str) -> None:
        self.image_size_var.set(text)

    def mark_unique(self, bank: int) -> None:
        self.canvas.itemconfigure(
            self._cell_text_ids[bank],
            text=f"{bank:02X}",
            fill="#c62828",
        )

    def mark_mirror(self, bank: int, before: int) -> None:
        self.canvas.itemconfigure(
            self._cell_text_ids[bank],
            text=f"({before:02X})",
            fill="#808080",
        )

    def set_busy(self, busy: bool) -> None:
        self.analyze_button.configure(state="disabled" if busy else "normal")

    def _on_cell_click(self, bank: int) -> None:
        self._select_bank_callback(bank)


class HexViewer(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        data: bytes,
        on_change=None,
        on_byte_edited=None,
        on_mapper_bank_move=None,
    ) -> None:
        super().__init__(master)
        self._on_change = on_change
        self._on_byte_edited = on_byte_edited
        self._on_mapper_bank_move = on_mapper_bank_move
        self._original_data = bytearray(data)
        self._current_data = bytearray(data)
        self._changed_offsets: set[int] = set()
        self._mode = FLAT_MODE
        self._mapper_window_length = 0x2000
        self._mapper_bank = 0
        self._mapper_window_start = 0x4000
        self._address_column_width = self._compute_address_column_width()
        self._ascii_column_start = self._address_column_width + HEX_COLUMN_WIDTH + 2
        mono = font.nametofont("TkFixedFont")
        self._header = tk.Text(
            self,
            wrap="none",
            height=1,
            borderwidth=0,
            highlightthickness=0,
            takefocus=0,
            cursor="arrow",
        )
        self._text = tk.Text(
            self,
            wrap="none",
            width=78,
            height=32,
            borderwidth=0,
            highlightthickness=0,
            undo=False,
            insertwidth=2,
            cursor="xterm",
        )
        self._header.configure(font=mono)
        self._text.configure(font=mono)
        self._header.tag_configure("address_bg", background="#ececec")
        self._header.tag_configure("cursor_byte", background="#fff2a8")
        self._text.tag_configure("address_bg", background="#ececec")
        self._text.tag_configure("cursor_line", background="#fff2a8")
        self._text.tag_configure("changed", foreground="#c62828")
        self._text.bind("<KeyPress>", self._on_key_press)
        self._text.bind("<Button-1>", self._on_click)
        self._text.bind("<B1-Motion>", self._on_click)
        self._text.bind("<FocusIn>", self._on_focus_in)
        self._editable = True

        self._y_scroll = ttk.Scrollbar(self, orient="vertical", command=self._text.yview)
        self._x_scroll = ttk.Scrollbar(self, orient="horizontal", command=self._xview)
        self._text.configure(
            yscrollcommand=self._y_scroll.set,
            xscrollcommand=self._x_scroll.set,
        )

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._header.grid(row=0, column=0, sticky="ew")
        self._text.grid(row=1, column=0, sticky="nsew")
        self._y_scroll.grid(row=1, column=1, sticky="ns")
        self._x_scroll.grid(row=2, column=0, sticky="ew")

        self.set_data(data)

    def configure_mode(
        self,
        mode: str,
        mapper_window_length: int = 0x2000,
        mapper_bank: int = 0,
        mapper_window_start: int = 0x4000,
    ) -> None:
        self._mode = mode
        self._mapper_window_length = mapper_window_length
        self._mapper_bank = mapper_bank
        self._mapper_window_start = mapper_window_start
        self._address_column_width = self._compute_address_column_width()
        self._ascii_column_start = self._address_column_width + HEX_COLUMN_WIDTH + 2
        self._render_all()

    def set_data(
        self,
        data: bytes,
        cursor_offset: int = 0,
        cursor_nibble: int = 0,
        keep_view: bool = False,
    ) -> None:
        view_state = self.get_view_state() if keep_view else None
        self._original_data = bytearray(data)
        self._current_data = bytearray(data)
        self._changed_offsets.clear()
        self._render_all()
        self._move_cursor_to_offset(
            cursor_offset,
            cursor_nibble,
            keep_view=keep_view,
        )
        if view_state is not None:
            self.restore_view_state(view_state)
        self._notify_change()

    def get_data(self) -> bytes:
        return bytes(self._current_data)

    def has_changes(self) -> bool:
        return bool(self._changed_offsets)

    def replace_slice(self, start: int, data: bytes) -> None:
        if not data:
            return
        end = min(start + len(data), len(self._current_data))
        payload = data[: end - start]
        self._original_data[start:end] = payload
        self._current_data[start:end] = payload
        self._changed_offsets = {
            offset for offset in self._changed_offsets if not (start <= offset < end)
        }
        start_row = start // BYTES_PER_ROW
        end_row = (end - 1) // BYTES_PER_ROW
        for row_index in range(start_row, end_row + 1):
            self._update_row(row_index)
        self._notify_change()

    def set_editable(self, editable: bool) -> None:
        self._editable = editable

    def get_cursor_position(self) -> tuple[int, int]:
        return self._insert_position()

    def set_cursor_position(self, offset: int, nibble: int = 0) -> None:
        self._move_cursor_to_offset(offset, nibble)
        self._text.focus_set()

    def scroll_to_address(self, offset: int) -> None:
        self.set_cursor_position(offset, 0)

    def focus_editor(self) -> None:
        self._text.focus_set()

    def get_view_state(self) -> tuple[float, float]:
        y_first, _y_last = self._text.yview()
        x_first, _x_last = self._text.xview()
        return y_first, x_first

    def restore_view_state(self, view_state: tuple[float, float]) -> None:
        y_first, x_first = view_state
        self._text.yview_moveto(y_first)
        self._header.xview_moveto(x_first)
        self._text.xview_moveto(x_first)

    def _render_all(self) -> None:
        lines = []
        for base in range(0, len(self._current_data), BYTES_PER_ROW):
            lines.append(self._format_row(base))

        self._header.configure(state="normal")
        self._header.delete("1.0", tk.END)
        self._header.insert("1.0", self._header_text())
        self._header.tag_add("address_bg", "1.0", f"1.{self._address_column_width}")
        self._header.configure(state="disabled")
        self._text.delete("1.0", tk.END)
        self._text.insert("1.0", "\n".join(lines))
        self._refresh_tags()

    def _format_row(self, base: int) -> str:
        row = self._current_data[base : base + BYTES_PER_ROW]
        hex_part = " ".join(f"{value:02X}" for value in row)
        ascii_part = "".join(to_printable(value) for value in row)
        return f"{self._format_address(base)}  {hex_part:<{HEX_COLUMN_WIDTH}}  {ascii_part}"

    def _apply_changed_tags(self) -> None:
        self._text.tag_remove("changed", "1.0", tk.END)
        for offset in self._changed_offsets:
            line_no = (offset // BYTES_PER_ROW) + 1
            byte_in_row = offset % BYTES_PER_ROW
            hex_col = self._address_column_width + (byte_in_row * 3)
            ascii_col = self._ascii_column_start + byte_in_row
            self._text.tag_add(
                "changed",
                f"{line_no}.{hex_col}",
                f"{line_no}.{hex_col + 2}",
            )
            self._text.tag_add(
                "changed",
                f"{line_no}.{ascii_col}",
                f"{line_no}.{ascii_col + 1}",
            )

    def _apply_address_tags(self) -> None:
        self._text.tag_remove("address_bg", "1.0", tk.END)
        line_count = (len(self._current_data) + BYTES_PER_ROW - 1) // BYTES_PER_ROW
        for line_no in range(1, line_count + 1):
            self._text.tag_add(
                "address_bg",
                f"{line_no}.0",
                f"{line_no}.{self._address_column_width}",
            )

    def _apply_cursor_line_tag(self) -> None:
        self._text.tag_remove("cursor_line", "1.0", tk.END)
        line_no = int(self._text.index("insert").split(".")[0])
        self._text.tag_add("cursor_line", f"{line_no}.0", f"{line_no}.end+1c")

    def _apply_header_cursor_tag(self) -> None:
        self._header.tag_remove("cursor_byte", "1.0", tk.END)
        offset, _nibble = self._insert_position()
        byte_in_row = offset % BYTES_PER_ROW
        start_col = self._address_column_width + (byte_in_row * 3)
        self._header.tag_add(
            "cursor_byte",
            f"1.{start_col}",
            f"1.{start_col + 2}",
        )

    def _refresh_tags(self) -> None:
        self._apply_address_tags()
        self._apply_changed_tags()
        self._apply_cursor_line_tag()
        self._apply_header_cursor_tag()
        self._text.tag_raise("cursor_line")
        self._text.tag_raise("changed")
        self._header.tag_raise("cursor_byte")

    def _on_focus_in(self, _event) -> str:
        self.after_idle(self._snap_insert_to_editable)
        return None

    def _on_click(self, event) -> str:
        self._text.focus_set()
        index = self._text.index(f"@{event.x},{event.y}")
        offset, nibble = self._nearest_edit_position(index)
        self._move_cursor_to_offset(offset, nibble)
        return "break"

    def _on_key_press(self, event) -> str:
        if not self._editable:
            return "break"
        if event.keysym in {
            "Left",
            "Right",
            "Up",
            "Down",
            "Home",
            "End",
            "Prior",
            "Next",
        }:
            self._handle_navigation(event.keysym, event.state)
            return "break"
        if event.keysym in {"BackSpace", "Delete", "Return"}:
            return "break"
        if event.keysym == "Tab":
            return None

        if len(event.char) != 1 or event.char.upper() not in "0123456789ABCDEF":
            return "break"

        offset, nibble = self._insert_position()
        digit = int(event.char, 16)
        current = self._current_data[offset]
        if nibble == 0:
            updated = (digit << 4) | (current & 0x0F)
        else:
            updated = (current & 0xF0) | digit
        self._current_data[offset] = updated

        if updated == self._original_data[offset]:
            self._changed_offsets.discard(offset)
        else:
            self._changed_offsets.add(offset)

        self._update_row(offset // BYTES_PER_ROW)
        self._notify_change()

        if nibble == 0:
            self._move_cursor_to_offset(offset, 1)
        else:
            next_offset = min(offset + 1, len(self._current_data) - 1)
            self._move_cursor_to_offset(next_offset, 0)
            if self._on_byte_edited is not None:
                self._on_byte_edited(offset, updated, next_offset)
        return "break"

    def _handle_navigation(self, keysym: str, state: int = 0) -> None:
        offset, nibble = self._insert_position()
        if self._mode == MAPPER_MODE:
            if self._handle_mapper_navigation(offset, nibble, keysym, state):
                return
        ctrl_pressed = bool(state & 0x0004)
        if keysym == "Left":
            if nibble == 1:
                nibble = 0
            elif offset > 0:
                offset -= 1
                nibble = 1
        elif keysym == "Right":
            if nibble == 0:
                nibble = 1
            elif offset < len(self._current_data) - 1:
                offset += 1
                nibble = 0
        elif keysym == "Up" and offset >= BYTES_PER_ROW:
            offset -= BYTES_PER_ROW
        elif keysym == "Down" and offset + BYTES_PER_ROW < len(self._current_data):
            offset += BYTES_PER_ROW
        elif keysym == "Home":
            offset -= offset % BYTES_PER_ROW
            nibble = 0
        elif keysym == "End":
            offset = min(
                (offset - (offset % BYTES_PER_ROW)) + (BYTES_PER_ROW - 1),
                len(self._current_data) - 1,
            )
            nibble = 1
        elif keysym == "Prior":
            offset = max(offset - (0x800 if ctrl_pressed else 0x100), 0)
        elif keysym == "Next":
            offset = min(
                offset + (0x800 if ctrl_pressed else 0x100),
                len(self._current_data) - 1,
            )
        self._move_cursor_to_offset(offset, nibble)

    def _handle_mapper_navigation(
        self,
        offset: int,
        nibble: int,
        keysym: str,
        state: int,
    ) -> bool:
        ctrl_pressed = bool(state & 0x0004)
        window_length = len(self._current_data)
        if window_length <= 0:
            return False

        full_offset = (self._mapper_bank * window_length) + offset
        max_full_offset = (0x100 * window_length) - 1
        target_full_offset = full_offset
        target_nibble = nibble
        moved = True

        if keysym == "Left":
            if nibble == 1:
                target_nibble = 0
            elif full_offset > 0:
                target_full_offset -= 1
                target_nibble = 1
            else:
                moved = False
        elif keysym == "Right":
            if nibble == 0:
                target_nibble = 1
            elif full_offset < max_full_offset:
                target_full_offset += 1
                target_nibble = 0
            else:
                moved = False
        elif keysym == "Up":
            if full_offset >= BYTES_PER_ROW:
                target_full_offset -= BYTES_PER_ROW
            else:
                moved = False
        elif keysym == "Down":
            if full_offset + BYTES_PER_ROW <= max_full_offset:
                target_full_offset += BYTES_PER_ROW
            else:
                moved = False
        elif keysym == "Home":
            target_full_offset -= offset % BYTES_PER_ROW
            target_nibble = 0
        elif keysym == "End":
            row_start = full_offset - (offset % BYTES_PER_ROW)
            target_full_offset = min(row_start + (BYTES_PER_ROW - 1), max_full_offset)
            target_nibble = 1
        elif keysym == "Prior":
            delta = 0x800 if ctrl_pressed else 0x100
            if full_offset >= delta:
                target_full_offset -= delta
            else:
                moved = False
        elif keysym == "Next":
            delta = 0x800 if ctrl_pressed else 0x100
            if full_offset + delta <= max_full_offset:
                target_full_offset += delta
            else:
                moved = False
        else:
            return False

        if not moved:
            return True

        target_bank = target_full_offset // window_length
        target_offset = target_full_offset % window_length
        if target_bank == self._mapper_bank:
            self._move_cursor_to_offset(target_offset, target_nibble)
            return True

        if self._on_mapper_bank_move is None:
            return True
        return bool(self._on_mapper_bank_move(target_bank, target_offset, target_nibble))

    def _update_row(self, row_index: int) -> None:
        base = row_index * BYTES_PER_ROW
        line_no = row_index + 1
        line_start = f"{line_no}.0"
        line_end = f"{line_no}.end"
        self._text.delete(line_start, line_end)
        self._text.insert(line_start, self._format_row(base))
        self._refresh_tags()

    def _insert_position(self) -> tuple[int, int]:
        return self._nearest_edit_position(self._text.index("insert"))

    def _nearest_edit_position(self, index: str) -> tuple[int, int]:
        line_str, col_str = index.split(".")
        line = max(int(line_str), 1)
        col = int(col_str)
        max_line = max(1, len(self._current_data) // BYTES_PER_ROW)
        line = min(line, max_line)
        row_offset = (line - 1) * BYTES_PER_ROW
        last_row_bytes = min(BYTES_PER_ROW, len(self._current_data) - row_offset)

        if col < self._address_column_width:
            byte_in_row = 0
            nibble = 0
        elif col >= self._ascii_column_start:
            byte_in_row = min(max(col - self._ascii_column_start, 0), last_row_bytes - 1)
            nibble = 0
        else:
            relative = max(col - self._address_column_width, 0)
            byte_in_row = min(relative // 3, last_row_bytes - 1)
            nibble = 1 if (relative % 3) >= 1 else 0
        return row_offset + byte_in_row, nibble

    def _move_cursor_to_offset(
        self,
        offset: int,
        nibble: int,
        keep_view: bool = False,
    ) -> None:
        offset = min(max(offset, 0), len(self._current_data) - 1)
        line_no = (offset // BYTES_PER_ROW) + 1
        byte_in_row = offset % BYTES_PER_ROW
        column = self._address_column_width + (byte_in_row * 3) + nibble
        index = f"{line_no}.{column}"
        self._text.mark_set("insert", index)
        if not keep_view:
            self._text.see(index)
        self._apply_cursor_line_tag()
        self._apply_header_cursor_tag()
        self._text.tag_raise("cursor_line")
        self._text.tag_raise("changed")
        self._header.tag_raise("cursor_byte")

    def _snap_insert_to_editable(self) -> None:
        offset, nibble = self._insert_position()
        self._move_cursor_to_offset(offset, nibble)

    def _notify_change(self) -> None:
        if self._on_change is not None:
            self._on_change(self.has_changes(), len(self._changed_offsets))

    def _xview(self, *args) -> None:
        self._header.xview(*args)
        self._text.xview(*args)

    def _compute_address_column_width(self) -> int:
        return len(self._format_address(0)) + 2

    def _format_address(self, base: int) -> str:
        if self._mode == MAPPER_MODE:
            full_address = (self._mapper_bank * self._mapper_window_length) + base
            return f"{full_address:06X}:{self._mapper_bank:02X}:{base:04X}"
        return f"{base:04X}"

    def _header_text(self) -> str:
        prefix = FLAT_HEADER_PREFIX if self._mode == FLAT_MODE else MAPPER_HEADER_PREFIX
        return (
            prefix.ljust(self._address_column_width)
            + " ".join(f"+{index:X}" for index in range(BYTES_PER_ROW))
            + "  0123456789ABCDEF"
        )


class AdapterApp(tk.Tk):
    def __init__(self, device: str, debug: bool) -> None:
        super().__init__()
        self.device = ""
        self.selected_device = device
        self.debug = debug
        self.buffer = bytes(BUFFER_SIZE)
        self.flat_buffer = bytes(BUFFER_SIZE)
        self.mapper_buffer = bytes(WINDOW_CONFIGS[DEFAULT_WINDOW][1])
        self._event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._adapter: CartridgeAdapter | None = None
        self._busy = False
        self._access_log_lines: list[str] = []
        self._access_log_dialog: AccessLogDialog | None = None
        self._manual_dialog: ManualCommandDialog | None = None
        self._mapper_map_dialog: MapperMapDialog | None = None
        self._updating_mapper_controls = False
        self._pending_mapper_cursor: tuple[int, int] | None = None
        self._last_analyzed_unique_banks: int | None = None
        self._current_mode = FLAT_MODE
        self._current_mapper_window_length = WINDOW_CONFIGS[DEFAULT_WINDOW][1]
        self._current_mapper_window_start = WINDOW_CONFIGS[DEFAULT_WINDOW][0]
        self._current_mapper_bank = 0

        self.title(APP_TITLE)
        self.geometry("980x760")
        self.minsize(720, 480)

        self.status_var = tk.StringVar(value=self._disconnected_status())
        self.mapper_enabled_var = tk.BooleanVar(value=False)
        self.mapper_type_var = tk.StringVar(value=DEFAULT_MAPPER_TYPE)
        self.window_var = tk.StringVar(value=DEFAULT_WINDOW)
        self.switch_addr_var = tk.StringVar(value=DEFAULT_SWITCH_ADDR)
        self.bank_var = tk.StringVar(value="00")

        self._build_menu()
        self._build_toolbar()
        self._build_mapper_bar()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self.selected_device:
            self.after_idle(lambda: self._start_device_switch(self.selected_device, startup=True))

    def _disconnected_status(self) -> str:
        if self.selected_device:
            return f"device: {self.selected_device} / selected / not connected"
        return "device: not selected / select serial"

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self)

        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="保存", command=self._save_buffer)
        file_menu.add_separator()
        file_menu.add_command(label="終了", command=self._on_close)
        menu_bar.add_cascade(label="ファイル", menu=file_menu)

        settings_menu = tk.Menu(menu_bar, tearoff=False)
        settings_menu.add_command(label="シリアル", command=self._open_serial_dialog)
        menu_bar.add_cascade(label="設定", menu=settings_menu)

        debug_menu = tk.Menu(menu_bar, tearoff=False)
        debug_menu.add_command(label="手動実行", command=self._open_manual_dialog)
        debug_menu.add_command(label="ログ", command=self._open_access_log_dialog)
        menu_bar.add_cascade(label="デバッグ", menu=debug_menu)

        self.configure(menu=menu_bar)

    def _build_toolbar(self) -> None:
        toolbar = ttk.Frame(self, padding=(8, 8, 8, 4))
        toolbar.pack(side="top", fill="x")

        ttk.Button(toolbar, text="💾", width=3, command=self._save_buffer).pack(
            side="left"
        )
        ttk.Button(toolbar, text="⚙", width=3, command=self._open_serial_dialog).pack(
            side="left", padx=(8, 0)
        )
        for label, address in reversed(TOOLBAR_JUMP_ADDRESSES):
            ttk.Button(
                toolbar,
                text=label,
                width=4,
                command=lambda addr=address: self._scroll_to_address(addr),
            ).pack(side="right")

    def _build_mapper_bar(self) -> None:
        mapper_bar = ttk.Frame(self, padding=(8, 0, 8, 4))
        mapper_bar.pack(side="top", fill="x")

        self.mapper_toggle = ttk.Checkbutton(
            mapper_bar,
            text="Mapper",
            variable=self.mapper_enabled_var,
            command=self._on_mapper_mode_toggle,
        )
        self.mapper_toggle.pack(side="left")

        ttk.Label(mapper_bar, text="Type:").pack(side="left", padx=(12, 4))
        self.mapper_type_combo = ttk.Combobox(
            mapper_bar,
            textvariable=self.mapper_type_var,
            values=MAPPER_TYPE_CHOICES,
            state="readonly",
            width=12,
        )
        self.mapper_type_combo.pack(side="left")
        self.mapper_type_combo.bind("<<ComboboxSelected>>", self._on_mapper_type_selected)

        ttk.Label(mapper_bar, text="Window:").pack(side="left", padx=(12, 4))
        self.window_combo = ttk.Combobox(
            mapper_bar,
            textvariable=self.window_var,
            values=WINDOW_CHOICES,
            state="readonly",
            width=16,
        )
        self.window_combo.pack(side="left")
        self.window_combo.bind("<<ComboboxSelected>>", self._on_window_selected)

        ttk.Label(mapper_bar, text="Switch:").pack(side="left", padx=(12, 4))
        self.switch_addr_combo = ttk.Combobox(
            mapper_bar,
            textvariable=self.switch_addr_var,
            values=SWITCH_ADDR_CHOICES,
            width=8,
        )
        self.switch_addr_combo.pack(side="left")
        self.switch_addr_combo.bind("<<ComboboxSelected>>", self._on_switch_addr_changed)
        self.switch_addr_combo.bind("<FocusOut>", self._on_switch_addr_changed)
        self.switch_addr_combo.bind("<Return>", self._on_switch_addr_changed)

        ttk.Label(mapper_bar, text="Bank:").pack(side="left", padx=(12, 4))
        self.bank_spinbox = ttk.Spinbox(
            mapper_bar,
            textvariable=self.bank_var,
            values=tuple(f"{bank:02X}" for bank in range(0x100)),
            width=4,
            wrap=True,
            command=self._on_bank_changed,
        )
        self.bank_spinbox.pack(side="left")
        self.bank_spinbox.bind("<FocusOut>", self._on_bank_changed)
        self.bank_spinbox.bind("<Return>", self._on_bank_changed)

        self.map_button = ttk.Button(
            mapper_bar,
            text="Map",
            command=self._open_mapper_map_dialog,
            width=6,
        )
        self.map_button.pack(side="left", padx=(12, 0))

        self._set_mapper_controls_enabled(False)

    def _build_layout(self) -> None:
        main = ttk.Frame(self, padding=(8, 4, 8, 8))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)

        hex_frame = ttk.Frame(main)
        hex_frame.columnconfigure(0, weight=1)
        hex_frame.rowconfigure(0, weight=1)
        self.hex_viewer = HexViewer(
            hex_frame,
            self.buffer,
            on_change=self._on_hex_change,
            on_byte_edited=self._on_hex_byte_edited,
            on_mapper_bank_move=self._on_mapper_bank_move,
        )
        self.hex_viewer.grid(row=0, column=0, sticky="nsew")
        hex_frame.grid(row=0, column=0, sticky="nsew")

        status = ttk.Frame(main)
        status.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        status.columnconfigure(0, weight=1)
        status.columnconfigure(1, weight=0)

        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(
            status,
            orient="horizontal",
            mode="determinate",
            length=220,
            maximum=0x100,
        )
        self.progress_bar.grid(row=0, column=1, sticky="e")
        self.progress_bar.grid_remove()

    def _not_implemented(self) -> None:
        messagebox.showinfo("未実装", "この機能は次の段階で実装します。")

    def _set_mapper_controls_enabled(self, enabled: bool) -> None:
        state = "readonly" if enabled else "disabled"
        self.mapper_type_combo.configure(state=state)
        self.window_combo.configure(state=state)
        self.switch_addr_combo.configure(state="normal" if enabled else "disabled")
        self.bank_spinbox.configure(state="normal" if enabled else "disabled")
        self._update_map_button_state()

    def _update_map_button_state(self) -> None:
        enabled = (
            self.mapper_enabled_var.get()
            and self._adapter is not None
            and not self._busy
        )
        self.map_button.configure(state="normal" if enabled else "disabled")
        if self._mapper_map_dialog is not None and self._mapper_map_dialog.winfo_exists():
            self._mapper_map_dialog.set_busy(self._busy)

    def _set_progress_visible(self, visible: bool, value: int = 0, maximum: int = 0x100) -> None:
        if visible:
            self.progress_bar.configure(maximum=maximum, value=value)
            self.progress_bar.grid()
        else:
            self.progress_bar.grid_remove()

    def _window_config(self) -> tuple[int, int]:
        return WINDOW_CONFIGS[self.window_var.get()]

    def _switch_addr_value(self) -> int:
        return parse_hex_address(self.switch_addr_var.get())

    def _build_dump_config(self) -> dict[str, object]:
        window_label = self.window_var.get()
        window_start, window_length = WINDOW_CONFIGS[window_label]
        return {
            "mode": MAPPER_MODE if self.mapper_enabled_var.get() else FLAT_MODE,
            "mapper_type": self.mapper_type_var.get(),
            "window_label": window_label,
            "window_start": window_start,
            "window_length": window_length,
            "switch_addr": self._switch_addr_value(),
            "bank": self._bank_value(),
        }

    def _validated_dump_config(self) -> dict[str, object] | None:
        try:
            return self._build_dump_config()
        except ValueError:
            messagebox.showerror(
                "入力エラー",
                "Mapper の設定値が不正です。\nSwitchAddr と Bank を確認してください。",
            )
            return None

    def _bank_value(self) -> int:
        return parse_hex_byte(self.bank_var.get())

    def _apply_mapper_preset(self, mapper_type: str) -> None:
        window_label, switch_addr = MAPPER_PRESETS[mapper_type]
        self._updating_mapper_controls = True
        try:
            self.mapper_type_var.set(mapper_type)
            self.window_var.set(window_label)
            self.switch_addr_var.set(switch_addr)
        finally:
            self._updating_mapper_controls = False

    def _sync_mapper_type_to_custom(self) -> None:
        if self._updating_mapper_controls:
            return
        current = (self.window_var.get(), self.switch_addr_var.get().strip().upper())
        preset = MAPPER_PRESETS.get(self.mapper_type_var.get())
        if preset is None or current != preset:
            self.mapper_type_var.set("Custom")

    def _apply_display_buffer(
        self,
        mode: str,
        data: bytes,
        mapper_window_length: int,
        mapper_window_start: int,
        mapper_bank: int,
    ) -> None:
        self._current_mode = mode
        self._current_mapper_window_length = mapper_window_length
        self._current_mapper_window_start = mapper_window_start
        self._current_mapper_bank = mapper_bank
        self.hex_viewer.configure_mode(
            mode,
            mapper_window_length,
            mapper_bank=mapper_bank,
            mapper_window_start=mapper_window_start,
        )
        self.buffer = data
        self.hex_viewer.set_data(self.buffer)

    def _apply_zero_view(self) -> None:
        self._set_mapper_controls_enabled(self.mapper_enabled_var.get())
        if self.mapper_enabled_var.get():
            mapper_window_start, mapper_window_length = self._window_config()
            mapper_bank = self._bank_value()
            self.mapper_buffer = bytes(mapper_window_length)
            self._apply_display_buffer(
                MAPPER_MODE,
                self.mapper_buffer,
                mapper_window_length,
                mapper_window_start,
                mapper_bank,
            )
        else:
            self.flat_buffer = bytes(BUFFER_SIZE)
            self._apply_display_buffer(FLAT_MODE, self.flat_buffer, BUFFER_SIZE, 0, 0)

    def _apply_loaded_data(self, data: bytes, config: dict[str, object]) -> None:
        mode = str(config["mode"])
        if mode == MAPPER_MODE:
            mapper_window_length = int(config["window_length"])
            mapper_window_start = int(config["window_start"])
            mapper_bank = int(config["bank"])
            self.mapper_enabled_var.set(True)
            self._set_mapper_controls_enabled(True)
            self.bank_var.set(f"{mapper_bank:02X}")
            self.mapper_buffer = data
            self._apply_display_buffer(
                MAPPER_MODE,
                self.mapper_buffer,
                mapper_window_length,
                mapper_window_start,
                mapper_bank,
            )
        else:
            self.mapper_enabled_var.set(False)
            self._set_mapper_controls_enabled(False)
            self.flat_buffer = data
            self._apply_display_buffer(FLAT_MODE, self.flat_buffer, BUFFER_SIZE, 0, 0)

    def _refresh_current_mode_dump(self) -> None:
        if self._busy:
            return
        self._set_mapper_controls_enabled(self.mapper_enabled_var.get())
        if self._adapter is None:
            self._apply_zero_view()
            return
        if self._validated_dump_config() is None:
            return
        self._start_dump_worker()

    def _start_dump_worker(self) -> None:
        if self._adapter is None:
            return
        config = self._build_dump_config()
        self._set_busy(True)
        if config["mode"] == MAPPER_MODE:
            self._current_mapper_window_length = int(config["window_length"])
            self.status_var.set(f"device: {self.device} / mapper dumping")
            self._set_progress_visible(True, 0, int(config["window_length"]))
            worker = threading.Thread(
                target=self._mapper_dump_worker,
                args=(config,),
                daemon=True,
            )
        else:
            self.status_var.set(f"device: {self.device} / reading")
            self._set_progress_visible(False)
            worker = threading.Thread(
                target=self._flat_dump_worker,
                daemon=True,
            )
        worker.start()
        self.after(50, self._poll_events)

    def _on_mapper_mode_toggle(self) -> None:
        self._refresh_current_mode_dump()

    def _on_mapper_type_selected(self, _event=None) -> None:
        if self._updating_mapper_controls:
            return
        self._apply_mapper_preset(self.mapper_type_var.get())
        if self.mapper_enabled_var.get():
            self._refresh_current_mode_dump()
        else:
            self._set_mapper_controls_enabled(False)

    def _on_window_selected(self, _event=None) -> None:
        self._sync_mapper_type_to_custom()
        if self.mapper_enabled_var.get():
            self._refresh_current_mode_dump()

    def _on_switch_addr_changed(self, _event=None) -> str | None:
        value = self.switch_addr_var.get().strip().upper()
        if value:
            self.switch_addr_var.set(value)
        self._sync_mapper_type_to_custom()
        if self.mapper_enabled_var.get():
            try:
                self._switch_addr_value()
            except ValueError:
                messagebox.showerror("入力エラー", "SwitchAddr は16進数で入力してください。")
                return "break"
            self._refresh_current_mode_dump()
        return None

    def _on_bank_changed(self, _event=None) -> str | None:
        value = self.bank_var.get().strip().upper()
        if value:
            self.bank_var.set(value)
        if self.mapper_enabled_var.get():
            try:
                self._bank_value()
            except ValueError:
                messagebox.showerror("入力エラー", "Bank は00-FFの16進数で入力してください。")
                return "break"
            self._refresh_current_mode_dump()
        return None

    def _open_access_log_dialog(self) -> None:
        if self._access_log_dialog is None or not self._access_log_dialog.winfo_exists():
            self._access_log_dialog = AccessLogDialog(self, self._access_log_lines)
            self._access_log_dialog.protocol(
                "WM_DELETE_WINDOW",
                self._close_access_log_dialog,
            )
        else:
            self._access_log_dialog.lift()
            self._access_log_dialog.focus_set()

    def _close_access_log_dialog(self) -> None:
        if self._access_log_dialog is not None and self._access_log_dialog.winfo_exists():
            self._access_log_dialog.destroy()
        self._access_log_dialog = None

    def _open_manual_dialog(self) -> None:
        if self._manual_dialog is None or not self._manual_dialog.winfo_exists():
            self._manual_dialog = ManualCommandDialog(self, self._submit_manual_command)
            self._manual_dialog.protocol(
                "WM_DELETE_WINDOW",
                self._close_manual_dialog,
            )
            self._manual_dialog.set_busy(self._busy or self._adapter is None)
        else:
            self._manual_dialog.lift()
            self._manual_dialog.focus_entry()

    def _close_manual_dialog(self) -> None:
        if self._manual_dialog is not None and self._manual_dialog.winfo_exists():
            self._manual_dialog.destroy()
        self._manual_dialog = None

    def _open_mapper_map_dialog(self) -> None:
        if self._mapper_map_dialog is None or not self._mapper_map_dialog.winfo_exists():
            self._mapper_map_dialog = MapperMapDialog(
                self,
                self._start_mapper_map_analysis,
                self._select_mapper_map_bank,
            )
            self._mapper_map_dialog.protocol(
                "WM_DELETE_WINDOW",
                self._close_mapper_map_dialog,
            )
            self._mapper_map_dialog.set_busy(self._busy)
        else:
            self._mapper_map_dialog.lift()
            self._mapper_map_dialog.focus_set()

    def _close_mapper_map_dialog(self) -> None:
        if self._mapper_map_dialog is not None and self._mapper_map_dialog.winfo_exists():
            self._mapper_map_dialog.destroy()
        self._mapper_map_dialog = None

    def _select_mapper_map_bank(self, bank: int) -> None:
        if self._busy:
            return
        self.bank_var.set(f"{bank:02X}")
        self._pending_mapper_cursor = (0, 0)
        if self._adapter is None:
            self._apply_zero_view()
            self.hex_viewer.set_cursor_position(0, 0)
            self.hex_viewer.focus_editor()
            return
        self._start_dump_worker()

    def _scroll_to_address(self, address: int) -> None:
        self.hex_viewer.scroll_to_address(address)

    def _start_mapper_map_analysis(self) -> None:
        if self._busy or self._adapter is None:
            return
        config = self._validated_dump_config()
        if config is None or config["mode"] != MAPPER_MODE:
            return
        if self._mapper_map_dialog is not None and self._mapper_map_dialog.winfo_exists():
            self._mapper_map_dialog.reset_cells()
            self._mapper_map_dialog.set_busy(True)
        self._set_busy(True)
        self.status_var.set(f"device: {self.device} / mapper analyze")
        self._set_progress_visible(True, 0, 0x100)
        worker = threading.Thread(
            target=self._mapper_map_worker,
            args=(config,),
            daemon=True,
        )
        worker.start()
        self.after(50, self._poll_events)

    def _on_mapper_bank_move(
        self,
        target_bank: int,
        target_offset: int,
        target_nibble: int,
    ) -> bool:
        if self._busy:
            return False
        self.bank_var.set(f"{target_bank:02X}")
        self._pending_mapper_cursor = (target_offset, target_nibble)
        if self._adapter is None:
            self._apply_zero_view()
            self.hex_viewer.set_cursor_position(target_offset, target_nibble)
            self.hex_viewer.focus_editor()
            return True
        self._start_dump_worker()
        return True

    def _save_buffer(self) -> None:
        data = self.hex_viewer.get_data()
        if not data:
            messagebox.showwarning("保存不可", "保存できるデータがありません。")
            return

        analyzed_end_bank = "FF"
        if self._last_analyzed_unique_banks is not None and self._last_analyzed_unique_banks > 0:
            analyzed_end_bank = f"{min(self._last_analyzed_unique_banks - 1, 0xFF):02X}"
        dialog = SaveBufferDialog(
            self,
            initial_mode="mapper" if self.mapper_enabled_var.get() else "flat",
            mapper_defaults={
                "mapper_type": self.mapper_type_var.get(),
                "window_label": self.window_var.get(),
                "switch_addr": self.switch_addr_var.get().strip().upper(),
            },
            analyzed_end_bank=analyzed_end_bank,
        )
        result = dialog.show()
        if result is None:
            return
        if result["mode"] == "flat":
            start_address = int(result["start_address"])
            end_address = int(result["end_address"])
            save_data = data[start_address : end_address + 1]

            try:
                with open(str(result["path"]), "wb") as handle:
                    handle.write(save_data)
            except OSError as exc:
                messagebox.showerror("保存失敗", str(exc))
                return

            self.status_var.set(f"device: {self.device} / saved / {result['path']}")
            self._append_access_log(
                "APP: saved "
                f"{len(save_data)} bytes to {result['path']} "
                f"({start_address:04X}-{end_address:04X})"
            )
            return

        if self._adapter is None:
            messagebox.showerror("保存失敗", "Mapper 保存には接続中の Adapter が必要です。")
            return
        self._set_busy(True)
        self.status_var.set(f"device: {self.device} / mapper saving")
        bank_count = int(result["end_bank"]) - int(result["start_bank"]) + 1
        self._set_progress_visible(True, 0, bank_count)
        worker = threading.Thread(
            target=self._mapper_save_worker,
            args=(result,),
            daemon=True,
        )
        worker.start()
        self.after(50, self._poll_events)

    def _open_serial_dialog(self) -> None:
        if self._busy:
            return
        if self.hex_viewer.has_changes():
            confirmed = messagebox.askyesno(
                "確認",
                "未保存の編集内容があります。\nシリアルデバイスを切り替えると表示内容は破棄されます。\n続行しますか。",
            )
            if not confirmed:
                return

        dialog = SerialDeviceDialog(self, self.selected_device)
        selected_device = dialog.show()
        if selected_device is None:
            return
        self.selected_device = selected_device
        if self._adapter is not None and selected_device == self.device:
            return
        self._start_device_switch(selected_device)

    def _append_access_log(self, line: str) -> None:
        self._access_log_lines.append(line)
        if self._access_log_dialog is not None and self._access_log_dialog.winfo_exists():
            self._access_log_dialog.set_lines(self._access_log_lines)

    def _append_manual_log(self, line: str) -> None:
        if self._manual_dialog is not None and self._manual_dialog.winfo_exists():
            self._manual_dialog.append_log(line)

    def _on_hex_change(self, changed: bool, count: int) -> None:
        marker = "*" if changed else ""
        self.title(f"{marker}{APP_TITLE}")
        if changed:
            self.status_var.set(f"device: {self.device} / edited / {count} bytes changed")

    def _on_hex_byte_edited(self, offset: int, value: int, next_offset: int) -> None:
        if self._busy or self._adapter is None:
            return
        worker_config = self._validated_dump_config()
        if worker_config is None:
            return
        self._set_busy(True)
        self.status_var.set(
            f"device: {self.device} / direct write / {offset:04X}={value:02X}"
        )
        if worker_config["mode"] == MAPPER_MODE:
            worker = threading.Thread(
                target=self._mapper_direct_write_worker,
                args=(offset, value, next_offset, worker_config),
                daemon=True,
            )
        else:
            worker = threading.Thread(
                target=self._direct_write_worker,
                args=(offset, value, next_offset),
                daemon=True,
            )
        worker.start()
        self.after(50, self._poll_events)

    def _start_device_switch(self, device: str, startup: bool = False) -> None:
        config = self._validated_dump_config()
        if config is None:
            return
        self._set_busy(True)
        self.status_var.set(f"device: {device} / connecting")
        if config["mode"] == MAPPER_MODE:
            self._current_mapper_window_length = int(config["window_length"])
            self._set_progress_visible(True, 0, int(config["window_length"]))
        else:
            self._set_progress_visible(False)
        worker = threading.Thread(
            target=self._device_switch_worker,
            args=(device, startup, config),
            daemon=True,
        )
        worker.start()
        self.after(50, self._poll_events)

    def _device_switch_worker(self, device: str, startup: bool, config: dict[str, object]) -> None:
        started_at = time.monotonic()
        self._publish_app_log(f"device switch start {device}")
        try:
            adapter = CartridgeAdapter(
                device,
                debug=self.debug,
                log_callback=self._publish_log,
            )
            self._publish_app_log(
                f"device switch open ok {device} ({time.monotonic() - started_at:.3f}s)"
            )
        except Exception as exc:
            self._publish_app_log(
                f"device switch open error {device} ({time.monotonic() - started_at:.3f}s): {exc}"
            )
            event_name = "error" if startup else "switch_error"
            self._event_queue.put((event_name, str(exc)))
            return

        try:
            self._event_queue.put(("status", f"device: {device} / HVER"))
            hver_lines = adapter.get_version_info()
            self._publish_app_log(
                f"HVER ok {device} ({time.monotonic() - started_at:.3f}s)"
            )
            payload = self._load_data_for_config(adapter, config)
            self._publish_app_log(
                f"initial load ok {device} ({time.monotonic() - started_at:.3f}s)"
            )
            self._event_queue.put(("loaded", (device, adapter, hver_lines, payload, config)))
        except Exception as exc:
            adapter.close()
            self._publish_app_log(
                f"device switch init error {device} ({time.monotonic() - started_at:.3f}s): {exc}"
            )
            event_name = "error" if startup else "switch_error"
            self._event_queue.put((event_name, str(exc)))

    def _flat_dump_worker(self) -> None:
        try:
            payload = self._adapter.read_cartridge_64kb(self._publish_progress)
            config = self._build_dump_config()
            self._event_queue.put(("reloaded", (payload, config)))
        except Exception as exc:
            self._event_queue.put(("switch_error", str(exc)))

    def _mapper_dump_worker(self, config: dict[str, object]) -> None:
        try:
            payload = self._load_data_for_config(self._adapter, config)
            self._event_queue.put(("reloaded", (payload, config)))
        except Exception as exc:
            self._event_queue.put(("switch_error", str(exc)))

    def _load_data_for_config(self, adapter: CartridgeAdapter, config: dict[str, object]) -> bytes:
        if config["mode"] == MAPPER_MODE:
            self._event_queue.put(
                (
                    "status",
                    f"device: {adapter.device} / mapper dumping / bank {int(config['bank']):02X}",
                )
            )
            return adapter.read_mapper_bank(
                int(config["bank"]),
                int(config["switch_addr"]),
                int(config["window_start"]),
                int(config["window_length"]),
                self._publish_mapper_progress,
            )
        self._event_queue.put(("status", f"device: {adapter.device} / reading"))
        return adapter.read_cartridge_64kb(self._publish_progress)

    def _submit_manual_command(self, command: str) -> None:
        if not command or self._busy or self._adapter is None:
            return
        self._set_busy(True)
        self.status_var.set(f"device: {self.device} / running {command}")
        worker = threading.Thread(
            target=self._command_worker,
            args=(command, "manual"),
            daemon=True,
        )
        worker.start()
        self.after(50, self._poll_events)

    def _command_worker(self, command: str, source: str) -> None:
        try:
            response = self._adapter.run_command(command)
            self._event_queue.put(("command_done", (command, response, source)))
        except Exception as exc:
            self._event_queue.put(("command_error", (command, str(exc), source)))

    def _direct_write_worker(self, offset: int, value: int, next_offset: int) -> None:
        try:
            payload = self._adapter.write_byte_and_read_cartridge_64kb(
                offset,
                value,
                self._publish_progress,
            )
            self._event_queue.put(
                ("flat_direct_write_done", (offset, value, next_offset, payload))
            )
        except Exception as exc:
            self._event_queue.put(("direct_write_error", (offset, value, str(exc))))

    def _mapper_direct_write_worker(
        self,
        offset: int,
        value: int,
        next_offset: int,
        config: dict[str, object],
    ) -> None:
        try:
            window_length = int(config["window_length"])
            bank = int(config["bank"])
            bank_offset = offset
            payload = self._adapter.write_mapper_byte_and_read_bank(
                bank,
                int(config["switch_addr"]),
                int(config["window_start"]),
                window_length,
                bank_offset,
                value,
            )
            self._event_queue.put(
                ("mapper_direct_write_done", (offset, value, next_offset, bank, payload, config))
            )
        except Exception as exc:
            self._event_queue.put(("direct_write_error", (offset, value, str(exc))))

    def _mapper_map_worker(self, config: dict[str, object]) -> None:
        try:
            hashes: dict[str, int] = {}
            consecutive_unique_count = 0
            for bank in range(0x100):
                payload = self._adapter.read_mapper_bank(
                    bank,
                    int(config["switch_addr"]),
                    int(config["window_start"]),
                    int(config["window_length"]),
                )
                digest = hashlib.sha1(payload).hexdigest()
                before = hashes.get(digest)
                if before is None:
                    hashes[digest] = bank
                    if bank == consecutive_unique_count:
                        consecutive_unique_count += 1
                self._event_queue.put(("mapper_map_progress", (bank + 1, 0x100)))
                self._event_queue.put(("mapper_map_cell", (bank, before)))
            image_kbyte = (consecutive_unique_count * int(config["window_length"])) // 1024
            self._event_queue.put(("mapper_map_done", (image_kbyte, consecutive_unique_count)))
        except Exception as exc:
            self._event_queue.put(("mapper_map_error", str(exc)))

    def _mapper_save_worker(self, config: dict[str, object]) -> None:
        try:
            window_start, window_length = WINDOW_CONFIGS[str(config["window_label"])]
            start_bank = int(config["start_bank"])
            end_bank = int(config["end_bank"])
            switch_addr = int(config["switch_addr"])
            dump = bytearray()
            total_banks = end_bank - start_bank + 1
            for offset, bank in enumerate(range(start_bank, end_bank + 1), start=1):
                payload = self._adapter.read_mapper_bank(
                    bank,
                    switch_addr,
                    window_start,
                    window_length,
                )
                dump.extend(payload)
                self._event_queue.put(("mapper_save_progress", (offset, total_banks)))
            with open(str(config["path"]), "wb") as handle:
                handle.write(dump)
            self._event_queue.put(
                (
                    "mapper_save_done",
                    (
                        str(config["path"]),
                        len(dump),
                        start_bank,
                        end_bank,
                        str(config["mapper_type"]),
                        str(config["window_label"]),
                        switch_addr,
                    ),
                )
            )
        except Exception as exc:
            self._event_queue.put(("mapper_save_error", str(exc)))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if self._manual_dialog is not None and self._manual_dialog.winfo_exists():
            self._manual_dialog.set_busy(busy or self._adapter is None)
        if self._mapper_map_dialog is not None and self._mapper_map_dialog.winfo_exists():
            self._mapper_map_dialog.set_busy(busy)
        self.hex_viewer.set_editable(not busy)
        self.mapper_toggle.configure(state="disabled" if busy else "normal")
        if busy:
            self.mapper_type_combo.configure(state="disabled")
            self.window_combo.configure(state="disabled")
            self.switch_addr_combo.configure(state="disabled")
            self.bank_spinbox.configure(state="disabled")
        else:
            self._set_mapper_controls_enabled(self.mapper_enabled_var.get())

    def _publish_progress(self, last_address: int) -> None:
        return None

    def _publish_mapper_progress(self, last_address: int) -> None:
        self._event_queue.put(("mapper_progress", (last_address + 1, self._current_mapper_window_length)))

    def _publish_log(self, direction: str, payload: str) -> None:
        self._event_queue.put(("log", f"{direction}: {payload}"))

    def _publish_app_log(self, payload: str) -> None:
        self._publish_log("APP", payload)

    def _poll_events(self) -> None:
        keep_polling = True
        while True:
            try:
                event, payload = self._event_queue.get_nowait()
            except queue.Empty:
                break

            if event == "status":
                self.status_var.set(str(payload))
            elif event == "mapper_progress":
                done_bytes, total_bytes = payload
                self._set_progress_visible(True, done_bytes, total_bytes)
            elif event == "mapper_map_progress":
                done_banks, total_banks = payload
                self._set_progress_visible(True, done_banks, total_banks)
            elif event == "mapper_map_cell":
                bank, before = payload
                if self._mapper_map_dialog is not None and self._mapper_map_dialog.winfo_exists():
                    if before is None:
                        self._mapper_map_dialog.mark_unique(bank)
                    else:
                        self._mapper_map_dialog.mark_mirror(bank, before)
            elif event == "mapper_map_done":
                image_kbyte, unique_banks = payload
                self._last_analyzed_unique_banks = int(unique_banks)
                self.status_var.set(f"device: {self.device} / mapper analyze done")
                if self._mapper_map_dialog is not None and self._mapper_map_dialog.winfo_exists():
                    window_kbyte = self._current_mapper_window_length // 1024
                    bank_count = int(unique_banks)
                    self._mapper_map_dialog.set_image_size(
                        f"{bank_count} Bank * {window_kbyte}kB = {int(image_kbyte)} kByte"
                    )
                self._set_progress_visible(False)
                self._set_busy(False)
            elif event == "mapper_map_error":
                self.status_var.set(f"device: {self.device} / mapper analyze error")
                self._set_progress_visible(False)
                self._set_busy(False)
                self.after(
                    0,
                    lambda m=str(payload): messagebox.showerror(
                        "Mapper 解析失敗",
                        m,
                    ),
                )
            elif event == "mapper_save_progress":
                done_banks, total_banks = payload
                self._set_progress_visible(True, done_banks, total_banks)
            elif event == "mapper_save_done":
                path, size, start_bank, end_bank, mapper_type, window_label, switch_addr = payload
                self.status_var.set(f"device: {self.device} / saved / {path}")
                self._set_progress_visible(False)
                self._set_busy(False)
                self._append_access_log(
                    "APP: saved "
                    f"{size} bytes to {path} "
                    f"(mapper {mapper_type}, {window_label}, switch {switch_addr:04X}, "
                    f"bank {start_bank:02X}-{end_bank:02X})"
                )
            elif event == "mapper_save_error":
                self.status_var.set(f"device: {self.device} / mapper save error")
                self._set_progress_visible(False)
                self._set_busy(False)
                self.after(
                    0,
                    lambda m=str(payload): messagebox.showerror(
                        "保存失敗",
                        m,
                    ),
                )
            elif event == "log":
                self._append_access_log(str(payload))
            elif event == "loaded":
                device, adapter, hver_lines, data, config = payload
                if self._adapter is not None:
                    self._adapter.close()
                self._adapter = adapter
                self.device = device
                self.selected_device = device
                self._apply_loaded_data(data, config)
                self.status_var.set(
                    f"device: {self.device} / ready / {' | '.join(hver_lines)}"
                )
                if self._pending_mapper_cursor is not None and self._current_mode == MAPPER_MODE:
                    offset, nibble = self._pending_mapper_cursor
                    self.hex_viewer.set_cursor_position(offset, nibble)
                    self.hex_viewer.focus_editor()
                self._pending_mapper_cursor = None
                self._set_progress_visible(False)
                self._set_mapper_controls_enabled(self.mapper_enabled_var.get())
                self._set_busy(False)
            elif event == "reloaded":
                data, config = payload
                self._apply_loaded_data(data, config)
                self.status_var.set(f"device: {self.device} / ready")
                if self._pending_mapper_cursor is not None and self._current_mode == MAPPER_MODE:
                    offset, nibble = self._pending_mapper_cursor
                    self.hex_viewer.set_cursor_position(offset, nibble)
                    self.hex_viewer.focus_editor()
                self._pending_mapper_cursor = None
                self._set_progress_visible(False)
                self._set_mapper_controls_enabled(self.mapper_enabled_var.get())
                self._set_busy(False)
            elif event == "command_done":
                command, response, source = payload
                self.status_var.set(f"device: {self.device} / ready / {command}")
                if source == "manual":
                    self._append_manual_log(f"> {command}")
                    if response:
                        for line in response:
                            self._append_manual_log(f"< {line}")
                    self._append_manual_log("< OK")
                self._set_busy(False)
            elif event == "command_error":
                command, message, source = payload
                self.status_var.set(f"device: {self.device} / command error")
                if source == "manual":
                    self._append_manual_log(f"> {command}")
                    self._append_manual_log(f"< FAIL: {message}")
                self._set_busy(False)
                self.after(
                    0,
                    lambda c=command, m=message: messagebox.showerror(
                        "コマンド失敗", f"{c}\n\n{m}"
                    ),
                )
            elif event == "flat_direct_write_done":
                offset, value, next_offset, data = payload
                self.flat_buffer = data
                self.buffer = data
                self.hex_viewer.set_data(
                    data,
                    cursor_offset=next_offset,
                    cursor_nibble=0,
                    keep_view=True,
                )
                self.hex_viewer.focus_editor()
                self.status_var.set(
                    f"device: {self.device} / direct write done / {offset:04X}={value:02X}"
                )
                self._set_busy(False)
            elif event == "mapper_direct_write_done":
                offset, value, next_offset, bank, payload, config = payload
                self.mapper_buffer = payload
                self.buffer = self.mapper_buffer
                self.hex_viewer.set_data(
                    payload,
                    cursor_offset=next_offset,
                    cursor_nibble=0,
                    keep_view=True,
                )
                self.hex_viewer.focus_editor()
                self.status_var.set(
                    f"device: {self.device} / direct write done / bank {bank:02X} / {offset:04X}={value:02X}"
                )
                self._set_progress_visible(False)
                self._set_busy(False)
            elif event == "direct_write_error":
                offset, value, message = payload
                self.status_var.set(f"device: {self.device} / direct write error")
                self._set_progress_visible(False)
                self._set_busy(False)
                self.after(
                    0,
                    lambda a=offset, v=value, m=message: messagebox.showerror(
                        "直接書き込み失敗",
                        f"{a:04X}={v:02X}\n\n{m}",
                    ),
                )
            elif event == "error":
                if self.selected_device:
                    self.status_var.set(f"device: {self.selected_device} / error")
                else:
                    self.status_var.set("device: not selected / error")
                self._set_progress_visible(False)
                self._set_busy(False)
                self.after(
                    0,
                    lambda m=str(payload): messagebox.showerror(
                        "起動失敗",
                        m,
                    ),
                )
            elif event == "switch_error":
                if self._adapter is None:
                    self.status_var.set(self._disconnected_status())
                else:
                    self.status_var.set(f"device: {self.device} / ready")
                self._set_progress_visible(False)
                self._set_mapper_controls_enabled(self.mapper_enabled_var.get())
                self._pending_mapper_cursor = None
                self._set_busy(False)
                self.after(
                    0,
                    lambda m=str(payload): messagebox.showerror(
                        "シリアル接続失敗",
                        m,
                    ),
                )

        if keep_polling:
            self.after(50, self._poll_events)

    def _show_startup_error(self, message: str) -> None:
        messagebox.showerror("起動失敗", message)
        self.destroy()

    def _on_close(self) -> None:
        if self._adapter is not None:
            self._adapter.close()
            self._adapter = None
        self.destroy()


def main() -> None:
    args = parse_args()
    app = AdapterApp(device=args.device, debug=args.debug)
    app.mainloop()


if __name__ == "__main__":
    main()
