# sender.py
import socket
import threading
import os
import json
import uuid
import time
import base64
from service_discovery import ServiceAnnouncer

# --- Constantes de Red ---
MULTICAST_GROUP = '239.192.1.100'
MULTICAST_PORT = 5007
MULTICAST_TTL = 1
HANDSHAKE_PORT = 5008

# --- Constantes de Transmisión ---
CHUNK_SIZE = 1024

class Sender:
    def __init__(self, file_path, session_name, username, progress_callback, status_callback, 
                 client_connected_callback=None, client_disconnected_callback=None):
        self.file_path = file_path
        self.session_name = session_name
        self.session_id = str(uuid.uuid4())
        self.username = username
        
        # Callbacks
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.client_connected_callback = client_connected_callback
        self.client_disconnected_callback = client_disconnected_callback

        self.is_active = False
        self.transmission_started = False
        self.multiclient_mode = False # Se establecerá desde la app
        
        # Sockets y Threads
        self.session_thread = None
        self.service_announcer = None
        self.handshake_socket = None
        self.connected_clients = {} # {client_id: {'conn': conn, 'username': name}}
        self.clients_lock = threading.Lock()

    def start_session(self, multiclient=False):
        if not os.path.exists(self.file_path):
            self.status_callback("Error: El archivo no existe.")
            return
        self.multiclient_mode = multiclient
        self.is_active = True
        self.session_thread = threading.Thread(target=self._session_lifecycle)
        self.session_thread.daemon = True
        self.session_thread.start()

    def stop_session(self):
        self.is_active = False
        
        local_ip_to_unblock = None
        if self.service_announcer:
            local_ip_to_unblock = self.service_announcer.local_ip
            self.service_announcer.stop()
        
        with self.clients_lock:
            for client_id, client_data in self.connected_clients.items():
                try:
                    client_data['conn'].close()
                except: pass
            self.connected_clients.clear()

        if self.handshake_socket:
            try:
                if local_ip_to_unblock:
                    socket.create_connection((local_ip_to_unblock, HANDSHAKE_PORT), timeout=0.1)
            except: pass
            finally:
                self.handshake_socket.close()
        
        self.status_callback("Sesión cancelada.")

    def _session_lifecycle(self):
        self.service_announcer = ServiceAnnouncer(
            self.session_id, self.session_name, HANDSHAKE_PORT, self.username
        )
        self.service_announcer.start()
        
        if self.multiclient_mode:
            self._run_multiclient_lobby()
        else:
            self._run_single_client_session()
        
        if self.service_announcer:
            self.service_announcer.stop()
        self.is_active = False

    def _run_single_client_session(self):
        self.status_callback("Esperando a que un receptor se conecte...")
        receiver_info = self._listen_for_single_handshake()
        
        if self.is_active and receiver_info:
            self.service_announcer.update_status('busy')
            receiver_name = receiver_info.get('username', 'un receptor')
            self.status_callback(f"Conectado con '{receiver_name}'. Iniciando envío...")
            self._transmit_file()

    def _run_multiclient_lobby(self):
        self.handshake_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.handshake_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.handshake_socket.bind(('', HANDSHAKE_PORT))
            self.handshake_socket.listen()
            self.status_callback("Lobby abierto. Esperando conexiones...")

            while self.is_active and not self.transmission_started:
                try:
                    conn, addr = self.handshake_socket.accept()
                    if not self.is_active: break
                    handler_thread = threading.Thread(target=self._handle_client_connection, args=(conn, addr))
                    handler_thread.daemon = True
                    handler_thread.start()
                except socket.error:
                    break # El socket fue cerrado por stop_session
        finally:
            self.handshake_socket.close()

    def _handle_client_connection(self, conn, addr):
        client_id = str(uuid.uuid4())
        try:
            data = conn.recv(1024)
            if not data: return
            
            request = json.loads(data.decode('utf-8'))
            if request.get('session_id') != self.session_id:
                conn.close()
                return

            conn.sendall(b'ACK_MULTI') # Avisar al cliente que es modo lobby
            
            username = request.get('username', f'Cliente {addr[0]}')
            with self.clients_lock:
                self.connected_clients[client_id] = {'conn': conn, 'username': username}
            
            if self.client_connected_callback:
                self.client_connected_callback(client_id, username)
        except (json.JSONDecodeError, OSError, ConnectionResetError):
            conn.close()
            with self.clients_lock:
                self.connected_clients.pop(client_id, None)
            if self.client_disconnected_callback:
                self.client_disconnected_callback(client_id)

    def start_transmission(self):
        if not self.multiclient_mode or self.transmission_started:
            return
        
        self.transmission_started = True
        self.is_active = False # Para detener el bucle de accept()
        
        self.service_announcer.update_status('busy')
        self.status_callback("Cerrando lobby e iniciando transmisión...")
        
        with self.clients_lock:
            for client_data in self.connected_clients.values():
                try:
                    client_data['conn'].sendall(b'START')
                    client_data['conn'].close()
                except OSError:
                    pass # El cliente pudo haberse desconectado
        
        # Pequeña pausa para que los clientes procesen la señal START
        time.sleep(0.5) 
        self.is_active = True # Reactivamos para la transmisión
        self._transmit_file()

    def _listen_for_single_handshake(self):
        self.handshake_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.handshake_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.handshake_socket.bind(('', HANDSHAKE_PORT))
            self.handshake_socket.listen(1)
            conn, addr = self.handshake_socket.accept()
            with conn:
                if not self.is_active: return None
                data = conn.recv(1024)
                conn.sendall(b'ACK_SINGLE') # Avisar que la transmisión es inmediata
                request = json.loads(data.decode('utf-8'))
                if request.get('session_id') == self.session_id:
                    return request
            return None
        except Exception:
            return None
        finally:
            if self.handshake_socket:
                self.handshake_socket.close()

    def _transmit_file(self):
        multicast_socket = None
        try:
            multicast_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            multicast_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
            
            file_size = os.path.getsize(self.file_path)
            file_name = os.path.basename(self.file_path)
            total_chunks = (file_size // CHUNK_SIZE) + (1 if file_size % CHUNK_SIZE > 0 else 0)

            metadata = {
                "type": "metadata", "session_id": self.session_id, "session_name": self.session_name,
                "file_name": file_name, "file_size": file_size, "total_chunks": total_chunks
            }
            for _ in range(3):
                if not self.is_active: break
                multicast_socket.sendto(json.dumps(metadata).encode('utf-8'), (MULTICAST_GROUP, MULTICAST_PORT))
                time.sleep(0.1)

            with open(self.file_path, 'rb') as f:
                for seq_num in range(total_chunks):
                    if not self.is_active:
                        self.status_callback("Transmisión cancelada.")
                        break
                    chunk = f.read(CHUNK_SIZE)
                    if not chunk: break
                    
                    payload = base64.b64encode(chunk).decode('utf-8')
                    data_packet = {
                        "type": "data", "session_id": self.session_id, "seq": seq_num, "payload": payload
                    }
                    multicast_socket.sendto(json.dumps(data_packet).encode('utf-8'), (MULTICAST_GROUP, MULTICAST_PORT))
                    
                    progress = ((seq_num + 1) / total_chunks) * 100
                    self.progress_callback(progress)
                    time.sleep(0.001)
            
            if self.is_active:
                self.status_callback("Transmisión completada.")
                self.progress_callback(100)
            
        except Exception as e:
            self.status_callback(f"Error en transmisión: {e}")
        finally:
            eof_packet = {"type": "eof", "session_id": self.session_id}
            for _ in range(3):
                if multicast_socket:
                    multicast_socket.sendto(json.dumps(eof_packet).encode('utf-8'), (MULTICAST_GROUP, MULTICAST_PORT))
                time.sleep(0.1)
            if multicast_socket:
                multicast_socket.close()
