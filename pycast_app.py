# pycast_app.py
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, TclError
import socket
import json
import threading
import os
import argparse
import sys
import time
from tkinterdnd2 import DND_FILES, TkinterDnD

from sender import Sender, HANDSHAKE_PORT
from receiver import Receiver
from service_discovery import PyCastServiceBrowser
from config_manager import load_config, save_config, get_default_config, CONFIG_METADATA, CONFIG_PRESETS

# --- INICIO: LÓGICA Y FUNCIONES AUXILIARES PARA EL MODO CLI ---

def _cli_print_progress(bytes_processed, total_bytes):
    """Muestra una barra de progreso en la terminal."""
    if total_bytes == 0: return
    percentage = (bytes_processed / total_bytes) * 100
    bar_len = 40
    filled_len = int(round(bar_len * percentage / 100.0))
    bar = '█' * filled_len + '-' * (bar_len - filled_len)
    
    processed_mb = bytes_processed / (1024 * 1024)
    total_mb = total_bytes / (1024 * 1024)
    
    sys.stdout.write(f'\rProgreso: [{bar}] {percentage:.1f}% ({processed_mb:.2f}/{total_mb:.2f} MB)')
    sys.stdout.flush()
    if bytes_processed == total_bytes:
        sys.stdout.write('\n')

def run_cli_sender(args, config):
    """Ejecuta la lógica del emisor en modo CLI."""
    if not os.path.exists(args.file_path):
        print(f"Error: El archivo '{args.file_path}' no existe.")
        return

    session_name = args.name if args.name else os.path.basename(args.file_path)
    
    clients_connected = []
    # --- MODIFICADO: Callback de conexión de cliente mejorado ---
    prompt_message = f"\n>>> Para iniciar la transmisión para todos, pulsa Enter... "
    
    def _client_connected(client_id, username):
        clients_connected.append(username)
        # Limpia la línea actual para evitar sobreescribir el prompt de input() de forma desordenada
        sys.stdout.write('\r' + ' ' * 80 + '\r')
        print(f"> '{username}' se ha unido al lobby. Clientes actuales: {len(clients_connected)}.")
        sys.stdout.write(prompt_message)
        sys.stdout.flush()

    def _client_disconnected(client_id):
        pass

    sender = Sender(
        args.file_path,
        session_name,
        config,
        _cli_print_progress,
        lambda msg: print(f"\n[Estado] {msg}"), # Añadido \n para mejor formato
        _client_connected,
        _client_disconnected
    )

    try:
        sender.start_session(multiclient=args.multi)
        
        if args.multi:
            print(f"Lobby abierto para la sesión '{session_name}'.")
            print("Esperando que los clientes se unan...")
            # Muestra el prompt inicial y espera la entrada del usuario
            input(prompt_message) 
            
            if not clients_connected:
                print("\nAdvertencia: No hay clientes en el lobby. Iniciando de todas formas.")
            sender.start_transmission()
        
        while sender.is_active:
            time.sleep(1)

        print("\nOperación finalizada.")

    except KeyboardInterrupt:
        print("\nOperación cancelada por el usuario.")
    finally:
        sender.stop_session()


