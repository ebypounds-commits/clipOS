FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json ./

# `npm ci` is the preferred reproducible install. The checked-in lockfile still
# needs a one-time regeneration for the Next.js runtime dependencies added in
# CLIP-003, so install (which syncs the lockfile) is used until node is
# available in a network-enabled environment. See docs/development.md.
RUN npm install --no-audit --no-fund --loglevel=error

COPY . .

RUN npm run build

EXPOSE 3000

CMD ["npm", "run", "start"]
