import os
import re
import uuid
import threading
import subprocess
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory


# ============================================================
# CONFIGURAÇÃO
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "outputs"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

PORT = int(os.environ.get("PORT", "3000"))

app = Flask(__name__, static_folder="static")

# Jobs ficam em memória nesta versão.
jobs = {}


# ============================================================
# UTILIDADES
# ============================================================

def run_command(command):
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(result.stdout[-5000:])

    return result.stdout


def get_video_duration(video_path):
    output = run_command([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(video_path)
    ])

    return float(output.strip())


# ============================================================
# ENCONTRAR MOMENTOS
# ============================================================

def find_best_moments(video_path, duration, number_of_clips, clip_duration):

    """
    Sistema gratuito para encontrar momentos automaticamente.

    Ele analisa o volume do áudio em pequenas janelas.
    Momentos com maior atividade sonora recebem prioridade.

    Não usa OpenAI nem outra API paga.
    """

    window = 8

    samples = []

    total_windows = max(1, int(duration / window))

    for index in range(total_windows):

        start = index * window

        if start >= duration:
            break

        current_duration = min(window, duration - start)

        try:

            output = run_command([
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-ss",
                str(start),
                "-t",
                str(current_duration),
                "-i",
                str(video_path),
                "-af",
                "volumedetect",
                "-f",
                "null",
                "-"
            ])

            match = re.search(
                r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB",
                output
            )

            if match:
                volume = float(match.group(1))
            else:
                volume = -100

            samples.append({
                "start": start,
                "volume": volume
            })

        except Exception:
            continue

    # Mais alto = mais atividade sonora
    samples.sort(
        key=lambda item: item["volume"],
        reverse=True
    )

    selected = []

    for sample in samples:

        start = sample["start"]

        # Mantém o corte dentro do vídeo
        start = max(
            0,
            min(start, max(0, duration - clip_duration))
        )

        # Evita cortes muito próximos
        too_close = False

        for existing in selected:

            if abs(existing - start) < clip_duration * 0.7:
                too_close = True
                break

        if too_close:
            continue

        selected.append(start)

        if len(selected) >= number_of_clips:
            break

    # Caso o vídeo não tenha áudio analisável
    if not selected:

        selected = []

        for i in range(number_of_clips):

            start = i * clip_duration

            if start >= duration:
                break

            selected.append(
                min(start, max(0, duration - clip_duration))
            )

    return selected


# ============================================================
# GERAR CORTES
# ============================================================

def create_clips(job_id, video_path):

    try:

        jobs[job_id]["status"] = "processing"
        jobs[job_id]["progress"] = 5
        jobs[job_id]["message"] = "Lendo vídeo..."

        duration = get_video_duration(video_path)

        if duration < 3:
            raise RuntimeError(
                "O vídeo é muito curto."
            )

        # Configuração simples para uso pessoal
        number_of_clips = 5

        requested_duration = 45

        clip_duration = min(
            requested_duration,
            duration
        )

        jobs[job_id]["progress"] = 10
        jobs[job_id]["message"] = (
            "Procurando os melhores momentos..."
        )

        moments = find_best_moments(
            video_path,
            duration,
            number_of_clips,
            clip_duration
        )

        clips = []

        total = len(moments)

        for index, start in enumerate(moments):

            clip_number = index + 1

            output_name = (
                f"{job_id}_corte_{clip_number:02d}.mp4"
            )

            output_path = OUTPUT_DIR / output_name

            jobs[job_id]["message"] = (
                f"Gerando corte {clip_number}/{total}..."
            )

            jobs[job_id]["progress"] = (
                20 + int((index / max(1, total)) * 70)
            )

            # Formato vertical 9:16
            video_filter = (
                "scale=720:-2,"
                "crop=ih*9/16:ih"
            )

            run_command([
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",

                "-ss",
                str(start),

                "-i",
                str(video_path),

                "-t",
                str(clip_duration),

                "-vf",
                video_filter,

                "-c:v",
                "libx264",

                "-preset",
                "veryfast",

                "-crf",
                "23",

                "-c:a",
                "aac",

                "-b:a",
                "128k",

                "-movflags",
                "+faststart",

                str(output_path)
            ])

            clips.append({
                "name": output_name,
                "url": "/outputs/" + output_name
            })

        jobs[job_id]["status"] = "completed"
        jobs[job_id]["progress"] = 100
        jobs[job_id]["message"] = (
            f"Pronto! {len(clips)} cortes foram gerados."
        )
        jobs[job_id]["clips"] = clips

    except Exception as error:

        jobs[job_id]["status"] = "error"
        jobs[job_id]["progress"] = 0
        jobs[job_id]["message"] = "Erro no processamento."
        jobs[job_id]["error"] = str(error)