def run_cli_receiver(args, config):
    """Ejecuta la lógica del receptor en modo CLI."""
    active_sessions = {}
    sessions_lock = threading.Lock()
    download_complete_event = threading.Event()
    download_status = "pending"

    def _add_session(details):
        with sessions_lock:
            active_sessions[details['session_id']] = details
    
    def _remove_session(session_id):
        with sessions_lock:
            active_sessions.pop(session_id, None)

    def _on_cli_download_complete(status):
        nonlocal download_status
        download_status = status
        download_complete_event.set()

    receiver = Receiver(config, _cli_print_progress, lambda msg: print(f"[Estado] {msg}"), _on_cli_download_complete)
    service_browser = PyCastServiceBrowser(_add_session, _remove_session, _add_session)
    
    try:
        receiver.start_listening()
        print("Buscando sesiones en la red (Ctrl+C para salir)...")
        
        chosen_session = None
        while not chosen_session:
            time.sleep(2)
            with sessions_lock:
                if not active_sessions:
                    print("\rEsperando sesiones...", end="")
                    sys.stdout.flush()
                    continue

                print("\n\nSesiones disponibles:")
                sorted_sessions = sorted(active_sessions.values(), key=lambda x: x['session_name'])
                for i, details in enumerate(sorted_sessions):
                    print(f"  {i+1}) '{details['session_name']}' por {details['username']} [{details['status']}]")

            try:
                choice = input("\nElige el número de la sesión a descargar (o 'q' para salir): ")
                if choice.lower() == 'q':
                    return
                choice_idx = int(choice) - 1
                if 0 <= choice_idx < len(sorted_sessions):
                    session_info = sorted_sessions[choice_idx]
                    if session_info['status'] != 'available':
                        print("Esa sesión no está disponible (está ocupada). Inténtalo de nuevo.")
                        continue
                    chosen_session = session_info
                else:
                    print("Selección inválida.")
            except ValueError:
                print("Por favor, introduce un número.")

        output_dir = args.output_dir if args.output_dir else config['download_folder']
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)
            print(f"Carpeta de destino creada: {output_dir}")

        print(f"Intentando conectar con la sesión '{chosen_session['session_name']}'...")
        
        try:
            handshake_sock = socket.create_connection((chosen_session['address'], HANDSHAKE_PORT), timeout=10)
            with handshake_sock:
                payload = json.dumps({'session_id': chosen_session['session_id'], 'username': config.get('username')}).encode('utf-8')
                handshake_sock.sendall(payload)
                response = handshake_sock.recv(1024)
                if response == b'ACK_MULTI':
                    print("Conectado al lobby. Esperando que el emisor inicie la transmisión...")
                    handshake_sock.settimeout(None)
                    start_signal = handshake_sock.recv(1024)
                    if start_signal != b'START':
                        raise ConnectionAbortedError("Señal de inicio inválida.")
                elif response != b'ACK_SINGLE':
                    raise ConnectionAbortedError("Respuesta de handshake desconocida.")
            
            receiver.join_session(chosen_session, output_dir)
            print("¡Conexión exitosa! Esperando datos...")
            
            download_complete_event.wait()
            
            if download_status == "completed":
                print("\n¡Descarga completada y verificada con éxito!")
            elif download_status == "failed_verification":
                print("\n¡ERROR! El archivo recibido estaba corrupto y ha sido eliminado.")
            else:
                print("\nLa descarga fue cancelada.")

        except (socket.timeout, ConnectionRefusedError, OSError, ConnectionAbortedError) as e:
            print(f"\nError de Conexión: No se pudo contactar al emisor. {e}")

    except KeyboardInterrupt:
        print("\nOperación cancelada por el usuario.")
    finally:
        if service_browser: service_browser.stop()
        if receiver: receiver.stop_listening()
        print("Cerrando la aplicación CLI.")

# --- FIN: LÓGICA Y FUNCIONES AUXILIARES PARA EL MODO CLI ---


