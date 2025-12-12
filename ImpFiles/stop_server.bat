@echo off
echo ==========================================
echo   DOCKER CLEANUP - WINDOWS SAFE VERSION
echo ==========================================
echo.

echo Stopping all running containers...
for /f "tokens=*" %%i in ('docker ps -q') do (
    echo Stopping container %%i
    docker stop %%i
)

echo Removing all containers...
for /f "tokens=*" %%i in ('docker ps -a -q') do (
    echo Removing container %%i
    docker rm -f %%i
)

echo Removing all Docker images...
for /f "tokens=*" %%i in ('docker images -q') do (
    echo Removing image %%i
    docker rmi -f %%i
)

echo Pruning remaining resources...
docker system prune -a -f --volumes

echo.
echo ==========================================
echo   CLEANUP COMPLETED
echo ==========================================
pause
