# Bot Peluquería

## Descripción general
Bot Peluquería es un asistente virtual para salones de belleza que automatiza la gestión de reservas a través de WhatsApp. La aplicación expone un webhook construido con Flask que recibe los mensajes de la WhatsApp Cloud API, los interpreta con modelos de OpenAI y mantiene el estado de la conversación para ofrecer un flujo guiado de reserva, modificación o cancelación de citas. Toda la información de peluquerías, servicios y reservas se persiste en MySQL y se sincroniza con Google Calendar, mientras que Redis se utiliza opcionalmente para idempotencia y limitación de peticiones.【F:Bot_ia_secretaria_peluqueria/app.py†L1220-L1399】【F:Bot_ia_secretaria_peluqueria/models.py†L1-L73】【F:Bot_ia_secretaria_peluqueria/reserva_utils.py†L1-L160】【F:Bot_ia_secretaria_peluqueria/settings.py†L18-L41】

## Funcionalidades principales
- **Integración con WhatsApp Cloud API**: endpoints `/webhook/whatsapp` para verificación y recepción de mensajes, validación de firmas y control de idempotencia por mensaje.【F:Bot_ia_secretaria_peluqueria/app.py†L1220-L1399】
- **Motor conversacional**: endpoint `/webhook` que gestiona el estado por sesión, aplica límites de uso y coordina la lógica de reserva, modificación, cancelación y resolución de dudas usando OpenAI.【F:Bot_ia_secretaria_peluqueria/app.py†L1555-L2927】【F:Bot_ia_secretaria_peluqueria/interpretador_ia.py†L1-L191】
- **Persistencia relacional**: modelos SQLAlchemy para peluquerías, servicios y reservas, con índices y relaciones listas para migraciones Alembic.【F:Bot_ia_secretaria_peluqueria/models.py†L1-L73】【F:migrations/env.py†L19-L137】
- **Sincronización con Google Calendar**: utilidades dedicadas para crear, modificar o cancelar eventos mediante una cuenta de servicio de Google.【F:Bot_ia_secretaria_peluqueria/google_calendar_utils.py†L1-L200】
- **Observabilidad y hardening**: integración opcional con Sentry, logging rotativo, límites de velocidad configurables y endpoints de salud `/live`, `/health` y `/ready`.【F:Bot_ia_secretaria_peluqueria/app.py†L60-L119】【F:Bot_ia_secretaria_peluqueria/routers/health.py†L1-L78】

## Requisitos previos
- Python 3.11+
- MySQL/MariaDB (se usa MariaDB 10.11 en Docker) y acceso a un Redis opcional para almacenamiento de estado compartido.【F:Bot_ia_secretaria_peluqueria/docker-compose.yaml†L3-L38】
- Cuenta de OpenAI con clave de API válida y credenciales de servicio de Google Calendar.
- Credenciales de WhatsApp Business Platform (app secret, verify token y token de envío).

## Variables de entorno
Crea un archivo `.env` en la raíz del proyecto (o junto a `docker-compose.yaml`) y define las variables necesarias. Los valores por defecto corresponden a la configuración de desarrollo.

| Variable | Descripción | Valor por defecto |
| --- | --- | --- |
| `MYSQL_USER` | Usuario de la base de datos MySQL. | `bot`【F:Bot_ia_secretaria_peluqueria/settings.py†L18-L23】 |
| `MYSQL_PASS` | Contraseña de la base de datos. | `botpass`【F:Bot_ia_secretaria_peluqueria/settings.py†L18-L23】 |
| `MYSQL_HOST` | Host o IP de MySQL. | `localhost`【F:Bot_ia_secretaria_peluqueria/settings.py†L18-L23】 |
| `MYSQL_DB` | Nombre de la base de datos. | `bot_pelu`【F:Bot_ia_secretaria_peluqueria/settings.py†L18-L23】 |
| `OPENAI_API_KEY` | Clave de API de OpenAI (obligatoria). | —【F:Bot_ia_secretaria_peluqueria/settings.py†L25-L26】 |
| `CAL_TZ` | Zona horaria para las reservas. | `Europe/Madrid`【F:Bot_ia_secretaria_peluqueria/settings.py†L28-L31】 |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | Ruta al JSON de la cuenta de servicio para Google Calendar. | `credentials.json`【F:Bot_ia_secretaria_peluqueria/settings.py†L28-L31】 |
| `WABA_VERIFY_TOKEN` | Token para verificar el webhook de WhatsApp. | `changeme`【F:Bot_ia_secretaria_peluqueria/settings.py†L32-L36】 |
| `WABA_APP_SECRET` | App secret usado para validar firmas de Meta. | `changeme`【F:Bot_ia_secretaria_peluqueria/settings.py†L32-L36】 |
| `WABA_TOKEN` | Token de acceso para enviar mensajes desde WhatsApp Cloud. | —【F:Bot_ia_secretaria_peluqueria/settings.py†L32-L36】 |
| `GRAPH_API_VERSION` | Versión del Graph API usada para WhatsApp. | `v23.0`【F:Bot_ia_secretaria_peluqueria/settings.py†L32-L36】 |
| `STORAGE_BACKEND` | Backend de almacenamiento (`memory` o `redis`). | `memory`【F:Bot_ia_secretaria_peluqueria/settings.py†L38-L41】 |
| `REDIS_URL` | URL de Redis si se usa almacenamiento compartido. | `redis://localhost:6379/0`【F:Bot_ia_secretaria_peluqueria/settings.py†L38-L41】 |
| `RATE_LIMIT_PER_MIN` | Mensajes permitidos por minuto y sesión. | `100`【F:Bot_ia_secretaria_peluqueria/settings.py†L38-L41】 |
| `SENTRY_DSN` | (Opcional) DSN de Sentry para captura de errores. | —【F:Bot_ia_secretaria_peluqueria/app.py†L60-L119】 |
| `FLASK_ENV` | Entorno (`development`, `production`, etc.). | `production` si no se define.【F:Bot_ia_secretaria_peluqueria/app.py†L71-L83】 |
| `BOT_INTERNAL_URL` | URL interna a la que se reenvían los mensajes procesados. | `http://127.0.0.1:5000`【F:Bot_ia_secretaria_peluqueria/app.py†L1388-L1410】 |
| `DATABASE_URL` | Cadena de conexión que usa Alembic para las migraciones. | —【F:migrations/env.py†L62-L137】 |
| `MYSQL_ROOT_PASSWORD` | (Solo Docker) contraseña del usuario root de MariaDB. | `root`【F:Bot_ia_secretaria_peluqueria/docker-compose.yaml†L3-L16】 |

