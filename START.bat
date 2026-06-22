@echo off
echo ============================================
echo  CourseMap - Massey University Planner
echo ============================================
echo.
echo Starting server on http://localhost:8000
echo Press Ctrl+C in this window to stop.
echo.
echo If you see any errors, make sure you ran:
echo   pip install -r requirements.txt
echo.
python -m uvicorn coursemap.api.server:app --reload --port 8000
pause
