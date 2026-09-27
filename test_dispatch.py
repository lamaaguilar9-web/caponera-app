import server
import json

def test_full_dispatch_flow():
    app = server.app.test_client()
    
    print("\n--- 1. CONSULTAR CONDUCTORES ACTIVOS ---")
    res = app.get("/api/conductores/activos")
    assert res.status_code == 200
    drivers = res.get_json()
    print(f"Conductores en l\u00ednea: {len(drivers)}")
    assert len(drivers) >= 1
    
    print("\n--- 2. PASAJERO PIDE VIAJE ---")
    payload_viaje = {
        "origen": "Parque Central de Granada",
        "destino": "Mercado Municipal",
        "tarifa": 25.0,
        "lat": 12.1364,
        "lng": -86.2514
    }
    res_viaje = app.post("/api/viajes/crear", json=payload_viaje)
    assert res_viaje.status_code == 200
    viaje_data = res_viaje.get_json()
    viaje_id = viaje_data["viaje_id"]
    print(f"Viaje creado con ID #{viaje_id}. Estado inicial: {viaje_data['estado']}")
    assert viaje_data["estado"] == "buscando"
    
    print("\n--- 3. CONDUCTOR EN L\u00cdNEA RECIBE ALERTA DE VIAJE ---")
    res_pendientes = app.get(f"/api/conductor/viajes-pendientes?lat=12.1370&lng=-86.2520&radio_km=3.0")
    assert res_pendientes.status_code == 200
    pendientes = res_pendientes.get_json()
    print(f"Viajes pendientes detectados por el chofer: {len(pendientes)}")
    encontrado = any(p["id"] == viaje_id for p in pendientes)
    assert encontrado, "El viaje creado debe aparecer en la lista de pendientes del chofer"
    
    print("\n--- 4. CONDUCTOR ACEPTA LA CARRERA ---")
    res_aceptar = app.post(f"/api/viajes/{viaje_id}/aceptar", json={"conductor_id": 1})
    assert res_aceptar.status_code == 200
    res_aceptar_json = res_aceptar.get_json()
    assert res_aceptar_json["success"] is True
    print(f"Resultado de aceptaci\u00f3n: {res_aceptar_json['mensaje']}")
    
    print("\n--- 5. PANTALLA DEL PASAJERO DETECTA ASIGNACI\u00d3N EN VIVO ---")
    res_estado = app.get(f"/api/viajes/{viaje_id}/estado")
    assert res_estado.status_code == 200
    estado_data = res_estado.get_json()
    print(f"Estado del viaje: {estado_data['estado']}")
    print(f"Conductor asignado: {estado_data['conductor']['nombre']} ({estado_data['conductor']['unidad']})")
    print(f"Tel\u00e9fono de contacto: {estado_data['conductor']['telefono']}")
    assert estado_data["estado"] == "aceptado"
    assert estado_data["conductor"]["id"] == 1
    
    print("\n--- 6. VERIFICAR QUE OTRO CONDUCTOR YA NO PUEDA TOMAR EL MISMO VIAJE ---")
    res_duplicado = app.post(f"/api/viajes/{viaje_id}/aceptar", json={"conductor_id": 2})
    assert res_duplicado.status_code == 409
    print("Correcto: El sistema bloque\u00f3 que dos choferes tomen la misma carrera.")
    
    print("\n========================================================")
    print("[OK] \u00a1TODAS LAS PRUEBAS DE DESPACHO AUTOM\u00c1TICO PASARON EXITOSAMENTE!")
    print("========================================================")

if __name__ == "__main__":
    test_full_dispatch_flow()
