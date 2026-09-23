# Generic Cosmos DB MCP server

This reusable Azure Functions app exposes read-only MCP tools over a
Function-key-protected Streamable HTTP endpoint:

- `list_allowed_containers` reports the deployment's configured allowlist.
- `list_documents` returns a bounded, explicit projection from an allowed
  container.
- `vector_search` embeds natural language and performs Cosmos DB vector search,
  returning only explicitly requested fields.

The Function App uses managed identity for both Cosmos DB and Azure OpenAI. The
MCP endpoint itself uses a Function key, which should be stored in a
Microsoft Foundry project connection rather than source code or agent prompts.
Container names and projected fields are validated before they are interpolated
into Cosmos SQL, and result counts are capped at 10.

Required application settings:

```text
COSMOS_ENDPOINT
COSMOS_DATABASE
COSMOS_ALLOWED_CONTAINERS
COSMOS_DEFAULT_CONTAINER
AZURE_OPENAI_ENDPOINT
AZURE_OPENAI_DEPLOYMENT
AZURE_OPENAI_API_VERSION
```

Deploy the directory as an Azure Functions Python package, grant its managed
identity Cosmos DB Built-in Data Reader on the allowed containers and Cognitive
Services OpenAI User on the embedding account, then connect Foundry to:

```text
https://<function-app>.azurewebsites.net/api/mcp
```

Configure the project connection with `CustomKeys` authentication and the
Function key in the `x-functions-key` header.

Use `scripts/foundry_cosmos_mcp_agent.py` to create a prompt agent for a specific
use case and run a verification question. Install its dependencies in a separate
virtual environment from the Function App because the Foundry SDK and deployed
Function App intentionally have independent dependency sets:

```text
python -m pip install -r scripts/requirements-foundry-cosmos-mcp.txt
```
