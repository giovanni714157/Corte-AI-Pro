# Corte AI Pro V3

Editor web de cortes automáticos. Aceita upload ou URL do YouTube, cria candidatos por análise real de áudio, usa transcrição/IA quando `OPENAI_API_KEY` estiver configurada, gera cortes em lote e pode queimar legendas.

## Rodar

### Docker (recomendado)
1. copie `.env.example` para `.env`
2. coloque sua chave `OPENAI_API_KEY` para transcrição + ranking semântico
3. `docker compose up --build`
4. abra `http://localhost:3000`

### Sem Docker
Requer Python 3.12, FFmpeg/ffprobe e `yt-dlp` no PATH.
`pip install -r requirements.txt`
`python server.py`

## Fluxo
URL/upload -> download -> análise -> transcrição -> seleção -> render -> downloads.

Use somente vídeos que você tenha direito/autorização para baixar e editar.
