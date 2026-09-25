FROM node:20-alpine

WORKDIR /app/frontend

COPY package.json package-lock.json /app/frontend/
RUN npm ci

COPY . /app/frontend

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
