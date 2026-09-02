# CORTE AI PRO — versão final para publicação

SaaS de cortes automáticos. O projeto inclui interface, backend de processamento, upload/YouTube, FFmpeg, transcrição opcional por IA, geração de cortes, legendas, painel de assinatura e endpoint de webhook para a Kiwify.

## Antes de publicar
Você ainda precisa configurar suas próprias credenciais/contas:
- domínio/servidor
- KIWIFY_CHECKOUT_URL do seu plano de R$120/mês
- webhook da Kiwify apontando para `/api/kiwify/webhook`
- OPENAI_API_KEY para análise/transcrição por IA

Não coloque chaves secretas no navegador.

## Kiwify
Crie um produto como "Corte AI Pro" em Assinatura recorrente, frequência mensal e preço R$120. A Kiwify fornece o checkout e webhooks para comunicar compras/renovações/cancelamentos ao seu aplicativo.

## Rodar
Docker:
  docker compose up --build

Local:
  Python 3 + FFmpeg + yt-dlp
  python3 server.py

Abra http://localhost:3000
