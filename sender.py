# sender.py
import socket
import threading
import os
import json
import uuid
import time
import zlib
from service_discovery import ServiceAnnouncer

# --- Constantes de Red (solo las que no son configurables) ---
MULTICAST_GROUP = '239.192.1.100'
MULTICAST_PORT = 5007
MULTICAST_TTL = 1
HANDSHAKE_PORT = 5008
NACK_PORT = 5009

def _calculate_file_crc32(file_path):
    """Calcula el checksum CRC32 de un archivo leyéndolo en trozos."""
    crc_value = 0
    with open(file_path, 'rb') as f:
        while chunk := f.read(65536):  # Leer en trozos de 64KB
            crc_value = zlib.crc32(chunk, crc_value)
    return crc_value

class Sender:
    def __init__(self, file_path, session_name, config, progress_callback, status_callback, 
                 client_connected_callback=None, client_disconnected_callback=None):
        self.file_path = file_path
        self.session_name = session_name
        self.config = config
        self.username = config.get('username')
        self.session_id = str(uuid.uuid4())
        
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.client_connected_callback = client_connected_callback
        self.client_disconnected_callback = client_disconnected_callback
        
        net_conf = self.config.get('network_settings')
        self.CHUNK_SIZE = net_conf.get('chunk_size', 8192)
        self.BLOCK_SIZE_PACKETS = net_conf.get('block_size_packets', 256)
        self.REPAIR_ROUNDS = net_conf.get('repair_rounds', 5)
        self.NACK_LISTEN_TIMEOUT = net_conf.get('nack_listen_timeout', 0.2)

        # --- NUEVO: Imprimir configuración al inicio ---
        print("\n--- [SENDER] Configuración de Red Inicial ---")
        print(f"  - CHUNK_SIZE: {self.CHUNK_SIZE} bytes")
        print(f"  - BLOCK_SIZE_PACKETS: {self.BLOCK_SIZE_PACKETS} paquetes")
        print(f"  - REPAIR_ROUNDS: {self.REPAIR_ROUNDS} rondas")
        print(f"  - NACK_LISTEN_TIMEOUT: {self.NACK_LISTEN_TIMEOUT} segundos")
        print("-------------------------------------------\n")

        self.is_active = False
        self.transmission_started = False
        self.multiclient_mode = False
        
        self.session_thread = None
        self.service_announcer = None
        self.handshake_socket = None
        self.connected_clients = {}
        self.clients_lock = threading.Lock()
        
        self.transmission_start_event = threading.Event()

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
        was_active = self.is_active
        self.is_active = False 
        self.transmission_started = False
        self.transmission_start_event.set()
        
        if was_active: self._send_cancellation_message()
        if self.service_announcer: self.service_announcer.stop()

        if self.handshake_socket:
            try:
                socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(('127.0.0.1', HANDSHAKE_PORT))
            except socket.error: pass
            finally: self.handshake_socket.close()
        
        self.status_callback("Sesión cancelada.")

    def _send_cancellation_message(self):
        cancel_packet = {"type": "cancel", "session_id": self.session_id}
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as sock:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
                for _ in range(3): 
                    sock.sendto(json.dumps(cancel_packet).encode('utf-8'), (MULTICAST_GROUP, MULTICAST_PORT))
                    time.sleep(0.02)
        except Exception as e:
            print(f"Error enviando mensaje de cancelación: {e}")

    def _session_lifecycle(self):
        self.service_announcer = ServiceAnnouncer(self.session_id, self.session_name, HANDSHAKE_PORT, self.username)
        self.service_announcer.start()
        
        if self.multiclient_mode: self._run_multiclient_lobby()
        else: self._run_single_client_session()
        
        if self.service_announcer: self.service_announcer.stop()
        self.is_active = False
        self.transmission_started = False

    def _run_single_client_session(self):
        self.status_callback("Esperando a que un receptor se conecte...")
        receiver_info = self._listen_for_single_handshake()
        
        if self.is_active and receiver_info:
            self.service_announcer.update_status('busy')
            self.status_callback(f"Conectado con '{receiver_info.get('username', 'un receptor')}'. Iniciando envío...")
            self.transmission_started = True
            self._transmit_file()

    def _run_multiclient_lobby(self):
        self.transmission_start_event.clear()
        self.handshake_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.handshake_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.handshake_socket.bind(('', HANDSHAKE_PORT))
            self.handshake_socket.listen()
            self.status_callback("Lobby abierto. Esperando conexiones...")
            while self.is_active and not self.transmission_started:
                try:
                    conn, addr = self.handshake_socket.accept()
                    if not self.is_active or self.transmission_started: 
                        conn.close()
                        break
                    handler_thread = threading.Thread(target=self._handle_client_connection, args=(conn, addr)); handler_thread.daemon = True
                    handler_thread.start()
                except socket.error: break
        finally:
            if self.handshake_socket: self.handshake_socket.close()

    def _handle_client_connection(self, conn, addr):
        client_id = str(uuid.uuid4())
        try:
            request = json.loads(conn.recv(1024).decode('utf-8'))
            if request.get('session_id') != self.session_id: return

            conn.sendall(b'ACK_MULTI')
            username = request.get('username', f'Cliente {addr[0]}')
            
            with self.clients_lock:
                self.connected_clients[client_id] = {'username': username}
            
            if self.client_connected_callback:
                self.client_connected_callback(client_id, username)

            self.transmission_start_event.wait()

            if self.is_active and self.transmission_started:
                try:
                    conn.sendall(b'START')
                except OSError: pass
        
        except (json.JSONDecodeError, OSError, ConnectionResetError) as e:
            print(f"Error de conexión en el lobby con {addr}: {e}")
        
        finally:
            try: conn.close()
            except: pass
            with self.clients_lock:
                if client_id in self.connected_clients: self.connected_clients.pop(client_id)
            if self.client_disconnected_callback:
                self.client_disconnected_callback(client_id)

    def start_transmission(self):
        if not self.multiclient_mode or self.transmission_started: return
        self.transmission_started = True
        if self.service_announcer: self.service_announcer.update_status('busy')
        self.status_callback("Cerrando lobby e iniciando transmisión...")
        self.transmission_start_event.set()
        time.sleep(0.5) 
        self._transmit_file()

    def _listen_for_single_handshake(self):
        self.handshake_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.handshake_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.handshake_socket.bind(('', HANDSHAKE_PORT))
            self.handshake_socket.listen(1)
            conn, _ = self.handshake_socket.accept()
            with conn:
                if not self.is_active: return None
                request = json.loads(conn.recv(1024).decode('utf-8'))
                conn.sendall(b'ACK_SINGLE')
                if request.get('session_id') == self.session_id: return request
            return None
        except Exception: return None
        finally:
            if self.handshake_socket: self.handshake_socket.close()


    def _transmit_file(self):
        multicast_socket, nack_socket = None, None
        try:
            multicast_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            multicast_socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MULTICAST_TTL)
            nack_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            nack_socket.bind(('', NACK_PORT))
            nack_socket.setblocking(False)

            file_size = os.path.getsize(self.file_path)
            
            self.status_callback("Calculando checksum del archivo...")
            file_crc32 = _calculate_file_crc32(self.file_path)
            self.status_callback("Checksum calculado. Iniciando envío...")
            
            total_chunks = (file_size // self.CHUNK_SIZE) + (1 if file_size % self.CHUNK_SIZE > 0 else 0)
            total_blocks = (total_chunks // self.BLOCK_SIZE_PACKETS) + (1 if total_chunks % self.BLOCK_SIZE_PACKETS > 0 else 0)

            metadata = {
                "type": "metadata", "session_id": self.session_id, "session_name": self.session_name,
                "file_name": os.path.basename(self.file_path), "file_size": file_size, 
                "file_crc32": file_crc32,
                "total_chunks": total_chunks,
                # Parámetros de red
                "chunk_size": self.CHUNK_SIZE, 
                "block_size_packets": self.BLOCK_SIZE_PACKETS,
                "nack_listen_timeout": self.NACK_LISTEN_TIMEOUT,
                "repair_rounds": self.REPAIR_ROUNDS
            }
            for _ in range(3):
                if not self.is_active: break
                multicast_socket.sendto(json.dumps(metadata).encode('utf-8'), (MULTICAST_GROUP, MULTICAST_PORT))
                time.sleep(0.1)

            if not self.is_active: return
            
            session_id_bytes = uuid.UUID(self.session_id).bytes
            
            with open(self.file_path, 'rb') as f:
                for block_idx in range(total_blocks):
                    if not self.is_active: break
                    start_seq = block_idx * self.BLOCK_SIZE_PACKETS
                    end_seq = min((block_idx + 1) * self.BLOCK_SIZE_PACKETS, total_chunks)
                    
                    print(f"\n[SND] Enviando bloque {block_idx} (paquetes {start_seq}-{end_seq-1})...")

                    for seq_num in range(start_seq, end_seq):
                        if not self.is_active: break
                        f.seek(seq_num * self.CHUNK_SIZE)
                        packet = session_id_bytes + seq_num.to_bytes(4, 'big') + f.read(self.CHUNK_SIZE)
                        multicast_socket.sendto(packet, (MULTICAST_GROUP, MULTICAST_PORT))
                        time.sleep(0.0001)
                    
                    if not self.is_active: break

                    last_round_had_nacks = False
                    for repair_round in range(self.REPAIR_ROUNDS):
                        if not self.is_active: break
                        
                        print(f"[SND] Fin del bloque {block_idx}. Ronda de reparación {repair_round + 1}/{self.REPAIR_ROUNDS}. Esperando NACKs...")
                        
                        block_end_packet = {"type": "block_end", "session_id": self.session_id, "block_index": block_idx}
                        for _ in range(2):
                            multicast_socket.sendto(json.dumps(block_end_packet).encode('utf-8'), (MULTICAST_GROUP, MULTICAST_PORT))
                            time.sleep(0.01)

                        missing_seqs = set()
                        listen_end = time.time() + self.NACK_LISTEN_TIMEOUT
                        while time.time() < listen_end:
                            if not self.is_active: break
                            try:
                                data, addr = nack_socket.recvfrom(4096)
                                nack_req = json.loads(data.decode('utf-8'))
                                if nack_req.get('session_id') == self.session_id and nack_req.get('block_index') == block_idx:
                                    print(f"[SND] RECIBIDO NACK de {addr} para bloque {block_idx}: {len(nack_req.get('missing_seqs', []))} paquetes.")
                                    missing_seqs.update(nack_req.get('missing_seqs', []))
                            except (BlockingIOError, json.JSONDecodeError): 
                                time.sleep(0.01)

                        if not missing_seqs:
                            print(f"[SND] Bloque {block_idx} confirmado. No se recibieron NACKs. Avanzando.")
                            last_round_had_nacks = False
                            break

                        last_round_had_nacks = True
                        print(f"[SND] Retransmitiendo {len(missing_seqs)} paquetes para el bloque {block_idx}.")
                        
                        for seq_num in sorted(list(missing_seqs)):
                            if not self.is_active: break
                            f.seek(seq_num * self.CHUNK_SIZE)
                            packet = session_id_bytes + seq_num.to_bytes(4, 'big') + f.read(self.CHUNK_SIZE)
                            multicast_socket.sendto(packet, (MULTICAST_GROUP, MULTICAST_PORT))
                            time.sleep(0.0002)

                    if last_round_had_nacks:
                        print(f"[SND] ADVERTENCIA: Se superaron las rondas de reparación para el bloque {block_idx}. Puede haber clientes desincronizados.")

                    bytes_processed = min(end_seq * self.CHUNK_SIZE, file_size)
                    self.progress_callback(bytes_processed, file_size)

            if self.is_active: 
                print("[SND] Transmisión completada. Enviando EOF.")
                self.status_callback("Transmisión completada.")
            
        except Exception as e:
            if self.is_active: self.status_callback(f"Error en transmisión: {e}")
        finally:
            if self.is_active:
                eof_packet = {"type": "eof", "session_id": self.session_id}
                for _ in range(5):
                    if multicast_socket: multicast_socket.sendto(json.dumps(eof_packet).encode('utf-8'), (MULTICAST_GROUP, MULTICAST_PORT))
                    time.sleep(0.1)
            
            if multicast_socket: multicast_socket.close()
            if nack_socket: nack_socket.close()
            self.is_active = self.transmission_started = False
