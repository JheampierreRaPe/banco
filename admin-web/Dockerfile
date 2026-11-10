# admin-web: imagen real del panel (E3-T10; reemplaza el placeholder Q-T06).
# Build del panel Vite + React + TS y servicio estático con nginx en :80
# (compose publica `${ADMIN_WEB_PORT:-8080}:80`). Contexto = raíz del repo.
FROM node:22-alpine AS build
WORKDIR /app
COPY admin-web/package.json admin-web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY admin-web/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY admin-web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
