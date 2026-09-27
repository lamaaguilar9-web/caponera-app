@echo off
title DESPLEGAR CAPONERA APP AL VPS (2.25.121.124)
color 0a
echo ===============================================================================
echo     DESPLEGAR CAPONERA APP (NUEVA INTERFAZ + PUBLICIDAD + MOTOR SSE) AL VPS
echo ===============================================================================
echo.
echo [1/3] Subiendo index.html, privacidad.html y server.py actualizados a /root/caponera_app/...
echo (Introduce la contrasenia de root del VPS si te la solicita)
scp "C:\Users\luis\caponera_app\index.html" "C:\Users\luis\caponera_app\privacidad.html" "C:\Users\luis\caponera_app\server.py" root@2.25.121.124:/root/caponera_app/

echo.
echo [2/3] Reiniciando el motor de Caponera en el VPS...
ssh root@2.25.121.124 "if docker ps -a | grep -q caponera_app; then cd /root/caponera_app && docker compose up -d --build; elif systemctl list-units --type=service | grep -q caponera; then systemctl restart caponera; else pkill -f 'python.*server.py' 2>/dev/null; cd /root/caponera_app && nohup python3 server.py > caponera.log 2>&1 & fi"

echo.
echo [3/3] Verificando respuesta en vivo en puerto 5054...
timeout /t 3 /nobreak >nul
curl -s -I http://2.25.121.124:5054 | head -n 5

echo.
echo ===============================================================================
echo  DESPLIEGUE FINALIZADO! Caponera App esta 100%% actualizada en http://2.25.121.124:5054
echo ===============================================================================
pause
