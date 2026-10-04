import os
import re
import math
import json
import queue
import sqlite3
import datetime
import hmac
import secrets
import time
import threading
from collections import defaultdict
from typing import List
from flask import Flask, request, jsonify, send_from_directory, render_template_string, Response

app = Flask(__name__, static_folder=".", static_url_path="")

DB_PATH = os.path.join(os.path.dirname(__file__), "caponera.db")

# =========================================================
# CORS HEADER (REQUERIDO PARA CAPONERA-APP.SURGE.SH)
# =========================================================
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    return response

# =========================================================
# RATE LIMITING & TTL DE VIAJES (CA-6)
# =========================================================
def get_client_ip() -> str:
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip

    xff = request.headers.get("X-Forwarded-For", "").strip()
    if xff:
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if parts:
            return parts[-1]

    return request.remote_addr or "127.0.0.1"

class InMemoryRateLimiter:
    def __init__(self, max_requests: int = 5, window_sec: int = 60):
        self.max_requests = max_requests
        self.window_sec = window_sec
        self.requests = defaultdict(list)
        self.lock = threading.Lock()

    def is_allowed(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            timestamps = self.requests[key]
            valid = [t for t in timestamps if now - t < self.window_sec]
            if len(valid) >= self.max_requests:
                self.requests[key] = valid
                return False
            valid.append(now)
            self.requests[key] = valid
            return True

viaje_rate_limiter = InMemoryRateLimiter(max_requests=5, window_sec=60)
recarga_rate_limiter = InMemoryRateLimiter(max_requests=5, window_sec=60)

def registrar_evento_bitacora(viaje_id: int, anterior: str, nuevo: str, actor: str = "sistema", detalles: str = ""):
    """Registra de forma inmutable cada cambio de estado en la tabla bitacora_estados (caja negra de auditoría)."""
    try:
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO bitacora_estados (viaje_id, estado_anterior, estado_nuevo, actor, detalles)
                VALUES (?, ?, ?, ?, ?)
            """, (viaje_id, anterior, nuevo, actor, detalles))
            conn.commit()
    except Exception:
        pass

def purge_expired_trips():
    """Barre viajes en estado 'buscando' que superan el TTL y los marca como 'expirado' (CA-6)."""
    try:
        ttl_sec = int(os.getenv("CAPONERA_VIAJE_TTL_SEC", "900"))
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id FROM viajes
                WHERE estado = 'buscando'
                  AND (strftime('%s', 'now') - strftime('%s', created_at)) > ?
            """, (ttl_sec,))
            expired_ids = [r["id"] for r in cursor.fetchall()]
            if expired_ids:
                cursor.execute("""
                    UPDATE viajes
                    SET estado = 'expirado', updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({})
                """.format(",".join("?" * len(expired_ids))), expired_ids)
                conn.commit()
                for x_id in expired_ids:
                    registrar_evento_bitacora(x_id, "buscando", "expirado", actor="sistema", detalles="TTL de búsqueda expirado")
    except Exception:
        pass

@app.before_request
def before_request_hook():
    purge_expired_trips()


# =========================================================
# BASE DE DATOS Y CONEXIONES (MODO WAL)
# =========================================================
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn

