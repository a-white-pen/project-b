FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY . .

ENV PORT=8080
EXPOSE 8080

CMD ["uvicorn", "telegram.webhook:app", "--host", "0.0.0.0", "--port", "8080"]
