FROM node:20-alpine AS deps
WORKDIR /app
COPY apps/frontend/package.json apps/frontend/package-lock.json* ./
RUN npm ci

FROM node:20-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY apps/frontend/ ./

# Allow callers to inject the API URL at build time. Empty = same-origin.
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

# SQL protocol selection is compiled into the browser bundle.
ARG NEXT_PUBLIC_ANALYSIS_BACKEND=legacy
ENV NEXT_PUBLIC_ANALYSIS_BACKEND=$NEXT_PUBLIC_ANALYSIS_BACKEND

# Backend URL used by Next.js rewrites (/api/* -> backend). Must match the
# compose `environment` value so build-time and runtime rewrites agree.
ARG BACKEND_INTERNAL_URL
ENV BACKEND_INTERNAL_URL=$BACKEND_INTERNAL_URL

RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV PORT=3000
ENV NEXT_TELEMETRY_DISABLED=1
ENV XDG_CONFIG_HOME=/tmp XDG_CACHE_HOME=/tmp
RUN apk add --no-cache chromium font-noto-cjk

RUN addgroup --system --gid 1001 nodejs && \
    adduser --system --uid 1001 nextjs

# Copy only what's needed at runtime
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
COPY --from=builder --chown=nextjs:nodejs /app/.next ./.next
COPY --from=builder --chown=nextjs:nodejs /app/node_modules ./node_modules
COPY --from=builder --chown=nextjs:nodejs /app/package.json ./package.json
COPY --from=builder --chown=nextjs:nodejs /app/next.config.ts ./next.config.ts

USER nextjs

EXPOSE 3000

CMD ["npm", "start"]
