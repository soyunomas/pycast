# config_manager.py
import json
import os
import socket

CONFIG_FILE = 'config.json'

def get_default_username():
    """Genera un nombre de usuario por defecto a partir del nombre del host."""
    hostname = socket.gethostname()
    return hostname.split('.')[0]

def get_default_config():
    """Retorna la configuración por defecto."""
    downloads_path = os.path.join(os.path.expanduser('~'), 'Downloads')
    if not os.path.exists(downloads_path):
        downloads_path = os.path.expanduser('~')

    return {
        'download_folder': downloads_path,
        'username': get_default_username(),
        'multiclient_enabled_by_default': False  # <-- NUEVA OPCIÓN
    }

def load_config():
    """Carga la configuración desde el archivo JSON. Si no existe, lo crea."""
    default_config = get_default_config()
    if not os.path.exists(CONFIG_FILE):
        save_config(default_config)
        return default_config
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            # Asegurarse de que todas las claves existen para evitar errores
            for key, value in default_config.items():
                if key not in config:
                    config[key] = value
            return config
    except (json.JSONDecodeError, IOError):
        # Si el archivo está corrupto o hay un error, cargar el por defecto
        return default_config

def save_config(config_data):
    """Guarda el diccionario de configuración en el archivo JSON."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_data, f, indent=4)
    except IOError as e:
        print(f"Error al guardar la configuración: {e}")
