// ==========================================================================
// CAPONERA APP - AUTOMATED DISPATCH CONTROLLER & REAL-TIME ENGINE
// ==========================================================================

let mapInstance = null;
let driverMarkers = [];
let simulatedRouteLine = null;
let userMarker = null;
let currentSelectedFare = 20;
let currentSelectedDriver = "José Ramón (Unidad #7)";
let userOrigin = "Mi ubicación actual";
let userDestino = "Mercado Municipal";

// Default coordinates: Granada / Managua central fallback
let userCoords = [12.1364, -86.2514];

// Active Trip Tracking State
let activeTripId = null;
let tripPollInterval = null;

// Driver Mode State
let driverPollInterval = null;
let currentDriverId = 1; // José Ramón · Unidad #7

// Configuration State (CA-9)
let appConfig = {
  ciudad: 'Masaya',
  tarifa_min: 15,
  tarifa_max: 250,
  zonas: []
};

async function fetchAppConfig() {
  try {
    const res = await fetch('/api/config');
    if (res.ok) {
      appConfig = await res.json();
    }
  } catch (e) {
    console.log("Config fallback activa:", e);
  }
}

document.addEventListener('DOMContentLoaded', () => {
  fetchAppConfig();
  initPassengerMap();
  setupUIEventListeners();
  fetchRealDrivers();
  setInterval(fetchRealDrivers, 8000);
  requestRealLocation();
  startDriverModeListeners();
});

// =========================================================
// 1. SOUND ALERT (Web Audio API - Zero External Dependencies)
// =========================================================
function playTripAlertSound() {
  try {
    const AudioContext = window.AudioContext || window.webkitAudioContext;
    if (!AudioContext) return;
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();

    osc.type = 'sine';
    osc.frequency.setValueAtTime(587.33, ctx.currentTime); // D5
    osc.frequency.setValueAtTime(880.0, ctx.currentTime + 0.12); // A5
    osc.frequency.setValueAtTime(1174.66, ctx.currentTime + 0.24); // D6

    gain.gain.setValueAtTime(0.35, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.55);

    osc.connect(gain);
    gain.connect(ctx.destination);

    osc.start();
    osc.stop(ctx.currentTime + 0.55);
  } catch (e) {
    console.log('Audio error:', e);
  }
}

// =========================================================
// 2. LEAFLET INTERACTIVE MAP INITIALIZATION
// =========================================================
function initPassengerMap() {
  const mapElement = document.getElementById('interactivePassengerMap');
  if (!mapElement) return;

  mapInstance = L.map('interactivePassengerMap', {
    zoomControl: false,
    attributionControl: false
  }).setView(userCoords, 15);

  L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
    maxZoom: 19
  }).addTo(mapInstance);

  const userIcon = L.divIcon({
    className: 'custom-user-pin',
    html: `<div style="background:#10b981; width:22px; height:22px; border-radius:50%; border:3px solid #ffffff; box-shadow:0 0 18px #10b981;"></div>`,
    iconSize: [22, 22]
  });
  userMarker = L.marker(userCoords, { icon: userIcon }).addTo(mapInstance);
}

// Actualizar conductores reales en el mapa
async function fetchRealDrivers() {
  try {
    const res = await fetch('/api/conductores/activos');
    if (!res.ok) return;
    const drivers = await res.json();
    renderDriversOnMap(drivers);
  } catch (e) {
    // Si corre offline o en Surge demo, mantener marcadores existentes
  }
}

