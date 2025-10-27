# receiver.py
import socket
import threading
import json
import os
import uuid
import shutil
import zlib

# --- Constantes de Red ---
MULTICAST_GROUP = '239.192.1.100'
MULTICAST_PORT = 5007
NACK_PORT = 5009

def _calculate_file_crc32(file_path):
    """Calcula el checksum CRC32 de un archivo leyéndolo en trozos."""
    crc_value = 0
    try:
        with open(file_path, 'rb') as f:
            while chunk := f.read(65536): # Leer en trozos de 64KB
                crc_value = zlib.crc32(chunk, crc_value)
    except IOError:
        return None # Devuelve None si el archivo no se puede leer
    return crc_value

class Receiver:
    def __init__(self, config, progress_callback, status_callback, completion_callback):
        self.config = config
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.completion_callback = completion_callback
        
        self.CHUNK_SIZE = self.config.get('network_settings', {}).get('chunk_size', 8192)
        self.BUFFER_SIZE = 32768 + 2048 

        print("\n--- [RECEIVER] Configuración de Red Inicial (Local) ---")
        print(f"  - CHUNK_SIZE (default): {self.CHUNK_SIZE} bytes")
        print(f"  - BUFFER_SIZE (fijo): {self.BUFFER_SIZE} bytes")
        print("-----------------------------------------------------\n")

        self.listen_socket = None
        self.nack_socket = None
        self.is_listening = False
        self.thread = None
        
        self.current_session_info = {}
        self.joined_session_id = None
        self.sender_address = None

        self.output_file = None
        self.temp_file_path = None
        
        self.received_seqs_current_block = set()
        self.last_processed_block = -1

    def _setup_socket(self):
        try:
            self.listen_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listen_socket.bind(('', MULTICAST_PORT))
            mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton('0.0.0.0')
            self.listen_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.nack_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.status_callback("Socket configurado. Esperando instrucciones...")
            return True
        except Exception as e:
            self.status_callback(f"Error al configurar socket: {e}")
            return False

    def start_listening(self):
        if self._setup_socket():
            self.is_listening = True
            self.thread = threading.Thread(target=self._listen_loop, daemon=True)
            self.thread.start()

    def stop_listening(self):
        self.is_listening = False
        if self.listen_socket: self.listen_socket.close()
        if self.nack_socket: self.nack_socket.close()
        self._cleanup_temp_file()
        self.status_callback("Escucha detenida.")

    def join_session(self, session_info, destination_folder):
        self._cleanup_temp_file()
        self.current_session_info = session_info.copy()
        self.progress_callback(0, 0)
        self.received_seqs_current_block.clear()
        self.last_processed_block = -1
        
        self.joined_session_id = session_info['session_id']
        self.sender_address = session_info['address']
        self.current_session_info['destination_folder'] = destination_folder
        self.status_callback(f"Uniéndose a la sesión {self.joined_session_id[:8]}...")


    def _listen_loop(self):
        while self.is_listening:
            try:
                data, _ = self.listen_socket.recvfrom(self.BUFFER_SIZE)
                self._process_packet(data)
            except socket.error:
                if not self.is_listening: break

    def _process_packet(self, data):
        try:
            packet = json.loads(data.decode('utf-8'))
            session_id = packet.get('session_id')
            if not session_id or session_id != self.joined_session_id: return

            ptype = packet.get('type')
            if ptype == 'metadata' and not self.output_file:
                self._handle_metadata(packet)
            elif ptype == 'block_end':
                self._handle_block_end(packet)
            elif ptype == 'eof':
                print("[RCV] RECIBIDO EOF. Finalizando y verificando archivo.")
                self._reassemble_file()
                self.joined_session_id = None
            elif ptype == 'cancel':
                self.status_callback("La transmisión fue cancelada por el emisor.")
                self._cleanup_temp_file()
                self.joined_session_id = None
                self.completion_callback(status="cancelled", metadata=None)
        
        except (json.JSONDecodeError, UnicodeDecodeError):
            if not self.output_file: return
            if uuid.UUID(bytes=data[:16]) != uuid.UUID(self.joined_session_id): return
            
            seq_num = int.from_bytes(data[16:20], 'big')
            self.output_file.seek(seq_num * self.CHUNK_SIZE)
            self.output_file.write(data[20:])
            self.received_seqs_current_block.add(seq_num)
    
    # --- INICIO DE LA CORRECCIÓN REFACTORIZADA ---

    def _check_and_process_block(self, block_to_check):
        """
        Verifica un único bloque. Si está completo, lo procesa y devuelve True.
        Si está incompleto, envía un NACK y devuelve False.
        """
        block_size = self.current_session_info['block_size_packets']
        total_chunks = self.current_session_info['total_chunks']
        start_seq = block_to_check * block_size
        end_seq = min((block_to_check + 1) * block_size, total_chunks)
        
        expected_seqs = set(range(start_seq, end_seq))
        received_in_this_block = {s for s in self.received_seqs_current_block if start_seq <= s < end_seq}
        missing_seqs = list(expected_seqs - received_in_this_block)

        if missing_seqs:
            print(f"[RCV] Bloque {block_to_check}: Incompleto. Faltan {len(missing_seqs)} paquetes. Enviando NACK.")
            nack_packet = {"session_id": self.joined_session_id, "block_index": block_to_check, "missing_seqs": missing_seqs}
            try:
                self.nack_socket.sendto(json.dumps(nack_packet).encode('utf-8'), (self.sender_address, NACK_PORT))
            except Exception as e:
                print(f"[RCV] Error enviando NACK para bloque {block_to_check}: {e}")
            return False
        else:
            print(f"[RCV] Bloque {block_to_check}: Completo y verificado.")
            self.received_seqs_current_block -= expected_seqs
            total_bytes = self.current_session_info['file_size']
            bytes_processed = min((block_to_check + 1) * block_size * self.CHUNK_SIZE, total_bytes)
            self.progress_callback(bytes_processed, total_bytes)
            self.status_callback(f"Bloque {block_to_check + 1} recibido correctamente.")
            return True

    def _handle_block_end(self, packet):
        """
        Orquesta la verificación de bloques. Itera sobre todos los bloques
        pendientes desde el último procesado hasta el actual.
        """
        block_idx_received = packet['block_index']
        if block_idx_received <= self.last_processed_block:
            return

        can_advance_state = True
        for block_to_check in range(self.last_processed_block + 1, block_idx_received + 1):
            is_block_complete = self._check_and_process_block(block_to_check)
            
            # Solo podemos avanzar nuestro estado 'last_processed_block' si los
            # bloques se completan en orden secuencial.
            if can_advance_state and is_block_complete:
                self.last_processed_block = block_to_check
            else:
                # En cuanto encontramos un bloque incompleto, rompemos la secuencia.
                # Podemos seguir enviando NACKs para bloques futuros, pero no podemos
                # marcarlos como 'procesados'.
                can_advance_state = False
    
    # --- FIN DE LA CORRECCIÓN REFACTORIZADA ---

    def _handle_metadata(self, packet):
        self.current_session_info.update(packet)
        original_chunk_size = self.CHUNK_SIZE
        self.CHUNK_SIZE = self.current_session_info.get('chunk_size', self.CHUNK_SIZE)
        
        print("\n--- [RECEIVER] Configuración de Red Recibida del Emisor ---")
        if self.CHUNK_SIZE != original_chunk_size:
            print(f"  - CHUNK_SIZE: {self.CHUNK_SIZE} bytes (Cambiado desde {original_chunk_size})")
        else:
            print(f"  - CHUNK_SIZE: {self.CHUNK_SIZE} bytes (Coincide con el local)")
        
        print(f"  - FILE_NAME (real): {self.current_session_info.get('file_name')}")
        print(f"  - BLOCK_SIZE_PACKETS: {self.current_session_info.get('block_size_packets')}")
        print("-----------------------------------------------------------\n")

        self.temp_file_path = os.path.join(self.current_session_info['destination_folder'], f".{self.current_session_info['file_name']}.pycast-tmp")
        try:
            self.output_file = open(self.temp_file_path, "wb")
            if self.current_session_info.get('file_size', 0) > 0:
                self.output_file.truncate(self.current_session_info['file_size'])
            self.status_callback(f"Descargando: {self.current_session_info['session_name']}")
        except IOError as e:
            self.status_callback(f"Error al crear archivo temporal: {e}")
            self._cleanup_temp_file()

    def _reassemble_file(self):
        output_path = None
        try:
            if self.output_file:
                 self.output_file.close()
                 self.output_file = None
            
            output_path = os.path.join(self.current_session_info['destination_folder'], self.current_session_info['file_name'])
            shutil.move(self.temp_file_path, output_path)
            
            if not os.path.exists(output_path):
                self.status_callback("Error CRÍTICO: El archivo no se pudo guardar.")
                return

            self.status_callback("Verificando integridad del archivo...")
            expected_crc = self.current_session_info.get('file_crc32')
            expected_size = self.current_session_info.get('file_size')

            if expected_crc is None:
                self.status_callback(f"Archivo '{self.current_session_info['file_name']}' descargado. (Sin verificación CRC)")
                self.completion_callback(status="completed", metadata=self.current_session_info)
                return

            received_crc = _calculate_file_crc32(output_path)
            received_size = os.path.getsize(output_path)

            if received_size == expected_size and received_crc == expected_crc:
                self.status_callback(f"Archivo '{self.current_session_info['file_name']}' descargado y verificado con éxito.")
                total_bytes = self.current_session_info.get('file_size', 1)
                self.progress_callback(total_bytes, total_bytes)
                self.completion_callback(status="completed", metadata=self.current_session_info)
            else:
                print(f"[RCV] ¡FALLO DE VERIFICACIÓN! Esperado: (size={expected_size}, crc={expected_crc}), Recibido: (size={received_size}, crc={received_crc})")
                self.status_callback("¡ERROR! El archivo está corrupto. Eliminando...")
                os.remove(output_path) 
                self.completion_callback(status="failed_verification", metadata=self.current_session_info)

        except Exception as e:
            self.status_callback(f"Error al finalizar la descarga: {e}")
            if output_path and os.path.exists(output_path):
                try: os.remove(output_path)
                except: pass
        finally:
            self._cleanup_temp_file()


    def _cleanup_temp_file(self):
        if self.output_file:
            try: self.output_file.close()
            except: pass
            self.output_file = None
        if self.temp_file_path and os.path.exists(self.temp_file_path):
            try: os.remove(self.temp_file_path)
            except: pass
            self.temp_file_path = None
