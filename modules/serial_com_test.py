"""Native Qt serial testing panel, embedded in the single-file survey app."""
import datetime
import json
from pathlib import Path
import queue
import threading
from PyQt6 import QtCore as C, QtGui as G, QtWidgets as W
import serial
from serial.tools import list_ports


def encode_message(text, hex_mode=False, ending='CRLF'):
    payload = bytes.fromhex(text) if hex_mode else text.encode('utf-8')
    return payload + {'None': b'', 'CR': b'\r', 'LF': b'\n', 'CRLF': b'\r\n'}[ending]


def io_loop(port, outgoing, events, stop):
    stream = None
    transfer = None
    try:
        while not stop.is_set():
            try:
                action, value = outgoing.get_nowait() if stream is None else (None, None)
            except queue.Empty:
                action = None
            if action == 'send':
                written = port.write(value)
                if written != len(value):
                    raise OSError(f'Partial write: {written}/{len(value)} bytes')
                events.put(('tx', value))
            elif action == 'dtr':
                port.dtr = value
            elif action == 'rts':
                port.rts = value
            elif action == 'file':
                name, cancel = value
                stream = open(name, 'rb')
                transfer = {'sent': 0, 'total': Path(name).stat().st_size, 'cancel': cancel}
            if stream is not None:
                cancelled = transfer['cancel'].is_set()
                chunk = b'' if cancelled else stream.read(min(1024, max(1, port.baudrate // 100)))
                if chunk:
                    written = port.write(chunk)
                    if written != len(chunk):
                        raise OSError(f'Partial file write: {written}/{len(chunk)} bytes')
                    transfer['sent'] += written
                    events.put(('tx_file', written))
                    events.put(('file_progress', (transfer['sent'], transfer['total'])))
                else:
                    stream.close()
                    stream = None
                    events.put(('file_done', (transfer['sent'], transfer['total'], cancelled)))
            data = port.read(min(max(port.in_waiting, 1), 16384))
            if data:
                events.put(('rx', data))
    except Exception as exc:
        events.put(('error', str(exc)))
    finally:
        if stream is not None:
            stream.close()
        try:
            port.close()
        except Exception:
            pass
        events.put(('closed', None))


class SerialComTest(W.QWidget):
    def __init__(self, parent=None, settings=None, can_open=None):
        super().__init__(parent)
        self.setObjectName('serialComTest')
        self.can_open = can_open or (lambda: True)
        self.worker = None
        self.connected = False
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.outgoing = queue.Queue(maxsize=100)
        self.log = None
        self.rx_count = self.tx_count = 0
        self.file_busy = False
        self.file_cancel = threading.Event()
        outer = W.QHBoxLayout(self)
        split = W.QSplitter(C.Qt.Orientation.Horizontal)
        outer.addWidget(split)
        left = W.QWidget()
        layout = W.QVBoxLayout(left)
        layout.setContentsMargins(0, 0, 0, 0)
        split.addWidget(left)
        note = W.QLabel('Open a port to receive data. Send transmits the entered text or HEX bytes. '
                        'Use loop:// for a software loopback test.')
        note.setWordWrap(True)
        layout.addWidget(note)
        box = W.QGroupBox('Serial connection')
        grid = W.QGridLayout(box)
        layout.addWidget(box)
        self.fields = {}
        defaults = [
            ('port', 'COM port / URL', [], str(getattr(settings, 'port', ''))),
            ('baud', 'Baud rate', ['4800','9600','19200','38400','57600','115200','230400','460800','921600'], str(getattr(settings, 'baud_rate', 115200))),
            ('bits', 'Data bits', ['5','6','7','8'], str(getattr(settings, 'data_width', 8))),
            ('parity', 'Parity', ['None','Even','Odd','Mark','Space'], str(getattr(settings, 'parity', 'None'))),
            ('stops', 'Stop bits', ['1','1.5','2'], str(getattr(settings, 'stop_bits', 1)).removesuffix('.0')),
            ('flow', 'Flow control', ['None','RTS/CTS','XON/XOFF','DSR/DTR'], 'None'),
        ]
        parity_names = {'N':'None','E':'Even','O':'Odd','M':'Mark','S':'Space'}
        for index, (key, label, values, default) in enumerate(defaults):
            row, col = divmod(index, 3)
            grid.addWidget(W.QLabel(label), row * 2, col)
            combo = W.QComboBox()
            combo.addItems(values)
            combo.setEditable(key in ('port','baud'))
            if key == 'parity':
                default = parity_names.get(default, default)
            combo.setCurrentText(default)
            grid.addWidget(combo, row * 2 + 1, col)
            self.fields[key] = combo
        buttons = W.QHBoxLayout()
        grid.addLayout(buttons, 4, 0, 1, 3)
        self.refresh_button = W.QPushButton('Refresh ports')
        self.refresh_button.clicked.connect(self.refresh_ports)
        buttons.addWidget(self.refresh_button)
        self.open_button = W.QPushButton('Open port')
        self.open_button.clicked.connect(self.toggle_connection)
        buttons.addWidget(self.open_button)
        self.dtr = W.QCheckBox('DTR')
        self.rts = W.QCheckBox('RTS')
        buttons.addWidget(self.dtr)
        buttons.addWidget(self.rts)
        self.dtr.toggled.connect(lambda value: self.set_line('dtr', value))
        self.rts.toggled.connect(lambda value: self.set_line('rts', value))
        buttons.addStretch()
        toolbar = W.QHBoxLayout()
        layout.addLayout(toolbar)
        self.hex_receive = W.QCheckBox('RX as HEX')
        self.timestamps = W.QCheckBox('Timestamps')
        self.timestamps.setChecked(True)
        toolbar.addWidget(self.hex_receive)
        toolbar.addWidget(self.timestamps)
        clear = W.QPushButton('Clear display')
        save = W.QPushButton('Save display')
        self.log_button = W.QPushButton('Start raw RX log')
        for button in (clear, save, self.log_button): toolbar.addWidget(button)
        self.traffic = W.QPlainTextEdit()
        self.traffic.setReadOnly(True)
        self.traffic.setFont(G.QFont('Consolas', 10))
        self.traffic.document().setMaximumBlockCount(6000)
        layout.addWidget(self.traffic, 1)
        clear.clicked.connect(self.traffic.clear)
        save.clicked.connect(self.save_display)
        self.log_button.clicked.connect(self.toggle_log)
        self.message = W.QPlainTextEdit()
        self.message.setPlaceholderText('Command / text to send, or HEX bytes such as 01 02 FF')
        self.message.setMaximumHeight(90)
        layout.addWidget(self.message)
        send = W.QHBoxLayout()
        layout.addLayout(send)
        self.hex_send = W.QCheckBox('HEX input')
        send.addWidget(self.hex_send)
        send.addWidget(W.QLabel('Append'))
        self.ending = W.QComboBox()
        self.ending.addItems(['None','CR','LF','CRLF'])
        self.ending.setCurrentText('CRLF')
        send.addWidget(self.ending)
        send_button = W.QPushButton('Send')
        send_button.clicked.connect(self.send_once)
        send.addWidget(send_button)
        send.addWidget(W.QLabel('Repeat (ms)'))
        self.interval = W.QSpinBox()
        self.interval.setRange(20, 3600000)
        self.interval.setValue(1000)
        send.addWidget(self.interval)
        self.repeat_button = W.QPushButton('Start repeat')
        self.repeat_button.clicked.connect(self.toggle_repeat)
        send.addWidget(self.repeat_button)
        files = W.QGroupBox('File transfer — sends original file bytes to the serial port')
        file_layout = W.QGridLayout(files)
        self.file_path = W.QLineEdit()
        self.file_path.setPlaceholderText('Select a file to send')
        file_layout.addWidget(self.file_path, 0, 0, 1, 4)
        browse = W.QPushButton('Select file')
        browse.clicked.connect(self.select_file)
        preview = W.QPushButton('View file')
        preview.clicked.connect(self.view_file)
        self.file_send_button = W.QPushButton('Send file')
        self.file_send_button.clicked.connect(self.send_file)
        self.file_cancel_button = W.QPushButton('Stop file')
        self.file_cancel_button.clicked.connect(lambda: self.file_cancel.set())
        self.file_cancel_button.setEnabled(False)
        for col, button in enumerate((browse, preview, self.file_send_button, self.file_cancel_button)):
            file_layout.addWidget(button, 1, col)
        self.file_progress = W.QProgressBar()
        self.file_progress.setRange(0, 100)
        file_layout.addWidget(self.file_progress, 2, 0, 1, 4)
        layout.addWidget(files)
        self.status = W.QLabel('Disconnected')
        self.status.setWordWrap(True)
        self.counts = W.QLabel('RX: 0 bytes    TX: 0 bytes')
        layout.addWidget(self.status)
        layout.addWidget(self.counts)
        self.repeat = C.QTimer(self)
        self.repeat.timeout.connect(self.repeat_send)
        self.batch = C.QTimer(self)
        self.batch.setSingleShot(True)
        self.batch.timeout.connect(self.batch_tick)
        self.batch_commands = []
        self.build_commands(split)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([650, 470])
        self.poll_timer = C.QTimer(self)
        self.poll_timer.timeout.connect(self.drain_events)
        self.poll_timer.start(40)
        self.refresh_ports()
        # Parent settings windows close without calling child closeEvent.
        if parent is not None:
            parent.installEventFilter(self)
            parent.destroyed.connect(self.cleanup)
        W.QApplication.instance().aboutToQuit.connect(self.cleanup)

    def eventFilter(self, obj, event):
        if event.type() == C.QEvent.Type.Close:
            self.cleanup()
        return super().eventFilter(obj, event)

    def refresh_ports(self):
        box = self.fields['port']
        current = box.currentText()
        box.clear()
        box.addItems(sorted(p.device for p in list_ports.comports()))
        if current: box.setCurrentText(current)

    def toggle_connection(self):
        if self.connected: self.disconnect_port()
        else: self.open_port()

    def open_port(self):
        if not self.can_open():
            self.status.setText('Stop survey recording and I/O monitoring before opening a test port.')
            return False
        if self.worker is not None and self.worker.is_alive():
            self.status.setText('Previous connection is still closing.')
            return False
        port = None
        try:
            values = {key: box.currentText().strip() for key,box in self.fields.items()}
            if not values['port']: raise ValueError('Select a COM port or enter loop://.')
            baud = int(values['baud'])
            if baud <= 0: raise ValueError('Baud rate must be positive.')
            flow = values['flow']
            port = serial.serial_for_url(values['port'], do_not_open=True,
                baudrate=baud, bytesize=int(values['bits']),
                parity={'None':'N','Even':'E','Odd':'O','Mark':'M','Space':'S'}[values['parity']],
                stopbits=float(values['stops']), timeout=0.05, write_timeout=0.5,
                rtscts=flow=='RTS/CTS', xonxoff=flow=='XON/XOFF', dsrdtr=flow=='DSR/DTR')
            port.dtr, port.rts = self.dtr.isChecked(), self.rts.isChecked()
            port.open()
        except Exception as exc:
            if port is not None: port.close()
            self.status.setText(f'Open failed: {exc}')
            return False
        self.events = queue.Queue()
        self.outgoing = queue.Queue(maxsize=100)
        self.stop = threading.Event()
        self.connected = True
        self.worker = threading.Thread(target=io_loop, args=(port,self.outgoing,self.events,self.stop), daemon=True)
        self.worker.start()
        for box in self.fields.values(): box.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.dtr.setEnabled(flow!='DSR/DTR')
        self.rts.setEnabled(flow!='RTS/CTS')
        self.open_button.setText('Close port')
        self.status.setText(f"Connected: {values['port']} at {baud} baud")
        return True

    def disconnected_ui(self):
        self.connected = False
        self.stop_repeat()
        self.stop_commands()
        self.file_busy = False
        self.file_cancel.set()
        self.file_send_button.setEnabled(True)
        self.file_cancel_button.setEnabled(False)
        for box in self.fields.values(): box.setEnabled(True)
        for item in (self.refresh_button,self.dtr,self.rts): item.setEnabled(True)
        self.open_button.setText('Open port')

    def disconnect_port(self):
        self.connected = False
        self.stop_repeat()
        self.stop_commands()
        self.file_cancel.set()
        self.stop.set()
        if self.worker is not None: self.worker.join(timeout=1)
        while not self.events.empty(): self.drain_events()
        self.disconnected_ui()
        self.status.setText('Disconnected')

    def set_line(self, name, value):
        if self.connected:
            try: self.outgoing.put_nowait((name,value))
            except queue.Full: self.status.setText('Transmit queue full; line change was not queued.')

    def send_once(self):
        return self.send_command(self.message.toPlainText(), self.hex_send.isChecked(), self.ending.currentText())

    def send_command(self, text, hex_mode=False, ending='CRLF'):
        if not self.connected:
            self.status.setText('Open a port first.')
            return False
        if self.file_busy:
            self.status.setText('Stop or finish the file transfer before sending commands.')
            return False
        if not self.can_open():
            self.disconnect_port()
            self.status.setText('Test port closed because survey recording or monitoring started.')
            return False
        try:
            data = encode_message(text, hex_mode, ending)
            if not data: raise ValueError('Enter data to send.')
            if len(data)>65536: raise ValueError('Maximum single transmission is 65,536 bytes.')
            self.outgoing.put_nowait(('send',data))
            return True
        except queue.Full:
            self.status.setText('Transmit queue full; repeat stopped.')
        except Exception as exc:
            self.status.setText(f'Cannot send: {exc}')
        return False

    def toggle_repeat(self):
        if self.repeat.isActive(): self.stop_repeat()
        elif self.send_once():
            self.stop_commands()
            self.repeat.start(self.interval.value())
            self.repeat_button.setText('Stop repeat')

    def repeat_send(self):
        if not self.send_once(): self.stop_repeat()

    def stop_repeat(self):
        self.repeat.stop()
        self.repeat_button.setText('Start repeat')

    def append(self, kind, text):
        stamp = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]+' ' if self.timestamps.isChecked() else ''
        self.traffic.appendPlainText(f'{stamp}{kind.upper()}: {text}')
        bar=self.traffic.verticalScrollBar();bar.setValue(bar.maximum())

    def drain_events(self):
        for _ in range(200):
            try: kind,value=self.events.get_nowait()
            except queue.Empty: break
            if kind in ('rx','tx'):
                if kind=='rx':
                    self.rx_count+=len(value)
                    if self.log is not None:
                        try: self.log.write(value);self.log.flush()
                        except OSError as exc:
                            self.stop_log();self.append('error',f'RX log failed: {exc}')
                else: self.tx_count+=len(value)
                use_hex=self.hex_receive.isChecked() if kind=='rx' else self.hex_send.isChecked()
                self.append(kind,value.hex(' ').upper() if use_hex else repr(value)[2:-1])
            elif kind=='error':
                self.append(kind,value)
                self.status.setText(f'Serial error: {value}')
                self.disconnected_ui()
            elif kind=='closed' and self.connected:
                self.disconnected_ui();self.status.setText('Connection closed')
            elif kind=='tx_file':
                self.tx_count += value
            elif kind=='file_progress':
                sent, total = value
                self.file_progress.setValue(int(100 * sent / total) if total else 100)
                self.status.setText(f'File sent to serial driver: {sent:,} / {total:,} bytes')
            elif kind=='file_done':
                sent, total, cancelled = value
                self.file_busy = False
                self.file_send_button.setEnabled(True)
                self.file_cancel_button.setEnabled(False)
                self.file_progress.setValue(int(100 * sent / total) if total else 100)
                self.status.setText(f"File {'stopped' if cancelled else 'sent to serial driver'}: {sent:,} / {total:,} bytes")
        self.counts.setText(f'RX: {self.rx_count:,} bytes    TX: {self.tx_count:,} bytes')
        if self.connected and not self.can_open():
            self.disconnect_port()
            self.status.setText('Test port closed because survey recording or monitoring started.')

    def toggle_log(self):
        if self.log is not None: self.stop_log();return
        name,_=W.QFileDialog.getSaveFileName(self,'Raw received bytes (new file)','','Binary log (*.bin);;All files (*)')
        if name:
            try:
                self.log=open(name,'wb')
                self.log_button.setText('Stop raw RX log')
                self.status.setText(f'Raw RX log: {name}')
            except OSError as exc: self.status.setText(f'Log failed: {exc}')

    def stop_log(self):
        if self.log is not None:
            try: self.log.close()
            except OSError: pass
            self.log=None
        self.log_button.setText('Start raw RX log')

    def save_display(self):
        name,_=W.QFileDialog.getSaveFileName(self,'Save traffic display','','Text (*.txt)')
        if name:
            try: Path(name).write_text(self.traffic.toPlainText(),encoding='utf-8')
            except OSError as exc: self.status.setText(f'Save failed: {exc}')

    def cleanup(self, *args):
        self.disconnect_port()
        self.stop_log()

    def closeEvent(self, event):
        self.cleanup()
        super().closeEvent(event)

    def build_commands(self, splitter):
        box = W.QGroupBox('Command list')
        layout = W.QVBoxLayout(box)
        self.choose_all = W.QCheckBox('Choose all commands')
        self.choose_all.toggled.connect(self.select_all_commands)
        layout.addWidget(self.choose_all)
        self.commands = W.QTableWidget(29, 5)
        self.commands.setHorizontalHeaderLabels(['Use', 'Command', 'HEX', 'Enter', 'Send'])
        self.commands.horizontalHeader().setSectionResizeMode(1, W.QHeaderView.ResizeMode.Stretch)
        for col in (0, 2, 3, 4):
            self.commands.horizontalHeader().setSectionResizeMode(col, W.QHeaderView.ResizeMode.ResizeToContents)
        self.commands.setMinimumWidth(350)
        self.commands.setToolTip('Commands are sent only when you press a row button or Send selected. Enter appends CRLF.')
        self.command_rows = []
        for row in range(29):
            use, hex_box, enter = W.QCheckBox(), W.QCheckBox(), W.QCheckBox()
            enter.setChecked(True)
            text = W.QLineEdit()
            text.setPlaceholderText(f'Command {row + 1}')
            send = W.QPushButton(str(row + 1))
            send.clicked.connect(lambda checked=False, index=row: self.send_row(index))
            for col, widget in enumerate((use, text, hex_box, enter, send)):
                self.commands.setCellWidget(row, col, widget)
            self.command_rows.append((use, text, hex_box, enter))
        layout.addWidget(self.commands, 1)
        presets = W.QHBoxLayout()
        for label, callback in [('Load set', self.load_commands), ('Save set', self.save_commands), ('Clear set', self.clear_commands)]:
            button = W.QPushButton(label)
            button.clicked.connect(callback)
            presets.addWidget(button)
        layout.addLayout(presets)
        run = W.QHBoxLayout()
        run.addWidget(W.QLabel('Delay (ms)'))
        self.command_delay = W.QSpinBox()
        self.command_delay.setRange(20, 3600000)
        self.command_delay.setValue(1000)
        run.addWidget(self.command_delay)
        self.run_commands = W.QPushButton('Send selected')
        self.run_commands.clicked.connect(self.start_commands)
        run.addWidget(self.run_commands)
        stop = W.QPushButton('Stop list')
        stop.clicked.connect(self.stop_commands)
        run.addWidget(stop)
        layout.addLayout(run)
        hint = W.QLabel('Each numbered button sends its row. Load/save command sets as JSON; text files load one command per line. No commands run automatically.')
        hint.setWordWrap(True)
        layout.addWidget(hint)
        splitter.addWidget(box)

    def select_all_commands(self, checked):
        for use, text, hex_box, enter in self.command_rows:
            use.setChecked(checked)

    def send_row(self, index):
        use, text, hex_box, enter = self.command_rows[index]
        return self.send_command(text.text(), hex_box.isChecked(), 'CRLF' if enter.isChecked() else 'None')

    def start_commands(self):
        if self.batch.isActive() or self.batch_commands:
            self.status.setText('Command list is already running.')
            return
        selected = []
        try:
            for use, text, hex_box, enter in self.command_rows:
                if use.isChecked():
                    command = (text.text(), hex_box.isChecked(), 'CRLF' if enter.isChecked() else 'None')
                    if not command[0]: raise ValueError('A selected command is empty.')
                    payload = encode_message(*command)
                    if not payload or len(payload) > 65536: raise ValueError('A selected command has an invalid length.')
                    selected.append(command)
            if not selected: raise ValueError('Select at least one command using its Use checkbox.')
        except Exception as exc:
            self.status.setText(f'Cannot run command list: {exc}')
            return
        self.stop_repeat()
        self.batch_commands = selected
        self.run_commands.setEnabled(False)
        self.batch_tick()

    def batch_tick(self):
        if not self.batch_commands:
            self.stop_commands()
            return
        if not self.send_command(*self.batch_commands.pop(0)):
            self.stop_commands()
            return
        if self.batch_commands: self.batch.start(self.command_delay.value())
        else: self.stop_commands()

    def stop_commands(self):
        self.batch.stop()
        self.batch_commands = []
        self.run_commands.setEnabled(True)

    def command_set(self):
        return {'version': 1, 'delay_ms': self.command_delay.value(), 'commands': [
            {'text': text.text(), 'hex': hex_box.isChecked(), 'enter': enter.isChecked(), 'selected': use.isChecked()}
            for use, text, hex_box, enter in self.command_rows]}

    def apply_command_set(self, data):
        if not isinstance(data, dict) or data.get('version') != 1:
            raise ValueError('Unsupported command-set format.')
        rows = data.get('commands')
        if not isinstance(rows, list) or len(rows) > 29:
            raise ValueError('A command set supports up to 29 rows.')
        delay = data.get('delay_ms', 1000)
        if type(delay) is not int or not 20 <= delay <= 3600000: raise ValueError('Invalid delay.')
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('text', ''), str) or len(row.get('text', '')) > 65536:
                raise ValueError('Invalid command text.')
            if any(type(row.get(k, False if k != 'enter' else True)) is not bool for k in ('hex', 'enter', 'selected')):
                raise ValueError('Invalid command options.')
        self.stop_commands()
        self.clear_commands()
        self.command_delay.setValue(delay)
        for widgets, row in zip(self.command_rows, rows):
            use, text, hex_box, enter = widgets
            text.setText(row.get('text', ''))
            hex_box.setChecked(row.get('hex', False))
            enter.setChecked(row.get('enter', True))
            use.setChecked(row.get('selected', False))

    def clear_commands(self):
        self.stop_commands()
        self.choose_all.setChecked(False)
        for use, text, hex_box, enter in self.command_rows:
            use.setChecked(False);text.clear();hex_box.setChecked(False);enter.setChecked(True)

    def load_commands(self):
        name, _ = W.QFileDialog.getOpenFileName(self, 'Load command set', '', 'Command set (*.json);;Commands, one per line (*.txt)')
        if not name: return
        try:
            if Path(name).stat().st_size > 2 * 1024 * 1024: raise ValueError('Command set must be smaller than 2 MB.')
            text = Path(name).read_text(encoding='utf-8-sig')
            data = json.loads(text) if Path(name).suffix.lower() == '.json' else {
                'version': 1, 'commands': [{'text': line} for line in text.splitlines()]}
            self.apply_command_set(data)
            self.status.setText(f'Loaded command set: {Path(name).name}; nothing sent.')
        except Exception as exc: self.status.setText(f'Load failed: {exc}')

    def save_commands(self):
        name, _ = W.QFileDialog.getSaveFileName(self, 'Save command set', '', 'Command set (*.json)')
        if name:
            try:
                Path(name).write_text(json.dumps(self.command_set(), indent=2), encoding='utf-8')
                self.status.setText(f'Saved command set: {Path(name).name}')
            except OSError as exc: self.status.setText(f'Save failed: {exc}')

    def select_file(self):
        name, _ = W.QFileDialog.getOpenFileName(self, 'Select file to send', '', 'All files (*)')
        if name: self.file_path.setText(name)

    def view_file(self):
        try:
            with open(self.file_path.text(), 'rb') as stream: data = stream.read(65537)
            dialog = W.QDialog(self)
            dialog.setWindowTitle('File preview — first 64 KB')
            dialog.resize(750, 450)
            layout = W.QVBoxLayout(dialog)
            preview = W.QPlainTextEdit()
            preview.setReadOnly(True)
            preview.setPlainText(data[:65536].hex(' ').upper() if self.hex_receive.isChecked() else data[:65536].decode('utf-8', errors='replace'))
            layout.addWidget(preview)
            dialog.exec()
        except OSError as exc: self.status.setText(f'Cannot view file: {exc}')

    def send_file(self):
        if not self.connected or not self.can_open():
            self.status.setText('Open a test port with survey acquisition stopped first.')
            return False
        if self.file_busy:
            self.status.setText('A file transfer is already running.')
            return False
        try:
            path = Path(self.file_path.text())
            if not path.is_file(): raise ValueError('Select a readable file first.')
            with path.open('rb'): pass
            self.stop_repeat()
            self.stop_commands()
            self.file_cancel = threading.Event()
            self.outgoing.put_nowait(('file', (str(path), self.file_cancel)))
            self.file_busy = True
            self.file_progress.setValue(0)
            self.file_send_button.setEnabled(False)
            self.file_cancel_button.setEnabled(True)
            self.status.setText(f'Queued file: {path.name} ({path.stat().st_size:,} bytes); sent without added terminators.')
            return True
        except Exception as exc:
            self.status.setText(f'Cannot send file: {exc}')
            return False
