.PHONY: install pipeline api app mlflow test docker reset-store
install:  ; pip install -r requirements.txt
pipeline: ; python -m src.pipeline
api:      ; uvicorn api.main:app --reload --port 8000
app:      ; streamlit run app/main.py
mlflow:   ; mlflow ui --backend-store-uri sqlite:///mlflow.db --port 5000
test:     ; pytest -q
docker:   ; docker compose up --build
reset-store: ; python -c "from app import db; db.init_db(force=True)"
