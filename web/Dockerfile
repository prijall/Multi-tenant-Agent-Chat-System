FROM node:22-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
COPY web/package.json ./web/
RUN npm ci
COPY web ./web
# The browser's API address is compiled in. Auth values here only satisfy the build: the real ones
# come from the environment when the container runs.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEON_AUTH_BASE_URL=https://build.invalid/auth \
    NEON_AUTH_COOKIE_SECRET=build-only-placeholder-at-least-32-characters
RUN npm run build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production HOSTNAME=0.0.0.0 PORT=3000
# The lockfile is at the repository root, so Next nests the server under web/.
COPY --from=build /app/web/.next/standalone ./
COPY --from=build /app/web/.next/static ./web/.next/static
USER node
EXPOSE 3000
CMD ["node", "web/server.js"]
