@echo on
REM Navigate to project folder
cd /d "D:/TradingViewAlgo"

REM Build and run Docker containers
docker-compose up -d --build

REM Give Docker a few seconds to start ngrok
timeout /t 5 /nobreak >nul

REM Optional: open FastAPI root in browser
start https://nongranular-uncavernous-hannelore.ngrok-free.dev/health

pause
