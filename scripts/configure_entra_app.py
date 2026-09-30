#!/usr/bin/env python3
"""Create or update the Entra application used by the OmniVec browser login."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import uuid
from urllib.parse import quote


GRAPH = "https://graph.microsoft.com/v1.0"
AZ_CLI = shutil.which("az") or shutil.which("az.cmd")


def az_json(*args: str, body: dict | None = None) -> dict:
    if not AZ_CLI:
        raise RuntimeError("Azure CLI was not found on PATH")
    command = [AZ_CLI, *args, "--only-show-errors", "-o", "json"]
    if body is not None:
        command.extend(["--body", json.dumps(body, separators=(",", ":"))])
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        message = result.stderr.strip() or result.stdout.strip() or "Azure CLI failed"
        raise RuntimeError(message)
    return json.loads(result.stdout or "{}")


def graph(method: str, path: str, body: dict | None = None) -> dict:
    return az_json(
        "rest",
        "--method",
        method,
        "--url",
        f"{GRAPH}{path}",
        "--headers",
        "Content-Type=application/json",
        body=body,
    )


def find_application(client_id: str) -> dict | None:
    response = graph(
        "GET",
        f"/applications?$filter={quote(f'appId eq {client_id!r}')}",
    )
    values = response.get("value") or []
    return values[0] if values else None


def find_application_by_name(display_name: str) -> dict | None:
    response = graph(
        "GET",
        f"/applications?$filter={quote(f'displayName eq {display_name!r}')}",
    )
    values = response.get("value") or []
    return values[0] if values else None


def find_service_principal(client_id: str) -> dict | None:
    response = graph(
        "GET",
        f"/servicePrincipals?$filter={quote(f'appId eq {client_id!r}')}",
    )
    values = response.get("value") or []
    return values[0] if values else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--redirect-uri", required=True)
    parser.add_argument("--client-id")
    parser.add_argument("--assign-current-user-admin", action="store_true")
    args = parser.parse_args()

    if not args.redirect_uri.startswith("https://"):
        parser.error("--redirect-uri must use HTTPS")
    redirect_uri = f"{args.redirect_uri.rstrip('/')}/auth-callback.html"

    account = az_json("account", "show")
    tenant_id = account["tenantId"]

    application = (
        find_application(args.client_id)
        if args.client_id
        else find_application_by_name(args.display_name)
    )
    if application is None:
        application = graph(
            "POST",
            "/applications",
            {
                "displayName": args.display_name,
                "signInAudience": "AzureADMyOrg",
                "spa": {"redirectUris": [redirect_uri]},
            },
        )

    object_id = application["id"]
    client_id = application["appId"]
    scope_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{client_id}:access_as_user"))
    role_ids = {
        "OmniVec.Admin": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{client_id}:admin")),
        "OmniVec.Operator": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{client_id}:operator")),
        "OmniVec.Viewer": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{client_id}:viewer")),
    }
    scope = f"api://{client_id}/access_as_user"
    permission_scope = {
        "adminConsentDescription": "Sign in to and use OmniVec.",
        "adminConsentDisplayName": "Use OmniVec",
        "id": scope_id,
        "isEnabled": True,
        "type": "User",
        "userConsentDescription": "Sign in to and use OmniVec.",
        "userConsentDisplayName": "Use OmniVec",
        "value": "access_as_user",
    }
    graph(
        "PATCH",
        f"/applications/{object_id}",
        {
            "identifierUris": [f"api://{client_id}"],
            "spa": {"redirectUris": [redirect_uri]},
            "api": {
                "requestedAccessTokenVersion": 2,
                "oauth2PermissionScopes": [permission_scope],
            },
            "appRoles": [
                {
                    "allowedMemberTypes": ["User"],
                    "description": f"{value.removeprefix('OmniVec.')} access to OmniVec.",
                    "displayName": value.replace(".", " "),
                    "id": role_id,
                    "isEnabled": True,
                    "value": value,
                }
                for value, role_id in role_ids.items()
            ],
        },
    )
    # Graph validates pre-authorized permission IDs against the already-saved
    # permission set, so reconcile it in a second request.
    time.sleep(2)
    graph(
        "PATCH",
        f"/applications/{object_id}",
        {
            "api": {
                "requestedAccessTokenVersion": 2,
                "oauth2PermissionScopes": [permission_scope],
                "preAuthorizedApplications": [
                    {"appId": client_id, "delegatedPermissionIds": [scope_id]}
                ],
            }
        },
    )

    service_principal = find_service_principal(client_id)
    if service_principal is None:
        service_principal = graph("POST", "/servicePrincipals", {"appId": client_id})

    if args.assign_current_user_admin:
        user = az_json("ad", "signed-in-user", "show")
        assignment = {
            "principalId": user["id"],
            "resourceId": service_principal["id"],
            "appRoleId": role_ids["OmniVec.Admin"],
        }
        try:
            graph(
                "POST",
                f"/servicePrincipals/{service_principal['id']}/appRoleAssignedTo",
                assignment,
            )
        except RuntimeError as exc:
            if "Permission being assigned already exists" not in str(exc):
                raise

    print(
        json.dumps(
            {
                "tenant_id": tenant_id,
                "client_id": client_id,
                "audience": f"api://{client_id}",
                "scope": scope,
                "admin_role": "OmniVec.Admin",
                "operator_role": "OmniVec.Operator",
                "viewer_role": "OmniVec.Viewer",
                "redirect_uri": redirect_uri,
                "enterprise_app_url": (
                    "https://entra.microsoft.com/#view/Microsoft_AAD_IAM/"
                    f"ManagedAppMenuBlade/~/Users/objectId/{service_principal['id']}"
                ),
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (KeyError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Failed to configure Entra application: {exc}", file=sys.stderr)
        raise SystemExit(1)
