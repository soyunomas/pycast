# config_manager.py
import json
import os
import socket

CONFIG_FILE = 'config.json'

CONFIG_PRESETS = {
    "Wi-Fi Inestable": {
        "help": "Ideal para redes Wi-Fi congestionadas o con mala señal. Prioriza la estabilidad sobre la velocidad.",
        "settings": {
            "chunk_size": 4096,
            "block_size_packets": 128,
            "nack_listen_timeout": 0.5,
            "repair_rounds": 8
        }
    },
    "Wi-Fi (Estándar)": {
        "help": "Un perfil equilibrado para la mayoría de redes inalámbricas. Es el recomendado por defecto.",
        "settings": {
            "chunk_size": 8192,
            "block_size_packets": 256,
            "nack_listen_timeout": 0.2,
            "repair_rounds": 5
        }
    },
    "Ethernet (Rápido)": {
        "help": "Optimizado para redes cableadas (1Gbps). Aumenta la velocidad asumiendo una conexión estable y de baja latencia.",
        "settings": {
            "chunk_size": 16384,
            "block_size_packets": 512,
            "nack_listen_timeout": 0.15,
            "repair_rounds": 4
        }
    },
    "Ethernet (Extremo)": {
        "help": "Configuración agresiva para exprimir al máximo redes Gigabit. Puede fallar si la red no es perfecta.",
        "settings": {
            # --- VALORES MODIFICADOS PARA ESTABILIDAD ---
            "chunk_size": 32768,
            "block_size_packets": 512,      # Reducido de 1024 para evitar desbordamiento de búfer.
            "nack_listen_timeout": 0.15,    # Aumentado de 0.1 para dar más tiempo de respuesta.
            "repair_rounds": 5              # Aumentado de 3 para mayor robustez.
        }
    }
}

# Estructura centralizada con metadatos para la configuración
CONFIG_METADATA = {
    "username": {
        "default": None, # Se genera dinámicamente
        "label": "Nombre de Usuario",
        "help": "Tu nombre tal como aparecerá a otros usuarios en la red."
    },
    "download_folder": {
        "default": None, # Se genera dinámicamente
        "label": "Carpeta de Descargas",
        "help": "La carpeta donde se guardarán los archivos recibidos."
    },
    "multiclient_enabled_by_default": {
        "default": False,
        "label": "Habilitar modo multi-cliente por defecto",
        "help": "Si está marcado, la opción 'Enviar a múltiples clientes' estará activada por defecto en la pantalla de envío."
    },
    "network_settings": {
        "chunk_size": {
            "default": 16384, # Corresponde a "Ethernet (Rápido)"
            "label": "Tamaño de Paquete (bytes)",
            "help": "El tamaño de cada 'trozo' de datos enviado por la red. Valores más altos pueden mejorar la velocidad en redes estables (Ethernet), mientras que valores más bajos son más resilientes en redes inestables (Wi-Fi)."
        },
        "block_size_packets": {
            "default": 512, # Corresponde a "Ethernet (Rápido)"
            "label": "Paquetes por Bloque",
            "help": "Número de paquetes a enviar antes de verificar si se recibieron correctamente. Un valor más alto mejora la velocidad en redes de baja latencia, pero uno más bajo es mejor para redes con pérdida de paquetes."
        },
        "nack_listen_timeout": {
            "default": 0.15, # Corresponde a "Ethernet (Rápido)"
            "label": "Espera NACK (segundos)",
            "help": "El tiempo que el emisor espera por informes de error (NACKs) tras enviar un bloque. Aumentar este valor puede ayudar en redes con alta latencia, pero ralentizará la transferencia."
        },
        "repair_rounds": {
            "default": 4, # Corresponde a "Ethernet (Rápido)"
            "label": "Rondas de Reparación",
            "help": "Número máximo de intentos para reenviar los paquetes perdidos de un bloque. Aumenta la robustez en redes muy inestables a costa de posibles pausas si la red es mala."
        }
    }
}


def get_default_username():
    """Genera un nombre de usuario por defecto a partir del nombre del host."""
    hostname = socket.gethostname()
    return hostname.split('.')[0]

def get_default_config():
    """
    Retorna la configuración por defecto, construida a partir de CONFIG_METADATA.
    """
    downloads_path = os.path.join(os.path.expanduser('~'), 'Downloads')
    if not os.path.exists(downloads_path):
        downloads_path = os.path.expanduser('~')

    config = {
        'username': get_default_username(),
        'download_folder': downloads_path,
        'multiclient_enabled_by_default': CONFIG_METADATA['multiclient_enabled_by_default']['default'],
        'network_settings': {
            key: data['default'] 
            for key, data in CONFIG_METADATA['network_settings'].items()
        }
    }
    return config

def _update_nested_dict(default, user):
    """
    Función recursiva para actualizar un diccionario de configuración por defecto
    con los valores del usuario, añadiendo claves faltantes.
    """
    for key, value in default.items():
        if isinstance(value, dict):
            user.setdefault(key, {})
            _update_nested_dict(value, user[key])
        else:
            user.setdefault(key, value)
    return user

def load_config():
    """
    Carga la configuración desde el archivo JSON. Si no existe o está incompleto,
    lo crea/actualiza usando los valores por defecto definidos en CONFIG_METADATA.
    """
    default_config = get_default_config()
    if not os.path.exists(CONFIG_FILE):
        save_config(default_config)
        return default_config
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            user_config = json.load(f)
        
        updated_config = _update_nested_dict(default_config, user_config)
        
        save_config(updated_config)
        
        return updated_config
        
    except (json.JSONDecodeError, IOError):
        save_config(default_config)
        return default_config

def save_config(config_data):
    """Guarda el diccionario de configuración en el archivo JSON."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_data, f, indent=4)
    except IOError as e:
        print(f"Error al guardar la configuración: {e}")
