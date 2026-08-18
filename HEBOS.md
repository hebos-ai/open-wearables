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
