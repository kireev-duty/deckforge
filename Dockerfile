# deckforge — сервис + LibreOffice для PDF/PNG/VLM-судьи. Сборка: docker compose build
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    SOFFICE_PATH=/usr/bin/soffice DECKFORGE_IN_DOCKER=1

# LibreOffice Impress без Java и рекомендаций (≈400 МБ); шрифты Liberation/DejaVu — подмена Play/Montserrat
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-impress libreoffice-calc fonts-liberation fonts-dejavu-core fontconfig curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY deckforge ./deckforge
RUN pip install -e .

# код, промпты, стратегии, конфиги; данные (шаблоны LFS), примеры и out — через volumes в compose.yaml
COPY skills ./skills
COPY strategies ./strategies
COPY configs ./configs
COPY tools ./tools
COPY examples/content_pack ./examples/content_pack

EXPOSE 8000 8501
ENTRYPOINT ["python", "-m", "deckforge.cli"]
CMD ["--help"]
