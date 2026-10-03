import os
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

def purge_expired_trips():
    """Barre viajes en estado 'buscando' que superan el TTL y los marca como 'expirado' (CA-6)."""
    try:
        ttl_sec = int(os.getenv("CAPONERA_VIAJE_TTL_SEC", "900"))
        with get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE viajes
                SET estado = 'expirado', updated_at = CURRENT_TIMESTAMP
                WHERE estado = 'buscando'
                  AND (strftime('%s', 'now') - strftime('%s', created_at)) > ?
            """, (ttl_sec,))
            conn.commit()
    except Exception:
        pass

@app.before_request
def before_request_hook():
    purge_expired_trips()

# =========================================================
# BUS DE EVENTOS EN MEMORIA (SSE - TIEMPO REAL)
# =========================================================
class EventBus:
    def __init__(self):
        self.listeners: List[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=100)
        self.listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        if q in self.listeners:
            try:
                self.listeners.remove(q)
            except ValueError:
                pass

    def publish(self, event_type: str, data: dict):
        payload = f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
        for q in list(self.listeners):
            try:
                q.put_nowait(payload)
            except (queue.Full, Exception):
                self.unsubscribe(q)

event_bus = EventBus()

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
        
        # Insertar conductores iniciales si la tabla está vacía Y CAPONERA_SEED_DEMO == "1"
        cursor.execute("SELECT COUNT(*) FROM conductores")
        if cursor.fetchone()[0] == 0 and os.getenv("CAPONERA_SEED_DEMO", "0") == "1":
            exp_date = (datetime.datetime.now() + datetime.timedelta(days=15)).strftime("%Y-%m-%d")
            initial_drivers = [
                ("José Ramón", "50589130414", "Unidad #7 · Caponera Express", secrets.token_hex(16), 12.1370, -86.2520, 1, 1, "Pionero (15 Días Gratis)", exp_date, 1),
                ("Alex Mendoza", "50588881111", "Caponera #14 (Tarifa Básica)", secrets.token_hex(16), 12.1390, -86.2490, 1, 1, "Pionero (15 Días Gratis)", exp_date, 1),
                ("María González", "50588882222", "Moto Taxi #09", secrets.token_hex(16), 12.1340, -86.2540, 1, 1, "Pionero (15 Días Gratis)", exp_date, 1)
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
    try:
        import subprocess
        ver = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL).decode("utf-8").strip()
        if ver:
            return ver
    except Exception:
        pass
    if os.path.exists("version.txt"):
        try:
            with open("version.txt", "r", encoding="utf-8") as f:
                return f.read().strip()
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
# STREAMING SSE EN TIEMPO REAL
# =========================================================
@app.route("/api/stream")
def sse_stream():
    """Canal continuo SSE con heartbeat periódico (20s) y auto-purga de conexiones muertas."""
    def event_generator():
        client_queue = event_bus.subscribe()
        try:
            yield f"event: ping\ndata: {json.dumps({'time': datetime.datetime.now().isoformat()})}\n\n"
            while True:
                try:
                    # Timeout de 20s para despachar heartbeat activo
                    msg = client_queue.get(timeout=20.0)
                    yield msg
                except queue.Empty:
                    # Heartbeat activo: detecta inmediatamente desconexiones del cliente
                    yield f": heartbeat {datetime.datetime.now().isoformat()}\n\n"
        except (GeneratorExit, BrokenPipeError, ConnectionResetError, Exception):
            pass
        finally:
            event_bus.unsubscribe(client_queue)

    return Response(
        event_generator(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive"
        }
    )

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

    # Difusión en tiempo real por SSE
    event_bus.publish("conductor_movimiento", {
        "conductor_id": conductor_id,
        "lat": lat,
        "lng": lng,
        "is_online": is_online
    })
        
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
    origen = str(data.get("origen", "Punto Actual"))[:150]
    destino = str(data.get("destino", "Destino Indicado"))[:150]
    # Token criptográfico de sesión para autorización de cancelación (Cero IDOR)
    session_token = str(data.get("session_token") or os.urandom(16).hex())
    
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
            INSERT INTO viajes (session_token, pasajero_nombre, origen, destino, tarifa, lat_origen, lng_origen, estado)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'buscando')
        """, (session_token, pasajero, origen, destino, tarifa, lat_o, lng_o))
        viaje_id = cursor.lastrowid
        conn.commit()

    # Notificar a los conductores conectados en vivo
    event_bus.publish("nuevo_viaje", {
        "viaje_id": viaje_id,
        "pasajero": pasajero,
        "origen": origen,
        "destino": destino,
        "tarifa": tarifa,
        "lat": lat_o,
        "lng": lng_o
    })
        
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

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT v.id, v.session_token, v.estado, v.tarifa, v.origen, v.destino, v.conductor_id,
                   c.id as cond_id, c.nombre as conductor_nombre, c.telefono as conductor_telefono, c.unidad as conductor_unidad
            FROM viajes v
            LEFT JOIN conductores c ON v.conductor_id = c.id
            WHERE v.id = ?
        """, (viaje_id,))
        row = cursor.fetchone()
        
    if not row:
        return jsonify({"success": False, "error": "Viaje no encontrado"}), 404

    reg_token = (row["session_token"] or "").strip()
    if not token or not reg_token or not hmac.compare_digest(token, reg_token):
        return jsonify({"success": False, "error": "UNAUTHORIZED: Token de sesión requerido o inválido para consultar estado del viaje"}), 403
    
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
        cursor.execute("SELECT session_token FROM viajes WHERE id = ?", (viaje_id,))
        row = cursor.fetchone()
        if not row:
            return jsonify({"success": False, "error": "Viaje no encontrado"}), 404

        reg_token = (row["session_token"] or "").strip()
        # Control de Autorización estricto (Anti-IDOR) en tiempo constante
        if not reg_token or not hmac.compare_digest(token, reg_token):
            return jsonify({"success": False, "error": "UNAUTHORIZED: Token de sesión no coincide con el emisor del viaje"}), 403

        cursor.execute("""
            UPDATE viajes 
            SET estado = 'cancelado', lat_origen = NULL, lng_origen = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND estado IN ('buscando', 'aceptado')
        """, (viaje_id,))
        conn.commit()

    event_bus.publish("viaje_cancelado", {"viaje_id": viaje_id})
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

    # Notificar al pasajero en tiempo real por SSE
    event_bus.publish("viaje_aceptado", {
        "viaje_id": viaje_id,
        "conductor": cond_data
    })
        
    return jsonify({
        "success": True, 
        "mensaje": "¡Viaje asignado con éxito! Dirígete al punto de recogida.",
        "conductor": cond_data
    })

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
        <tr><th>ID</th><th>Pasajero</th><th>Origen ➔ Destino</th><th>Tarifa</th><th>Estado</th></tr>
      </thead>
      <tbody>
        {% for v in viajes %}
        <tr>
          <td>#{{ v.id }}</td>
          <td>{{ v.pasajero_nombre }}</td>
          <td>{{ v.origen }} ➔ {{ v.destino }}</td>
          <td>C$ {{ v.tarifa }}</td>
          <td><span class="badge badge-warning">{{ v.estado }}</span></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
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
    return render_template_string(ADMIN_HTML, conductores=conductores, viajes=viajes)

if __name__ == "__main__":
    host_bind = os.getenv("HOST", "0.0.0.0")
    print("==================================================")
    print(f"[OK] CAPONERA ENGINE ACTIVO en http://{host_bind}:5054")
    print("   Tiempo Real (SSE) y API de Despacho Listos")
    print("==================================================")
    app.run(host=host_bind, port=5054, debug=False, threaded=True)
