# pycast_app.py
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import socket
import json
import threading
import os
import argparse
import sys
import time

from sender import Sender, HANDSHAKE_PORT
from receiver import Receiver
from service_discovery import PyCastServiceBrowser
from config_manager import load_config, save_config

# ----------------------------------------------------------------------------
# ----- PARTE 1: Lógica de la Interfaz Gráfica (GUI) - Sin cambios --------
# ----------------------------------------------------------------------------

class PyCastApp:
    def __init__(self, root):
        self.root = root
        self.root.title("PyCast - Transferencia de Archivos LAN")
        self.root.protocol("WM_DELETE_WINDOW", self._on_closing)

        self.config = load_config()

        self.style = ttk.Style()
        self.style.theme_use('clam')

        self.main_container = ttk.Frame(self.root, padding="10")
        self.main_container.pack(expand=True, fill=tk.BOTH)

        self.sender = None
        self.receiver = None
        self.service_browser = None
        
        # Variables de estado de la UI
        self.selected_file_path = tk.StringVar()
        self.selected_folder_path = tk.StringVar(value=self.config.get('download_folder'))
        self.session_name = tk.StringVar()
        self.progress_var = tk.DoubleVar()
        self.progress_text = tk.StringVar()
        self.multiclient_mode_var = tk.BooleanVar(value=self.config.get('multiclient_enabled_by_default'))
        self.active_sessions = {}

        self.status_label = None
        self.sessions_tree = None

        self.create_welcome_screen()

    def _clear_container(self):
        for widget in self.main_container.winfo_children():
            widget.destroy()

    def create_welcome_screen(self):
        self._clear_container()
        self.root.geometry("400x350")
        self._cleanup_network_services()
        welcome_frame = ttk.Frame(self.main_container)
        welcome_frame.pack(expand=True)
        ttk.Label(welcome_frame, text="Bienvenido a PyCast", font=("Helvetica", 16, "bold")).pack(pady=(0, 20))
        ttk.Label(welcome_frame, text=f"Tu nombre de usuario: {self.config.get('username')}", font=("Helvetica", 9)).pack(pady=(0, 25))
        
        ttk.Button(welcome_frame, text="Enviar un Archivo", command=self.show_sender_ui, width=20).pack(pady=5)
        ttk.Button(welcome_frame, text="Recibir un Archivo", command=self.show_receiver_ui, width=20).pack(pady=5)
        
        ttk.Separator(welcome_frame, orient='horizontal').pack(fill='x', pady=20, padx=20)
        ttk.Button(welcome_frame, text="Configuración", command=self.show_config_window, width=20).pack(pady=5)

    def show_config_window(self):
        config_win = tk.Toplevel(self.root)
        config_win.title("Configuración")
        config_win.geometry("480x250")
        config_win.resizable(False, False)
        config_win.transient(self.root)
        config_win.grab_set()
        
        main_frame = ttk.Frame(config_win, padding="15")
        main_frame.pack(expand=True, fill=tk.BOTH)

        username_var = tk.StringVar(value=self.config.get('username'))
        folder_var = tk.StringVar(value=self.config.get('download_folder'))
        multiclient_var = tk.BooleanVar(value=self.config.get('multiclient_enabled_by_default'))

        ttk.Label(main_frame, text="Nombre de Usuario:").grid(row=0, column=0, sticky="w", pady=5, padx=(0, 10))
        ttk.Entry(main_frame, textvariable=username_var).grid(row=0, column=1, sticky="ew")

        ttk.Label(main_frame, text="Carpeta de Descargas:").grid(row=1, column=0, sticky="w", pady=5, padx=(0, 10))
        folder_entry = ttk.Entry(main_frame, textvariable=folder_var, state="readonly")
        folder_entry.grid(row=1, column=1, sticky="ew")
        
        def _select_config_folder():
            path = filedialog.askdirectory(title="Selecciona la carpeta de descargas", parent=config_win)
            if path: folder_var.set(path)

        ttk.Button(main_frame, text="...", command=_select_config_folder, width=3).grid(row=1, column=2, padx=(5,0))
        
        ttk.Checkbutton(main_frame, text="Habilitar modo multi-cliente por defecto", variable=multiclient_var).grid(row=2, column=0, columnspan=3, sticky='w', pady=10)

        buttons_frame = ttk.Frame(main_frame)
        buttons_frame.grid(row=3, column=0, columnspan=3, pady=(20, 0))

        def _save_and_close():
            self.config['username'] = username_var.get().strip()
            self.config['download_folder'] = folder_var.get().strip()
            self.config['multiclient_enabled_by_default'] = multiclient_var.get()
            self.selected_folder_path.set(self.config['download_folder'])
            self.multiclient_mode_var.set(self.config['multiclient_enabled_by_default'])
            
            save_config(self.config)
            messagebox.showinfo("Guardado", "La configuración ha sido guardada.", parent=config_win)
            config_win.destroy()
            self.create_welcome_screen()

        ttk.Button(buttons_frame, text="Guardar", command=_save_and_close).pack(side=tk.LEFT, padx=10)
        ttk.Button(buttons_frame, text="Cancelar", command=config_win.destroy).pack(side=tk.LEFT, padx=10)
        main_frame.columnconfigure(1, weight=1)

    # --- UI del Emisor (SENDER) ---
    def show_sender_ui(self):
        self._clear_container()
        self.root.geometry("550x550")
        sender_frame = ttk.Frame(self.main_container, padding="10")
        sender_frame.pack(expand=True, fill=tk.BOTH)
        sender_frame.grid_columnconfigure(1, weight=1)
        sender_frame.grid_rowconfigure(4, weight=1) # Fila del Treeview
        
        ttk.Label(sender_frame, text="Nombre de la Sesión:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.session_name_entry = ttk.Entry(sender_frame, textvariable=self.session_name)
        self.session_name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=5, pady=5)
        
        ttk.Label(sender_frame, text="Archivo a Enviar:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.file_path_entry = ttk.Entry(sender_frame, textvariable=self.selected_file_path, state="readonly")
        self.file_path_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=5)
        self.select_file_btn = ttk.Button(sender_frame, text="Seleccionar...", command=self._select_file)
        self.select_file_btn.grid(row=1, column=2, sticky="ew", padx=5, pady=5)

        self.multiclient_checkbox = ttk.Checkbutton(sender_frame, text="Enviar a múltiples clientes", variable=self.multiclient_mode_var, command=self._on_multiclient_toggle)
        self.multiclient_checkbox.grid(row=2, column=0, columnspan=3, sticky='w', padx=5, pady=5)
        
        self.clients_tree_label = ttk.Label(sender_frame, text="Clientes Conectados:")
        self.clients_tree = ttk.Treeview(sender_frame, columns=("cliente",), show="headings", height=5)
        self.clients_tree.heading("cliente", text="Nombre de Usuario")
        
        self.action_btn = ttk.Button(sender_frame, text="Enviar Archivo", command=self._handle_sender_action)
        self.action_btn.grid(row=5, column=0, columnspan=3, pady=10, ipady=5)
        
        self.progress_text.set("")
        self.progress_bar = ttk.Progressbar(sender_frame, variable=self.progress_var, maximum=100)
        self.progress_label = ttk.Label(sender_frame, textvariable=self.progress_text, anchor="center")
        
        self.status_label = ttk.Label(sender_frame, text="Selecciona un archivo para enviar.")
        self.status_label.grid(row=8, column=0, columnspan=3, sticky="w", padx=5, pady=10)
        
        back_btn = ttk.Button(sender_frame, text="← Volver al Menú Principal", command=self.create_welcome_screen)
        back_btn.grid(row=9, column=0, columnspan=3, sticky="w", padx=5, pady=20)
        
        self._set_sender_ui_state('initial')

    def _on_multiclient_toggle(self):
        if self.action_btn['state'] == 'normal':
            is_multi = self.multiclient_mode_var.get()
            self.action_btn.config(text="Enviar Archivo" if not is_multi else "Abrir Lobby")

    def _set_sender_ui_state(self, state):
        self.clients_tree_label.grid_remove()
        self.clients_tree.grid_remove()
        self.progress_bar.grid_remove()
        self.progress_label.grid_remove()

        is_multi = self.multiclient_mode_var.get()

        if state == 'initial':
            self.session_name_entry.config(state="normal")
            self.select_file_btn.config(state="normal")
            self.multiclient_checkbox.config(state="normal")
            self.action_btn.config(text="Enviar Archivo" if not is_multi else "Abrir Lobby", state="disabled")
            self._update_status("Selecciona un archivo para enviar.")
        elif state == 'ready':
            self.session_name_entry.config(state="normal")
            self.select_file_btn.config(state="normal")
            self.multiclient_checkbox.config(state="normal")
            self.action_btn.config(text="Enviar Archivo" if not is_multi else "Abrir Lobby", state="normal")
            self._update_status("Listo. Pulsa el botón para empezar.")
        elif state == 'lobby':
            self.session_name_entry.config(state="disabled")
            self.select_file_btn.config(state="disabled")
            self.multiclient_checkbox.config(state="disabled")
            self.clients_tree_label.grid(row=3, column=0, columnspan=3, sticky='w', padx=5, pady=(10,0))
            self.clients_tree.grid(row=4, column=0, columnspan=3, sticky='nsew', padx=5)
            self.action_btn.config(text="Iniciar Transmisión", state="normal")
            self._update_status("Lobby abierto. Esperando conexiones...")
        elif state == 'sending':
            self.session_name_entry.config(state="disabled")
            self.select_file_btn.config(state="disabled")
            self.multiclient_checkbox.config(state="disabled")
            self.progress_bar.grid(row=6, column=0, columnspan=3, sticky="ew", padx=5, pady=5, ipady=5)
            self.progress_label.grid(row=7, column=0, columnspan=3, sticky="ew", padx=5)
            self.action_btn.config(text="Cancelar", state="normal")

    def _handle_sender_action(self):
        if self.sender and self.sender.multiclient_mode and self.sender.is_active and not self.sender.transmission_started:
            if not self.sender.connected_clients:
                if not messagebox.askyesno("Lobby Vacío", "¿No hay clientes conectados. Quieres iniciar la transmisión de todas formas?", parent=self.root):
                    return
            self._set_sender_ui_state('sending')
            self.sender.start_transmission()
            return

        if self.sender and self.sender.is_active:
            self.sender.stop_session()
            self.sender = None
            self._set_sender_ui_state('ready')
            self._update_progress(0, "")
            for i in self.clients_tree.get_children(): self.clients_tree.delete(i)
            return

        file_path = self.selected_file_path.get()
        session_name = self.session_name.get()
        if not file_path or not session_name:
            messagebox.showerror("Error", "Selecciona un archivo y asigna un nombre a la sesión.")
            return

        is_multiclient = self.multiclient_mode_var.get()
        
        self.sender = Sender(
            file_path, session_name, self.config.get('username'),
            self._update_progress, self._update_status,
            self._add_client_to_list, self._remove_client_from_list
        )
        self.sender.start_session(multiclient=is_multiclient)

        if is_multiclient:
            self._set_sender_ui_state('lobby')
        else:
            self._set_sender_ui_state('sending')
    
    def _add_client_to_list(self, client_id, username):
        self.root.after(0, lambda: self.clients_tree.insert('', 'end', iid=client_id, values=(username,)))

    def _remove_client_from_list(self, client_id):
        if self.clients_tree.exists(client_id):
            self.root.after(0, lambda: self.clients_tree.delete(client_id))

    def show_receiver_ui(self):
        self._clear_container()
        self.root.geometry("600x550")
        receiver_frame = ttk.Frame(self.main_container, padding="10")
        receiver_frame.pack(expand=True, fill=tk.BOTH)
        receiver_frame.grid_columnconfigure(1, weight=1)
        receiver_frame.grid_rowconfigure(2, weight=1)
        
        ttk.Label(receiver_frame, text="Guardar en:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        folder_path_entry = ttk.Entry(receiver_frame, textvariable=self.selected_folder_path, state="readonly")
        folder_path_entry.grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        ttk.Button(receiver_frame, text="Seleccionar...", command=self._select_folder).grid(row=0, column=2, sticky="ew", padx=5, pady=5)
        
        ttk.Label(receiver_frame, text="Sesiones Disponibles:").grid(row=1, column=0, columnspan=3, sticky="w", padx=5, pady=5)
        cols = ("Sesión", "Enviado por", "Estado")
        self.sessions_tree = ttk.Treeview(receiver_frame, columns=cols, show="headings", height=8)
        self.sessions_tree.heading("Sesión", text="Nombre de la Sesión")
        self.sessions_tree.heading("Enviado por", text="Enviado por")
        self.sessions_tree.heading("Estado", text="Estado")
        self.sessions_tree.column("Enviado por", width=150, anchor='center')
        self.sessions_tree.column("Estado", width=100, anchor='center')
        self.sessions_tree.grid(row=2, column=0, columnspan=3, sticky="nsew", padx=5, pady=5)
        
        self.join_btn = ttk.Button(receiver_frame, text="Unirse y Descargar", command=self._join_session)
        self.join_btn.grid(row=3, column=0, columnspan=3, pady=10)
        
        self.progress_text.set("")
        progress_bar = ttk.Progressbar(receiver_frame, variable=self.progress_var, maximum=100)
        progress_bar.grid(row=4, column=0, columnspan=3, sticky="ew", padx=5, pady=5, ipady=5)
        progress_label = ttk.Label(receiver_frame, textvariable=self.progress_text, anchor="center")
        progress_label.grid(row=5, column=0, columnspan=3, sticky="ew", padx=5)

        self.status_label = ttk.Label(receiver_frame, text="Buscando sesiones...")
        self.status_label.grid(row=6, column=0, columnspan=3, sticky="w", padx=5, pady=10)
        
        back_btn = ttk.Button(receiver_frame, text="← Volver al Menú Principal", command=self.create_welcome_screen)
        back_btn.grid(row=7, column=0, columnspan=3, sticky="w", padx=5, pady=20)
        
        self._start_receiver_logic()

    def _update_status(self, message):
        if self.status_label and self.status_label.winfo_exists():
            self.root.after(0, lambda: self.status_label.config(text=message))

    def _update_progress(self, value, text=None):
        if text is None: text = f"{value:.1f}%"
        def task():
            if hasattr(self, 'progress_var'): self.progress_var.set(value)
            if hasattr(self, 'progress_text'): self.progress_text.set(text)
        self.root.after(0, task)

    def _select_file(self):
        path = filedialog.askopenfilename()
        if path:
            self.selected_file_path.set(path)
            self.session_name.set(os.path.basename(path))
            self._set_sender_ui_state('ready')

    def _select_folder(self):
        path = filedialog.askdirectory()
        if path: self.selected_folder_path.set(path)
    
    def _start_receiver_logic(self):
        self.receiver = Receiver(self._update_progress, self._update_status, self._on_download_complete)
        self.service_browser = PyCastServiceBrowser(self._add_session, self._remove_session, self._update_session)
        self.receiver.start_listening()
        
    def _on_download_complete(self):
        def task():
            if self.join_btn and self.join_btn.winfo_exists(): self.join_btn.config(state='normal')
            self._update_status("¡Descarga completa! Listo para una nueva descarga.")
        self.root.after(0, task)

    def _add_session(self, details):
        if self.sessions_tree and self.sessions_tree.winfo_exists():
            session_id = details['session_id']
            if not self.sessions_tree.exists(session_id):
                self.active_sessions[session_id] = details
                values = (details['session_name'], details['username'], details['status'].capitalize())
                self.root.after(0, lambda: self.sessions_tree.insert('', 'end', iid=session_id, values=values))

    def _remove_session(self, session_id):
        if self.sessions_tree and self.sessions_tree.winfo_exists() and self.sessions_tree.exists(session_id):
            self.active_sessions.pop(session_id, None)
            self.root.after(0, lambda: self.sessions_tree.delete(session_id))

    def _update_session(self, details):
        session_id = details['session_id']
        if self.sessions_tree and self.sessions_tree.winfo_exists() and self.sessions_tree.exists(session_id):
            self.active_sessions[session_id] = details
            values = (details['session_name'], details['username'], details['status'].capitalize())
            self.root.after(0, lambda: self.sessions_tree.item(session_id, values=values))

    def _join_session(self):
        selected_item_id = self.sessions_tree.focus()
        if not selected_item_id:
            messagebox.showerror("Error", "Por favor, selecciona una sesión de la lista.")
            return
            
        destination_folder = self.selected_folder_path.get()
        session_info = self.active_sessions.get(selected_item_id)
        if not session_info or session_info.get('status') == 'busy':
            messagebox.showwarning("Sesión Ocupada", "Esta sesión no está disponible.", parent=self.root)
            return

        self._update_progress(0, "")
        self.join_btn.config(state="disabled")
        
        threading.Thread(
            target=self._perform_handshake_and_wait, 
            args=(session_info, destination_folder), 
            daemon=True
        ).start()

    def _perform_handshake_and_wait(self, session_info, destination_folder):
        try:
            self._update_status(f"Conectando con {session_info.get('username')}...")
            handshake_sock = socket.create_connection((session_info['address'], HANDSHAKE_PORT), timeout=5)
            
            with handshake_sock:
                payload = json.dumps({
                    'session_id': session_info['session_id'],
                    'username': self.config.get('username')
                }).encode('utf-8')
                handshake_sock.sendall(payload)

                response = handshake_sock.recv(1024)
                if response == b'ACK_MULTI':
                    self._update_status("Conectado. Esperando que el emisor inicie...")
                    handshake_sock.settimeout(None)
                    start_signal = handshake_sock.recv(1024)
                    if start_signal != b'START':
                        raise ConnectionAbortedError("Señal de inicio inválida.")
                elif response != b'ACK_SINGLE':
                    raise ConnectionAbortedError("Respuesta de handshake desconocida.")
            
            self.receiver.join_session(session_info['session_id'], destination_folder)

        except (socket.timeout, ConnectionRefusedError, OSError, ConnectionAbortedError) as e:
            messagebox.showerror("Error de Conexión", f"No se pudo contactar al emisor: {e}", parent=self.root)
            self.root.after(0, lambda: self.join_btn.config(state="normal"))
        
    def _cleanup_network_services(self):
        if self.sender: self.sender.stop_session()
        self.sender = None
        if self.service_browser: self.service_browser.stop()
        self.service_browser = None
        if self.receiver: self.receiver.stop_listening()
        self.receiver = None

    def _on_closing(self):
        if messagebox.askokcancel("Salir", "¿Estás seguro de que quieres salir?"):
            self._cleanup_network_services()
            self.root.destroy()

    def run(self):
        self.root.mainloop()

# ----------------------------------------------------------------------------
# -------- PARTE 2: Lógica de la Interfaz de Línea de Comandos (CLI) ----------
# ----------------------------------------------------------------------------

# <-- AÑADIDO: Lock para proteger la salida de la consola de accesos simultáneos
print_lock = threading.Lock()

def cli_status_callback(message):
    """Callback para mostrar mensajes de estado en la consola de forma segura."""
    with print_lock:
        # Imprime una línea vacía para no sobrescribir la barra de progreso
        sys.stdout.write('\r' + ' ' * 80 + '\r') 
        print(f"[INFO] {message}")
        sys.stdout.flush()

def cli_progress_callback(value, text=None):
    """Callback para mostrar una barra de progreso en la consola de forma segura."""
    with print_lock:
        bar_length = 40
        progress = int((value / 100) * bar_length)
        bar = '#' * progress + '-' * (bar_length - progress)
        percent_str = f"{value:.1f}%"
        # \r mueve el cursor al inicio, end='' evita el salto de línea
        sys.stdout.write(f"\rProgreso: [{bar}] {percent_str.rjust(6)}")
        sys.stdout.flush()
        if value >= 100:
            sys.stdout.write('\n')

def cli_client_connected(client_id, username):
    """Callback para notificar la conexión de un cliente en modo lobby."""
    with print_lock:
        print(f"\n[+] Cliente conectado: {username} ({client_id[:8]})")

def cli_client_disconnected(client_id):
    """Callback para notificar la desconexión de un cliente."""
    with print_lock:
        print(f"\n[-] Cliente desconectado: {client_id[:8]}")

def handle_cli_send(args):
    """Maneja la lógica para el subcomando 'send'."""
    if not os.path.exists(args.file_path):
        print(f"Error: El archivo '{args.file_path}' no fue encontrado.", file=sys.stderr)
        sys.exit(1)
    
    config = load_config()
    session_name = args.name or os.path.basename(args.file_path)
    
    sender = Sender(
        args.file_path,
        session_name,
        config.get('username', 'cli-user'),
        cli_progress_callback,
        cli_status_callback,
        cli_client_connected,
        cli_client_disconnected
    )

    try:
        sender.start_session(multiclient=args.multi)

        if args.multi:
            input("Lobby abierto. Presiona Enter para iniciar la transmisión...")
            if not sender.connected_clients:
                print("Advertencia: No hay clientes conectados. Iniciando de todas formas.")
            sender.start_transmission()

        # Espera activa mientras la transmisión está en curso
        while sender.is_active or sender.transmission_started:
            if sender.transmission_started and not sender.is_active:
                break 
            time.sleep(0.5)

    except KeyboardInterrupt:
        print("\nCancelando por el usuario...")
    finally:
        sender.stop_session()
    
    print("Operación finalizada.")

def handle_cli_receive(args):
    """Maneja la lógica para el subcomando 'receive'."""
    config = load_config()
    output_dir = args.output_dir or config.get('download_folder')
    if not os.path.isdir(output_dir):
        print(f"Error: El directorio de salida '{output_dir}' no existe.", file=sys.stderr)
        sys.exit(1)

    print(f"Guardando archivos en: {output_dir}")

    active_sessions = {}
    sessions_lock = threading.Lock()
    completion_event = threading.Event()

    def on_download_complete():
        with print_lock:
            print("\n¡Descarga completada!")
        completion_event.set()

    receiver = Receiver(cli_progress_callback, cli_status_callback, on_download_complete)
    
    def add_s(d):
        with sessions_lock: active_sessions[d['session_id']] = d
    def rem_s(sid):
        with sessions_lock: active_sessions.pop(sid, None)
    def upd_s(d):
        with sessions_lock: active_sessions[d['session_id']] = d

    browser = PyCastServiceBrowser(add_s, rem_s, upd_s)
    receiver.start_listening()

    try:
        print("Buscando sesiones en la red (Ctrl+C para salir)...")
        time.sleep(3) # Esperar un poco para descubrir servicios

        with sessions_lock:
            if not active_sessions:
                print("No se encontraron sesiones. Saliendo.")
                return
            
            print("\nSesiones disponibles:")
            session_list = list(active_sessions.values())
            for i, sess in enumerate(session_list):
                print(f"  {i+1}) '{sess['session_name']}' por {sess['username']} [{sess['status']}]")
        
        choice = input("Elige el número de la sesión a descargar (o 'q' para salir): ")
        if choice.lower() == 'q': return

        try:
            index = int(choice) - 1
            if not (0 <= index < len(session_list)):
                raise ValueError
            session_info = session_list[index]
        except ValueError:
            print("Selección inválida.", file=sys.stderr)
            return

        if session_info.get('status') == 'busy':
            print("Esa sesión está ocupada.", file=sys.stderr)
            return
            
        print(f"Intentando unirse a la sesión '{session_info['session_name']}'...")
        # Lógica de Handshake (copiada y adaptada de la GUI)
        try:
            handshake_sock = socket.create_connection((session_info['address'], HANDSHAKE_PORT), timeout=5)
            with handshake_sock:
                payload = json.dumps({
                    'session_id': session_info['session_id'],
                    'username': config.get('username')
                }).encode('utf-8')
                handshake_sock.sendall(payload)
                response = handshake_sock.recv(1024)
                if response == b'ACK_MULTI':
                    cli_status_callback("Conectado al lobby. Esperando que el emisor inicie...")
                    handshake_sock.settimeout(None)
                    start_signal = handshake_sock.recv(1024)
                    if start_signal != b'START':
                        raise ConnectionAbortedError("Señal de inicio inválida.")
                elif response != b'ACK_SINGLE':
                    raise ConnectionAbortedError("Respuesta de handshake desconocida.")
            receiver.join_session(session_info['session_id'], output_dir)
            completion_event.wait() # Esperar a que la descarga termine
        except Exception as e:
            print(f"\nError de conexión: {e}", file=sys.stderr)

    except KeyboardInterrupt:
        print("\nCancelando por el usuario...")
    finally:
        browser.stop()
        receiver.stop_listening()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PyCast: Herramienta de transferencia de archivos en LAN.")
    subparsers = parser.add_subparsers(dest='command', help='Comando a ejecutar')

    # Subcomando 'send'
    send_parser = subparsers.add_parser('send', help='Enviar un archivo.')
    send_parser.add_argument('file_path', metavar='ARCHIVO', help='Ruta al archivo que se va a enviar.')
    send_parser.add_argument('--name', help='Nombre personalizado para la sesión (por defecto: nombre del archivo).')
    send_parser.add_argument('--multi', action='store_true', help='Habilitar modo multi-cliente (lobby).')

    # Subcomando 'receive'
    receive_parser = subparsers.add_parser('receive', help='Recibir un archivo.')
    receive_parser.add_argument('--output-dir', metavar='CARPETA', help='Carpeta para guardar los archivos descargados.')

    args = parser.parse_args()

    if args.command == 'send':
        handle_cli_send(args)
    elif args.command == 'receive':
        handle_cli_receive(args)
    else:
        # Si no se especifica ningún comando, se lanza la interfaz gráfica
        main_window = tk.Tk()
        app = PyCastApp(main_window)
        app.run()
