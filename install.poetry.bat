@echo off
pip install poetry
poetry config virtualenvs.in-project true
poetry install
poetry run pip install httpx
pause