#!/usr/bin/env python3
"""Create a Cosmos MCP agent or resume a response after per-user OAuth consent."""

from __future__ import annotations

import argparse
import json

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MCPTool, PromptAgentDefinition
from azure.identity import AzureDeveloperCliCredential


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--tenant-id")
    parser.add_argument("--connection")
    parser.add_argument("--server-url")
    parser.add_argument("--model")
    parser.add_argument("--container")
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
    parser.add_argument("--agent-name")
    parser.add_argument("--agent-version", help="Saved version; required when resuming.")
    parser.add_argument(
        "--resume-response-id",
        help="Resume after OAuth consent without creating another agent or conversation.",
    )
    parser.add_argument(
        "--question",
        help="Question to ask; repeat the original question when resuming after consent.",
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
    args = parser.parse_args(argv)
    required = (
        ("agent_name", "agent_version", "question") if args.resume_response_id
        else ("connection", "server_url", "model", "container")
    )
    for name in required:
        if not getattr(args, name):
            parser.error(f"--{name.replace('_', '-')} is required for this operation")
    if args.agent_version and not args.resume_response_id:
        parser.error("--agent-version is only used with --resume-response-id")
    question = args.question or "What airfare is required for flights shorter than six hours?"

    with AIProjectClient(
        endpoint=args.project_endpoint,
        credential=AzureDeveloperCliCredential(tenant_id=args.tenant_id),
    ) as project, project.get_openai_client() as openai:
        conversation_id = None
        if args.resume_response_id:
            agent_name, agent_version = args.agent_name, args.agent_version
            continuation = {"previous_response_id": args.resume_response_id}
        else:
            tool = MCPTool(
                server_label="bami-cosmos",
                server_url=args.server_url,
                allowed_tools=["list_allowed_containers", "vector_search"],
                require_approval="never",
                project_connection_id=args.connection,
            )
            agent = project.agents.create_version(
                agent_name=args.agent_name or "bami-cosmos-policy-agent",
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
            agent_name, agent_version = agent.name, str(agent.version)
            conversation_id = openai.conversations.create().id
            continuation = {"conversation": conversation_id}
        response = openai.with_options(max_retries=0, timeout=120).responses.create(
            **continuation,
            input=question,
            tool_choice="required",
            extra_body={
                "agent_reference": {
                    "name": agent_name, "version": agent_version, "type": "agent_reference",
                },
            },
        )

    output_types = [getattr(item, "type", None) for item in response.output]
    consent_links = []
    for item in response.output:
        if getattr(item, "type", None) == "oauth_consent_request":
            link = getattr(item, "consent_link", None)
            if not isinstance(link, str) or not link:
                raise RuntimeError("Foundry returned an OAuth consent request without a consent link")
            consent_links.append(link)
    status = "requires_consent" if consent_links else response.status
    if status == "completed" and not (response.output_text or "").strip():
        status = "answer_missing"
    print(
        json.dumps(
            {
                "status": status,
                "agent_name": agent_name,
                "agent_version": agent_version,
                "conversation_id": conversation_id,
                "response_id": response.id,
                "output_types": output_types,
                "consent_links": consent_links,
                "answer": response.output_text if status == "completed" else None,
            },
            indent=2,
        )
    )
    if status not in {"completed", "requires_consent"}:
        raise SystemExit("Foundry did not complete an answer; inspect the response ID before retrying.")


if __name__ == "__main__":
    main()
