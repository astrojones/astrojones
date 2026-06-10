# Replace this with a Dockerfile that actually builds your app.
# It must listen on the port declared in .nuklaut/deployment.yml (default 8080)
# and that port must be `expose:`d in docker-compose.yml.
FROM node:22-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
EXPOSE 8080
CMD ["node", "server.js"]