function renderDriversOnMap(drivers) {
  if (!mapInstance) return;

  // Limpiar marcadores antiguos
  driverMarkers.forEach(item => mapInstance.removeLayer(item.marker));
  driverMarkers = [];

  const listContainer = document.getElementById('driverOptionsList');
  if (listContainer && drivers.length > 0) {
    listContainer.innerHTML = drivers.map((d, idx) => {
      const driverName = d.nombre || d.name || 'Conductor';
      const driverUnit = d.unidad || d.unit || 'Caponera';
      return `
      <div class="modern-driver-card ${idx === 0 ? 'active' : ''}" data-fare="${20 + idx * 5}" data-driver="${driverName}">
        <div class="driver-avatar-wrap">
          <div class="driver-photo">🛺</div>
          <span class="status-dot online"></span>
        </div>
        <div class="driver-meta">
          <div class="driver-name-row">
            <h4 class="driver-name">${driverName}</h4>
            <span class="driver-stars">⭐ 4.9</span>
          </div>
          <p class="driver-sub-info">${driverUnit} · <span class="eta-text">En Línea</span></p>
        </div>
        <div class="driver-price-action">
          <span class="fare-amount">C$ ${20 + idx * 5}.00</span>
          <button class="btn-accept-chip" onclick="selectDriverOption(this, ${20 + idx * 5}, '${driverName}')">Elegir</button>
        </div>
      </div>
    `;}).join('');
  }

  drivers.forEach(driver => {
    const driverName = driver.nombre || driver.name || 'Conductor';
    const driverUnit = driver.unidad || driver.unit || 'Caponera';
    const caponeraIcon = L.divIcon({
      className: 'custom-caponera-marker',
      html: `<div style="background:rgba(245,158,11,0.95); width:36px; height:36px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:1.25rem; border:2px solid #ffffff; box-shadow:0 0 16px rgba(245,158,11,0.9); cursor:pointer;">🛺</div>`,
      iconSize: [36, 36]
    });

    const marker = L.marker([driver.lat, driver.lng], { icon: caponeraIcon }).addTo(mapInstance);
    marker.bindPopup(`<strong>${driverName}</strong><br>${driverUnit}<br>🟢 Conectado`);
    driverMarkers.push({ marker, data: driver });
  });
}

function requestRealLocation() {
  if ('geolocation' in navigator) {
    navigator.geolocation.getCurrentPosition(
      (position) => {
        const lat = position.coords.latitude;
        const lng = position.coords.longitude;
        userCoords = [lat, lng];

        if (userMarker) userMarker.setLatLng([lat, lng]);
        if (mapInstance) mapInstance.setView([lat, lng], 15);

        fetch(`https://nominatim.openstreetmap.org/reverse?lat=${lat}&lon=${lng}&format=json`)
          .then(res => res.json())
          .then(data => {
            const city = data.address?.city || data.address?.town || data.address?.village || data.address?.county || "Tu Ubicación";
            const cityLabel = document.getElementById('currentCityLabel');
            if (cityLabel) cityLabel.textContent = city;
          })
          .catch(() => {});
      },
      () => {},
      { timeout: 8000 }
    );
  }
}

// =========================================================
// 3. UI EVENT LISTENERS & MODALS
// =========================================================
function setupUIEventListeners() {
  document.getElementById('btnOpenSearch')?.addEventListener('click', () => {
    document.getElementById('modalSearchAddress')?.classList.add('active');
  });

  document.getElementById('btnCloseSearch')?.addEventListener('click', () => {
    document.getElementById('modalSearchAddress')?.classList.remove('active');
  });

  document.getElementById('btnAplicarRuta')?.addEventListener('click', () => {
    const destinoInput = document.getElementById('inputModalDestino')?.value;
    if (destinoInput) {
      userDestino = destinoInput;
      showToast(`📍 Destino fijado: ${userDestino}`);
    }
    document.getElementById('modalSearchAddress')?.classList.remove('active');
  });

  document.getElementById('tabViajes')?.addEventListener('click', () => {
    document.getElementById('tabViajes')?.classList.add('active');
    document.getElementById('tabEnvios')?.classList.remove('active');
    showToast("🛺 Modo Pasajeros activo");
  });

  document.getElementById('tabEnvios')?.addEventListener('click', () => {
    document.getElementById('tabEnvios')?.classList.add('active');
    document.getElementById('tabViajes')?.classList.remove('active');
    showToast("📦 Modo Envíos y Mandados Express activo");
  });

  document.getElementById('btnLocateMe')?.addEventListener('click', () => {
    requestRealLocation();
    showToast("📍 Actualizando tu ubicación en tiempo real...");
  });

  // BOTÓN PRINCIPAL DE PEDIR VIAJE (DESPACHO AUTOMÁTICO)
  document.getElementById('btnConfirmarPedido')?.addEventListener('click', () => {
    solicitarViajeAutomatico();
  });

  document.getElementById('btnWhatsappDirect')?.addEventListener('click', () => {
    solicitarViajeAutomatico();
  });
}

