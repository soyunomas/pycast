# receiver.py (SIN CAMBIOS)
import socket
import threading
import json
import os
import base64

# --- Constantes de Red (deben coincidir con sender.py) ---
MULTICAST_GROUP = '239.192.1.100'
MULTICAST_PORT = 5007

# --- Constantes del Receptor ---
BUFFER_SIZE = 2048 

class Receiver:
    """
    Gestiona la lógica de recepción de archivos desde un grupo multicast.
    """
    def __init__(self, progress_callback, status_callback, completion_callback):
        """
        Inicializa el Receptor.
        :param progress_callback: Función a llamar para actualizar el progreso.
        :param status_callback: Función a llamar para actualizar mensajes de estado.
        :param completion_callback: Función a llamar cuando la descarga finaliza.
        """
        self.progress_callback = progress_callback
        self.status_callback = status_callback
        self.completion_callback = completion_callback
        
        self.listen_socket = None
        self.is_listening = False
        self.thread = None
        
        self.current_session_info = {} 
        self.joined_session_id = None 
        self.received_chunks = {}

    def _setup_socket(self):
        """Configura el socket UDP para escuchar transmisiones multicast."""
        try:
            self.listen_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self.listen_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listen_socket.bind(('', MULTICAST_PORT))
            mreq = socket.inet_aton(MULTICAST_GROUP) + socket.inet_aton('0.0.0.0')
            self.listen_socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self.status_callback("Socket configurado. Esperando instrucciones para descargar...")
            return True
        except Exception as e:
            self.status_callback(f"Error al configurar socket: {e}")
            return False

    def start_listening(self):
        """Inicia el proceso de escucha en un nuevo hilo."""
        if self._setup_socket():
            self.is_listening = True
            self.thread = threading.Thread(target=self._listen_loop)
            self.thread.daemon = True
            self.thread.start()

    def stop_listening(self):
        """Detiene la escucha y cierra el socket."""
        self.is_listening = False
        if self.listen_socket:
            self.listen_socket.close()
        self.status_callback("Escucha detenida.")

    def join_session(self, session_id, destination_folder):
        """
        Elige una sesión para unirse y comenzar la descarga.
        """
        self.received_chunks = {}
        self.current_session_info = {} 
        self.progress_callback(0)
        
        self.joined_session_id = session_id
        self.current_session_info['destination_folder'] = destination_folder
        self.status_callback(f"Preparado para unirse a la sesión {session_id[:8]}... Esperando metadatos.")

    def _listen_loop(self):
        """Bucle principal que escucha paquetes en la red."""
        while self.is_listening:
            try:
                data, _ = self.listen_socket.recvfrom(BUFFER_SIZE)
                packet = json.loads(data.decode('utf-8'))
                self._process_packet(packet)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            except socket.error:
                if not self.is_listening:
                    break

    def _process_packet(self, packet):
        """Procesa un paquete recibido según su tipo."""
        ptype = packet.get('type')
        session_id = packet.get('session_id')
        
        if not session_id or session_id != self.joined_session_id:
            return

        if ptype == 'metadata':
            self.current_session_info.update(packet)
            session_name = self.current_session_info.get('session_name', 'desconocida')
            self.status_callback(f"Metadatos recibidos. Descargando: {session_name}")

        elif ptype == 'data':
            if 'total_chunks' not in self.current_session_info:
                return 

            seq_num = packet.get('seq')
            if seq_num is not None and seq_num not in self.received_chunks:
                self.received_chunks[seq_num] = base64.b64decode(packet['payload'])
                
                total_chunks = self.current_session_info['total_chunks']
                progress = (len(self.received_chunks) / total_chunks) * 100
                self.progress_callback(progress)

        elif ptype == 'eof':
            self._reassemble_file()
            self.joined_session_id = None
            self.progress_callback(100)

    def _reassemble_file(self):
        """Reensambla los trozos recibidos en un archivo final."""
        try:
            if not self.current_session_info or 'total_chunks' not in self.current_session_info:
                self.status_callback("Error: No se recibieron los metadatos para reensamblar.")
                return

            total_chunks = self.current_session_info['total_chunks']
            if len(self.received_chunks) != total_chunks:
                self.status_callback(f"Error: Faltan paquetes. Recibidos {len(self.received_chunks)}/{total_chunks}")
                return
            
            file_name = self.current_session_info['file_name']
            destination_folder = self.current_session_info['destination_folder']
            output_path = os.path.join(destination_folder, file_name)

            try:
                with open(output_path, 'wb') as f:
                    for i in range(total_chunks):
                        f.write(self.received_chunks[i])
                self.status_callback(f"Archivo '{file_name}' descargado con éxito.")
            except KeyError:
                self.status_callback(f"Error crítico: Se perdió un paquete. Descarga fallida.")
            except Exception as e:
                self.status_callback(f"Error al guardar el archivo: {e}")
        finally:
            self.completion_callback()
