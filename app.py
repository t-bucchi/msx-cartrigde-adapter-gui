import argparse
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
ADDRESS_COLUMN_WIDTH = 6
HEX_COLUMN_WIDTH = (BYTES_PER_ROW * 3) - 1
ASCII_COLUMN_START = ADDRESS_COLUMN_WIDTH + HEX_COLUMN_WIDTH + 2
RULER_TEXT = (
    "ADDR  "
    + " ".join(f"+{index:X}" for index in range(BYTES_PER_ROW))
    + "  0123456789ABCDEF"
)
SAVE_START_CHOICES = ("0000", "4000", "8000", "C000")
SAVE_END_CHOICES = ("3FFF", "7FFF", "BFFF", "FFFF")
DEFAULT_SLOT = 1
TOOLBAR_JUMP_ADDRESSES = (
    ("00", 0x0000),
    ("40", 0x4000),
    ("80", 0x8000),
    ("C0", 0xC000),
    ("FF", 0xFFFF),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MSXPLAYer Game Cartidge Adapter GUI"
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
    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self.result: tuple[str, int, int] | None = None

        self.title("バッファ保存")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.path_var = tk.StringVar(value="msx_adapter_dump.bin")
        self.start_var = tk.StringVar(value=SAVE_START_CHOICES[0])
        self.end_var = tk.StringVar(value=SAVE_END_CHOICES[-1])
        self.error_var = tk.StringVar(value="")

        self.columnconfigure(0, weight=1)
        frame = ttk.Frame(self, padding=12)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="ファイル名").grid(row=0, column=0, sticky="w")
        path_entry = ttk.Entry(frame, textvariable=self.path_var, width=40)
        path_entry.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(frame, text="参照", command=self._browse).grid(row=0, column=2)

        ttk.Label(frame, text="開始アドレス").grid(row=1, column=0, sticky="w", pady=(10, 0))
        start_combo = ttk.Combobox(
            frame,
            textvariable=self.start_var,
            values=SAVE_START_CHOICES,
            width=10,
        )
        start_combo.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        ttk.Label(frame, text="終了アドレス").grid(row=2, column=0, sticky="w", pady=(10, 0))
        end_combo = ttk.Combobox(
            frame,
            textvariable=self.end_var,
            values=SAVE_END_CHOICES,
            width=10,
        )
        end_combo.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        error_label = ttk.Label(frame, textvariable=self.error_var, foreground="#c62828")
        error_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))

        button_frame = ttk.Frame(frame)
        button_frame.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(button_frame, text="キャンセル", command=self._cancel).grid(row=0, column=0)
        ttk.Button(button_frame, text="保存", command=self._submit).grid(row=0, column=1, padx=(8, 0))

        self.bind("<Return>", lambda _event: self._submit())
        self.bind("<Escape>", lambda _event: self._cancel())
        path_entry.focus_set()

    def show(self) -> tuple[str, int, int] | None:
        self.wait_window()
        return self.result

    def _browse(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self,
            title="保存先選択",
            defaultextension=".bin",
            filetypes=[
                ("Binary", "*.bin"),
                ("ROM", "*.rom"),
                ("All files", "*"),
            ],
            initialfile=self.path_var.get() or "msx_adapter_dump.bin",
        )
        if path:
            self.path_var.set(path)

    def _submit(self) -> None:
        path = self.path_var.get().strip()
        start_text = self.start_var.get().strip().upper()
        end_text = self.end_var.get().strip().upper()

        if not path:
            self.error_var.set("ファイル名を指定してください。")
            return

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

        self.result = (path, start_address, end_address)
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


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


