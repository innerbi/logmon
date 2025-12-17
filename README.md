# Logmon - Real-time Log Monitor

Monitor de logs en tiempo real para Lumen. Se suscribe a Redis pub/sub y muestra logs de backend, batch y ray workers en una TUI.

## Uso

```bash
# Requiere port-forward a Redis
kubectl port-forward -n workers svc/redis 6379:6379

# En otra terminal
cd logmon
python main.py
```

## Controles

| Tecla | Accion |
|-------|--------|
| Arrows | Scroll arriba/abajo |
| End | Ir al final (logs mas recientes) |
| P | Pausar/reanudar |
| C | Limpiar logs |
| L | Copiar logs filtrados al clipboard |
| 1-5 | Filtrar por nivel (DEBUG, INFO, WARNING, ERROR, CRITICAL) |
| B | Filtrar solo Backend |
| W | Filtrar solo Batch (Workers) |
| R | Filtrar solo Ray |
| A | Mostrar todos (quitar filtros) |
| Q | Salir |

## Publicar Logs a Redis

Para que tus logs aparezcan en logmon, publica mensajes JSON al canal `logs:{source}`.

### Formato del Mensaje

```json
{
    "timestamp": "2025-12-17 02:30:00,000",
    "level": "INFO",
    "component": "ray",
    "logger": "my_module",
    "message": "Este es el mensaje de log"
}
```

| Campo | Tipo | Descripcion |
|-------|------|-------------|
| timestamp | string | Formato: `YYYY-MM-DD HH:MM:SS,mmm` |
| level | string | DEBUG, INFO, WARNING, ERROR, CRITICAL |
| component | string | Nombre del source (backend, batch, ray) |
| logger | string | Nombre del logger/modulo |
| message | string | Contenido del log |

### Canales

- `logs:backend` - Logs del backend FastAPI
- `logs:batch` - Logs del batch worker
- `logs:ray` - Logs de Ray tasks

### Ejemplo Python

```python
import redis
import json
from datetime import datetime

def publish_log(redis_url: str, source: str, level: str, message: str, logger: str = "app"):
    """Publica un log a Redis para logmon."""
    r = redis.from_url(redis_url)

    log_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3],
        "level": level,
        "component": source,
        "logger": logger,
        "message": message
    }

    channel = f"logs:{source}"
    r.publish(channel, json.dumps(log_entry))

# Uso
publish_log("redis://localhost:6379/0", "backend", "INFO", "Server started")
publish_log("redis://localhost:6379/0", "ray", "ERROR", "Task failed", "ray_task")
```

### Logging Handler para Python

Para integrar con el logging standard de Python:

```python
import logging
import redis
import json
from datetime import datetime

class RedisLogHandler(logging.Handler):
    """Handler que publica logs a Redis pub/sub."""

    def __init__(self, redis_url: str, component: str):
        super().__init__()
        self.redis = redis.from_url(redis_url)
        self.channel = f"logs:{component}"
        self.component = component

    def emit(self, record):
        try:
            log_entry = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S,%f")[:-3],
                "level": record.levelname,
                "component": self.component,
                "logger": record.name,
                "message": record.getMessage()
            }
            self.redis.publish(self.channel, json.dumps(log_entry))
        except Exception:
            pass  # No romper la app si Redis falla

# Uso
logger = logging.getLogger("my_app")
logger.addHandler(RedisLogHandler("redis://localhost:6379/0", "backend"))
logger.info("Este log aparece en logmon")
```

## Agregar Nuevo Source

1. Editar `config.py`:

```python
sources=[
    LogSource("backend", "cyan"),
    LogSource("batch", "yellow"),
    LogSource("ray", "magenta"),
    LogSource("mi_nuevo_source", "green"),  # Agregar aqui
]
```

2. Agregar shortcut en `monitor.py` (opcional):

```python
elif key_lower == 'x':  # Nueva tecla
    self.display.set_source_filter('mi_nuevo_source')
```

3. Actualizar footer en `ui.py` con el nuevo shortcut.

## Arquitectura

```
logmon/
├── main.py      # Entry point
├── config.py    # Configuracion (sources, redis_url)
├── monitor.py   # Loop principal, keyboard, reconnect
├── tail.py      # Redis subscriber (background thread)
└── ui.py        # Rich TUI components
```

## Troubleshooting

**No veo logs:**
- Verificar port-forward: `kubectl port-forward -n workers svc/redis 6379:6379`
- Verificar que el source este en config.py
- Verificar formato JSON del mensaje (necesita campo `component`)

**Se cuelga con muchos logs:**
- El cache de lineas filtradas deberia evitar esto
- Si persiste, reducir `max_lines` en config.py

**DISCONNECTED:**
- Logmon intenta reconectar automaticamente cada 5 segundos
- Verificar que el port-forward siga activo
