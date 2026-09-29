"""Run the full pipeline end-to-end without DVC:  python -m src.pipeline"""
from src.data import generate, prepare
from src.features import build
from src.models import train
from src.monitoring import drift

if __name__ == "__main__":
    for step in (generate, prepare, build, train, drift):
        print(f"\n=== {step.__name__} ===")
        step.main()