class HexViewer(ttk.Frame):
    def __init__(
        self,
        master: tk.Misc,
        data: bytes,
        on_change=None,
        on_byte_edited=None,
    ) -> None:
        super().__init__(master)
        self._on_change = on_change
        self._on_byte_edited = on_byte_edited
        self._original_data = bytearray(data)
        self._current_data = bytearray(data)
        self._changed_offsets: set[int] = set()
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
        self._header.insert("1.0", RULER_TEXT)
        self._header.tag_add("address_bg", "1.0", f"1.{ADDRESS_COLUMN_WIDTH}")
        self._header.configure(state="disabled")
        self._text.delete("1.0", tk.END)
        self._text.insert("1.0", "\n".join(lines))
        self._refresh_tags()

    def _format_row(self, base: int) -> str:
        row = self._current_data[base : base + BYTES_PER_ROW]
        hex_part = " ".join(f"{value:02X}" for value in row)
        ascii_part = "".join(to_printable(value) for value in row)
        return f"{base:04X}  {hex_part:<{HEX_COLUMN_WIDTH}}  {ascii_part}"

    def _apply_changed_tags(self) -> None:
        self._text.tag_remove("changed", "1.0", tk.END)
        for offset in self._changed_offsets:
            line_no = (offset // BYTES_PER_ROW) + 1
            byte_in_row = offset % BYTES_PER_ROW
            hex_col = ADDRESS_COLUMN_WIDTH + (byte_in_row * 3)
            ascii_col = ASCII_COLUMN_START + byte_in_row
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
            self._text.tag_add("address_bg", f"{line_no}.0", f"{line_no}.{ADDRESS_COLUMN_WIDTH}")

    def _apply_cursor_line_tag(self) -> None:
        self._text.tag_remove("cursor_line", "1.0", tk.END)
        line_no = int(self._text.index("insert").split(".")[0])
        self._text.tag_add("cursor_line", f"{line_no}.0", f"{line_no}.end+1c")

    def _apply_header_cursor_tag(self) -> None:
        self._header.tag_remove("cursor_byte", "1.0", tk.END)
        offset, _nibble = self._insert_position()
        byte_in_row = offset % BYTES_PER_ROW
        start_col = ADDRESS_COLUMN_WIDTH + (byte_in_row * 3)
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

        if col < ADDRESS_COLUMN_WIDTH:
            byte_in_row = 0
            nibble = 0
        elif col >= ASCII_COLUMN_START:
            byte_in_row = min(max(col - ASCII_COLUMN_START, 0), last_row_bytes - 1)
            nibble = 0
        else:
            relative = max(col - ADDRESS_COLUMN_WIDTH, 0)
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
        column = ADDRESS_COLUMN_WIDTH + (byte_in_row * 3) + nibble
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


class AdapterApp(tk.Tk):
    def __init__(self, device: str, debug: bool) -> None:
        super().__init__()
        self.device = ""
        self.selected_device = device
        self.debug = debug
        self.buffer = bytes(BUFFER_SIZE)
        self._event_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._adapter: CartridgeAdapter | None = None
        self._busy = False
        self._access_log_lines: list[str] = []
        self._access_log_dialog: AccessLogDialog | None = None
        self._manual_dialog: ManualCommandDialog | None = None

        self.title("MSXPLAYer Game Cartidge Adapter")
        self.geometry("980x760")
        self.minsize(720, 480)

        self.status_var = tk.StringVar(value="device: not selected / select serial")

        self._build_menu()
        self._build_toolbar()
        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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
        )
        self.hex_viewer.grid(row=0, column=0, sticky="nsew")
        hex_frame.grid(row=0, column=0, sticky="nsew")

        status = ttk.Frame(main)
        status.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        status.columnconfigure(0, weight=1)

        ttk.Label(status, textvariable=self.status_var).grid(row=0, column=0, sticky="w")

    def _not_implemented(self) -> None:
        messagebox.showinfo("未実装", "この機能は次の段階で実装します。")

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

    def _scroll_to_address(self, address: int) -> None:
        self.hex_viewer.scroll_to_address(address)

    def _save_buffer(self) -> None:
        data = self.hex_viewer.get_data()
        if not data:
            messagebox.showwarning("保存不可", "保存できるデータがありません。")
            return

        dialog = SaveBufferDialog(self)
        result = dialog.show()
        if result is None:
            return
        path, start_address, end_address = result
        save_data = data[start_address : end_address + 1]

        try:
            with open(path, "wb") as handle:
                handle.write(save_data)
        except OSError as exc:
            messagebox.showerror("保存失敗", str(exc))
            return

        self.status_var.set(f"device: {self.device} / saved / {path}")
        self._append_access_log(
            "APP: saved "
            f"{len(save_data)} bytes to {path} "
            f"({start_address:04X}-{end_address:04X})"
        )

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
        self.title(f"{marker}MSXPLAYer Game Cartidge Adapter")
        if changed:
            self.status_var.set(f"device: {self.device} / edited / {count} bytes changed")

    def _on_hex_byte_edited(self, offset: int, value: int, next_offset: int) -> None:
        if self._busy or self._adapter is None:
            return
        self._set_busy(True)
        self.status_var.set(
            f"device: {self.device} / direct write / {offset:04X}={value:02X}"
        )
        worker = threading.Thread(
            target=self._direct_write_worker,
            args=(offset, value, next_offset),
            daemon=True,
        )
        worker.start()
        self.after(50, self._poll_events)

    def _start_device_switch(self, device: str, startup: bool = False) -> None:
        self._set_busy(True)
        self.status_var.set(f"device: {device} / connecting")
        worker = threading.Thread(
            target=self._device_switch_worker,
            args=(device, startup),
            daemon=True,
        )
        worker.start()
        self.after(50, self._poll_events)

    def _device_switch_worker(self, device: str, startup: bool) -> None:
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
            self._event_queue.put(("status", f"device: {device} / reading"))
            payload = adapter.read_cartridge_64kb(self._publish_progress)
            self._publish_app_log(
                f"initial read ok {device} ({time.monotonic() - started_at:.3f}s)"
            )
            self._event_queue.put(("loaded", (device, adapter, hver_lines, payload)))
        except Exception as exc:
            adapter.close()
            self._publish_app_log(
                f"device switch init error {device} ({time.monotonic() - started_at:.3f}s): {exc}"
            )
            event_name = "error" if startup else "switch_error"
            self._event_queue.put((event_name, str(exc)))

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
                ("direct_write_done", (offset, value, next_offset, payload))
            )
        except Exception as exc:
            self._event_queue.put(("direct_write_error", (offset, value, str(exc))))

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if self._manual_dialog is not None and self._manual_dialog.winfo_exists():
            self._manual_dialog.set_busy(busy or self._adapter is None)
        self.hex_viewer.set_editable(not busy)

    def _publish_progress(self, last_address: int) -> None:
        return None

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
            elif event == "log":
                self._append_access_log(str(payload))
            elif event == "loaded":
                device, adapter, hver_lines, data = payload
                if self._adapter is not None:
                    self._adapter.close()
                self._adapter = adapter
                self.device = device
                self.selected_device = device
                self.buffer = data
                self.hex_viewer.set_data(self.buffer)
                self.status_var.set(
                    f"device: {self.device} / ready / {' | '.join(hver_lines)}"
                )
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
            elif event == "direct_write_done":
                offset, value, next_offset, data = payload
                self.buffer = data
                self.hex_viewer.set_data(
                    self.buffer,
                    cursor_offset=next_offset,
                    cursor_nibble=0,
                    keep_view=True,
                )
                self.hex_viewer.focus_editor()
                self.status_var.set(
                    f"device: {self.device} / direct write done / {offset:04X}={value:02X}"
                )
                self._set_busy(False)
            elif event == "direct_write_error":
                offset, value, message = payload
                self.status_var.set(f"device: {self.device} / direct write error")
                self._set_busy(False)
                self.after(
                    0,
                    lambda a=offset, v=value, m=message: messagebox.showerror(
                        "直接書き込み失敗",
                        f"{a:04X}={v:02X}\n\n{m}",
                    ),
                )
            elif event == "error":
                self.status_var.set("device: not selected / error")
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
                    self.status_var.set("device: not selected / select serial")
                else:
                    self.status_var.set(f"device: {self.device} / ready")
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
