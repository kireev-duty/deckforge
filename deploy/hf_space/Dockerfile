# deckforge — публичный демо-стенд на Hugging Face Space (Docker SDK): Streamlit UI на :7860.
# Собирается из staging-папки, которую готовит .github/workflows/deploy-space.yml: в корне Space лежат
# deckforge/, skills/, strategies/, configs/, tools/, data/, examples/ и этот Dockerfile. Volumes в Space нет,
# поэтому шаблоны датасета, разметка их образцов и outline примеров копируются в образ.
# Ключ и модели — Secrets / Variables Space (LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, VLM_MODEL, T2I_MODEL).
# Локальная проверка: python deploy/hf_space/stage.py → docker build out/hf_space (DEVELOPMENT.md, «Демо-стенд»).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    SOFFICE_PATH=/usr/bin/soffice DECKFORGE_IN_DOCKER=1 DECKFORGE_PUBLIC=1

# LibreOffice Impress без Java; Montserrat (ЛЦТ2026) — из Debian, Play (шаблоны VK) — из google/fonts (OFL):
# без них LibreOffice подставляет DejaVu, и PNG-превью / PDF / картинки для судьи расходятся с PowerPoint
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-impress libreoffice-calc fonts-liberation fonts-dejavu-core fonts-montserrat fontconfig curl \
    && mkdir -p /usr/share/fonts/truetype/play \
    && for f in Play-Regular Play-Bold; do \
        curl -fsSL -o /usr/share/fonts/truetype/play/$f.ttf "https://github.com/google/fonts/raw/main/ofl/play/$f.ttf"; \
    done \
    && fc-cache -f \
    && rm -rf /var/lib/apt/lists/*

# HF запускает контейнер от uid 1000
RUN useradd -m -u 1000 user
WORKDIR /app
COPY pyproject.toml README.md ./
COPY deckforge ./deckforge
RUN pip install -e .

COPY skills ./skills
COPY strategies ./strategies
COPY configs ./configs
COPY tools ./tools
COPY data ./data
COPY examples ./examples
RUN mkdir -p /app/out && chown -R user:user /app/out

USER user
ENV HOME=/home/user
EXPOSE 7860
# XSRF и CORS выключены: UI открывается в iframe huggingface.co, и file_uploader иначе отвечает 403
CMD ["python", "-m", "streamlit", "run", "deckforge/ui/app.py", \
     "--server.port", "7860", "--server.address", "0.0.0.0", "--server.headless", "true", \
     "--server.enableXsrfProtection", "false", "--server.enableCORS", "false", \
     "--server.maxUploadSize", "60", "--browser.gatherUsageStats", "false"]