def init_db():
    with get_db() as conn:
        cursor = conn.cursor()
        
        # 1. Conductores
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conductores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                telefono TEXT NOT NULL UNIQUE,
                unidad TEXT NOT NULL,
                driver_token TEXT UNIQUE,
                lat REAL DEFAULT 12.1364,
                lng REAL DEFAULT -86.2514,
                is_online INTEGER DEFAULT 0,
                plan_activo INTEGER DEFAULT 1,
                plan_nombre TEXT DEFAULT 'Pionero (15 Días Gratis)',
                plan_expira TEXT,
                is_demo INTEGER DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        try:
            cursor.execute("ALTER TABLE conductores ADD COLUMN driver_token TEXT;")
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE conductores ADD COLUMN is_demo INTEGER DEFAULT 0;")
        except sqlite3.OperationalError:
            pass

        # Marcar conductores demo existentes
        cursor.execute("UPDATE conductores SET is_demo = 1 WHERE telefono IN ('50589130414', '50588881111', '50588882222')")

        # Generar driver_token para conductores existentes que no lo tengan
        cursor.execute("SELECT id FROM conductores WHERE driver_token IS NULL OR driver_token = ''")
        for row in cursor.fetchall():
            cursor.execute("UPDATE conductores SET driver_token = ? WHERE id = ?", (secrets.token_hex(16), row["id"]))
        
        # 2. Viajes
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS viajes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_token TEXT,
                pasajero_nombre TEXT DEFAULT 'Pasajero Express',
                cliente_telefono TEXT DEFAULT '',
                origen TEXT NOT NULL,
                destino TEXT NOT NULL,
                tarifa REAL NOT NULL,
                lat_origen REAL,
                lng_origen REAL,
                estado TEXT DEFAULT 'buscando',
                conductor_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conductor_id) REFERENCES conductores(id)
            )
        """)
        try:
            cursor.execute("ALTER TABLE viajes ADD COLUMN session_token TEXT;")
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE viajes ADD COLUMN cliente_telefono TEXT DEFAULT '';")
        except sqlite3.OperationalError:
            pass
        
        # 3. Recargas Banpro
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS recargas_banpro (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conductor_id INTEGER NOT NULL,
                plan_nombre TEXT NOT NULL,
                monto REAL NOT NULL,
                referencia TEXT,
                estado TEXT DEFAULT 'pendiente',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (conductor_id) REFERENCES conductores(id)
            )
        """)
        
        # 4. Registro de Visitas y Analítica
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS visitas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ip TEXT,
                user_agent TEXT,
                origen_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 5. Bitácora de Estados (Caja Negra de Auditoría)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bitacora_estados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                viaje_id INTEGER NOT NULL,
                estado_anterior TEXT,
                estado_nuevo TEXT NOT NULL,
                actor TEXT DEFAULT 'sistema',
                detalles TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (viaje_id) REFERENCES viajes(id)
            )
        """)
        
        # Insertar conductores iniciales si la tabla está vacía Y CAPONERA_SEED_DEMO == "1"
        cursor.execute("SELECT COUNT(*) FROM conductores")
        if cursor.fetchone()[0] == 0 and os.getenv("CAPONERA_SEED_DEMO", "0") == "1":
            is_launch_free = os.environ.get("PLAN_GRATIS_LAUNCH", "").strip() == "1"
            exp_days = 365 if is_launch_free else 15
            plan_seed_nom = "Lanzamiento (Gratis)" if is_launch_free else "Pionero (15 Días Gratis)"
            exp_date = (datetime.datetime.now() + datetime.timedelta(days=exp_days)).strftime("%Y-%m-%d")
            initial_drivers = [
                ("José Ramón", "50589130414", "Unidad #7 · Caponera Express", secrets.token_hex(16), 12.1370, -86.2520, 1, 1, plan_seed_nom, exp_date, 1),
                ("Alex Mendoza", "50588881111", "Caponera #14 (Tarifa Básica)", secrets.token_hex(16), 12.1390, -86.2490, 1, 1, plan_seed_nom, exp_date, 1),
                ("María González", "50588882222", "Moto Taxi #09", secrets.token_hex(16), 12.1340, -86.2540, 1, 1, plan_seed_nom, exp_date, 1)
            ]
            cursor.executemany("""
                INSERT INTO conductores (nombre, telefono, unidad, driver_token, lat, lng, is_online, plan_activo, plan_nombre, plan_expira, is_demo)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, initial_drivers)
            
        conn.commit()

init_db()

# Cálculo de distancia Haversine en KM
def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    try:
        R = 6371.0
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
        c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
        return round(R * c, 2)
    except Exception:
        return 1.0

# =========================================================
# AUTENTICACIÓN DEL LADO CONDUCTOR (CA-2)
# =========================================================
def get_authenticated_driver(expected_conductor_id=None):
    """
    Autentica al conductor mediante el header X-Driver-Token en tiempo constante (CA-2).
    Si expected_conductor_id es provisto, valida que el token pertenezca exactamente a ese conductor.
    Si expected_conductor_id no es provisto, busca el conductor asociado al token.
    Retorna (driver_dict, None) si es válido, o (None, (error_response, 403)).
    """
    token = request.headers.get("X-Driver-Token", "").strip()
    if not token:
        return None, (jsonify({"success": False, "error": "Autenticación requerida: Header X-Driver-Token ausente"}), 403)

    with get_db() as conn:
        cursor = conn.cursor()
        if expected_conductor_id is not None:
            try:
                cid = int(expected_conductor_id)
            except (ValueError, TypeError):
                return None, (jsonify({"success": False, "error": "ID de conductor inválido"}), 400)
            cursor.execute("SELECT * FROM conductores WHERE id = ?", (cid,))
            driver = cursor.fetchone()
            if not driver or not driver["driver_token"] or not hmac.compare_digest(token, driver["driver_token"]):
                return None, (jsonify({"success": False, "error": "Acceso denegado: X-Driver-Token no coincide con el conductor"}), 403)
            return dict(driver), None
        else:
            cursor.execute("SELECT * FROM conductores WHERE driver_token IS NOT NULL AND driver_token != ''")
            for d in cursor.fetchall():
                if hmac.compare_digest(token, d["driver_token"]):
                    return dict(d), None
            return None, (jsonify({"success": False, "error": "Acceso denegado: X-Driver-Token inválido"}), 403)

# =========================================================
# RUTAS ESTÁTICAS Y PWA
# =========================================================
@app.route("/")
def index():
    try:
        ip = get_client_ip()
        ua = request.headers.get('User-Agent', '')[:255]
        ref = (request.referrer or '')[:255]
        with get_db() as conn:
            conn.cursor().execute("INSERT INTO visitas (ip, user_agent, origen_url) VALUES (?, ?, ?)", (ip, ua, ref))
            conn.commit()
    except Exception:
        pass
    return send_from_directory(".", "index.html")

@app.route("/manifest.json")
def manifest():
    return send_from_directory(".", "manifest.json", mimetype="application/manifest+json")

@app.route("/sw.js")
def service_worker():
    return send_from_directory(".", "sw.js", mimetype="application/javascript")

def get_app_version():
    if os.getenv("APP_VERSION"):
        return os.getenv("APP_VERSION")
    if os.path.exists("version.txt"):
        try:
            with open("version.txt", "r", encoding="utf-8") as f:
                content = f.read().strip()
                if content:
                    return content
        except Exception:
            pass
    try:
        import subprocess
        ver = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
        if ver:
            return ver
    except Exception:
        pass
    return "1.0.0-hardened"

