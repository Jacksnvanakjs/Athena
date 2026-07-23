FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/data

ENV PORT=8000
ENV ENV=production
ENV ENABLE_SCHEDULER=true
ENV TIMEZONE=Asia/Shanghai
ENV DATABASE_URL=sqlite:////app/data/funds.db

EXPOSE 8000

CMD ["python", "run.py"]
