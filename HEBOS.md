# Open Wearables — Hebos

Fork de [the-momentum/open-wearables](https://github.com/the-momentum/open-wearables) para Hebos / Welabox.

## Rol en Hebos

- **Lectura multi-proveedor** (Oura, Fitbit, Polar, Whoop, Suunto, Strava, Apple/Samsung vía SDK…).
- **Garmin NO va por aquí** para el tenant 0 (Hebos). Garmin read+write vive en `hebos-ai/garmin-connector`.
- Clinic/App no hablan con OW en directo: lo hace la fachada `hebos-ai/welabox`.

## Arranque local (puertos remapeados)

No uses el `docker-compose.yml` stock si ya tenés MySQL/Redis de `hebos-infra` en 3306/6379 y Clinic en 3000.

```bash
cp backend/config/.env.hebos.example backend/config/.env
cp frontend/.env.example frontend/.env
# Editar SECRET_KEY, ADMIN_PASSWORD; dejar GARMIN_* vacíos

docker compose -f docker-compose.yml -f docker-compose.hebos.yml up -d --build
```

| Servicio | Host |
|---|---|
| API | http://localhost:8000 |
| Frontend admin | http://localhost:3010 |
| Postgres | localhost:5434 |
| Redis | localhost:6380 |
| Svix | localhost:8071 |

## Sin Garmin

En `.env.hebos.example` las variables `GARMIN_CLIENT_ID/SECRET` están vacías a propósito.
No registres una app Garmin en este stack para el tenant Hebos.

## Guardar el crudo y las muestras de entreno

Plan del inventario de datos de salud, fase F1: no perder nada de lo que mandan las marcas.

### Crudo (`RAW_PAYLOAD_STORAGE`)

Open Wearables puede guardar tal cual cada aviso de webhook, cada respuesta de la API de una marca y cada subida del SDK, antes de normalizarlo. Lo hacen Polar, Oura, Whoop, Google Health, Suunto, Strava, Wahoo, Ultrahuman, Sensorbio y el SDK (`store_raw_payload`).

| Valor | Qué hace | En Hebos |
|---|---|---|
| `disabled` (por defecto) | No guarda nada | **Así se queda** hasta que haya bucket |
| `log` | Escribe el JSON en la salida estándar | Solo en local. En test o producción metería datos de salud en los logs |
| `s3` | Un objeto por crudo en `{RAW_PAYLOAD_S3_PREFIX}/{proveedor}/{origen}/{AAAA-MM-DD}/{usuario}/{uuid}.json` | Cuando exista el bucket |

Para activarlo en un entorno (**no está activado en ninguno**; el almacenamiento lo da Mariano):

1. Bucket de Hebos en una región de la UE, privado, cifrado (SSE-S3 o KMS), sin versión pública y con una regla de ciclo de vida que borre los objetos a los N días que fije el asesor (retención por fuente).
2. Rol o usuario con permiso solo de `s3:PutObject` sobre el prefijo (y `s3:GetObject` para quien reprocese).
3. Variables del servicio (api y workers de Celery):
   ```
   RAW_PAYLOAD_STORAGE=s3
   RAW_PAYLOAD_S3_BUCKET=<bucket>
   RAW_PAYLOAD_S3_PREFIX=raw-payloads
   AWS_REGION=<región de la UE>
   # credenciales: mejor el rol de la tarea (ECS) que AWS_ACCESS_KEY_ID/SECRET
   ```
   `RAW_PAYLOAD_MAX_SIZE_BYTES` (10 MB por defecto) descarta los crudos más grandes.
4. Reprocesar: `scripts/replay_raw_payloads.py` (ver `.env.hebos.example`).

Garmin no pasa por aquí: su crudo lo guarda `garmin-connector` (`garmin_webhook_raw`). El crudo es dato de salud: hay que borrarlo cuando se borra el usuario o se retira su consentimiento, y dejarlo fuera de analítica e IA.

### Muestras de entreno (`INGEST_WORKOUT_SAMPLES`)

Con `true` se guardan las muestras por segundo de cada entreno (FC, velocidad, cadencia, potencia, altitud y GPS) en `data_point_series`. En Hebos va a `true` (`.env.hebos.example`). Hoy solo lo aprovecha **Strava** (streams); Garmin también lo usa, pero en Hebos no entra por Open Wearables. Ocupa bastante base de datos: hay que vigilar el tamaño de `data_point_series` y la archivación (`archival_setting`).

`STORE_FIT_FILES=true` guardaría además los FIT originales en S3. Necesita el mismo bucket, así que también se queda apagado.