@app.route("/api/version", methods=["GET"])
@app.route("/health", methods=["GET"])
def get_version():
    return jsonify({
        "app": "caponera-app",
        "version": get_app_version(),
        "status": "healthy"
    })

@app.route("/privacidad")
def privacy_page():
    return send_from_directory(".", "privacidad.html")

@app.route("/<path:filename>")
def static_files(filename):
    return send_from_directory(".", filename)


# =========================================================
# RUTAS API: CONFIGURACIÓN Y CIUDAD OPERATIVA (CA-9)
# =========================================================
@app.route("/api/config", methods=["GET"])
def get_config():
    ciudad = os.getenv("CAPONERA_CIUDAD", "Masaya")
    try:
        tarifa_min = float(os.getenv("CAPONERA_TARIFA_MIN", "15.0"))
    except ValueError:
        tarifa_min = 15.0
    try:
        tarifa_max = float(os.getenv("CAPONERA_TARIFA_MAX", "250.0"))
    except ValueError:
        tarifa_max = 250.0

    zonas_env = os.getenv("CAPONERA_ZONAS", "")
    if zonas_env:
        zonas = [z.strip() for z in zonas_env.split(",") if z.strip()]
    else:
        zonas = [
            f"Mercado Municipal de {ciudad}",
            f"Parque Central de {ciudad}",
            f"Malecón de {ciudad}",
            "Monimbó",
            "San Jerónimo",
            "Las 7 Esquinas"
        ]
    return jsonify({
        "ciudad": ciudad,
        "tarifa_min": tarifa_min,
        "tarifa_max": tarifa_max,
        "zonas": zonas
    })

# =========================================================
# RUTAS API: CONDUCTORES
# =========================================================
@app.route("/api/conductores", methods=["GET"])
@app.route("/api/conductores/activos", methods=["GET"])
def get_conductores():
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    seed_demo = os.getenv("CAPONERA_SEED_DEMO", "0") == "1"
    
    with get_db() as conn:
        cursor = conn.cursor()
        is_launch_free = os.environ.get("PLAN_GRATIS_LAUNCH", "").strip() == "1"
        if is_launch_free:
            query = """
                SELECT id, nombre, unidad, lat, lng, is_online, plan_activo, plan_expira, is_demo 
                FROM conductores 
                WHERE is_online = 1
            """
        else:
            query = """
                SELECT id, nombre, unidad, lat, lng, is_online, plan_activo, plan_expira, is_demo 
                FROM conductores 
                WHERE is_online = 1 
                  AND plan_activo = 1
                  AND (plan_expira IS NULL OR date(plan_expira) >= date('now'))
            """
        if not seed_demo:
            query += " AND (is_demo IS NULL OR is_demo = 0)"

        cursor.execute(query)
        rows = cursor.fetchall()
        
    conductores = []
    for r in rows:
        d = {
            "id": r["id"],
            "nombre": r["nombre"],
            "unidad": r["unidad"],
            "lat": r["lat"],
            "lng": r["lng"],
            "is_online": r["is_online"],
            "plan_activo": r["plan_activo"]
        }
        if lat is not None and lng is not None and d["lat"] is not None and d["lng"] is not None:
            dist_km = calculate_distance(lat, lng, d["lat"], d["lng"])
            d["distancia_km"] = round(dist_km, 2)
            d["tiempo_llegada_min"] = max(2, int(dist_km * 4))
        else:
            d["distancia_km"] = 0.5
            d["tiempo_llegada_min"] = 3
        conductores.append(d)
        
    conductores.sort(key=lambda x: x.get("distancia_km", 0))
    
    # Si la petición viene de app.js clásico espera array directo, si viene de nueva versión espera dict
    if request.path == "/api/conductores/activos":
        return jsonify(conductores)
    return jsonify({"success": True, "conductores": conductores})