// =========================================================
// 4. MOTOR DE DESPACHO AUTOMÁTICO (LADO PASAJERO)
// =========================================================
async function solicitarViajeAutomatico() {
  if (currentSelectedFare < appConfig.tarifa_min || currentSelectedFare > appConfig.tarifa_max) {
    showToast(`⚠️ Tarifa inválida: debe estar entre C$ ${appConfig.tarifa_min} y C$ ${appConfig.tarifa_max}`);
    return;
  }

  const lbl = document.getElementById('lblConfirmarPedido');
  const btn = document.getElementById('btnConfirmarPedido');

  if (lbl) lbl.textContent = "📡 Buscando caponera cercana...";
  if (btn) btn.style.background = "#d97706";

  showToast("📡 Conectando con caponeras activas en tu zona...");

  try {
    const res = await fetch('/api/viajes/crear', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        origen: userOrigin,
        destino: userDestino,
        tarifa: currentSelectedFare,
        lat: userCoords[0],
        lng: userCoords[1]
      })
    });

    const data = await res.json();
    if (data.success) {
      activeTripId = data.viaje_id;
      if (data.session_token) {
        try {
          localStorage.setItem(`caponera_token_${activeTripId}`, data.session_token);
          localStorage.setItem('caponera_active_trip_token', data.session_token);
        } catch (err) {
          console.warn("No se pudo guardar session_token en localStorage:", err);
        }
      }
      iniciarMonitoreoViaje(activeTripId);
    } else {
      throw new Error(data.error || "Error al crear viaje");
    }
  } catch (e) {
    console.log("Error creando viaje:", e);
    showToast("⚠️ Conectando vía enlace alternativo...");
    // Fallback: Si no hay conexión al backend, abre WhatsApp con el número del despachador
    const msg = `🛺 *SOLICITUD DE CAPONERA* 🛺%0A*Origen:* ${userOrigin}%0A*Destino:* ${userDestino}%0A*Tarifa:* C$ ${currentSelectedFare}.00`;
    window.open(`https://wa.me/50589130414?text=${msg}`, '_blank');
    if (lbl) lbl.textContent = `⚡ Confirmar Viaje (C$ ${currentSelectedFare}.00)`;
    if (btn) btn.style.background = "";
  }
}

function iniciarMonitoreoViaje(viajeId) {
  if (tripPollInterval) clearInterval(tripPollInterval);

  const token = localStorage.getItem(`caponera_token_${viajeId}`) || localStorage.getItem('caponera_active_trip_token') || '';

  tripPollInterval = setInterval(async () => {
    try {
      const headers = {};
      if (token) headers['X-Session-Token'] = token;
      const url = token ? `/api/viajes/${viajeId}/estado?token=${encodeURIComponent(token)}` : `/api/viajes/${viajeId}/estado`;
      const res = await fetch(url, { headers });
      const data = await res.json();

      if (data.estado === 'aceptado' && data.conductor) {
        clearInterval(tripPollInterval);
        tripPollInterval = null;
        mostrarConfirmacionPasajero(data.conductor, data.tarifa);
      }
    } catch (e) {
      console.log("Error consultando estado:", e);
    }
  }, 1800);
}

window.cancelarViajeActivo = async function(viajeId) {
  const targetId = viajeId || activeTripId;
  if (!targetId) return;
  const token = localStorage.getItem(`caponera_token_${targetId}`) || localStorage.getItem('caponera_active_trip_token') || '';
  try {
    const res = await fetch(`/api/viajes/${targetId}/cancelar`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Session-Token': token
      },
      body: JSON.stringify({ viaje_id: targetId, session_token: token })
    });
    const resData = await res.json();
    if (resData.success) {
      if (tripPollInterval) {
        clearInterval(tripPollInterval);
        tripPollInterval = null;
      }
      try {
        localStorage.removeItem(`caponera_token_${targetId}`);
      } catch (err) {}
      showToast("Viaje cancelado exitosamente.");
    }
  } catch (e) {
    console.log("Error cancelando viaje:", e);
  }
};

