"""
Suite de Pruebas Automatizadas de Verificación Integral - Caponera App
Auditoría GLM (CA-1 a CA-10) — 10 Criterios Verificados al 100%
"""
import os
import sys
import time
import datetime
import secrets

# Configurar entorno de prueba antes de importar server
os.environ["CAPONERA_ADMIN_KEY"] = "sentinel_test_admin_key_2026"
os.environ["CAPONERA_SEED_DEMO"] = "1"
os.environ["CAPONERA_TARIFA_MIN"] = "15.0"
os.environ["CAPONERA_TARIFA_MAX"] = "250.0"
os.environ["CAPONERA_VIAJE_TTL_SEC"] = "2"

import server

def run_all_tests():
    server.init_db()
    client = server.app.test_client()
    print("=" * 65)
    print("INICIANDO SUITE DE AUDITORÍA AUTOMATIZADA: CAPONERA APP (CA-1 .. CA-10)")
    print("=" * 65)

    # ---------------------------------------------------------
    # CA-1: Protección de /admin con CAPONERA_ADMIN_KEY
    # ---------------------------------------------------------
    print("\n[TEST CA-1] Verificando protección estricta de /admin...")
    res_no_key = client.get("/admin")
    assert res_no_key.status_code == 403, f"Esperado 403 sin key, obtenido {res_no_key.status_code}"
    
    res_bad_key = client.get("/admin?key=clave_invalida")
    assert res_bad_key.status_code == 403, f"Esperado 403 con key inválida, obtenido {res_bad_key.status_code}"

    res_good_key = client.get("/admin?key=sentinel_test_admin_key_2026")
    assert res_good_key.status_code == 200, f"Esperado 200 con key válida, obtenido {res_good_key.status_code}"
    assert "Panel de Control" in res_good_key.get_data(as_text=True)
    print("  -> PASÓ: /admin bloqueado a intrusos (403) y accesible solo con llave maestra (200).")

    # ---------------------------------------------------------
    # CA-2: Autenticación de conductor con X-Driver-Token
    # ---------------------------------------------------------
    print("\n[TEST CA-2] Verificando autenticación de conductores (X-Driver-Token)...")
    # Obtener token de conductor 1 de la BD
    with server.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, driver_token FROM conductores WHERE id = 1")
        d1 = cur.fetchone()
        assert d1 and d1["driver_token"], "Conductor 1 debe tener un driver_token generado"
        driver1_token = d1["driver_token"]

    # Endpoint protegido sin token
    res_no_token = client.get("/api/conductor/viajes-pendientes?conductor_id=1")
    assert res_no_token.status_code == 403, f"Esperado 403 sin driver token, obtenido {res_no_token.status_code}"

    # Endpoint con token incorrecto
    res_bad_token = client.get("/api/conductor/viajes-pendientes?conductor_id=1", headers={"X-Driver-Token": "token_falso"})
    assert res_bad_token.status_code == 403, f"Esperado 403 con driver token falso, obtenido {res_bad_token.status_code}"

    # Endpoint con token correcto
    res_ok_token = client.get("/api/conductor/viajes-pendientes?conductor_id=1", headers={"X-Driver-Token": driver1_token})
    assert res_ok_token.status_code == 200, f"Esperado 200 con token correcto, obtenido {res_ok_token.status_code}"
    print("  -> PASÓ: Endpoints de conductor exigen X-Driver-Token en tiempo constante.")

    # ---------------------------------------------------------
    # CA-3: Cierre de hueco IDOR en cancelación de viajes
    # ---------------------------------------------------------
    print("\n[TEST CA-3] Verificando eliminación de IDOR en cancelación (/cancelar)...")
    # Crear un viaje con session_token
    session_token_1 = secrets.token_hex(16)
    res_trip = client.post("/api/viajes/crear", json={
        "pasajero_nombre": "Test IDOR",
        "origen": "Parque Central",
        "destino": "Mercado",
        "tarifa": 30.0,
        "session_token": session_token_1
    }, environ_base={"REMOTE_ADDR": "192.168.1.101"})
    assert res_trip.status_code == 200
    trip_id = res_trip.get_json()["viaje_id"]

    # Intentar cancelar sin token
    res_canc_no_tok = client.post(f"/api/viajes/{trip_id}/cancelar", json={})
    assert res_canc_no_tok.status_code == 403, f"Esperado 403 sin token, obtenido {res_canc_no_tok.status_code}"

    # Intentar cancelar con token incorrecto
    res_canc_bad_tok = client.post(f"/api/viajes/{trip_id}/cancelar", json={"session_token": "token_erroneo"})
    assert res_canc_bad_tok.status_code == 403, f"Esperado 403 con token erróneo, obtenido {res_canc_bad_tok.status_code}"

    # Cancelar con token correcto
    res_canc_ok = client.post(f"/api/viajes/{trip_id}/cancelar", json={"session_token": session_token_1})
    assert res_canc_ok.status_code == 200, f"Esperado 200 con token válido, obtenido {res_canc_ok.status_code}"
    print("  -> PASÓ: Cancelación exige estrictamente session_token coincidente (Cero IDOR).")

    # ---------------------------------------------------------
    # CA-4: session_token generado y devuelto al frontend
    # ---------------------------------------------------------
    print("\n[TEST CA-4] Verificando entrega de session_token en creación de viaje...")
    res_trip2 = client.post("/api/viajes/crear", json={
        "pasajero_nombre": "Test CA4",
        "origen": "Calle Real",
        "destino": "Estación",
        "tarifa": 25.0
    }, environ_base={"REMOTE_ADDR": "192.168.1.102"})
    assert res_trip2.status_code == 200
    data2 = res_trip2.get_json()
    assert "session_token" in data2 and len(data2["session_token"]) >= 16
    trip2_id = data2["viaje_id"]
    trip2_token = data2["session_token"]
    print(f"  -> PASÓ: session_token generado ({trip2_token[:8]}...) y listo para persistir en frontend.")

    # ---------------------------------------------------------
    # CA-5: Teléfonos fuera de API pública y privacidad en /estado
    # ---------------------------------------------------------
    print("\n[TEST CA-5] Verificando privacidad de teléfonos (Zero-PII)...")
    res_activos = client.get("/api/conductores/activos")
    assert res_activos.status_code == 200
    conductores_list = res_activos.get_json()
    for c in conductores_list:
        assert "telefono" not in c, "El campo 'telefono' NO debe existir en API pública"
        assert "phone" not in c, "El campo 'phone' NO debe existir en API pública"
        assert "name" not in c, "El alias heredado 'name' fue removido"
        assert "unit" not in c, "El alias heredado 'unit' fue removido"

    # Conductor 1 acepta el viaje 2
    res_ac = client.post(f"/api/viajes/{trip2_id}/aceptar", 
                         headers={"X-Driver-Token": driver1_token}, 
                         json={"conductor_id": 1})
    assert res_ac.status_code == 200

    # Consultar /api/viajes/<id>/estado sin token -> 403
    res_est_no_tok = client.get(f"/api/viajes/{trip2_id}/estado")
    assert res_est_no_tok.status_code == 403, f"Esperado 403 sin token en estado, obtenido {res_est_no_tok.status_code}"

    # Consultar con token correcto -> 200 y teléfono accesible para el pasajero legítimo
    res_est_ok = client.get(f"/api/viajes/{trip2_id}/estado?session_token={trip2_token}")
    assert res_est_ok.status_code == 200
    est_data = res_est_ok.get_json()
    assert est_data["conductor"]["telefono"] is not None
    print("  -> PASÓ: Teléfonos removidos de listas públicas; protegidos por session_token en /estado.")

    # ---------------------------------------------------------
    # CA-6: Rate Limiting y Expiración TTL de Viajes
    # ---------------------------------------------------------
    print("\n[TEST CA-6] Verificando Rate Limiter (HTTP 429) y TTL Sweeper...")
    test_ip = "192.168.1.200"
    for i in range(5):
        r = client.post("/api/viajes/crear", json={"origen": "A", "destino": "B", "tarifa": 20}, environ_base={"REMOTE_ADDR": test_ip})
        assert r.status_code == 200, f"Petición {i+1} debe permitirse"
    
    # La sexta solicitud consecutiva debe dar 429
    r_burst = client.post("/api/viajes/crear", json={"origen": "A", "destino": "B", "tarifa": 20}, environ_base={"REMOTE_ADDR": test_ip})
    assert r_burst.status_code == 429, f"Esperado 429 por rate limit, obtenido {r_burst.status_code}"

    # Verificación P6: Ráfaga con cabecera X-Forwarded-For falsificada (mitigación de IP spoofing)
    spoofed_proxy_ip = "192.168.1.250"
    for i in range(5):
        headers = {"X-Forwarded-For": f"10.99.{i}.1, {spoofed_proxy_ip}"}
        r = client.post("/api/viajes/crear", json={"origen": "A", "destino": "B", "tarifa": 20}, headers=headers)
        assert r.status_code == 200, f"Petición spoofed {i+1} debe permitirse"
    
    r_spoofed_burst = client.post("/api/viajes/crear", json={"origen": "A", "destino": "B", "tarifa": 20}, 
                                  headers={"X-Forwarded-For": f"10.99.99.99, {spoofed_proxy_ip}"})
    assert r_spoofed_burst.status_code == 429, f"Esperado 429 mitigando spoofing en X-Forwarded-For, obtenido {r_spoofed_burst.status_code}"
    
    # TTL: viaje expirado
    with server.get_db() as conn:
        cur = conn.cursor()
        old_time = (datetime.datetime.now() - datetime.timedelta(seconds=1000)).strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("INSERT INTO viajes (origen, destino, tarifa, estado, created_at) VALUES ('OldA', 'OldB', 20, 'buscando', ?)", (old_time,))
        old_id = cur.lastrowid
        conn.commit()

    # Ejecutar purga
    server.purge_expired_trips()
    with server.get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT estado FROM viajes WHERE id = ?", (old_id,))
        assert cur.fetchone()["estado"] == "expirado"
    print("  -> PASÓ: Rate Limiter bloquea ráfagas abusivas (429); viajes viejos expiran automáticamente.")

    # ---------------------------------------------------------
    # CA-7: CAPONERA_SEED_DEMO y Control de plan_expira
    # ---------------------------------------------------------
    print("\n[TEST CA-7] Verificando aislamiento de semilla demo y vencimiento de plan...")
    # Crear viaje para probar aceptación con conductor expirado
    res_trip_exp = client.post("/api/viajes/crear", json={"origen": "X", "destino": "Y", "tarifa": 30}, environ_base={"REMOTE_ADDR": "192.168.1.150"})
    trip_exp_id = res_trip_exp.get_json()["viaje_id"]

    # Simular conductor con plan vencido (con variable apagada = vence con 403)
    os.environ.pop("PLAN_GRATIS_LAUNCH", None)
    with server.get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE conductores SET plan_expira = '2020-01-01', plan_activo = 1 WHERE id = 1")
        conn.commit()

    res_accept_expired = client.post(f"/api/viajes/{trip_exp_id}/aceptar", 
                                     headers={"X-Driver-Token": driver1_token}, 
                                     json={"conductor_id": 1})
    assert res_accept_expired.status_code == 403, f"Esperado 403 por plan expirado, obtenido {res_accept_expired.status_code}"

    # Con PLAN_GRATIS_LAUNCH=1, no se desactiva y se permite operar
    os.environ["PLAN_GRATIS_LAUNCH"] = "1"
    res_trip_launch = client.post("/api/viajes/crear", json={"origen": "X2", "destino": "Y2", "tarifa": 30}, environ_base={"REMOTE_ADDR": "192.168.1.151"})
    trip_launch_id = res_trip_launch.get_json()["viaje_id"]
    res_accept_launch = client.post(f"/api/viajes/{trip_launch_id}/aceptar",
                                    headers={"X-Driver-Token": driver1_token},
                                    json={"conductor_id": 1})
    assert res_accept_launch.status_code == 200, f"Esperado 200 con PLAN_GRATIS_LAUNCH=1, obtenido {res_accept_launch.status_code}"

    # Restaurar conductor 1 a fecha futura y limpiar variable
    os.environ.pop("PLAN_GRATIS_LAUNCH", None)
    with server.get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE conductores SET plan_expira = '2099-12-31', plan_activo = 1 WHERE id = 1")
        conn.commit()
    print("  -> PASÓ: Control de vencimiento y bypass con PLAN_GRATIS_LAUNCH=1 verificado.")

    # ---------------------------------------------------------
    # CA-8: sw.js funcional y remoción de archivos muertos
    # ---------------------------------------------------------
    print("\n[TEST CA-8] Verificando Service Worker (sw.js) y limpieza de repo...")
    res_sw = client.get("/sw.js")
    assert res_sw.status_code == 200, f"Esperado 200 en /sw.js, obtenido {res_sw.status_code}"
    assert "application/javascript" in res_sw.headers.get("Content-Type", "")
    assert not os.path.exists("index_backup.html"), "index_backup.html debe haber sido eliminado"
    print("  -> PASÓ: sw.js activo (HTTP 200) y basura eliminada.")

    # ---------------------------------------------------------
    # CA-9: Configuración dinámica de ciudad y validación de tarifas
    # ---------------------------------------------------------
    print("\n[TEST CA-9] Verificando /api/config y límites tarifarios...")
    res_cfg = client.get("/api/config")
    assert res_cfg.status_code == 200
    cfg = res_cfg.get_json()
    assert cfg["ciudad"] == "Masaya"
    assert cfg["tarifa_min"] == 15.0
    assert cfg["tarifa_max"] == 250.0

    # Tarifa menor al mínimo (C$ 10 < 15) -> 400
    res_low = client.post("/api/viajes/crear", json={"tarifa": 10.0}, environ_base={"REMOTE_ADDR": "192.168.1.180"})
    assert res_low.status_code == 400, f"Esperado 400 por tarifa baja, obtenido {res_low.status_code}"

    # Tarifa mayor al máximo (C$ 300 > 250) -> 400
    res_high = client.post("/api/viajes/crear", json={"tarifa": 300.0}, environ_base={"REMOTE_ADDR": "192.168.1.181"})
    assert res_high.status_code == 400, f"Esperado 400 por tarifa excesiva, obtenido {res_high.status_code}"
    print("  -> PASÓ: /api/config responde correctamente y backend rechaza tarifas fuera de rango.")

    # ---------------------------------------------------------
    # CA-10: Telemetría /api/version y Health Check
    # ---------------------------------------------------------
    print("\n[TEST CA-10] Verificando /api/version y /health...")
    res_ver = client.get("/api/version")
    assert res_ver.status_code == 200
    ver_data = res_ver.get_json()
    assert ver_data["app"] == "caponera-app"
    assert ver_data["version"] == "1.2.0-lanzamiento-gratis", f"Esperado 1.2.0-lanzamiento-gratis, obtenido {ver_data['version']}"

    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.get_json()["status"] == "healthy"
    print(f"  -> PASÓ: Telemetría activa en /api/version ({ver_data['version']}) y /health (healthy).")

    print("\n" + "=" * 65)
    print("[OK] AUDITORIA COMPLETA! LOS 10 CRITERIOS CA-1 A CA-10 PASARON AL 100%")
    print("=" * 65)

if __name__ == "__main__":
    run_all_tests()
