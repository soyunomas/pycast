# service_discovery.py (SIN CAMBIOS)
import threading
import socket
import time
from zeroconf import ServiceInfo, Zeroconf, ServiceBrowser

SERVICE_TYPE = "_pycast._tcp.local."

def get_local_ip():
    """Obtiene la dirección IP local de la máquina."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.255.255.255', 1))
        IP = s.getsockname()[0]
    except Exception:
        IP = socket.gethostbyname(socket.gethostname())
    finally:
        s.close()
    return IP

class ServiceAnnouncer:
    """Anuncia un servicio PyCast en la red local usando Zeroconf."""
    def __init__(self, session_id, session_name, port, username):
        self.zeroconf = None
        self.thread = None
        self.is_running = False
        self.session_name = session_name
        
        properties = {
            'session_id': session_id,
            'username': username,
            'status': 'available' # Estado inicial
        }
        
        self.local_ip = get_local_ip()
        service_name = f"{session_name}.{SERVICE_TYPE}"
        
        self.service_info = ServiceInfo(
            SERVICE_TYPE,
            service_name,
            addresses=[socket.inet_aton(self.local_ip)],
            port=port,
            properties={k: v.encode('utf-8') for k, v in properties.items()},
            server=f"{socket.gethostname()}.local."
        )

    def start(self):
        if not self.is_running:
            self.is_running = True
            self.thread = threading.Thread(target=self._run)
            self.thread.daemon = True
            self.thread.start()

    def _run(self):
        self.zeroconf = Zeroconf()
        self.zeroconf.register_service(self.service_info)
        while self.is_running:
            time.sleep(1)
        if self.zeroconf:
            self.zeroconf.unregister_service(self.service_info)
            self.zeroconf.close()
            self.zeroconf = None

    def stop(self):
        self.is_running = False

    def update_status(self, new_status):
        """Actualiza el estado del servicio y lo vuelve a registrar."""
        if not self.is_running or not self.zeroconf:
            return
            
        self.zeroconf.unregister_service(self.service_info)
        
        new_properties = self.service_info.properties
        new_properties[b'status'] = new_status.encode('utf-8')

        self.service_info = ServiceInfo(
            self.service_info.type,
            self.service_info.name,
            addresses=self.service_info.addresses,
            port=self.service_info.port,
            properties=new_properties,
            server=self.service_info.server
        )
        self.zeroconf.register_service(self.service_info)

class PyCastServiceBrowser:
    """Busca servicios PyCast en la red."""
    def __init__(self, add_callback, remove_callback, update_callback):
        self.zeroconf = Zeroconf()
        self.listener = PyCastListener(add_callback, remove_callback, update_callback)
        self.browser = ServiceBrowser(self.zeroconf, SERVICE_TYPE, self.listener)

    def stop(self):
        if self.zeroconf:
            self.zeroconf.close()
            self.zeroconf = None

class PyCastListener:
    """Escucha eventos de Zeroconf y los traduce a callbacks de la aplicación."""
    def __init__(self, add_callback, remove_callback, update_callback):
        self.add_callback = add_callback
        self.remove_callback = remove_callback
        self.update_callback = update_callback

    def _get_service_details(self, zeroconf, type, name):
        """Función de ayuda para extraer detalles de un servicio."""
        info = zeroconf.get_service_info(type, name)
        if not info or not info.properties:
            return None
        
        properties = {k.decode('utf-8'): v.decode('utf-8') for k, v in info.properties.items()}
        properties['session_name'] = info.name.replace(f".{SERVICE_TYPE}", "")
        properties['server'] = info.server
        properties['address'] = socket.inet_ntoa(info.addresses[0])
        return properties

    def remove_service(self, zeroconf, type, name):
        details = self._get_service_details(zeroconf, type, name)
        if details:
            self.remove_callback(details['session_id'])

    def add_service(self, zeroconf, type, name):
        details = self._get_service_details(zeroconf, type, name)
        if details:
            self.add_callback(details)
    
    def update_service(self, zeroconf, type, name):
        details = self._get_service_details(zeroconf, type, name)
        if details:
            self.update_callback(details)