@app.route("/api/conductor/ubicacion", methods=["POST"])
@app.route("/api/conductor/<int:conductor_id>/posicion", methods=["POST"])
def update_posicion(conductor_id=None):
    data = request.get_json(silent=True) or {}
    
    if conductor_id is None:
        conductor_id = data.get("conductor_id")
        
    driver, auth_err = get_authenticated_driver(conductor_id)
    if auth_err:
        return auth_err
    conductor_id = driver["id"]

    try:
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Coordenadas numéricas requeridas"}), 400

    is_online = 1 if data.get("is_online", 1) in (1, True, "1") else 0
        
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE conductores 
            SET lat = ?, lng = ?, is_online = ?, updated_at = CURRENT_TIMESTAMP 
            WHERE id = ?
        """, (lat, lng, is_online, conductor_id))
        conn.commit()

    return jsonify({"success": True, "mensaje": "Posición actualizada"})

# =========================================================
# RUTAS API: VIAJES Y ASIGNACIÓN ATÓMICA
# =========================================================
@app.route("/api/viajes/crear", methods=["POST"])
@app.route("/api/viajes/solicitar", methods=["POST"])
def solicitar_viaje():
    client_ip = get_client_ip()
    if not viaje_rate_limiter.is_allowed(client_ip):
        return jsonify({"success": False, "error": "Demasiadas solicitudes. Límite de creación de viajes excedido por IP (HTTP 429)"}), 429

    data = request.get_json(silent=True) or {}
    pasajero = str(data.get("pasajero_nombre", "Pasajero Express"))[:100]
    cliente_telefono = str(data.get("cliente_telefono") or data.get("telefono") or data.get("whatsapp") or "").strip()
    origen = str(data.get("origen", "Punto Actual"))[:150]
    destino = str(data.get("destino", "Destino Indicado"))[:150]
    # Token criptográfico de sesión para autorización de cancelación (Cero IDOR)
    session_token = str(data.get("session_token") or os.urandom(16).hex())

    if not cliente_telefono:
        return jsonify({"success": False, "error": "El número de teléfono/WhatsApp del pasajero es obligatorio"}), 400

    if not re.match(r'^(\+?505)?[2578]\d{7}$', cliente_telefono):
        return jsonify({"success": False, "error": "Número de teléfono/WhatsApp inválido. Ingrese 8 dígitos válidos"}), 400
    
    try:
        tarifa = float(data.get("tarifa", 35.0))
        lat_o = float(data.get("lat_origen", data.get("lat", 12.1364)))
        lng_o = float(data.get("lng_origen", data.get("lng", -86.2514)))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Parámetros inválidos"}), 400

    try:
        tarifa_min = float(os.getenv("CAPONERA_TARIFA_MIN", "15.0"))
    except ValueError:
        tarifa_min = 15.0
    try:
        tarifa_max = float(os.getenv("CAPONERA_TARIFA_MAX", "250.0"))
    except ValueError:
        tarifa_max = 250.0

    if tarifa < tarifa_min or tarifa > tarifa_max:
        return jsonify({
            "success": False, 
            "error": f"Tarifa fuera de rango. Debe estar entre C$ {tarifa_min:.0f} y C$ {tarifa_max:.0f}"
        }), 400

    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO viajes (session_token, pasajero_nombre, cliente_telefono, origen, destino, tarifa, lat_origen, lng_origen, estado)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'buscando')
        """, (session_token, pasajero, cliente_telefono, origen, destino, tarifa, lat_o, lng_o))
        viaje_id = cursor.lastrowid
        conn.commit()

    registrar_evento_bitacora(viaje_id, None, "buscando", actor="pasajero", detalles=f"Viaje solicitado por {pasajero}")

    return jsonify({
        "success": True, 
        "viaje_id": viaje_id,
        "session_token": session_token,
        "estado": "buscando",
        "mensaje": "Buscando caponera cercana..."
    })