## Instalación

### Con Docker Compose
1. Copia o crea un archivo `.env` junto a `Bot_ia_secretaria_peluqueria/docker-compose.yaml` con las variables necesarias y coloca el `credentials.json` de Google en el mismo directorio.
2. Desde `Bot-Peluqueria/Bot_ia_secretaria_peluqueria` ejecuta:
   ```bash
   docker compose up --build
   ```
   El servicio aplicará automáticamente las migraciones iniciales (`python db.py`) antes de levantar Gunicorn en el puerto 8000.【F:Bot_ia_secretaria_peluqueria/docker-compose.yaml†L27-L38】
3. La API estará disponible en `http://localhost:8000` y expondrá los webhooks y endpoints de salud.

### Instalación local (sin Docker)
1. Asegúrate de tener MySQL/MariaDB en marcha y crea la base de datos definida en `MYSQL_DB`. Si quieres habilitar límites distribuidos, arranca un Redis accesible desde `REDIS_URL`.
2. Crea y activa un entorno virtual:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```
3. Instala las dependencias:
   ```bash
   pip install -r Bot_ia_secretaria_peluqueria/requirements.txt
   ```
4. Copia el archivo `credentials.json` en `Bot_ia_secretaria_peluqueria/` o actualiza `GOOGLE_SERVICE_ACCOUNT_FILE` con la ruta correspondiente.
5. Exporta las variables de entorno necesarias (ver tabla anterior). Para desarrollo local puedes usar un archivo `.env` en la raíz del proyecto.
6. Inicializa la base de datos ejecutando:
   ```bash
   python Bot_ia_secretaria_peluqueria/db.py
   ```
   Esto creará las tablas definidas en los modelos mediante SQLAlchemy.【F:Bot_ia_secretaria_peluqueria/db.py†L1-L44】

## Migraciones de base de datos
Este proyecto usa Alembic. Ejecuta los comandos desde la raíz del repositorio (`alembic.ini` está en la raíz):

```bash
export DATABASE_URL="mysql+pymysql://bot:botpass@localhost/bot_pelu?charset=utf8mb4"
alembic upgrade head
```

Para generar una nueva migración tras modificar los modelos:

```bash
alembic revision --autogenerate -m "descripcion"
```

Alembic obtendrá la conexión desde `DATABASE_URL` y usará la metadata de `models.Base`.【F:migrations/env.py†L19-L137】

## Puesta en marcha del servidor
- **Desarrollo local**: ejecuta
  ```bash
  python Bot_ia_secretaria_peluqueria/app.py
  ```
  El servidor se levantará en `http://127.0.0.1:5000` con recarga automática y modo debug habilitado.【F:Bot_ia_secretaria_peluqueria/app.py†L2923-L2932】
- **Producción (local o Docker)**: usa Gunicorn con la configuración incluida:
  ```bash
  cd Bot_ia_secretaria_peluqueria
  gunicorn -c gunicorn.conf.py app:app
  ```
  El contenedor oficial usa este enfoque para exponer la aplicación en el puerto 8000.【F:Bot_ia_secretaria_peluqueria/gunicorn.conf.py†L1-L8】【F:Bot_ia_secretaria_peluqueria/docker-compose.yaml†L27-L38】

## Ejecución de pruebas
Las dependencias de pruebas (`pytest`, `pytest-cov`) ya están incluidas en `requirements.txt`. Desde la raíz del repositorio ejecuta:

```bash
pytest
```

Agrega `-k`, `-x` o `--cov` según sea necesario para filtrar pruebas, detenerse ante el primer fallo o generar reportes de cobertura.【F:Bot_ia_secretaria_peluqueria/requirements.txt†L1-L34】

## Cómo contribuir
1. Haz un fork del repositorio y crea una rama descriptiva para tus cambios.
2. Instala las dependencias, configura el entorno y ejecuta las pruebas antes de enviar tu contribución.
3. Asegúrate de que cualquier cambio en los modelos vaya acompañado de su migración Alembic.
4. Abre un Pull Request describiendo el propósito del cambio, pasos de verificación y cualquier dependencia adicional.

## Reporte de problemas y soporte
Utiliza el apartado de *Issues* del repositorio para reportar bugs o solicitar nuevas funcionalidades. Incluye pasos de reproducción, logs relevantes y la información de tu entorno (versión de Python, base de datos y configuración de entorno) para facilitar el diagnóstico.