function mostrarConfirmacionPasajero(conductor, tarifa) {
  playTripAlertSound();

  const container = document.getElementById('rideBottomSheet');
  if (container) {
    container.innerHTML = `
      <div class="sheet-drag-handle"></div>
      <div style="text-align:center; padding: 18px 12px;">
        <div style="font-size: 3.2rem; margin-bottom: 6px; animation: bounce 1s infinite;">🛺💨</div>
        <h3 style="color:#10b981; font-weight:900; font-size:1.3rem; margin-bottom:4px;">¡TU CAPONERA VA EN CAMINO!</h3>
        <p style="color:#cbd5e1; font-size:0.95rem; margin-bottom:14px;">El conductor aceptó tu carrera de inmediato</p>
        
        <div style="background:rgba(255,255,255,0.06); border-radius:14px; padding:14px; margin-bottom:14px; border:1px solid rgba(255,255,255,0.15); text-align:left;">
          <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
            <span style="color:#94a3b8;">Conductor:</span>
            <strong style="color:#fff; font-size:1.05rem;">${conductor.nombre}</strong>
          </div>
          <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
            <span style="color:#94a3b8;">Caponera:</span>
            <strong style="color:#f59e0b; font-size:1.05rem;">${conductor.unidad}</strong>
          </div>
          <div style="display:flex; justify-content:space-between;">
            <span style="color:#94a3b8;">Tarifa Acordada:</span>
            <strong style="color:#10b981; font-size:1.15rem;">C$ ${tarifa}.00</strong>
          </div>
        </div>

        <a href="https://wa.me/${conductor.telefono}?text=Hola%20${encodeURIComponent(conductor.nombre)},%20ped%C3%AD%20tu%20caponera%20en%20la%20app.%20Te%20espero%20en%20${encodeURIComponent(userOrigin)}" 
           target="_blank" 
           class="btn-neon-emerald" 
           style="display:flex; align-items:center; justify-content:center; gap:8px; width:100%; text-decoration:none; padding:14px; font-weight:800; border-radius:12px; font-size:1rem; margin-bottom:10px;">
          💬 Abrir WhatsApp con el Conductor
        </a>

        <a href="tel:${conductor.telefono}" 
           style="display:flex; align-items:center; justify-content:center; gap:8px; width:100%; text-decoration:none; padding:12px; font-weight:700; border-radius:12px; color:#fff; background:rgba(255,255,255,0.1); font-size:0.95rem;">
          📞 Llamar al Celular (${conductor.telefono})
        </a>
      </div>
    `;
  }
  showToast("🎉 ¡Conductor en camino a recogerte!");
}

// =========================================================
// 5. MOTOR DE MODO CONDUCTOR (RECEPCIÓN Y ACEPTACIÓN)
// =========================================================
function startDriverModeListeners() {
  const btnPower = document.getElementById('btnToggleDriverOnline');
  if (!btnPower) return;

  btnPower.addEventListener('click', function() {
    this.classList.toggle('online');
    const isOnline = this.classList.contains('online');
    const lbl = document.getElementById('lblDriverStatus');
    if (lbl) lbl.textContent = isOnline ? 'EN LÍNEA' : 'DESCONECTADO';

    showToast(isOnline ? '🟢 Conectado: Recibiendo viajes en tiempo real' : '🔴 Modo Desconectado');

    if (isOnline) {
      activarGPSConductor();
      pollViajesConductor();
      if (driverPollInterval) clearInterval(driverPollInterval);
      driverPollInterval = setInterval(pollViajesConductor, 3000);
    } else {
      if (driverPollInterval) clearInterval(driverPollInterval);
    }
  });

  // Iniciar automáticamente si ya está con clase online
  if (btnPower.classList.contains('online')) {
    pollViajesConductor();
    driverPollInterval = setInterval(pollViajesConductor, 3000);
  }
}

function activarGPSConductor() {
  if ('geolocation' in navigator) {
    navigator.geolocation.watchPosition(
      (pos) => {
        fetch('/api/conductor/ubicacion', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            conductor_id: currentDriverId,
            lat: pos.coords.latitude,
            lng: pos.coords.longitude,
            is_online: 1
          })
        }).catch(() => {});
      },
      () => {},
      { enableHighAccuracy: true, maximumAge: 5000 }
    );
  }
}