# Clase auxiliar para crear Tooltips (cuadros de ayuda)
class Tooltip:
    """
    Crea un tooltip (cuadro de ayuda emergente) para un widget de tkinter.
    """
    def __init__(self, widget, text, wraplength=300):
        self.widget = widget
        self.text = text
        self.wraplength = wraplength
        self.tooltip_window = None
        self.id = None
        self.widget.bind("<Enter>", self.schedule_show)
        self.widget.bind("<Leave>", self.hide)
        self.widget.bind("<ButtonPress>", self.hide)

    def schedule_show(self, event=None):
        self.id = self.widget.after(500, self.show)

    def show(self):
        if self.tooltip_window or not self.text:
            return
        x, y, _, _ = self.widget.bbox("insert")
        x += self.widget.winfo_rootx() + 25
        y += self.widget.winfo_rooty() + 20
        self.tooltip_window = tk.Toplevel(self.widget)
        self.tooltip_window.wm_overrideredirect(True)
        self.tooltip_window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(self.tooltip_window, text=self.text, justify='left',
                         background="#ffffe0", relief='solid', borderwidth=1,
                         wraplength=self.wraplength, font=("tahoma", "8", "normal"))
        label.pack(ipadx=1)

    def hide(self, event=None):
        if self.id:
            self.widget.after_cancel(self.id)
            self.id = None
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None

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
        
        self.selected_file_path = tk.StringVar()
        self.selected_folder_path = tk.StringVar(value=self.config.get('download_folder'))
        self.session_name = tk.StringVar()
        self.progress_var = tk.DoubleVar()
        self.progress_text = tk.StringVar()
        self.multiclient_mode_var = tk.BooleanVar(value=self.config.get('multiclient_enabled_by_default'))
        self.active_sessions = {}

        self.status_label = None
        self.sessions_tree = None

        self.last_update_time = 0
        self.last_bytes_processed = 0

        self.create_welcome_screen()

    def _clear_container(self):
        for widget in self.main_container.winfo_children():
            widget.destroy()

    def create_welcome_screen(self):
        self._clear_container()
        self.root.geometry("400x350")
        self._cleanup_network_services()
        
        self.selected_file_path.set("")
        self.session_name.set("")
        self.progress_var.set(0)
        self.progress_text.set("")
        self.multiclient_mode_var.set(self.config.get('multiclient_enabled_by_default'))
        
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
        config_win.geometry("520x520") 
        config_win.resizable(False, False)
        config_win.transient(self.root)
        config_win.grab_set()
        
        main_frame = ttk.Frame(config_win, padding="15")
        main_frame.pack(expand=True, fill=tk.BOTH)
        main_frame.columnconfigure(1, weight=1)
        
        username_var = tk.StringVar(value=self.config.get('username'))
        folder_var = tk.StringVar(value=self.config.get('download_folder'))
        multiclient_var = tk.BooleanVar(value=self.config.get('multiclient_enabled_by_default'))

        user_label = ttk.Label(main_frame, text=CONFIG_METADATA['username']['label'] + ":")
        user_label.grid(row=0, column=0, sticky="w", pady=5, padx=(0, 10))
        user_entry = ttk.Entry(main_frame, textvariable=username_var)
        user_entry.grid(row=0, column=1, columnspan=2, sticky="ew")
        Tooltip(user_label, CONFIG_METADATA['username']['help'])

        folder_label = ttk.Label(main_frame, text=CONFIG_METADATA['download_folder']['label'] + ":")
        folder_label.grid(row=1, column=0, sticky="w", pady=5, padx=(0, 10))
        folder_entry = ttk.Entry(main_frame, textvariable=folder_var, state="readonly")
        folder_entry.grid(row=1, column=1, sticky="ew")
        Tooltip(folder_label, CONFIG_METADATA['download_folder']['help'])
        
        def _select_config_folder():
            path = filedialog.askdirectory(title="Selecciona la carpeta de descargas", parent=config_win)
            if path: folder_var.set(path)

        ttk.Button(main_frame, text="...", command=_select_config_folder, width=3).grid(row=1, column=2, padx=(5,0))
        
        multi_check = ttk.Checkbutton(main_frame, text=CONFIG_METADATA['multiclient_enabled_by_default']['label'], variable=multiclient_var)
        multi_check.grid(row=2, column=0, columnspan=3, sticky='w', pady=10)
        Tooltip(multi_check, CONFIG_METADATA['multiclient_enabled_by_default']['help'])
        
        ttk.Separator(main_frame, orient='horizontal').grid(row=3, column=0, columnspan=3, sticky='ew', pady=15)
        
        expert_frame = ttk.LabelFrame(main_frame, text=" Configuración de Red (Avanzado) ", padding="10")
        expert_frame.grid(row=5, column=0, columnspan=3, sticky='ew')
        expert_frame.columnconfigure(1, weight=1)
        
        net_settings = self.config.get('network_settings', get_default_config()['network_settings'])
        net_vars = {key: tk.StringVar(value=str(val)) for key, val in net_settings.items()}
        expert_entries = []
        
        preset_var = tk.StringVar()
        trace_id_map = {}

        def _apply_preset(event=None):
            preset_name = preset_var.get()
            if preset_name == "Personalizado" or preset_name not in CONFIG_PRESETS:
                return

            for key, var in net_vars.items():
                var.trace_remove("write", trace_id_map[key])

            settings = CONFIG_PRESETS[preset_name]['settings']
            for key, value in settings.items():
                net_vars[key].set(str(value))
            
            for key, var in net_vars.items():
                trace_id_map[key] = var.trace_add("write", _on_manual_edit)
            
        def _check_for_custom_settings():
            current_settings = {}
            try:
                current_settings['chunk_size'] = int(net_vars['chunk_size'].get())
                current_settings['block_size_packets'] = int(net_vars['block_size_packets'].get())
                current_settings['nack_listen_timeout'] = float(net_vars['nack_listen_timeout'].get())
                current_settings['repair_rounds'] = int(net_vars['repair_rounds'].get())
            except (ValueError, TclError):
                return "Personalizado"

            for name, preset in CONFIG_PRESETS.items():
                if preset['settings'] == current_settings:
                    return name
            return "Personalizado"

        def _on_manual_edit(*args):
            if preset_var.get() != "Personalizado":
                preset_var.set("Personalizado")

        preset_label = ttk.Label(main_frame, text="Perfil de Red:")
        preset_label.grid(row=4, column=0, sticky="w", pady=(5,0), padx=(0,10))
        preset_options = list(CONFIG_PRESETS.keys()) + ["Personalizado"]
        preset_combo = ttk.Combobox(main_frame, textvariable=preset_var, values=preset_options, state="readonly")
        preset_combo.grid(row=4, column=1, columnspan=2, sticky="ew", pady=(5,0))
        preset_combo.bind("<<ComboboxSelected>>", _apply_preset)
        Tooltip(preset_label, "Selecciona un perfil predefinido para ajustar automáticamente la configuración de red.")
        
        net_row = 0
        for key, var in net_vars.items():
            meta = CONFIG_METADATA['network_settings'][key]
            label = ttk.Label(expert_frame, text=meta['label'] + ":")
            label.grid(row=net_row, column=0, sticky="w", pady=3)
            Tooltip(label, meta['help'])
            entry = ttk.Entry(expert_frame, textvariable=var, state='disabled')
            entry.grid(row=net_row, column=1, sticky="ew")
            Tooltip(entry, meta['help'])
            expert_entries.append(entry)
            trace_id_map[key] = var.trace_add("write", _on_manual_edit)
            net_row += 1

        preset_var.set(_check_for_custom_settings())

        def _restore_defaults():
            preset_var.set("Wi-Fi (Estándar)")
            _apply_preset()
            messagebox.showinfo("Restaurado", "Valores de red restaurados al perfil estándar.", parent=config_win)

        restore_btn = ttk.Button(expert_frame, text="Restaurar Perfil Estándar", command=_restore_defaults, state='disabled')
        restore_btn.grid(row=net_row, column=0, columnspan=2, pady=(10,0))
        
        def _toggle_expert_mode():
            new_state = 'normal' if expert_mode_var.get() else 'disabled'
            for entry in expert_entries:
                entry.config(state=new_state)
            restore_btn.config(state=new_state)
            preset_combo.config(state=new_state)

        expert_mode_var = tk.BooleanVar()
        expert_check = ttk.Checkbutton(main_frame, text="Habilitar edición de configuración de red", variable=expert_mode_var, command=_toggle_expert_mode)
        expert_check.grid(row=6, column=0, columnspan=3, sticky='w', pady=(5,15))

        buttons_frame = ttk.Frame(main_frame)
        buttons_frame.grid(row=7, column=0, columnspan=3, pady=(20, 0))

        def _save_and_close():
            try:
                self.config['username'] = username_var.get().strip()
                self.config['download_folder'] = folder_var.get().strip()
                self.config['multiclient_enabled_by_default'] = multiclient_var.get()
                
                net_settings_data = self.config['network_settings']
                for key, var in net_vars.items():
                    if key == 'nack_listen_timeout':
                        net_settings_data[key] = float(var.get())
                    else:
                        net_settings_data[key] = int(var.get())

                self.selected_folder_path.set(self.config['download_folder'])
                self.multiclient_mode_var.set(self.config['multiclient_enabled_by_default'])
                
                save_config(self.config)
                messagebox.showinfo("Guardado", "La configuración ha sido guardada.", parent=config_win)
                config_win.destroy()
                self.create_welcome_screen()
            except ValueError:
                messagebox.showerror("Error de Validación", "Los valores de red deben ser números válidos.", parent=config_win)
            except Exception as e:
                messagebox.showerror("Error al Guardar", f"Ocurrió un error: {e}", parent=config_win)

        ttk.Button(buttons_frame, text="Guardar", command=_save_and_close).pack(side=tk.LEFT, padx=10)
        ttk.Button(buttons_frame, text="Cancelar", command=config_win.destroy).pack(side=tk.LEFT, padx=10)
        
        _toggle_expert_mode()
    
    def show_sender_ui(self):
        self._clear_container()
        self.root.geometry("550x600")
        sender_frame = ttk.Frame(self.main_container, padding="10")
        sender_frame.pack(expand=True, fill=tk.BOTH)
        sender_frame.grid_columnconfigure(1, weight=1)
        sender_frame.grid_rowconfigure(5, weight=1)
        
        ttk.Label(sender_frame, text="Nombre de la Sesión:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        self.session_name_entry = ttk.Entry(sender_frame, textvariable=self.session_name)
        self.session_name_entry.grid(row=0, column=1, columnspan=2, sticky="ew", padx=5, pady=5)
        
        ttk.Label(sender_frame, text="Archivo a Enviar:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.file_path_entry = ttk.Entry(sender_frame, textvariable=self.selected_file_path, state="readonly")
        self.file_path_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=5)
        self.select_file_btn = ttk.Button(sender_frame, text="Seleccionar...", command=self._select_file)
        self.select_file_btn.grid(row=1, column=2, sticky="ew", padx=5, pady=5)

        self.drop_target_frame = ttk.LabelFrame(sender_frame, text="Arrastra y suelta un archivo aquí", padding="10")
        self.drop_target_frame.grid(row=2, column=0, columnspan=3, sticky="ew", padx=5, pady=10)
        self.drop_target_frame.grid_columnconfigure(0, weight=1)
        ttk.Label(self.drop_target_frame, text="O haz clic en 'Seleccionar...' arriba", anchor="center").grid(row=0, column=0, sticky="ew", pady=5)

        self.drop_target_frame.drop_target_register(DND_FILES)
        self.drop_target_frame.dnd_bind('<<Drop>>', self._on_file_drop_dnd)
        self.drop_target_frame.bind("<Enter>", lambda e: self.drop_target_frame.config(text="¡Suelta el archivo!"))
        self.drop_target_frame.bind("<Leave>", lambda e: self.drop_target_frame.config(text="Arrastra y suelta un archivo aquí"))

        self.multiclient_checkbox = ttk.Checkbutton(sender_frame, text="Enviar a múltiples clientes", variable=self.multiclient_mode_var, command=self._on_multiclient_toggle)
        self.multiclient_checkbox.grid(row=3, column=0, columnspan=3, sticky='w', padx=5, pady=5)
        
        self.clients_tree_label = ttk.Label(sender_frame, text="Clientes Conectados:")
        
        self.clients_frame = ttk.Frame(sender_frame)
        self.clients_frame.grid_rowconfigure(0, weight=1)
        self.clients_frame.grid_columnconfigure(0, weight=1)

        self.clients_tree = ttk.Treeview(self.clients_frame, columns=("cliente",), show="headings", height=8)
        self.clients_tree.heading("cliente", text="Nombre de Usuario")
        self.clients_tree.grid(row=0, column=0, sticky='nsew')

        scrollbar = ttk.Scrollbar(self.clients_frame, orient="vertical", command=self.clients_tree.yview)
        scrollbar.grid(row=0, column=1, sticky='ns')
        self.clients_tree.configure(yscrollcommand=scrollbar.set)
        
        self.action_btn = ttk.Button(sender_frame, text="Enviar Archivo", command=self._handle_sender_action)
        self.action_btn.grid(row=6, column=0, columnspan=3, pady=10, ipady=5)
        
        self.progress_text.set("")
        self.progress_bar = ttk.Progressbar(sender_frame, variable=self.progress_var, maximum=100)
        self.progress_label = ttk.Label(sender_frame, textvariable=self.progress_text, anchor="center")
        
        self.status_label = ttk.Label(sender_frame, text="Selecciona un archivo para enviar.")
        self.status_label.grid(row=9, column=0, columnspan=3, sticky="w", padx=5, pady=10)
        
        back_btn = ttk.Button(sender_frame, text="← Volver al Menú Principal", command=self.create_welcome_screen)
        back_btn.grid(row=10, column=0, columnspan=3, sticky="w", padx=5, pady=20)
        
        self._set_sender_ui_state('initial')

    def _on_file_drop_dnd(self, event):
        files = self.root.tk.splitlist(event.data)
        if not files:
            messagebox.showwarning("Error de Arrastre", "No se pudo identificar el archivo soltado.", parent=self.root)
            return
        if len(files) > 1:
            messagebox.showinfo("Información", "Solo se puede enviar un archivo a la vez. Se usará el primero de la lista.", parent=self.root)
        file_path = files[0]
        if os.path.isfile(file_path):
            self.selected_file_path.set(file_path)
            self.session_name.set(os.path.basename(file_path))
            self._set_sender_ui_state('ready')
        else:
            messagebox.showwarning("Error de Arrastre", f"El elemento soltado no es un archivo válido o no existe:\n{file_path}", parent=self.root)
        self.drop_target_frame.config(text="Arrastra y suelta un archivo aquí")

    def _on_multiclient_toggle(self):
        if self.action_btn['state'] == 'normal':
            is_multi = self.multiclient_mode_var.get()
            self.action_btn.config(text="Enviar Archivo" if not is_multi else "Abrir Lobby")

    def _set_sender_ui_state(self, state):
        self.clients_tree_label.grid_remove()
        self.clients_frame.grid_remove()
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
            self.clients_tree_label.grid(row=4, column=0, columnspan=3, sticky='w', padx=5, pady=(10,0))
            self.clients_frame.grid(row=5, column=0, columnspan=3, sticky='nsew', padx=5)
            self.action_btn.config(text="Iniciar Transmisión", state="normal")
            self._update_status("Lobby abierto. Esperando conexiones...")
        elif state == 'sending':
            self.session_name_entry.config(state="disabled")
            self.select_file_btn.config(state="disabled")
            self.multiclient_checkbox.config(state="disabled")
            self.progress_bar.grid(row=7, column=0, columnspan=3, sticky="ew", padx=5, pady=5, ipady=5)
            self.progress_label.grid(row=8, column=0, columnspan=3, sticky="ew", padx=5)
            self.action_btn.config(text="Cancelar", state="normal")

    def _handle_sender_action(self):
        if self.sender and self.sender.multiclient_mode and not self.sender.transmission_started:
            if not self.sender.connected_clients:
                if not messagebox.askyesno("Lobby Vacío", "¿No hay clientes conectados. Quieres iniciar la transmisión de todas formas?", parent=self.root):
                    return
            self._set_sender_ui_state('sending')
            self.root.update_idletasks()
            self.last_update_time = time.time()
            self.last_bytes_processed = 0
            threading.Thread(target=self.sender.start_transmission, daemon=True).start()
            return

        if self.sender:
            self.sender.stop_session()
            self.sender = None
            self._set_sender_ui_state('ready')
            self._update_progress(0, 0)
            for i in self.clients_tree.get_children(): self.clients_tree.delete(i)
            return

        file_path = self.selected_file_path.get()
        session_name = self.session_name.get()
        if not file_path or not os.path.exists(file_path):
            messagebox.showerror("Error", "El archivo seleccionado no es válido o no existe.")
            return
        if not session_name:
            messagebox.showerror("Error", "Asigna un nombre a la sesión.")
            return

        is_multiclient = self.multiclient_mode_var.get()

        def setup_and_run_sender():
            self.sender = Sender(
                file_path, session_name, self.config,
                self._update_progress, self._update_status,
                self._add_client_to_list, self._remove_client_from_list
            )
            self.sender.start_session(multiclient=is_multiclient)

        if is_multiclient: self._set_sender_ui_state('lobby')
        else:
            self._set_sender_ui_state('sending')
            self.last_update_time = time.time()
            self.last_bytes_processed = 0
        
        self.root.update_idletasks()
        threading.Thread(target=setup_and_run_sender, daemon=True).start()
    
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
        ttk.Entry(receiver_frame, textvariable=self.selected_folder_path, state="readonly").grid(row=0, column=1, sticky="ew", padx=5, pady=5)
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
        ttk.Progressbar(receiver_frame, variable=self.progress_var, maximum=100).grid(row=4, column=0, columnspan=3, sticky="ew", padx=5, pady=5, ipady=5)
        ttk.Label(receiver_frame, textvariable=self.progress_text, anchor="center").grid(row=5, column=0, columnspan=3, sticky="ew", padx=5)

        self.status_label = ttk.Label(receiver_frame, text="Buscando sesiones...")
        self.status_label.grid(row=6, column=0, columnspan=3, sticky="w", padx=5, pady=10)
        
        ttk.Button(receiver_frame, text="← Volver al Menú Principal", command=self.create_welcome_screen).grid(row=7, column=0, columnspan=3, sticky="w", padx=5, pady=20)
        
        self._start_receiver_logic()

    def _update_status(self, message):
        if self.status_label and self.status_label.winfo_exists():
            self.root.after(0, lambda: self.status_label.config(text=message))

    def _update_progress(self, bytes_processed, total_bytes):
        def task():
            if not hasattr(self, 'progress_var') or total_bytes == 0:
                self.progress_var.set(0)
                self.progress_text.set("")
                return

            percentage = (bytes_processed / total_bytes) * 100
            self.progress_var.set(percentage)

            current_time = time.time()
            time_delta = current_time - self.last_update_time
            
            if time_delta > 0.2:
                bytes_delta = bytes_processed - self.last_bytes_processed
                speed_bps = (bytes_delta * 8) / time_delta
                speed_mbps = speed_bps / 1_000_000
                
                self.progress_text.set(f"{percentage:.1f}%  ({speed_mbps:.2f} Mbps)")
                
                self.last_update_time = current_time
                self.last_bytes_processed = bytes_processed
            elif self.last_bytes_processed == 0:
                self.progress_text.set(f"{percentage:.1f}%")

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
        self.receiver = Receiver(self.config, self._update_progress, self._update_status, self._on_download_complete)
        self.service_browser = PyCastServiceBrowser(self._add_session, self._remove_session, self._update_session)
        self.receiver.start_listening()
        
    def _on_download_complete(self, status="completed"):
        def task():
            if status == "completed":
                self.progress_var.set(100)
                self.progress_text.set("¡Descarga completa!")
                self._update_status("¡Descarga completa! Listo para una nueva descarga.")
            elif status == "cancelled":
                self.progress_var.set(0)
                self.progress_text.set("Descarga cancelada")
                self._update_status("La descarga fue cancelada. Listo para una nueva descarga.")
            elif status == "failed_verification":
                self.progress_var.set(0)
                self.progress_text.set("¡Verificación fallida!")
                self._update_status("El archivo recibido estaba corrupto y ha sido eliminado.")
                messagebox.showerror("Error de Verificación", 
                                     "La comprobación de integridad del archivo ha fallado. "
                                     "El archivo descargado estaba corrupto y ha sido eliminado.")

            if self.join_btn and self.join_btn.winfo_exists():
                self.join_btn.config(state='normal')
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
        
        self._update_progress(0, 0)
        self.last_update_time = time.time()
        self.last_bytes_processed = 0

        self.join_btn.config(state="disabled")
        threading.Thread(target=self._perform_handshake_and_wait, args=(session_info, destination_folder), daemon=True).start()

    def _perform_handshake_and_wait(self, session_info, destination_folder):
        try:
            self._update_status(f"Conectando con {session_info.get('username')}...")
            handshake_sock = socket.create_connection((session_info['address'], HANDSHAKE_PORT), timeout=5)
            
            with handshake_sock:
                payload = json.dumps({'session_id': session_info['session_id'], 'username': self.config.get('username')}).encode('utf-8')
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
            
            self.receiver.join_session(session_info, destination_folder)

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

if __name__ == "__main__":
    # --- MODIFICADO: argparse con ayuda mejorada y ejemplos ---
    examples = """
Ejemplos de uso:
  # Enviar un archivo en modo directo (un solo receptor)
  python pycast_app.py send ./documento.pdf

  # Enviar un archivo a múltiples receptores con un nombre de sesión personalizado
  python pycast_app.py send ./fotos.zip --name "Fotos de la Fiesta" --multi

  # Buscar y recibir un archivo en la carpeta por defecto
  python pycast_app.py receive

  # Recibir un archivo y guardarlo en una carpeta específica
  python pycast_app.py receive --output-dir /tmp/descargas/
"""
    parser = argparse.ArgumentParser(
        description="PyCast: Herramienta de transferencia de archivos en LAN.",
        epilog=examples,
        formatter_class=argparse.RawTextHelpFormatter
    )
    subparsers = parser.add_subparsers(dest='command', help='Comandos:')
    
    send_parser = subparsers.add_parser('send', help='Enviar un archivo.')
    send_parser.add_argument('file_path', metavar='ARCHIVO', help='Ruta al archivo que se va a enviar.')
    send_parser.add_argument('--name', help='Nombre personalizado para la sesión (por defecto: nombre del archivo).')
    send_parser.add_argument('--multi', action='store_true', help='Habilitar modo multi-cliente (lobby). Se esperará a que el usuario presione Enter para iniciar la transmisión.')
    
    receive_parser = subparsers.add_parser('receive', help='Recibir un archivo.')
    receive_parser.add_argument('--output-dir', metavar='CARPETA', help='Carpeta para guardar los archivos descargados (por defecto: la configurada en la app).')
    
    args = parser.parse_args()

    if args.command in ['send', 'receive']:
        print("La configuración de red desde config.json se usará para la CLI.")
        config = load_config()
        if args.command == 'send':
            run_cli_sender(args, config)
        elif args.command == 'receive':
            run_cli_receiver(args, config)
    else:
        main_window = TkinterDnD.Tk()
        app = PyCastApp(main_window)
        app.run()
