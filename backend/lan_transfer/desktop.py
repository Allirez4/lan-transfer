"""Native Tk laptop interface. All slow work runs outside the UI thread."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

from .desktop_controller import DesktopController, launch, stop_child


def size(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:,.1f} {unit}'
        value /= 1024


class DesktopApp:
    def __init__(self, root, controller, *, auto_refresh=True):
        self.root, self.controller = root, controller
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.events = queue.Queue()
        self.closed = False
        self.close_requested = False
        self.pending_work = 0
        self.refreshing = False
        self.server_process = None
        self.transfer_process = None
        self.transfer_starting = False
        self.snapshot = {}
        self.server_status = 'Checking…'
        self.seen_commands = set()
        self.auto_refresh = auto_refresh
        self.setting_vars = {}
        self.tables = {}
        root.title('LAN Transfer · Laptop')
        root.geometry('1100x760')
        root.minsize(940, 700)
        root.configure(background='#f3f5f7')
        root.protocol('WM_DELETE_WINDOW', self.close)
        self._style()
        outer = ttk.Frame(root, padding=18)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky='ew', pady=(0, 10))
        ttk.Label(header, text='LAN Transfer', style='Title.TLabel').pack(side='left')
        ttk.Label(header, text='LAPTOP CONTROL CENTER', style='Muted.TLabel').pack(side='left', padx=20)
        self.status = tk.StringVar(value='Checking…')
        ttk.Label(header, textvariable=self.status, style='Status.TLabel').pack(side='right')
        self.tabs = ttk.Notebook(outer)
        self.tabs.grid(row=1, column=0, sticky='nsew')
        self._server_tab()
        self._shares_tab()
        self._devices_tab()
        self._incoming_tab()
        self._companion_tab()
        self._settings_tab()
        bottom = ttk.Frame(outer)
        bottom.grid(row=2, column=0, sticky='ew', pady=(12, 0))
        self.activity = tk.StringVar(value='Ready')
        ttk.Label(bottom, textvariable=self.activity, style='Muted.TLabel').pack(side='left')
        ttk.Button(bottom, text='Refresh', command=self.refresh).pack(side='right')
        log_frame = ttk.LabelFrame(outer, text='Activity', padding=8)
        log_frame.grid(row=3, column=0, sticky='ew', pady=(10, 0))
        self.log = tk.Text(log_frame, height=4, wrap='word', background='#172333', foreground='#dce7ec',
                           borderwidth=0, font=('Consolas', 9), state='disabled')
        self.log.pack(fill='x')
        self._populate_settings()
        self.tabs.select(0)
        root.after(75, self._drain)
        if auto_refresh:
            root.after(100, self.refresh)

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 10), background='#f3f5f7', foreground='#172333')
        style.configure('TFrame', background='#f3f5f7')
        style.configure('Title.TLabel', font=('Segoe UI Semibold', 23))
        style.configure('Section.TLabel', font=('Segoe UI Semibold', 16))
        style.configure('Muted.TLabel', foreground='#5a697a')
        style.configure('Status.TLabel', foreground='#087a70', font=('Segoe UI Semibold', 11))
        style.configure('Code.TLabel', font=('Consolas', 22), foreground='#087a70')
        style.configure('TButton', padding=(12, 8), background='#e2e8ee')
        style.map('TButton', background=[('active', '#cbd9e3')])
        style.configure('Primary.TButton', background='#087a70', foreground='white')
        style.map('Primary.TButton', background=[('active', '#06665e'), ('disabled', '#a4b4bd')])
        style.configure('TNotebook', borderwidth=0)
        style.configure('TNotebook.Tab', padding=(14, 9))
        style.map('TNotebook.Tab', background=[('selected', '#ffffff')])
        style.configure('Treeview', rowheight=30, background='white', fieldbackground='white', borderwidth=0)
        style.configure('Treeview.Heading', font=('Segoe UI Semibold', 10), background='#e2e8ee', padding=7)
        style.map('Treeview', background=[('selected', '#d1eeea')], foreground=[('selected', '#172333')])

    def tab(self, title, subtitle):
        panel = ttk.Frame(self.tabs, padding=14)
        self.tabs.add(panel, text=title)
        ttk.Label(panel, text=title, style='Section.TLabel').pack(anchor='w')
        ttk.Label(panel, text=subtitle, style='Muted.TLabel', wraplength=1010).pack(anchor='w', pady=(5, 16))
        return panel

    def buttons(self, parent, items):
        bar = ttk.Frame(parent)
        bar.pack(fill='x', pady=(0, 12))
        for index, (label, callback) in enumerate(items):
            ttk.Button(bar, text=label, command=callback, style='Primary.TButton' if index == 0 else 'TButton').pack(side='left', padx=(0, 8))
        return bar

    def table(self, parent, name, columns, height=10):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=[key for key, _, _ in columns], show='headings', selectmode='browse', height=height)
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=65)
        scroll = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky='nsew')
        scroll.grid(row=0, column=1, sticky='ns')
        horizontal.grid(row=1, column=0, sticky='ew')
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.tables[name] = tree
        return tree

    def _server_tab(self):
        panel = self.tab('Server', 'Share with your LAN. Files you add stay in their original locations.')
        panel = self.scrollable(panel)
        self.buttons(panel, [('Start server', self.start_server), ('Stop server', self.stop_server),
                             ('Open browser interface', self.open_browser), ('Open received folder', self.open_received)])
        connect = ttk.LabelFrame(panel, text='Connect another device', padding=12)
        connect.pack(fill='x', pady=6)
        self.url = tk.StringVar(value=self._preferred_url())
        ttk.Label(connect, text='Open this address on the other device', style='Muted.TLabel').pack(anchor='w')
        ttk.Entry(connect, textvariable=self.url, state='readonly', font=('Segoe UI', 13)).pack(fill='x', pady=8)
        self.buttons(connect, [('Copy address', lambda: self.copy(self.url.get())), ('Show QR code', self.show_qr), ('New pairing code', self.new_code)])
        self.code = tk.StringVar(value='— — — —')
        ttk.Label(connect, textvariable=self.code, style='Code.TLabel').pack(anchor='w', pady=5)
        self.code_note = tk.StringVar(value='Start the server, then generate a code. Each code works once for five minutes.')
        ttk.Label(connect, textvariable=self.code_note, style='Muted.TLabel').pack(anchor='w')
        self.storage_info = tk.StringVar(value=f'Received files: {self.controller.config.storage / "received"}')
        ttk.Label(panel, textvariable=self.storage_info, wraplength=950).pack(anchor='w', pady=8)
        self.security_note = tk.StringVar()
        ttk.Label(panel, textvariable=self.security_note, wraplength=950, style='Muted.TLabel').pack(anchor='w')

    def _shares_tab(self):
        panel = self.tab('Shared files', 'Choose files or folders on this laptop. Removing a share never deletes its originals.')
        self.buttons(panel, [('Add files', self.add_files), ('Add folder', self.add_folder),
                             ('Refresh selected source', lambda: self.share_action('refresh')),
                             ('Remove from sharing', lambda: self.share_action('remove')),
                             ('Copy share ID', lambda: self.copy_selected('shares'))])
        self.table(panel, 'shares', [('name', 'Name', 190), ('source', 'Original location', 410),
                                     ('count', 'Entries', 75), ('size', 'Size', 110), ('status', 'Status', 160)])
        ttk.Label(panel, text='After editing a source, refresh its share. This replaces the published file versions; existing downloads must restart.',
                  wraplength=1000, style='Muted.TLabel').pack(anchor='w', pady=(12, 0))

    def _devices_tab(self):
        panel = self.tab('Devices', 'Paired devices can read published shares and send files. Unpairing cancels unfinished jobs but keeps partial data for cleanup.')
        self.buttons(panel, [('New pairing code', self.new_code), ('Unpair selected device', self.revoke_device)])
        self.table(panel, 'sessions', [('name', 'Device', 260), ('created', 'Paired on', 170), ('status', 'Access', 130), ('id', 'Device ID', 300)])

    def _incoming_tab(self):
        panel = self.tab('Incoming jobs', 'Manage transfers from every device, including expired and unpaired devices. Completed files are preserved.')
        self.buttons(panel, [('Pause', lambda: self.job_action('pause')), ('Resume', lambda: self.job_action('resume')),
                             ('Cancel', lambda: self.job_action('cancel')), ('Discard incomplete data', lambda: self.job_action('discard'))])
        self.table(panel, 'jobs', [('name', 'Batch', 230), ('device', 'Device', 180), ('state', 'State', 120),
                                  ('progress', 'Committed / total', 220), ('discarded', 'Cleanup', 150)])
        ttk.Label(panel, text='Resume permits the sending device to continue; it may need to reselect its original files. Showing the latest 1,000 jobs.',
                  style='Muted.TLabel', wraplength=1000).pack(anchor='w', pady=(12, 0))

    def _companion_tab(self):
        panel = self.tab('Other servers', 'Send to another LAN Transfer server or download its shares with durable resume and checksum verification.')
        form = ttk.Frame(panel)
        form.pack(fill='x', pady=(0, 10))
        self.remote_server = tk.StringVar(value='http://192.168.1.10:8000')
        self.remote_code = tk.StringVar()
        self.remote_ca = tk.StringVar()
        self.conflicts = tk.StringVar(value='rename')
        self.workers = tk.StringVar(value='2')
        ttk.Label(form, text='Server address').grid(row=0, column=0, sticky='w', pady=5)
        ttk.Entry(form, textvariable=self.remote_server, width=45).grid(row=0, column=1, sticky='ew', padx=8)
        ttk.Label(form, text='Pairing code').grid(row=0, column=2, sticky='w')
        ttk.Entry(form, textvariable=self.remote_code, width=15, show='•').grid(row=0, column=3, padx=8)
        ttk.Button(form, text='Pair', command=self.pair_remote).grid(row=0, column=4)
        ttk.Label(form, text='HTTPS CA certificate').grid(row=1, column=0, sticky='w', pady=5)
        ttk.Entry(form, textvariable=self.remote_ca).grid(row=1, column=1, sticky='ew', padx=8)
        ttk.Button(form, text='Browse…', command=self.choose_ca).grid(row=1, column=2, sticky='w')
        options = ttk.Frame(form)
        options.grid(row=2, column=0, columnspan=5, sticky='w', pady=(8, 0))
        ttk.Label(options, text='Existing names:').pack(side='left')
        ttk.Combobox(options, textvariable=self.conflicts, values=['rename', 'skip'], state='readonly', width=9).pack(side='left', padx=8)
        ttk.Label(options, text='Parallel uploads:').pack(side='left', padx=(15, 0))
        ttk.Combobox(options, textvariable=self.workers, values=['1', '2', '4'], state='readonly', width=4).pack(side='left', padx=8)
        form.columnconfigure(1, weight=1)
        self.buttons(panel, [('Load shares', self.load_remote_shares), ('Send file', lambda: self.send_remote(False)),
                             ('Send folder', lambda: self.send_remote(True)), ('Download selected share', self.download_remote),
                             ('Pause transfer', self.pause_transfer)])
        self.companion_tabs = ttk.Notebook(panel)
        self.companion_tabs.pack(fill='both', expand=True)
        remote_panel, history_panel = ttk.Frame(self.companion_tabs), ttk.Frame(self.companion_tabs)
        self.companion_tabs.add(remote_panel, text='Available shares')
        self.companion_tabs.add(history_panel, text='Transfer history')
        self.table(remote_panel, 'remote', [('name', 'Remote share', 320), ('count', 'Entries', 100), ('size', 'Size', 110), ('id', 'Share ID', 300)], height=4)
        resume_bar = ttk.Frame(history_panel)
        resume_bar.pack(fill='x', pady=(12, 8))
        ttk.Label(resume_bar, text='Companion history', style='Muted.TLabel').pack(side='left')
        ttk.Button(resume_bar, text='Resume selected job', command=self.resume_transfer).pack(side='right')
        self.table(history_panel, 'client', [('direction', 'Direction', 90), ('state', 'State', 90), ('server', 'Server', 250), ('location', 'Source / destination', 330)], height=4)
        self.companion_tabs.select(0)

    def _settings_tab(self):
        panel = self.tab('Settings', 'Changes take effect after restarting the server. HTTPS requires the local Caddy setup described in the guide.')
        panel = self.scrollable(panel)
        fields = [('bind', 'Listen address'), ('port', 'Port'), ('hosts', 'Allowed server hosts (comma separated)'),
                  ('origins', 'Browser origins (comma separated)'), ('allowed_networks', 'Allowed client networks (comma separated)'),
                  ('share_roots', 'Command-line share roots (semicolon separated)'), ('chunk_size', 'Upload chunk size (MiB)'),
                  ('active_files', 'Concurrent uploads / downloads'), ('session_days', 'Device access lifetime (days)'),
                  ('max_entries_per_job', 'Maximum entries per batch')]
        form = ttk.Frame(panel)
        form.pack(fill='x')
        for index, (key, title) in enumerate(fields):
            value = tk.StringVar()
            self.setting_vars[key] = value
            ttk.Label(form, text=title).grid(row=index, column=0, sticky='w', pady=5)
            ttk.Entry(form, textvariable=value).grid(row=index, column=1, sticky='ew', padx=(16, 0), pady=5)
        form.columnconfigure(1, weight=1)
        self.secure = tk.BooleanVar()
        ttk.Checkbutton(panel, text='Use HTTPS through a local reverse proxy (Secure cookies)', variable=self.secure).pack(anchor='w', pady=12)
        self.buttons(panel, [('Save settings', self.save_settings), ('Open HTTPS guide', self.open_https_guide)])
        ttk.Label(panel, text=f'Configuration: {self.controller.config_path}', wraplength=1000, style='Muted.TLabel').pack(anchor='w')

    def scrollable(self, parent):
        viewport = ttk.Frame(parent)
        viewport.pack(fill='both', expand=True)
        canvas = tk.Canvas(viewport, background='#f3f5f7', highlightthickness=0)
        scrollbar = ttk.Scrollbar(viewport, orient='vertical', command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)
        content = ttk.Frame(canvas)
        item = canvas.create_window((0, 0), window=content, anchor='nw')
        content.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda event: canvas.itemconfigure(item, width=event.width))
        canvas.bind('<MouseWheel>', lambda event: canvas.yview_scroll(-int(event.delta / 120), 'units'))
        return content

    def _preferred_url(self):
        origins = self.controller.config.origins
        return next((url for url in origins if 'localhost' not in url and '127.0.0.1' not in url), origins[0] if origins else '')

    def _populate_settings(self):
        config = self.controller.config
        for key, variable in self.setting_vars.items():
            value = getattr(config, key)
            if isinstance(value, list):
                value = ('; ' if key == 'share_roots' else ', ').join(value)
            if key == 'chunk_size':
                value //= 1024 * 1024
            variable.set(str(value))
        self.secure.set(config.secure)
        self.url.set(self._preferred_url())
        self.security_note.set('HTTPS configured. Keep the local reverse proxy running and trust its certificate on each device.' if config.secure else
                               'Trusted-LAN HTTP: files and credentials are not encrypted. Use the HTTPS guide for sensitive transfers. Device access expires after ' + str(config.session_days) + ' days.')

    def run(self, label, function, complete=None):
        if self.closed or self.close_requested:
            return
        self.pending_work += 1
        self.activity.set(label + '…')
        def work():
            try:
                result = function()
                self.events.put(('done', (label, result, complete)))
            except Exception as exc:
                self.events.put(('error', (label, str(exc))))
        self.executor.submit(work)

    def _drain(self):
        if self.closed:
            return
        for _ in range(100):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'log':
                self.append_log(payload)
            elif kind == 'error':
                self.pending_work -= 1
                label, error = payload
                self.activity.set(label + ' failed')
                self.append_log(f'{label}: {error}')
                if label == 'Start companion transfer':
                    self.transfer_starting = False
                if label == 'Refresh':
                    self.refreshing = False
                    if self.auto_refresh:
                        self.root.after(3000, self.refresh)
                else:
                    messagebox.showerror(label, error, parent=self.root)
            elif kind == 'done':
                self.pending_work -= 1
                label, result, callback = payload
                self.activity.set('Ready')
                if callback:
                    callback(result)
            elif kind == 'process_exit':
                process, category, code = payload
                self.append_log(f'{category} exited (code {code}).')
                if category == 'Companion' and self.transfer_process is process:
                    self.transfer_process = None
                self.refresh()
        if self.close_requested and self.pending_work == 0:
            self.close_requested = False
            self.close()
            if self.closed:
                return
        self.root.after(75, self._drain)

    def append_log(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text.rstrip() + '\n')
        if int(self.log.index('end-1c').split('.')[0]) > 600:
            self.log.delete('1.0', '100.0')
        self.log.see('end')
        self.log.configure(state='disabled')

    def watch(self, process, category):
        def read():
            try:
                for line in process.stdout:
                    # GUI pairing codes are shown only in the dedicated field.
                    if 'Single-use pairing code' not in line:
                        self.events.put(('log', f'{category}: {line.rstrip()}'))
            finally:
                process.stdout.close()
                self.events.put(('process_exit', (process, category, process.wait())))
        threading.Thread(target=read, daemon=True).start()

    def set_table(self, name, rows, values):
        tree = self.tables[name]
        selected = tree.selection()
        known = set(tree.get_children())
        for index, row in enumerate(rows):
            iid = row['id']
            if iid in known:
                tree.item(iid, values=values(row))
                known.remove(iid)
            else:
                tree.insert('', 'end', iid=iid, values=values(row))
            tree.move(iid, '', index)
        for iid in known:
            tree.delete(iid)
        if selected and tree.exists(selected[0]):
            tree.selection_set(selected[0])

    def refresh(self):
        if self.closed or self.close_requested or self.refreshing:
            return
        self.refreshing = True
        def read():
            return self.controller.snapshot(), self.controller.health(), self.controller.client_jobs()
        def show(result):
            self.refreshing = False
            self.snapshot, self.server_status, client_jobs = result
            self.status.set(self.server_status)
            self.storage_info.set(f"Received files: {self.controller.config.storage / 'received'}\n{size(self.snapshot['free'])} free on this disk")
            self.set_table('shares', self.snapshot['shares'], lambda r: (r['name'], r['source'] or r['root'], r['count'], size(r['size']), r['status']))
            self.set_table('sessions', self.snapshot['sessions'], lambda r: (r['name'], datetime.fromtimestamp(r['created']).strftime('%Y-%m-%d %H:%M'), r['status'], r['id']))
            self.set_table('jobs', self.snapshot['jobs'], lambda r: (r['name'], r['device'] or 'Unknown', r['state'], f"{size(r['committed'])} / {size(r['total'])}", 'Discarded' if r['discarded'] else 'Retained'))
            self.set_table('client', client_jobs, lambda r: (r['direction'], r['state'], r['server'], r['source'] or r['destination']))
            for command in reversed(self.snapshot['commands']):
                if command['state'] in ('completed', 'failed') and command['id'] not in self.seen_commands:
                    self.seen_commands.add(command['id'])
                    self.append_log(f"{command['action']}: {command['state']}" + (f" — {command['error']}" if command['error'] else ''))
            if self.auto_refresh:
                self.root.after(3000, self.refresh)
        self.run('Refresh', read, show)

    def selected(self, name):
        values = self.tables[name].selection()
        if not values:
            messagebox.showinfo('Select an item', 'Select an item in the list first.', parent=self.root)
            return None
        return values[0]

    def copy(self, value):
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.activity.set('Copied')

    def copy_selected(self, name):
        if sid := self.selected(name):
            self.copy(sid)

    def start_server(self):
        def started(process):
            self.server_process = process
            self.watch(process, 'Server')
            self.append_log('Starting the laptop server…')
            self.root.after(1000, self.refresh)
        self.run('Start server', self.controller.start_server, started)

    def stop_server(self):
        if messagebox.askyesno('Stop server', 'Stop the laptop server? Active transfers will need to resume after it starts again.', parent=self.root):
            self.local_command('stop')

    def open_browser(self):
        webbrowser.open(self.url.get())

    def open_received(self):
        path = self.controller.config.storage / 'received'
        path.mkdir(parents=True, exist_ok=True)
        if os.name == 'nt':
            os.startfile(path)
        else:
            webbrowser.open(path.as_uri())

    def new_code(self):
        def show(code):
            self.code.set(code)
            self.code_note.set('Single use · expires in five minutes · enter this code on the other device')
            self.tabs.select(0)
        self.run('Generate pairing code', self.controller.pairing_code, show)

    def show_qr(self):
        import qrcode
        code = qrcode.QRCode(border=3)
        code.add_data(self.url.get())
        code.make(fit=True)
        matrix = code.get_matrix()
        scale = max(4, 280 // len(matrix))
        window = tk.Toplevel(self.root)
        window.title('Scan to connect')
        window.transient(self.root)
        canvas = tk.Canvas(window, width=len(matrix) * scale, height=len(matrix) * scale, background='white', highlightthickness=0)
        canvas.pack(padx=20, pady=20)
        for y, row in enumerate(matrix):
            for x, value in enumerate(row):
                if value:
                    canvas.create_rectangle(x * scale, y * scale, (x + 1) * scale, (y + 1) * scale, fill='black', outline='')
        ttk.Label(window, text=self.url.get()).pack(padx=20, pady=(0, 20))

    def add_files(self):
        paths = filedialog.askopenfilenames(title='Add files to sharing', parent=self.root)
        if paths:
            self.add_sources(paths)

    def add_folder(self):
        path = filedialog.askdirectory(title='Add a folder to sharing', parent=self.root)
        if path:
            self.add_sources([path])

    def add_sources(self, paths):
        def done(results):
            failures = []
            for path, sid, error in results:
                self.append_log(f"{path}: {'shared (' + sid + ')' if sid else error}")
                if error:
                    failures.append(f'{path}: {error}')
            if failures:
                messagebox.showerror('Some sources could not be shared', '\n'.join(failures), parent=self.root)
            self.refresh()
        self.run('Register sources', lambda: self.controller.add_sources(paths), done)

    def share_action(self, action):
        if sid := self.selected('shares'):
            if action == 'refresh' and not messagebox.askyesno('Refresh source', 'Publish the current files? Existing downloads of this share will need to restart.', parent=self.root):
                return
            self.run('Update share', lambda: self.controller.share_action(sid, action), lambda _: self.refresh())

    def local_command(self, action, target=''):
        self.run(action.capitalize(), lambda: self.controller.command(action, target), lambda _: self.refresh())

    def revoke_device(self):
        if sid := self.selected('sessions'):
            if messagebox.askyesno('Unpair device', 'Revoke access and cancel its unfinished uploads? Partial data remains available for cleanup.', parent=self.root):
                self.local_command('revoke', sid)

    def job_action(self, action):
        if jid := self.selected('jobs'):
            if action in ('cancel', 'discard'):
                text = 'Cancel this batch? Partial data is retained.' if action == 'cancel' else 'Delete incomplete data for this canceled batch? Completed files stay available.'
                if not messagebox.askyesno(action.capitalize(), text, parent=self.root):
                    return
            self.local_command(action, jid)

    def choose_ca(self):
        path = filedialog.askopenfilename(title='Choose trusted CA certificate', filetypes=[('Certificates', '*.crt *.pem'), ('All files', '*')], parent=self.root)
        if path:
            self.remote_ca.set(path)

    def pair_remote(self):
        server, code, ca = self.remote_server.get().strip(), self.remote_code.get().strip(), self.remote_ca.get().strip()
        if not code:
            messagebox.showinfo('Pairing code', 'Enter the code shown on the other server.', parent=self.root)
            return
        def paired(value):
            self.remote_code.set('')
            self.remote_server.set(value)
            self.append_log('Paired with ' + value)
            self.load_remote_shares()
        self.run('Pair with server', lambda: self.controller.pair_remote(server, code, ca or None), paired)

    def load_remote_shares(self):
        server = self.remote_server.get().strip()
        self.companion_tabs.select(0)
        self.run('Load remote shares', lambda: self.controller.remote_shares(server),
                 lambda rows: self.set_table('remote', rows, lambda r: (r['name'], r['count'], size(r['size']), r['id'])))

    def start_transfer(self, arguments):
        if self.transfer_starting or (self.transfer_process and self.transfer_process.poll() is None):
            messagebox.showinfo('Transfer running', 'Pause the current companion transfer before starting another.', parent=self.root)
            return
        self.transfer_starting = True
        def started(process):
            self.transfer_starting = False
            self.transfer_process = process
            self.companion_tabs.select(1)
            self.watch(process, 'Companion')
            self.root.after(500, self.refresh)
        self.run('Start companion transfer', lambda: launch('lan_transfer_client.main', arguments), started)

    def send_remote(self, folder):
        path = filedialog.askdirectory(title='Send folder', parent=self.root) if folder else filedialog.askopenfilename(title='Send file', parent=self.root)
        if path:
            self.start_transfer(['upload', path, '--server', self.remote_server.get().strip(), '--on-conflict', self.conflicts.get(), '--workers', self.workers.get()])

    def download_remote(self):
        if sid := self.selected('remote'):
            destination = filedialog.askdirectory(title='Download into folder', parent=self.root)
            if destination:
                self.start_transfer(['download', sid, '--to', destination, '--server', self.remote_server.get().strip(), '--on-conflict', self.conflicts.get()])

    def resume_transfer(self):
        if jid := self.selected('client'):
            self.start_transfer(['resume', jid, '--on-conflict', self.conflicts.get(), '--workers', self.workers.get()])

    def pause_transfer(self):
        process = self.transfer_process
        if process and process.poll() is None:
            self.run('Pause companion', lambda: self.controller.pause_companion(process), lambda _: self.append_log('Companion stopped. Select its history entry to resume from durable checkpoints.'))

    def save_settings(self):
        if self.server_status not in ('Stopped', 'Port used by another service'):
            messagebox.showinfo('Stop the server first', 'Stop the server before changing its network settings.', parent=self.root)
            return
        values = {}
        try:
            for key, variable in self.setting_vars.items():
                value = variable.get().strip()
                if key in ('hosts', 'origins', 'allowed_networks', 'share_roots'):
                    values[key] = [item.strip() for item in value.split(';' if key == 'share_roots' else ',') if item.strip()]
                elif key in ('port', 'chunk_size', 'active_files', 'session_days', 'max_entries_per_job'):
                    values[key] = int(value) * (1024 * 1024 if key == 'chunk_size' else 1)
                else:
                    values[key] = value
            values['secure'] = self.secure.get()
            if values['secure'] and (values['bind'] != '127.0.0.1' or any(not origin.startswith('https://') for origin in values['origins'])):
                raise ValueError('For HTTPS, bind the backend to 127.0.0.1 and use the exact https:// browser origins from the Caddy guide.')
        except ValueError as exc:
            messagebox.showerror('Settings', str(exc), parent=self.root)
            return
        def saved(_):
            self._populate_settings()
            self.append_log('Settings saved. Start the server to apply them.')
            self.refresh()
        self.run('Save settings', lambda: self.controller.save_config(values), saved)

    def open_https_guide(self):
        path = Path(__file__).resolve().parents[2] / 'docs' / 'HTTPS.md'
        if os.name == 'nt':
            os.startfile(path)
        else:
            webbrowser.open(path.as_uri())

    def close(self):
        # Deliver outstanding launch callbacks before checking owned children.
        # Otherwise closing during startup could leave a server running unseen.
        if self.pending_work:
            self.close_requested = True
            self.activity.set('Finishing the current operation before closing…')
            return
        if self.transfer_process and self.transfer_process.poll() is None:
            if not messagebox.askyesno('Transfer running', 'Pause the companion transfer and close? Its checkpoints will be kept.', parent=self.root):
                return
            self.controller.pause_companion(self.transfer_process)
        if self.server_process and self.server_process.poll() is None:
            choice = messagebox.askyesno('Laptop server', 'Stop this window’s server and close?\nKeep this window open or minimized to continue sharing.', parent=self.root)
            if not choice:
                return
            if choice:
                try:
                    self.controller.command('stop')
                    self.server_process.wait(timeout=12)
                except Exception:
                    stop_child(self.server_process)
        self.closed = True
        self.executor.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()


def main(config_path=None):
    if config_path is None:
        import argparse
        parser = argparse.ArgumentParser(description='LAN Transfer laptop interface')
        parser.add_argument('--config', default='config.json')
        config_path = parser.parse_args().config
    root = tk.Tk()
    try:
        DesktopApp(root, DesktopController(config_path))
    except Exception as exc:
        root.withdraw()
        messagebox.showerror('LAN Transfer could not start', str(exc), parent=root)
        root.destroy()
        return
    root.mainloop()


if __name__ == '__main__':
    main()
