# Configuración de logging para emitir registros estructurados en producción
import json, logging, os

class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "lvl": record.levelname,
            "msg": record.getMessage(),
            "name": record.name,
        }, ensure_ascii=False)

def setup_logging():
    if os.getenv("ENV") == "prod":
        root = logging.getLogger()
        root.setLevel(logging.WARNING)   # ← solo WARNING/ERROR/CRITICAL
        if not root.handlers:
            h = logging.StreamHandler()
            h.setFormatter(JsonFormatter())
            root.addHandler(h)
