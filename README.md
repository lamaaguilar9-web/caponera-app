# Caponera App 🛺

Plataforma comunitaria Progressive Web App (PWA) de despacho y enlace directo para transporte liviano (mototaxis / caponeras) y mandados locales.

Diseñada con arquitectura ligera y desacoplada: Frontend Vanilla PWA (cero dependencias de framework, sonido sintético vía Web Audio API, mapas Leaflet) con Backend Flask impulsado por un bus reactivo de Server-Sent Events (SSE) y almacenamiento transaccional SQLite en modo WAL.

---

## 1. Arquitectura del Contenedor Docker

La aplicación está completamente dockerizada para despliegues reproducibles e independientes:

- **Imagen Base:** `python:3.11-slim`
- **Puerto Interno y Loopback:** `5054` (mapeado estrictamente como `127.0.0.1:5054:5054` en el host).
- **Aislamiento de Red:** Siguiendo la regla de seguridad de Sentinel Fleet, el contenedor se amarra exclusivamente a loopback local (`127.0.0.1`), impidiendo exposición directa a internet. El tráfico público ingresa mediante el reverse proxy seguro de Nginx en `caponera.sentinelfleet.tech` con terminación TLS.

### Arranque del Contenedor

Para construir y levantar el contenedor en segundo plano:

```bash
# Construir y levantar
docker compose up -d --build

# Verificar estado
docker compose ps

# Ver registros en tiempo real
docker compose logs -f caponera-app
```

---

## 2. Variables de Entorno y Configuración (.env)

El servicio lee sus parámetros del entorno operativo. Siguiendo la política de seguridad estricta:

> **REGLA DE HIGIENE:** Cero secretos, tokens o credenciales en el repositorio de Git ni en las imágenes de Docker. Cualquier variable sensible reside exclusivamente en el archivo `/root/caponera_app/.env` en el servidor VPS con permisos restrictivos `chmod 600`.

### Variables Soportadas

| Variable | Tipo | Valor por Defecto | Descripción |
| :--- | :--- | :--- | :--- |
| `HOST` | String | `127.0.0.1` | Dirección de enlace HTTP del servidor Python Flask. |
| `PORT` | Int | `5054` | Puerto TCP de escucha del servidor. |
| `DEBUG` | Bool | `False` | Modo de depuración de Flask (desactivado en producción). |

---

## 3. Persistencia de Datos y Política de Respaldos

### Persistencia en SQLite (Modo WAL)
Los datos transaccionales se almacenan en un archivo SQLite local (`caponera.db`), montado como volumen bind en `docker-compose.yml`:

```yaml
volumes:
  - ./caponera.db:/app/caponera.db
```

El motor de base de datos activa automáticamente las siguientes directivas de alto rendimiento y concurrencia:
- `PRAGMA journal_mode=WAL;` (Write-Ahead Logging para lecturas no bloqueantes durante escrituras concurrentes).
- `PRAGMA foreign_keys=ON;` (Integridad referencial activa).
- `PRAGMA busy_timeout=5000;` (Resiliencia ante bloqueos transitorios).

### Esquema de Tablas
1. **`conductores`**: Registro de conductores, unidades, coordenadas GPS de última posición, estado de conexión (`is_online`) y planes de membresía activa.
2. **`viajes`**: Solicitudes de carreras, token de sesión temporal, origen/destino, tarifa acordada, estado del viaje (`buscando`, `asignado`, `completado`, `cancelado`) y conductor asignado.
3. **`recargas_banpro`**: Registro de pagos y transferencias bancarias locales para renovación de planes.
4. **`visitas`**: Registro analítico interno de IPs anonimizadas, referrers y User-Agents.

### Procedimiento de Respaldo en Caliente (Hot Backup)
Dado que la base de datos opera en modo WAL, un respaldo seguro y consistente sin detener el contenedor se ejecuta utilizando la API de respaldo de SQLite:

```bash
# Ejecutar copia en caliente desde el host
sqlite3 caponera.db ".backup 'backups/caponera_backup_$(date +%Y%m%d_%H%M%S).db'"

# O dentro del contenedor Docker:
docker exec caponera_app sqlite3 /app/caponera.db ".backup '/app/caponera_hotbackup.db'"
```

---

## 4. Inventario de Parámetros Hardcodeados

*(Documentados para futura parametrización / multi-inquilino según directriz de auditoría GLM, conservando código actual sin modificaciones).*

### A. Ubicaciones y Coordenadas Geográficas
- **Ciudad Principal de Despliegue:** Masaya / Granada / Managua, Nicaragua.
- **Coordenadas de Referencia por Defecto (`server.py` y `app.js`):** `lat: 12.1364`, `lng: -86.2514` (Coordenadas céntricas Managua/Masaya para centrado de mapa Leaflet).
- **Zonas de Publicidad:** Masaya Centro, San Jerónimo, Mercado Municipal.

### B. Títulos y Marca
- **Título en HTML (`index.html`):** `Caponera App 🛺 | Transporte y Mandados en Masaya`
- **Nombre de Unidad Pionera:** `Unidad #7 · Caponera Express`
- **Términos Legales:** Referencia expresa a `Ley 787 · Caponera App Masaya` (Ley de Protección de Datos Personales de Nicaragua).

### C. Teléfonos y Enlaces de Contacto WhatsApp
- **Teléfono de Despacho Central / Administrador (`app.js` / `server.py`):** `50589130414` (`+505 8913-0414`).
- **Línea Banpro / Pagos (`BANPRO_WHATSAPP_PHONE` en `app.js`):** `50589130414`.
- **Conductores Semilla Iniciales (`server.py`):**
  - `José Ramón`: `50589130414` (Unidad #7 · Caponera Express, Coords: `12.1370, -86.2520`).
  - `Alex Mendoza`: `50588881111` (Caponera #14, Coords: `12.1390, -86.2490`).
  - `María González`: `50588882222` (Moto Taxi #09, Coords: `12.1340, -86.2540`).
- **Anunciantes Locales Semilla (`index.html`):**
  - Polarizados & Focos: `50589130414`.
  - Vigorón Mixto & Fritanga (Doña Tania): `50588889999`.
  - Taller de Mototaxis San Jerónimo: `tel:50588887777`.
  - Farmacia San Jerónimo: `50588886666`.

### D. Tarifas y Moneda
- **Moneda:** Córdobas Nicaragüenses (`C$`).
- **Selector de Fares Rápidos (`app.js`):** `20 + idx * 5` (`C$ 20.00`, `C$ 25.00`, `C$ 30.00`).
- **Tarifa Base Pre-pactada (`server.py` / `app.js`):** `C$ 35.00`.
- **Costo de Pauta Publicitaria Comunitaria:** `C$ 150` mensuales.

---

## 5. Control de Despliegue y Pruebas

- **Script de Despliegue Rápido al VPS:** `DESPLEGAR_CAPONERA_VPS.bat` (despliegue seguro vía SCP + restart condicional de Docker).
- **Pruebas de Despacho:** `test_dispatch.py` (simulación de eventos y verificación de latencia de entrega SSE).
- **Panel Administrativo:** Disponible localmente en `/admin` con vista de conductores activos y viajes solicitados.
