FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY Common/ Common/
COPY Destination/ Destination/
RUN dotnet publish Destination/OmniVec.Mock.Destination.csproj -c Release -o /app
FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_URLS=http://+:8080
ENTRYPOINT ["dotnet", "OmniVec.Mock.Destination.dll"]