async function pollViajesConductor() {
  const isOnline = document.getElementById('btnToggleDriverOnline')?.classList.contains('online');
  if (!isOnline) return;

  try {
    const res = await fetch(`/api/conductor/viajes-pendientes?lat=${userCoords[0]}&lng=${userCoords[1]}&radio_km=4.0`);
    if (!res.ok) return;
    const carreras = await res.json();
    renderFeedConductores(carreras);
  } catch (e) {}
}

function renderFeedConductores(carreras) {
  const feed = document.getElementById('driverRequestsFeed');
  if (!feed) return;

  if (carreras.length === 0) {
    feed.innerHTML = `
      <div style="text-align:center; padding:30px 15px; color:#94a3b8;">
        <div style="font-size:2.2rem; margin-bottom:8px;">📡</div>
        <p style="font-weight:600;">Monitoreando carreras en tu radio de 3 km...</p>
        <small style="color:#64748b;">Cuando un pasajero pida viaje, sonará una alarma aquí.</small>
      </div>
    `;
    return;
  }

  // Hacer sonar pitido de alerta si hay carreras
  playTripAlertSound();

  feed.innerHTML = carreras.map(c => `
    <div class="driver-trip-card" style="background:#1e293b; border:2px solid #10b981; border-radius:14px; padding:15px; margin-bottom:12px; box-shadow:0 6px 20px rgba(16,185,129,0.25);">
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px;">
        <span style="background:#10b981; color:#000; font-weight:800; font-size:0.75rem; padding:3px 10px; border-radius:20px; text-transform:uppercase;">¡NUEVA SOLICITUD!</span>
        <strong style="color:#f59e0b; font-size:1.3rem;">C$ ${c.tarifa}.00</strong>
      </div>
      <p style="margin:6px 0; color:#fff; font-size:0.95rem;">📍 <strong>Origen:</strong> ${c.origen}</p>
      <p style="margin:6px 0; color:#cbd5e1; font-size:0.95rem;">🏁 <strong>Destino:</strong> ${c.destino}</p>
      <small style="color:#94a3b8; display:block; margin:6px 0 12px 0;">📏 Distancia: a ${c.distancia_km} km de ti</small>
      <button onclick="conductorTomarCarrera(${c.id}, ${c.tarifa})" class="btn-neon-emerald" style="width:100%; padding:14px; font-weight:900; font-size:1.05rem; border-radius:10px; cursor:pointer; letter-spacing:0.5px;">
        ⚡ ACEPTAR CARRERA AHORA
      </button>
    </div>
  `).join('');
}

window.conductorTomarCarrera = async function(viajeId, tarifa) {
  try {
    const res = await fetch(`/api/viajes/${viajeId}/aceptar`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ conductor_id: currentDriverId })
    });

    const data = await res.json();
    if (data.success) {
      showToast("🎉 ¡Carrera Ganada! Dirígete al pasajero.");
      playTripAlertSound();

      // Incrementar estadísticas en el HUD
      const countEl = document.getElementById('hudViajesHoy');
      const cashEl = document.getElementById('hudGanadoHoy');
      if (countEl) countEl.textContent = parseInt(countEl.textContent || 0) + 1;
      if (cashEl) {
        const actual = parseFloat((cashEl.textContent || "0").replace(/[^0-9.]/g, '')) || 0;
        cashEl.textContent = `C$ ${(actual + tarifa).toFixed(2)}`;
      }

      pollViajesConductor();
    } else {
      showToast(`⚠️ ${data.error || 'Esta carrera ya fue tomada'}`);
      pollViajesConductor();
    }
  } catch (e) {
    showToast("❌ Error al aceptar carrera");
  }
};

// =========================================================
// 6. NAVEGACIÓN Y TOASTS
// =========================================================
window.selectDriverOption = function(btnElement, fare, driverName) {
  currentSelectedFare = fare;
  currentSelectedDriver = driverName;

  document.querySelectorAll('.modern-driver-card').forEach(el => el.classList.remove('active'));
  btnElement.closest('.modern-driver-card')?.classList.add('active');

  const lbl = document.getElementById('lblConfirmarPedido');
  if (lbl) lbl.textContent = `⚡ Confirmar Viaje (C$ ${fare}.00)`;
  showToast(`🛺 Seleccionado: ${driverName} (C$ ${fare}.00)`);
};