@app.route("/api/viajes/<int:viaje_id>/estado", methods=["GET"])
def get_estado_viaje(viaje_id):
    token = (
        request.headers.get("X-Session-Token")
        or request.args.get("token")
        or request.args.get("session_token")
        or ""
    ).strip()
    driver_token = (request.headers.get("X-Driver-Token") or "").strip()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT v.id, v.session_token, v.estado, v.tarifa, v.origen, v.destino, v.conductor_id, v.cliente_telefono,
                   c.id as cond_id, c.nombre as conductor_nombre, c.telefono as conductor_telefono, c.unidad as conductor_unidad,
                   c.driver_token as cond_driver_token
            FROM viajes v
            LEFT JOIN conductores c ON v.conductor_id = c.id
            WHERE v.id = ?
        """, (viaje_id,))
        row = cursor.fetchone()
        
    if not row:
        return jsonify({"success": False, "error": "Viaje no encontrado"}), 404

    reg_token = (row["session_token"] or "").strip()
    cond_driver_token = (row["cond_driver_token"] or "").strip()

    is_passenger = bool(token and reg_token and hmac.compare_digest(token, reg_token))
    is_assigned_driver = bool(driver_token and cond_driver_token and hmac.compare_digest(driver_token, cond_driver_token) and row["conductor_id"])

    if not is_passenger and not is_assigned_driver:
        return jsonify({"success": False, "error": "UNAUTHORIZED: Token de sesión o conductor requerido o inválido para consultar estado del viaje"}), 403
    
    conductor_obj = None
    if row["conductor_id"]:
        conductor_obj = {
            "id": row["cond_id"],
            "nombre": row["conductor_nombre"],
            "telefono": row["conductor_telefono"],
            "unidad": row["conductor_unidad"]
        }
        
    res = {
        "id": row["id"],
        "estado": row["estado"],
        "tarifa": row["tarifa"],
        "origen": row["origen"],
        "destino": row["destino"],
        "conductor_id": row["conductor_id"],
        "conductor_nombre": row["conductor_nombre"],
        "conductor_telefono": row["conductor_telefono"],
        "conductor_unidad": row["conductor_unidad"],
        "conductor": conductor_obj,
        "success": True
    }
    # El teléfono del cliente se muestra SOLO al conductor que aceptó ese viaje
    if is_assigned_driver:
        res["cliente_telefono"] = row["cliente_telefono"]
    return jsonify(res)

@app.route("/api/viajes/<int:viaje_id>/cancelar", methods=["POST"])
@app.route("/api/cancelar", methods=["POST"])
def cancelar_viaje(viaje_id=None):
    data = request.get_json(silent=True) or {}
    if viaje_id is None:
        viaje_id = data.get("viaje_id") or data.get("id")

    if not viaje_id:
        return jsonify({"success": False, "error": "ID de viaje requerido"}), 400

    token = (
        request.headers.get("X-Session-Token")
        or (data.get("session_token") if isinstance(data, dict) else None)
        or (data.get("token") if isinstance(data, dict) else None)
        or request.args.get("session_token")
        or request.args.get("token")
        or ""
    ).strip()

    if not token:
        return jsonify({"success": False, "error": "Autenticación requerida: session_token ausente"}), 403

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT session_token, estado FROM viajes WHERE id = ?", (viaje_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Viaje no encontrado"}), 404

        reg_token = (row["session_token"] or "").strip()
        # Control de Autorización estricto (Anti-IDOR) en tiempo constante
        if not reg_token or not hmac.compare_digest(token, reg_token):
            return jsonify({"success": False, "error": "UNAUTHORIZED: Token de sesión no coincide con el emisor del viaje"}), 403

        prev_estado = row["estado"]
        cursor.execute("""
            UPDATE viajes 
            SET estado = 'cancelado', lat_origen = NULL, lng_origen = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND estado IN ('buscando', 'aceptado')
        """, (viaje_id,))
        conn.commit()

    registrar_evento_bitacora(viaje_id, prev_estado, "cancelado", actor="pasajero", detalles="Cancelado por pasajero con session_token")

    return jsonify({"success": True, "mensaje": "Viaje cancelado exitosamente"})

@app.route("/api/conductor/viajes_pendientes", methods=["GET"])
@app.route("/api/conductor/viajes-pendientes", methods=["GET"])
def get_viajes_pendientes():
    conductor_id = request.args.get("conductor_id")
    driver, auth_err = get_authenticated_driver(conductor_id)
    if auth_err:
        return auth_err

    conductor_lat = float(request.args.get("lat", 12.1364))
    conductor_lng = float(request.args.get("lng", -86.2514))
    max_km = float(request.args.get("radio_km", 4.0))

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM viajes WHERE estado = 'buscando' ORDER BY id DESC LIMIT 10")
        rows = cursor.fetchall()
        
    carreras = []
    for r in rows:
        dist_km = calculate_distance(conductor_lat, conductor_lng, r["lat_origen"] or 12.1364, r["lng_origen"] or -86.2514)
        if dist_km <= max_km:
            carreras.append({
                "id": r["id"],
                "pasajero": r["pasajero_nombre"],
                "origen": r["origen"],
                "destino": r["destino"],
                "tarifa": r["tarifa"],
                "distancia_km": dist_km,
                "created_at": r["created_at"]
            })
            
    if request.path == "/api/conductor/viajes-pendientes":
        return jsonify(carreras)
    return jsonify({"success": True, "viajes": carreras})

@app.route("/api/viajes/<int:viaje_id>/aceptar", methods=["POST"])
def aceptar_viaje(viaje_id):
    data = request.get_json(silent=True) or {}
    raw_cid = data.get("conductor_id")
    conductor_id = None
    if raw_cid is not None:
        try:
            conductor_id = int(raw_cid)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "ID de conductor inválido"}), 400

    driver, auth_err = get_authenticated_driver(conductor_id)
    if auth_err:
        return auth_err
    conductor_id = driver["id"]

    # Validar que el plan del conductor esté activo y no expirado (CA-7)
    is_launch_free = os.environ.get("PLAN_GRATIS_LAUNCH", "").strip() == "1"
    if not is_launch_free:
        if driver.get("plan_activo") != 1:
            return jsonify({"success": False, "error": "Acceso denegado: El plan del conductor está inactivo"}), 403

        plan_exp = driver.get("plan_expira")
        if plan_exp:
            try:
                exp_d = datetime.datetime.strptime(str(plan_exp)[:10], "%Y-%m-%d").date()
                if exp_d < datetime.date.today():
                    with get_db() as c_up:
                        c_up.cursor().execute("UPDATE conductores SET plan_activo = 0 WHERE id = ?", (conductor_id,))
                        c_up.commit()
                    return jsonify({"success": False, "error": "Acceso denegado: El plan del conductor ha expirado"}), 403
            except Exception:
                pass
    
    with get_db() as conn:
        cursor = conn.cursor()
        # Asignación ATÓMICA: previene condiciones de carrera si dos conductores aceptan a la vez
        cursor.execute("""
            UPDATE viajes 
            SET estado = 'aceptado', conductor_id = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND estado = 'buscando'
        """, (conductor_id, viaje_id))
        conn.commit()
        
        if cursor.rowcount == 0:
            return jsonify({"success": False, "error": "El viaje ya fue tomado por otro conductor"}), 409

        cursor.execute("SELECT id, nombre, telefono, unidad FROM conductores WHERE id = ?", (conductor_id,))
        cond_row = cursor.fetchone()
        cond_data = dict(cond_row) if cond_row else {}

        cursor.execute("SELECT cliente_telefono FROM viajes WHERE id = ?", (viaje_id,))
        viaje_row = cursor.fetchone()
        cliente_tel = viaje_row["cliente_telefono"] if viaje_row else ""

    registrar_evento_bitacora(viaje_id, "buscando", "aceptado", actor="conductor", detalles=f"Aceptado por {cond_data.get('nombre', 'Conductor')} (ID {conductor_id})")

    return jsonify({
        "success": True, 
        "mensaje": "¡Viaje asignado con éxito! Dirígete al punto de recogida.",
        "conductor": cond_data,
        "cliente_telefono": cliente_tel
    })

@app.route("/api/viajes/<int:viaje_id>/actualizar-estado", methods=["POST"])
def actualizar_estado_viaje(viaje_id):
    data = request.get_json(silent=True) or {}
    nuevo_estado = (data.get("estado") or "").strip().lower()
    estados_validos = {"buscando", "aceptado", "en_camino", "completado", "cancelado"}
    if nuevo_estado not in estados_validos:
        return jsonify({"success": False, "error": f"Estado inválido. Opciones: {list(estados_validos)}"}), 400

    token_conductor = request.headers.get("X-Driver-Token", "").strip()
    token_sesion = (request.headers.get("X-Session-Token") or str(data.get("session_token", ""))).strip()

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM viajes WHERE id = ?", (viaje_id,))
        v = cursor.fetchone()
        if not v:
            return jsonify({"success": False, "error": "Viaje no encontrado"}), 404

        actor = "desconocido"
        if token_conductor:
            driver, auth_err = get_authenticated_driver(v["conductor_id"])
            if auth_err:
                return auth_err
            actor = f"conductor:{driver['nombre']}"
        elif token_sesion and v["session_token"] and hmac.compare_digest(token_sesion, v["session_token"]):
            actor = "pasajero"
        else:
            return jsonify({"success": False, "error": "No autorizado para cambiar el estado de este viaje"}), 403

        estado_ant = v["estado"]
        cursor.execute("UPDATE viajes SET estado = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (nuevo_estado, viaje_id))
        conn.commit()

    registrar_evento_bitacora(viaje_id, estado_ant, nuevo_estado, actor=actor, detalles=f"Transición a {nuevo_estado}")
    return jsonify({"success": True, "estado": nuevo_estado, "mensaje": f"Estado actualizado a {nuevo_estado}"})

# =========================================================
# RUTAS API: RECARGAS BANPRO
# =========================================================
@app.route("/api/conductor/recarga", methods=["POST"])
def registrar_recarga():
    client_ip = get_client_ip()
    if not recarga_rate_limiter.is_allowed(client_ip):
        return jsonify({"success": False, "error": "Demasiadas solicitudes de recarga. Intente más tarde (HTTP 429)"}), 429

    data = request.get_json(silent=True) or {}
    raw_cid = data.get("conductor_id")
    conductor_id = None
    if raw_cid is not None:
        try:
            conductor_id = int(raw_cid)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "Datos inválidos"}), 400

    driver, auth_err = get_authenticated_driver(conductor_id)
    if auth_err:
        return auth_err
    conductor_id = driver["id"]

    try:
        monto = float(data.get("monto", 50.0))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Datos inválidos"}), 400

    plan_nombre = str(data.get("plan_nombre", "Semanal (7 Días)"))[:50]
    referencia = str(data.get("referencia", ""))[:100]
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO recargas_banpro (conductor_id, plan_nombre, monto, referencia, estado)
            VALUES (?, ?, ?, ?, 'pendiente')
        """, (conductor_id, plan_nombre, monto, referencia))
        conn.commit()
        
    return jsonify({"success": True, "mensaje": "Comprobante registrado. En revisión."})

