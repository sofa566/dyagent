#!/bin/bash
docker compose -f docker-compose.yaml up -d

cd /hd18/23_Rasa/dyagent/backend
.venv/bin/python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
