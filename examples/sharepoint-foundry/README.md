# Isolated SharePoint to Foundry example

The three `.txt` files in this directory contain synthetic policies with distinctive
test values. They are not real company policies.

## Upload once, then use OmniVec

Create a new folder named **OmniVec-Foundry-Demo-20260923** at the root of the
document library already connected to OmniVec. Upload all three `.txt` files.
Do not put this folder inside an existing watched folder such as `Policies`.
Do not alter any existing document. Creating the folder and uploading these files
is the only step performed outside OmniVec.

After the upload, complete the remaining workflow in the OmniVec portal:

1. **MCP servers > Need a new isolated Cosmos vector container?** Prepare a
   container plan in an existing database, choosing the registered
   `text-embedding-3-small` model. Use a distinct container such as
   `sharepoint-foundry-demo-20260923`. Review and separately approve the plan.
2. **Destinations:** register that newly created Cosmos vector container using
   its returned endpoint/database/container. Test the connection and vector
   policy. Use managed identity, not an account key.
3. **Sources:** register a new SharePoint source using the connected site's
   existing site ID, drive ID and approved identity. Restrict `folder_path` to
   `OmniVec-Foundry-Demo-20260923`; do not register the entire drive.
4. **Pipelines:** create a new pipeline from this new source to this new
   destination, using the same embedding model, vector field `embedding`,
   process-existing enabled and stored text enabled in field `content`.
   Wait for the three source documents to be processed. Inspect processing
   status and search results; registration/readiness is not ingestion proof.
5. **Vector Search:** search the new destination for
   `NORTHSTAR-DEMO-TRAVEL-2026`. Confirm the synthetic travel policy is returned.
6. **MCP servers:** select the new destination and matching embedding model.
   Use returned fields `id,title,content,source_ref`, not `text`. Supply actual
   account resource IDs and an approved hosting resource group/region.
   Review and approve the new Function/storage/hosting and identity grants.
7. **Foundry agents:** select the completed MCP deployment and an existing
   Foundry project/chat deployment. Review and approve agent creation.
8. **Foundry agents > Test a deployed Foundry agent:** select the new agent and
   prepare a test plan for this question:

   **Under NORTHSTAR-DEMO-TRAVEL-2026, what is the domestic daily meal limit,
   when are individual meal receipts required, and how soon must the claim
   be submitted? Cite the source.**

   Review and approve the separate inference/embedding cost. Inspect the answer
   and source references displayed in the saved test job.

Expected answer: **47 USD per day; receipts for individual meals over 18 USD;
submission within 12 calendar days after the trip ends.** The returned source
must identify the new synthetic travel document, not an older SharePoint policy.

Do not call this an end-to-end pass until new-document ingestion, new-container
search, MCP retrieval and the Foundry answer/reference have all been observed.
A successful agent creation alone is not sufficient.

## Approval and isolation

Provisioning is disabled by default. An administrator must separately approve
enabling the scoped deployment identity, the resource plans and billable
inference. No SharePoint write access is needed by OmniVec for this example.
Existing sources, pipelines, vector containers and agents are not changed.
Example resources remain billable until an operator approves their cleanup.
See [deployment permissions and recovery](../../docs/cloud-deployments.md).
