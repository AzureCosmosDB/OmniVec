FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY connectors/worker/dotnet/OmniVec.Worker.csproj connectors/worker/dotnet/
COPY connectors/ingestion/dotnet/OmniVec.ChangeFeed.csproj connectors/ingestion/dotnet/
RUN dotnet restore connectors/ingestion/dotnet/OmniVec.ChangeFeed.csproj
COPY connectors/worker/dotnet/ connectors/worker/dotnet/
COPY connectors/ingestion/dotnet/ connectors/ingestion/dotnet/
RUN dotnet publish connectors/ingestion/dotnet/OmniVec.ChangeFeed.csproj -c Release -o /app --no-restore

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .
ENTRYPOINT ["dotnet", "OmniVec.ChangeFeed.dll"]
