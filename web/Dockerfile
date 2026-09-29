FROM node:22-alpine AS auth-assets
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --omit=dev

FROM nginx:alpine

# Copy static files
COPY static/ /usr/share/nginx/html/
COPY --from=auth-assets /build/node_modules/@azure/msal-browser/lib/msal-browser.min.js /usr/share/nginx/html/vendor/msal-browser.min.js

# Copy nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]
