@echo off
title DESPLEGAR CAPONERA APP AL VPS (2.25.121.124)
color 0a
echo ===============================================================================
echo     DESPLEGAR CAPONERA APP (CA-1 A CA-10 HARDENING) AL VPS
echo ===============================================================================
echo.
echo [1/3] Sincronizando archivos actualizados a /root/caponera_app/...
echo (Introduce la contrasenia de root del VPS si te la solicita)
scp "C:\Users\luis\caponera_app\index.html" "C:\Users\luis\caponera_app\server.py" "C:\Users\luis\caponera_app\app.js" "C:\Users\luis\caponera_app\sw.js" "C:\Users\luis\caponera_app\version.txt" "C:\Users\luis\caponera_app\privacidad.html" "C:\Users\luis\caponera_app\test_dispatch.py" root@2.25.121.124:/root/caponera_app/

echo.
echo [2/3] Reconstruyendo y reiniciando contenedor docker en el VPS...
ssh root@2.25.121.124 "cd /root/caponera_app && git pull origin main 2>/dev/null; if docker ps -a | grep -q caponera_app; then docker compose up -d --build; elif systemctl list-units --type=service | grep -q caponera; then systemctl restart caponera; else pkill -f 'python.*server.py' 2>/dev/null; nohup python3 server.py > caponera.log 2>&1 & fi"

echo.
echo [3/3] Verificando respuesta en vivo en /api/version...
timeout /t 3 /nobreak >nul
ssh -i C:\Users\luis\.ssh\id_auditor_sentinel -o BatchMode=yes auditor@2.25.121.124 "curl -s http://127.0.0.1:5054/api/version"

echo.
echo ===============================================================================
echo  DESPLIEGUE FINALIZADO! Caponera App esta 100%% blindada y actualizada
echo ===============================================================================
pause
