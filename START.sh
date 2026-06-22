#!/bin/bash
echo "============================================"
echo " CourseMap - Massey University Planner"
echo "============================================"
echo ""
echo "Starting server on http://localhost:8000"
echo "Press Ctrl+C to stop."
echo ""
python3 -m uvicorn coursemap.api.server:app --reload --port 8000