window.adjustOffer = function(amount) {
  currentSelectedFare = amount;
  const lbl = document.getElementById('lblConfirmarPedido');
  if (lbl) lbl.textContent = `⚡ Confirmar Viaje (C$ ${amount}.00)`;
  showToast(`Tarifa ajustada a C$ ${amount}.00`);
};

window.setFastDestino = function(lugar) {
  const input = document.getElementById('inputModalDestino');
  if (input) input.value = lugar;
  userDestino = lugar;
  document.getElementById('modalSearchAddress')?.classList.remove('active');
  showToast(`🏁 Destino seleccionado: ${lugar}`);
};

window.switchNav = function(tabKey) {
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.screen-view').forEach(el => el.classList.remove('active'));

  if (tabKey === 'home') {
    document.getElementById('navHome')?.classList.add('active');
    document.getElementById('screenMainPassenger')?.classList.add('active');
    setTimeout(() => mapInstance?.invalidateSize(), 200);
  } else if (tabKey === 'driver') {
    document.getElementById('navProfile')?.classList.add('active');
    document.getElementById('screenDriverMode')?.classList.add('active');
    pollViajesConductor();
  } else {
    showToast(`📂 Sección ${tabKey} cargada`);
    document.getElementById('navHome')?.classList.add('active');
    document.getElementById('screenMainPassenger')?.classList.add('active');
  }
};

function showToast(msg) {
  const container = document.getElementById('toastContainer');
  if (!container) return;

  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = msg;
  container.appendChild(toast);

  setTimeout(() => {
    toast.remove();
  }, 2600);
}

// =========================================================
// 7. BANPRO RECHARGE & PAYMENT AUTOMATION (NICARAGUA)
// =========================================================
let selectedBanproPlanPrice = 50;
let selectedBanproPlanName = "Semanal (7 Días)";
const BANPRO_WHATSAPP_PHONE = "50589130414";

function openBanproModal() {
  const modal = document.getElementById('modalBanproRecharge');
  if (modal) modal.classList.add('active');
}

function closeBanproModal() {
  const modal = document.getElementById('modalBanproRecharge');
  if (modal) modal.classList.remove('active');
}

function selectBanproPlan(monto, nombre, el) {
  selectedBanproPlanPrice = monto;
  selectedBanproPlanName = nombre;

  document.querySelectorAll('.banpro-plan-card').forEach(c => {
    c.classList.remove('active');
    const radio = c.querySelector('.plan-radio');
    if (radio) radio.textContent = '';
  });

  if (el) {
    el.classList.add('active');
    const radio = el.querySelector('.plan-radio');
    if (radio) radio.textContent = '✓ Seleccionado';
  }
}

function copyBanproData(text, message) {
  if (navigator.clipboard) {
    navigator.clipboard.writeText(text).then(() => {
      showToast('📋 ' + message);
    }).catch(() => {
      prompt('Copia este dato:', text);
    });
  } else {
    prompt('Copia este dato:', text);
  }
}

async function enviarComprobanteBanpro() {
  const ref = document.getElementById('inputBanproReferencia')?.value.trim() || 'Comprobante adjunto';

  // Registrar en backend
  try {
    await fetch('/api/conductor/recarga', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conductor_id: currentDriverId,
        plan_nombre: selectedBanproPlanName,
        monto: selectedBanproPlanPrice,
        referencia: ref
      })
    });
  } catch (e) {}

  const msg = 
`🛺 *SOLICITUD DE RECARGA - CAPONERA APP* 🛺

*Titular Cuenta:* Luis Mongrio
*Banco:* BANPRO Grupo Promerica 🇳🇮
*Plan:* ${selectedBanproPlanName} (C$ ${selectedBanproPlanPrice}.00)
*Nº Referencia / Minuta:* ${ref}

_Hola Luis, he realizado mi pago por Banpro para activar mi plan de conductor en Caponera App._`;

  const url = `https://wa.me/${BANPRO_WHATSAPP_PHONE}?text=${encodeURIComponent(msg)}`;
  window.open(url, '_blank');
  closeBanproModal();
  showToast('📲 Abriendo WhatsApp para enviar comprobante...');
}

// PWA Service Worker
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('./sw.js')
      .catch(() => {});
  });
}
