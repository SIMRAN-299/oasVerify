FROM node:22-slim AS builder
WORKDIR /frontend
COPY package*.json ./
RUN npm install --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=builder /frontend/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
ENV PORT=8080
ENV BACKEND_URL="http://localhost:8081"
CMD ["nginx", "-g", "daemon off;"]
