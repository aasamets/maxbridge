FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global url."https://github.com/".insteadOf ssh://git@github.com/

WORKDIR /app

COPY package.json .
RUN npm install --omit=dev

# fetchProps IQ больше не получает ответ от WA-серверов (server-side change).
# Патчим Baileys: делаем fetchProps непрерывным, чтобы таймаут не ломал
# весь executeInitQueries и WA продолжал принимать сообщения.
RUN sed -i \
    's/await Promise\.all(\[fetchProps(), fetchBlocklist(), fetchPrivacySettings()\])/await Promise.all([fetchProps().catch(e => logger.warn({ err: e }, "fetchProps skipped")), fetchBlocklist(), fetchPrivacySettings()])/' \
    /app/node_modules/@whiskeysockets/baileys/lib/Socket/chats.js

COPY . .

RUN mkdir -p /sessions/wa

ENV NODE_OPTIONS=--max-old-space-size=384

CMD ["node", "index.js"]
