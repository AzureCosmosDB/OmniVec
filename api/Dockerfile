FROM python:3.11-slim AS mcp-package
WORKDIR /mcp
COPY mcp_servers/cosmos/requirements.txt .
RUN pip install --no-cache-dir --target .python_packages/lib/site-packages -r requirements.txt
COPY mcp_servers/cosmos/function_app.py mcp_servers/cosmos/cosmos_tools.py mcp_servers/cosmos/host.json ./
RUN python -c "from pathlib import Path; from zipfile import ZipFile, ZIP_DEFLATED; z=ZipFile('/mcp-cosmos.zip','w',ZIP_DEFLATED); [z.write(p,p.as_posix()) for p in Path('.').rglob('*') if p.is_file()]; z.close()"

FROM python:3.11-slim

WORKDIR /app

# Install system dependencies + ODBC driver for MS SQL
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl gnupg2 unixodbc-dev \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor -o /usr/share/keyrings/microsoft-prod.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft-prod.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" > /etc/apt/sources.list.d/mssql-release.list \
    && apt-get update && ACCEPT_EULA=Y apt-get install -y msodbcsql18 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY api/ .
COPY web/ web/
COPY --from=mcp-package /mcp-cosmos.zip /app/mcp-cosmos.zip

EXPOSE 8080

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8080"]