# =========================================================
# PANEL DE ADMINISTRACIÓN
# =========================================================
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>Panel de Control · Caponera App</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #090d16; color: #fff; padding: 20px; }
    .card { background: #131c2e; padding: 20px; border-radius: 12px; margin-bottom: 20px; border: 1px solid #1e2d4a; }
    h1, h2 { color: #10b981; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; }
    th, td { padding: 10px; text-align: left; border-bottom: 1px solid #1e2d4a; font-size: 0.9rem; }
    th { color: #94a3b8; }
    .badge { padding: 4px 8px; border-radius: 6px; font-weight: 700; font-size: 0.75rem; }
    .badge-success { background: rgba(16,185,129,0.2); color: #10b981; }
    .badge-warning { background: rgba(245,158,11,0.2); color: #f59e0b; }
    .btn { display: inline-block; padding: 8px 16px; background: #10b981; color: #fff; text-decoration: none; border-radius: 6px; font-weight: 600; margin-bottom: 15px; }
  </style>
</head>
<body>
  <h1>🛺 Caponera App · Panel de Control</h1>
  <a href="/" class="btn">📱 Abrir App en Vivo</a>
  <div class="card">
    <h2>Conductores Registrados</h2>
    <table>
      <thead>
        <tr><th>ID</th><th>Nombre</th><th>Teléfono</th><th>Unidad</th><th>Driver Token (PIN)</th><th>Plan</th><th>Estado</th></tr>
      </thead>
      <tbody>
        {% for c in conductores %}
        <tr>
          <td>{{ c.id }}</td>
          <td>{{ c.nombre }}</td>
          <td>{{ c.telefono }}</td>
          <td>{{ c.unidad }}</td>
          <td><code>{{ c.driver_token }}</code></td>
          <td><span class="badge badge-success">{{ c.plan_nombre }}</span></td>
          <td>{{ 'Online 🟢' if c.is_online else 'Offline ⚪' }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  <div class="card">
    <h2>Últimos Viajes Solicitados</h2>
    <table>
      <thead>
        <tr><th>ID</th><th>Pasajero</th><th>Origen ➔ Destino</th><th>Tarifa</th><th>Estado</th><th>Expediente</th></tr>
      </thead>
      <tbody>
        {% for v in viajes %}
        <tr>
          <td>#{{ v.id }}</td>
          <td>{{ v.pasajero_nombre }}</td>
          <td>{{ v.origen }} ➔ {{ v.destino }}</td>
          <td>C$ {{ v.tarifa }}</td>
          <td><span class="badge badge-warning">{{ v.estado }}</span></td>
          <td><a href="/admin/caso/{{ v.id }}?key={{ admin_key }}" style="color: #38bdf8; text-decoration: none; font-weight: bold;">Ver Caso</a></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</body>
</html>
"""

CASE_DOSSIER_HTML = """
<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Expediente de Caso #{{ viaje.id }} - Caponera App Admin</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; background: #090d16; color: #f8fafc; margin: 0; padding: 24px; }
    .container { max-width: 900px; margin: 0 auto; }
    .header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #1e293b; padding-bottom: 16px; margin-bottom: 24px; }
    h1 { margin: 0; font-size: 1.5rem; color: #f59e0b; }
    .badge { display: inline-block; padding: 4px 10px; border-radius: 9999px; font-size: 0.75rem; font-weight: 700; text-transform: uppercase; }
    .badge-buscando { background: #0284c7; color: #fff; }
    .badge-aceptado { background: #d97706; color: #fff; }
    .badge-en_camino { background: #6366f1; color: #fff; }
    .badge-completado { background: #059669; color: #fff; }
    .badge-cancelado { background: #dc2626; color: #fff; }
    .badge-expirado { background: #475569; color: #fff; }
    .card { background: #0f172a; border: 1px solid #1e293b; border-radius: 12px; padding: 18px; margin-bottom: 18px; }
    h2 { font-size: 1rem; color: #cbd5e1; margin-top: 0; border-bottom: 1px solid #334155; padding-bottom: 8px; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
    .field-label { font-size: 0.75rem; color: #64748b; text-transform: uppercase; font-weight: 700; display: block; margin-bottom: 4px; }
    .field-value { font-size: 0.95rem; font-weight: 600; color: #f1f5f9; }
    table { width: 100%; border-collapse: collapse; margin-top: 10px; font-size: 0.85rem; }
    th, td { text-align: left; padding: 10px; border-bottom: 1px solid #1e293b; }
    th { color: #94a3b8; font-weight: 600; }
    .btn { display: inline-block; background: #334155; color: #fff; text-decoration: none; padding: 8px 14px; border-radius: 8px; font-size: 0.85rem; font-weight: 600; }
    .btn:hover { background: #475569; }
    .dossier-box { background: #020617; border: 1px solid #1e293b; border-radius: 8px; padding: 12px; font-family: monospace; font-size: 0.8rem; white-space: pre-wrap; color: #94a3b8; max-height: 200px; overflow-y: auto; }
  </style>
</head>
<body>
<div class="container">
  <div class="header">
    <div>
      <h1>Expediente de Caso #{{ viaje.id }}</h1>
      <span style="font-size: 0.8rem; color: #94a3b8;">Fecha creación: {{ viaje.created_at }}</span>
    </div>
    <div>
      <span class="badge badge-{{ viaje.estado }}">{{ viaje.estado }}</span>
      <a href="/admin?key={{ admin_key }}" class="btn" style="margin-left: 10px;">← Volver al Panel</a>
    </div>
  </div>

  <div class="card">
    <h2>1. Pasajero y Datos de la Carrera</h2>
    <div class="grid">
      <div>
        <span class="field-label">Nombre del Pasajero</span>
        <span class="field-value">{{ viaje.pasajero_nombre or 'No especificado' }}</span>
      </div>
      <div>
        <span class="field-label">WhatsApp / Teléfono Pasajero (Privado Admin)</span>
        <span class="field-value">
          {% if viaje.cliente_telefono %}
            <a href="https://wa.me/{{ viaje.cliente_telefono }}" target="_blank" style="color: #f59e0b;">{{ viaje.cliente_telefono }}</a>
          {% else %}
            <span style="color: #64748b;">No registrado</span>
          {% endif %}
        </span>
      </div>
      <div>
        <span class="field-label">Tarifa Acordada</span>
        <span class="field-value" style="color: #10b981;">C$ {{ viaje.tarifa }}</span>
      </div>
    </div>
    <div class="grid" style="margin-top: 10px;">
      <div>
        <span class="field-label">Punto de Recogida (Origen)</span>
        <span class="field-value">{{ viaje.origen }}</span>
      </div>
      <div>
        <span class="field-label">Destino</span>
        <span class="field-value">{{ viaje.destino }}</span>
      </div>
    </div>
  </div>

  <div class="card">
    <h2>2. Conductor Asignado</h2>
    {% if conductor %}
    <div class="grid">
      <div>
        <span class="field-label">Nombre del Conductor</span>
        <span class="field-value">{{ conductor.nombre }}</span>
      </div>
      <div>
        <span class="field-label">WhatsApp / Teléfono Conductor</span>
        <span class="field-value">
          <a href="https://wa.me/{{ conductor.telefono }}" target="_blank" style="color: #f59e0b;">{{ conductor.telefono }}</a>
        </span>
      </div>
      <div>
        <span class="field-label">Caponera / Unidad</span>
        <span class="field-value">{{ conductor.unidad }}</span>
      </div>
    </div>
    {% else %}
    <p style="color: #94a3b8; font-style: italic; margin: 5px 0;">No se asignó conductor a este viaje.</p>
    {% endif %}
  </div>

  <div class="card">
    <h2>3. Bitácora de Auditoría ("Caja Negra")</h2>
    <p style="font-size: 0.8rem; color: #94a3b8; margin: 0 0 10px 0;">Registro cronológico inmutable de transiciones de estado para auditoría y resolución de disputas.</p>
    <table>
      <thead>
        <tr>
          <th>Fecha / Hora</th>
          <th>Estado Anterior</th>
          <th>Estado Nuevo</th>
          <th>Actor Responsable</th>
          <th>Detalles / Observaciones</th>
        </tr>
      </thead>
      <tbody>
        {% for b in bitacora %}
        <tr>
          <td>{{ b.created_at }}</td>
          <td><code>{{ b.estado_anterior or '—' }}</code></td>
          <td><strong style="color: #f59e0b;">{{ b.estado_nuevo }}</strong></td>
          <td>{{ b.actor }}</td>
          <td>{{ b.detalles }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>4. Resumen Textual para Expediente / Evidencia</h2>
    <div id="rawDossierText" class="dossier-box">=== EXPEDIENTE OFICIAL CAPONERA APP ===
ID Viaje: #{{ viaje.id }}
Fecha Solicitud: {{ viaje.created_at }}
Estado Actual: {{ viaje.estado }}
Tarifa: C$ {{ viaje.tarifa }}
Origen: {{ viaje.origen }}
Destino: {{ viaje.destino }}
Pasajero: {{ viaje.pasajero_nombre }} (Tel: {{ viaje.cliente_telefono or 'N/A' }})
--- CONDUCTOR ASIGNADO ---
Nombre: {{ conductor.nombre if conductor else 'N/A' }}
Teléfono: {{ conductor.telefono if conductor else 'N/A' }}
Caponera/Unidad: {{ conductor.unidad if conductor else 'N/A' }}
--- BITÁCORA DE ESTADOS ---
{% for b in bitacora %}[{{ b.created_at }}] {{ b.estado_anterior or 'INICIO' }} -> {{ b.estado_nuevo }} | Actor: {{ b.actor }} | {{ b.detalles }}
{% endfor %}=======================================</div>
  </div>
</div>
</body>
</html>
"""

@app.route("/admin")
def admin_panel():
    admin_key = os.getenv("CAPONERA_ADMIN_KEY", "").strip()
    provided_key = (
        request.args.get("key")
        or request.headers.get("X-Admin-Key")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
    ).strip()

    if not admin_key or not provided_key or not hmac.compare_digest(provided_key, admin_key):
        return jsonify({"success": False, "error": "Acceso denegado: Llave de administración requerida o inválida"}), 403

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM conductores")
        conductores = [dict(r) for r in cursor.fetchall()]
        cursor.execute("SELECT * FROM viajes ORDER BY id DESC LIMIT 10")
        viajes = [dict(r) for r in cursor.fetchall()]
    return render_template_string(ADMIN_HTML, conductores=conductores, viajes=viajes, admin_key=provided_key)

@app.route("/admin/caso/<int:viaje_id>", methods=["GET"])
def admin_caso(viaje_id):
    admin_key = os.getenv("CAPONERA_ADMIN_KEY", "").strip()
    provided_key = (
        request.args.get("key")
        or request.headers.get("X-Admin-Key")
        or request.headers.get("Authorization", "").replace("Bearer ", "")
    ).strip()

    if not admin_key or not provided_key or not hmac.compare_digest(provided_key, admin_key):
        return jsonify({"success": False, "error": "Acceso denegado: Llave de administración requerida o inválida"}), 403

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM viajes WHERE id = ?", (viaje_id,))
        viaje_row = cursor.fetchone()
        if not viaje_row:
            return jsonify({"success": False, "error": "Viaje no encontrado"}), 404

        viaje = dict(viaje_row)

        conductor = None
        if viaje.get("conductor_id"):
            cursor.execute("SELECT id, nombre, telefono, unidad FROM conductores WHERE id = ?", (viaje["conductor_id"],))
            cond_row = cursor.fetchone()
            if cond_row:
                conductor = dict(cond_row)

        cursor.execute("SELECT * FROM bitacora_estados WHERE viaje_id = ? ORDER BY id ASC", (viaje_id,))
        bitacora = [dict(r) for r in cursor.fetchall()]

    if request.is_json or request.args.get("format") == "json" or request.headers.get("Accept") == "application/json":
        return jsonify({
            "success": True,
            "viaje": viaje,
            "conductor": conductor,
            "bitacora": bitacora
        })

    return render_template_string(CASE_DOSSIER_HTML, viaje=viaje, conductor=conductor, bitacora=bitacora, admin_key=provided_key)

if __name__ == "__main__":
    host_bind = os.getenv("HOST", "0.0.0.0")
    print("==================================================")
    print(f"[OK] CAPONERA ENGINE ACTIVO en http://{host_bind}:5054")
    print("   Tiempo Real (SSE) y API de Despacho Listos")
    print("==================================================")
    app.run(host=host_bind, port=5054, debug=False, threaded=True)
