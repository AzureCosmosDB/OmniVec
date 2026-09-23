#!/usr/bin/env python3
"""Create a Foundry prompt agent backed by a Cosmos MCP server."""

from __future__ import annotations

import argparse
import json

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.identity import AzureDeveloperCliCredential


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--tenant-id")
    parser.add_argument("--connection", required=True)
    parser.add_argument("--server-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument(
        "--fields",
        default="id,title,body,source_ref",
        help="Comma-separated fields vector_search should return.",
    )
    parser.add_argument(
        "--vector-field",
        default="embedding",
        help="Cosmos vector field vector_search should query.",
    )
    parser.add_argument("--agent-name", default="bami-cosmos-policy-agent")
    parser.add_argument(
        "--question",
        default="What airfare is required for flights shorter than six hours?",
    )
    parser.add_argument(
        "--instructions",
        default=(
            "Answer questions only from documents returned by the Cosmos MCP tools. "
            "Use vector_search with top_k set to 3 for questions, target the configured "
            "container and configured fields, and cite the returned source_ref or "
            "document title. If the tools do not contain the answer, say that the "
            "available data does not provide it."
        ),
    )
    args = parser.parse_args()

    project = AIProjectClient(
        endpoint=args.project_endpoint,
        credential=AzureDeveloperCliCredential(tenant_id=args.tenant_id),
    )
    openai = project.get_openai_client()
    tool = MCPTool(
        server_label="bami-cosmos",
        server_url=args.server_url,
        allowed_tools=[
            "list_allowed_containers",
            "vector_search",
        ],
        require_approval="never",
        project_connection_id=args.connection,
    )
    agent = project.agents.create_version(
        agent_name=args.agent_name,
        definition=PromptAgentDefinition(
            model=args.model,
            instructions=(
                f"{args.instructions} The target Cosmos container is "
                f"'{args.container}'. Use vector_field '{args.vector_field}' and "
                f"request the fields '{args.fields}'."
            ),
            tools=[tool],
        ),
    )
    conversation = openai.conversations.create()
    response = openai.responses.create(
        conversation=conversation.id,
        input=args.question,
        extra_body={
            "agent_reference": {"name": agent.name, "type": "agent_reference"}
        },
    )

    output_types = [getattr(item, "type", None) for item in response.output]
    print(
        json.dumps(
            {
                "agent_name": agent.name,
                "agent_version": agent.version,
                "conversation_id": conversation.id,
                "response_id": response.id,
                "output_types": output_types,
                "answer": response.output_text,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
