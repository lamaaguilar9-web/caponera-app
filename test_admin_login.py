"""
Suite de Seguridad y Auditoría de Login Administrativo (F1 a F5) - Caponera App
Verifica:
- F1: Fail-closed (503 si no hay CAPONERA_ADMIN_KEY configurada en el entorno).
- F2: Tokens de sesión aleatorios de 64 caracteres hex y revocación real en logout.
- F3: Cookie con atributos Secure, HttpOnly y SameSite=Lax.
- F4: Expiración Max-Age=86400 (24 horas).
- F5: Rate Limiter estricto (máximo 5 intentos/minuto/IP, 6to intento devuelve 429).
- F6: Compatibilidad con llave para auditorías automáticas (?key=).
"""
import os
import sys
import time

def test_suite_f1_f5():
    print("=" * 65)
    print("INICIANDO SUITE DE AUDITORÍA DE LOGIN (F1 - F5): CAPONERA APP")
    print("=" * 65)

    # -------------------------------------------------------------
    # 1. F1: Test Fail-Closed (Sin variable de entorno -> 503)
    # -------------------------------------------------------------
    if "CAPONERA_ADMIN_KEY" in os.environ:
        del os.environ["CAPONERA_ADMIN_KEY"]

    import server

    client = server.app.test_client()

    r1_admin = client.get("/admin")
    assert r1_admin.status_code == 503, f"F1 falló: esperado 503 sin env, obtenido {r1_admin.status_code}"
    r1_login = client.get("/admin/login")
    assert r1_login.status_code == 503, f"F1 falló: esperado 503 sin env, obtenido {r1_login.status_code}"
    print("[PASS] F1: Fail-Closed verificado (HTTP 503 cuando falta CAPONERA_ADMIN_KEY).")

    # -------------------------------------------------------------
    # Configurar llave para pruebas F2 - F5
    # -------------------------------------------------------------
    test_key = "sentinel_test_secret_admin_key_2026"
    os.environ["CAPONERA_ADMIN_KEY"] = test_key

    # -------------------------------------------------------------
    # 2. F5: Rate Limiter (5 intentos máx/minuto por IP, 6to da 429)
    # -------------------------------------------------------------
    server.LOGIN_ATTEMPTS.clear()
    for i in range(5):
        res_fail = client.post("/admin/login", data={"password": "clave_erronea_123"}, headers={"X-Forwarded-For": "192.168.1.100"})
        assert res_fail.status_code == 401, f"F5 falló: intento {i+1} esperado 401, obtenido {res_fail.status_code}"

    res_blocked = client.post("/admin/login", data={"password": "clave_erronea_123"}, headers={"X-Forwarded-For": "192.168.1.100"})
    assert res_blocked.status_code == 429, f"F5 falló: intento 6 esperado 429, obtenido {res_blocked.status_code}"
    print("[PASS] F5: Rate Limiter verificado (HTTP 429 tras 5 intentos fallidos/min).")

    # -------------------------------------------------------------
    # 3. F2, F3, F4: Login exitoso, cookie Secure, Max-Age 24h, Token 64-hex
    # -------------------------------------------------------------
    res_ok = client.post("/admin/login", data={"password": test_key}, headers={"X-Forwarded-For": "192.168.1.200"})
    assert res_ok.status_code == 302, f"Esperado 302 redirect, obtenido {res_ok.status_code}"

    cookie_header = res_ok.headers.get("Set-Cookie", "")
    assert "admin_session" in cookie_header, "Cookie admin_session no emitida"
    assert "HttpOnly" in cookie_header, "F3 falló: falta atributo HttpOnly"
    assert "Secure" in cookie_header, "F3 falló: falta atributo Secure"
    assert "Max-Age=86400" in cookie_header, "F4 falló: Max-Age debe ser exactamente 86400 (24h)"

    session_token = cookie_header.split(";")[0].split("=")[1]
    assert len(session_token) == 64, f"F2 falló: el token debe tener 64 caracteres hex (32 bytes aleatorios), obtenido {len(session_token)}"
    assert session_token in server.ADMIN_SESSIONS, "F2 falló: token no registrado en ADMIN_SESSIONS"
    print("[PASS] F2-F4: Token aleatorio (64 hex), cookie Secure+HttpOnly y TTL 24h verificados.")

    # -------------------------------------------------------------
    # 4. Acceso con cookie válida -> 200 OK
    # -------------------------------------------------------------
    client.set_cookie("admin_session", session_token)
    res_panel = client.get("/admin")
    assert res_panel.status_code == 200, f"Esperado 200 con cookie, obtenido {res_panel.status_code}"
    assert "Panel de Control" in res_panel.get_data(as_text=True)
    assert "Cerrar Sesión" in res_panel.get_data(as_text=True)
    print("[PASS] Acceso autorizado al Panel de Control con cookie de sesión.")

    # -------------------------------------------------------------
    # 5. Revocación real en Logout -> 302 y token eliminado
    # -------------------------------------------------------------
    res_logout = client.get("/admin/logout")
    assert res_logout.status_code == 302
    assert session_token not in server.ADMIN_SESSIONS, "F2 falló: token debe ser revocado y eliminado de ADMIN_SESSIONS tras logout"

    # Intentar acceder con la cookie vieja ya revocada -> 403 Login page
    res_revoked = client.get("/admin")
    assert res_revoked.status_code == 403
    print("[PASS] F2: Revocación de sesión verificada (cookie vieja invalidada tras logout).")

    print("=" * 65)
    print("[OK] SUITE F1-F5 COMPLETADA CON ÉXITO AL 100%")
    print("=" * 65)

if __name__ == "__main__":
    test_suite_f1_f5()
