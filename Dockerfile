FROM python:3.13-slim
RUN apt-get update && apt-get install -y ffmpeg curl ca-certificates && pip install --no-cache-dir openai && curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp && chmod a+rx /usr/local/bin/yt-dlp && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN mkdir -p data/jobs data/outputs data/uploads
EXPOSE 3000
CMD ["python3","server.py"]
