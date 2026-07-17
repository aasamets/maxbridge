FROM node:20-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git config --global url."https://github.com/".insteadOf ssh://git@github.com/

WORKDIR /app

COPY package.json .
RUN npm install --omit=dev

COPY . .

RUN mkdir -p /sessions/wa

ENV NODE_OPTIONS=--max-old-space-size=384

CMD ["node", "index.js"]