# ============================================================
# PÁGINA PRINCIPAL
# ============================================================

@app.route("/")
def index():

    return send_from_directory(
        "static",
        "index.html"
    )


# ============================================================
# DOWNLOAD DOS CORTES
# ============================================================

@app.route("/outputs/<path:filename>")
def download_output(filename):

    return send_from_directory(
        OUTPUT_DIR,
        filename,
        as_attachment=True
    )


# ============================================================
# CRIAR JOB
# ============================================================

@app.route("/api/jobs", methods=["POST"])
def create_job():

    job_id = uuid.uuid4().hex

    uploaded_file = request.files.get("video")

    youtube_url = request.form.get(
        "youtube_url",
        ""
    ).strip()

    try:

        # ----------------------------------------------------
        # UPLOAD NORMAL
        # ----------------------------------------------------

        if uploaded_file:

            extension = (
                Path(uploaded_file.filename or "video.mp4")
                .suffix
                .lower()
            )

            if not extension:
                extension = ".mp4"

            video_path = (
                UPLOAD_DIR /
                f"{job_id}{extension}"
            )

            uploaded_file.save(video_path)

        # ----------------------------------------------------
        # YOUTUBE
        # ----------------------------------------------------

        elif youtube_url:

            video_path = (
                UPLOAD_DIR /
                f"{job_id}.mp4"
            )

            try:

                run_command([
                    "yt-dlp",

                    "--no-playlist",

                    "-f",
                    "bv*+ba/b",

                    "--merge-output-format",
                    "mp4",

                    "-o",
                    str(video_path),

                    youtube_url
                ])

            except Exception as error:

                raise RuntimeError(
                    "O YouTube recusou o acesso a este vídeo. "
                    "Tente um vídeo que você tenha autorização "
                    "para usar ou envie o MP4 diretamente."
                ) from error

        else:

            return jsonify({
                "error":
                    "Envie um vídeo ou coloque uma URL do YouTube."
            }), 400

        # ----------------------------------------------------
        # CRIA JOB
        # ----------------------------------------------------

        jobs[job_id] = {
            "status": "queued",
            "progress": 1,
            "message": "Vídeo recebido. Iniciando...",
            "clips": []
        }

        thread = threading.Thread(
            target=create_clips,
            args=(job_id, video_path),
            daemon=True
        )

        thread.start()

        return jsonify({
            "id": job_id
        })

    except Exception as error:

        return jsonify({
            "error": str(error)
        }), 400


# ============================================================
# CONSULTAR JOB
# ============================================================

@app.route("/api/jobs/<job_id>")
def get_job(job_id):

    if job_id not in jobs:

        return jsonify({
            "error": "Processamento não encontrado."
        }), 404

    return jsonify(
        jobs[job_id]
    )


# ============================================================
# API CONFIG
# ============================================================

@app.route("/api/config")
def config():

    return jsonify({
        "personal_mode": True,
        "openai_required": False,
        "kiwify_required": False
    })


# ============================================================
# INICIAR
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=PORT
)
