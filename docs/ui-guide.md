# OmniVec — Web UI Guide

This guide walks you through the OmniVec platform using **only the web interface**. No command-line tools or API calls are needed.

**URL**: `http://<external-ip>/ui`

---

## Table of Contents

1. [Key Concepts](#1-key-concepts)
2. [Navigation](#2-navigation)
3. [Dashboard](#3-dashboard)
4. [Creating a Source](#4-creating-a-source)
5. [Creating a Destination (Vector Store)](#5-creating-a-destination-vector-store)
6. [Creating a Pipeline](#6-creating-a-pipeline)
7. [Monitoring Jobs](#7-monitoring-jobs)
8. [Searching Vectors (Playground)](#8-searching-vectors-playground)
9. [Managing Deployments (Operations)](#9-managing-deployments-operations)
10. [DocGrok Engine — Models & Transform Pipelines](#10-docgrok-engine--models--transform-pipelines)
11. [Walkthrough: End-to-End Example](#11-walkthrough-end-to-end-example)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Key Concepts

Before you begin, here are the building blocks of OmniVec:

### Source
A **Source** is where your raw documents live. OmniVec reads documents from sources and feeds them into pipelines for processing.

Supported source types:
| Type | Description |
|------|-------------|
| **Azure Blob Storage** | Files stored in an Azure Storage Account container (PDFs, text files, etc.) |
| **CosmosDB** | Documents stored in an Azure CosmosDB NoSQL container |

### Destination (Vector Store)
A **Destination** is where processed vector embeddings are written. After a document is read from a source and transformed into an embedding, the resulting vector is stored in the destination.

Supported destination types:
| Type | Description |
|------|-------------|
| **Azure CosmosDB (Vector)** | CosmosDB container with vector indexing enabled |

### Pipeline
A **Pipeline** connects a Source to a Destination through a transformation step. It defines the complete flow:

```
Source  →  Transform (Embedding Model)  →  Destination
```

When a pipeline is active, it continuously watches the source for new or changed documents and processes them automatically.

### Model
A **Model** is the embedding engine that converts text into numerical vectors. Models can be:
- **Internal (Native)** — hosted inside the OmniVec cluster
- **External** — third-party services like Azure OpenAI, OpenAI, or Cohere

### DocGrok Transform Pipeline
A **Transform Pipeline** is a multi-step processing chain defined in the DocGrok engine. For example, a pipeline might: extract text from PDF → chunk into paragraphs → generate embeddings. For simple use cases, you can skip this and use a single embedding model directly.

### Job
A **Job** is a unit of work — one document being processed. When a pipeline detects a new document, it creates a job. Jobs track status (Pending → Processing → Completed/Failed) and processing details.

---

## 2. Navigation

The left sidebar organizes the UI into sections:

| Section | Page | Description |
|---------|------|-------------|
| **Overview** | Dashboard | System stats and active pipelines |
| **Configuration** | Sources | Manage data sources |
| | Vector Stores | Manage vector store destinations |
| | Pipelines | Create and manage processing pipelines |
| **Playground** | Vector Search | Search your vector indexes |
| **Operations** | Deployments | View and manage API, Controller, and Worker pods |
| **DocGrok Engine** | Models | Manage embedding models |
| | Transform Pipelines | Build multi-step transform chains |
| | Logs | View model execution logs |
| | System | Health and system status |

The bottom of the sidebar shows health indicators for DocGrok and the API (green = healthy).

---

## 3. Dashboard

The Dashboard is the landing page. It shows six summary cards:

| Card | What it shows |
|------|---------------|
| **Sources** | Total number of configured sources |
| **Destinations** | Total number of vector stores |
| **Pipelines** | Number of active pipelines |
| **Events Processed** | Total documents processed (with today's count) |
| **Failed** | Total failed jobs (with today's count) |
| **Avg Processing Time** | Average time to process a single document |

Below the cards is the **Active Pipelines** table showing each running pipeline's name, source, listener type, and status.

Click **Refresh** (top right) to update all stats.

---

## 4. Creating a Source

1. Click **Sources** in the left sidebar.
2. Click the **Add Source** button (top right).
3. A two-panel modal opens:

### Left Panel — Configuration

| Field | Description |
|-------|-------------|
| **Name** | A friendly name for this source (e.g., "Production Documents") |
| **Type** | Choose `Azure Blob Storage` or `CosmosDB` |

**If you chose Azure Blob Storage:**

| Field | Example | Description |
|-------|---------|-------------|
| **Account URL** | `https://myaccount.blob.core.windows.net` | The storage account URL |
| **Container** | `documents` | The blob container name |
| **Prefix** (optional) | `uploads/` | Only process blobs matching this prefix |

**If you chose CosmosDB:**

| Field | Example | Description |
|-------|---------|-------------|
| **Endpoint** | `https://myaccount.documents.azure.com` | The CosmosDB account endpoint |
| **Database** | `mydb` | Database name |
| **Container** | `documents` | Container name |
| **Content Source** | `Field Value`, `Blob Storage URL`, or `HTTP URL` | How to read document content |
| **Content Field** | `content` | Which field holds the text (or URL) |

### Right Panel — Authentication

Choose an authentication method:

- **Managed Identity** *(Recommended)* — Uses the platform's built-in Azure identity. No credentials needed. Just make sure the OmniVec identity has read access to your storage account or CosmosDB.
- **Connection String** — Paste a connection string. Note: some Azure policies may block this.

### Test Connection

Before saving, click **Test Connection** to verify OmniVec can reach your data source. The result appears below the button:
- **Success**: shows the number of documents found
- **Failure**: shows the error message (e.g., "Connection timed out")

4. Click **Add Source** to save.

Your new source appears in the Sources table with its name, type, and status.

### Editing or Deleting a Source

Click any source row to open its detail modal. You can:
- Edit the name, configuration, or authentication
- Click **Test Connection** to re-verify
- Click **Save Changes** to update
- Click **Delete Source** (bottom left, red) to remove it

---

## 5. Creating a Destination (Vector Store)

1. Click **Vector Stores** in the left sidebar.
2. Click **Add Destination** (top right).
3. A two-panel modal opens:

### Left Panel — Vector Store Configuration

| Field | Example | Description |
|-------|---------|-------------|
| **Name** | `My Vector Store` | Friendly name |
| **Type** | `Azure CosmosDB (Vector)` | Currently the only option |
| **Endpoint** | `https://myaccount.documents.azure.com` | CosmosDB account endpoint |
| **Database** | `vectors` | Database name |
| **Container** | `embeddings` | Container with vector indexing |

### Right Panel — Authentication

Same as sources:
- **Managed Identity** *(Recommended)*
- **API Key / Connection String**

### Test Connection

Click **Test Connection** to verify write access to the vector store before saving.

4. Click **Add Destination** to save.

### Editing or Deleting a Destination

Click any destination row to open its detail modal. Edit fields, test the connection, save, or delete.

---

## 6. Creating a Pipeline

This is where everything comes together. The pipeline wizard has four steps.

1. Click **Pipelines** in the left sidebar.
2. Click **Create Pipeline** (top right).
3. A full-screen wizard opens with four steps shown in the left sidebar.

### Step 1: General

| Field | Description |
|-------|-------------|
| **Pipeline Name** | A descriptive name (e.g., "Blog Posts Ingestion") |
| **Description** (optional) | Explain what this pipeline does |
| **Process existing documents on creation** | When checked, all documents already in the source will be processed immediately after the pipeline is created. Uncheck if you only want to process new documents going forward. |

Click **Next** to continue.

### Step 2: Source

You have two options:

**Option A — Select Existing**: Pick a source you already created from the dropdown. Click **Test** next to the dropdown to verify the connection.

**Option B — Create New**: Toggle to "Create New" and fill in the source form (same fields as [Creating a Source](#4-creating-a-source)). The source will be created along with the pipeline.

Click **Next** to continue.

### Step 3: Transform

Choose how documents are transformed into vectors:

**Option A — Select Pipeline**: Pick a DocGrok Transform Pipeline from the dropdown. This is for multi-step processing (e.g., PDF extraction → chunking → embedding).

**Option B — Single Model**: Pick an embedding model directly from the dropdown. Models are listed as:
- `api:model_name` — Internal models running in the cluster
- `external:provider:model` — External models (e.g., Azure OpenAI)

For most use cases, **Single Model** is the simplest choice. A transform pipeline is created automatically behind the scenes.

Click **Next** to continue.

### Step 4: Destination

You have two options:

**Option A — Select Existing**: Pick a destination you already created from the dropdown. Click **Test** to verify the connection.

**Option B — Create New**: Toggle to "Create New" and fill in the destination form (same fields as [Creating a Destination](#5-creating-a-destination-vector-store)).

There is also a **Same as source** checkbox — if checked, vectors are written back into the same container as the source (useful for CosmosDB sources that store both documents and their embeddings).

Click **Create Pipeline** to finish.

### After Creation

The pipeline appears in the Pipelines table. If "Process existing documents" was checked, you'll see jobs start appearing immediately.

### Pipeline Detail View

Click any pipeline row to view its details:

- **General tab**: Edit name, description, view stats (created date, last updated, documents processed, status)
- **Sources & Destination tab**: Add or remove sources, change the destination
- **Model & Pipeline tab**: View the processing flow diagram

**Action buttons** in the modal footer:
| Button | What it does |
|--------|--------------|
| **Delete Pipeline** | Permanently remove the pipeline |
| **Pause** | Stop processing (can resume later) |
| **Resume** | Restart a paused pipeline |
| **Run Pipeline** | Manually trigger processing of all source documents |
| **Save Changes** | Save any edits to name, description, etc. |

---

## 7. Monitoring Jobs

Jobs are visible in the pipeline detail view and on the dashboard. Each job represents one document being processed.

Job statuses:
| Status | Meaning |
|--------|---------|
| **PENDING** | Job created, waiting to be picked up by a worker |
| **PROCESSING** | Worker is actively processing the document |
| **COMPLETED** | Document successfully embedded and stored |
| **FAILED** | Processing failed (check error details) |

The dashboard shows total processed and failed counts. The pipeline detail view shows per-pipeline stats.

---

## 8. Searching Vectors (Playground)

Once your pipeline has processed documents, you can search the resulting vectors.

1. Click **Vector Search** in the left sidebar.
2. Fill in the search form:

| Field | Description |
|-------|-------------|
| **Search Query** | Type a natural language question or phrase |
| **Vector Index** | Select the destination (vector store) to search |
| **Top K** | Number of results to return (1–20, default 5) |

3. Click **Search**.

### Results

Each result card shows:
- **Ranking** — numbered position (1, 2, 3...)
- **Source Reference** — the original document ID or blob path
- **Similarity Score** — how close the match is (higher = more relevant)
- **Text Excerpt** — the matching text content
- **Metadata** — key-value tags from the original document

Performance stats appear above the results: total time, embedding time, and search time.

---

## 9. Managing Deployments (Operations)

The Operations page lets you view and control the OmniVec system components.

1. Click **Deployments** in the left sidebar.

Three deployment cards are shown:

| Deployment | Role |
|------------|------|
| **omnivec-api** | The API server and web UI |
| **omnivec-controller** | Monitors sources and creates jobs |
| **omnivec-worker** | Processes jobs (embeds documents and writes vectors) |

Each card displays:
- **Status badge** — Running (green) or Degraded (orange)
- **Image version** — current container image tag
- **Ready replicas** — e.g., "1/1 ready"
- **Pod table** — individual pod names, status, restart count, age

### Actions

| Button | What it does |
|--------|--------------|
| **+** (Scale Up) | Add one more replica |
| **−** (Scale Down) | Remove one replica |
| **Restart** | Rolling restart of all pods (asks for confirmation) |
| **Pause** | Scale to 0 replicas (stops processing) |
| **Resume** | Scale back to 1 replica |

**Common use case**: Scale the worker from 1 to 3 replicas to speed up processing of a large backlog, then scale back down when done.

---

## 10. DocGrok Engine — Models & Transform Pipelines

### Models

The Models page lists all available embedding models.

**Filter tabs**: All | Native | External

To add an external model (e.g., Azure OpenAI):
1. Click **Add External Model**.
2. Fill in the form:

| Field | Example |
|-------|---------|
| **Provider Name** | `my-azure-openai` |
| **Provider Type** | `azure-openai` (or `openai`, `cohere`, `custom`) |
| **Endpoint URL** | `https://myoai.openai.azure.com` |
| **API Key** | Your API key |
| **API Version** | `2024-06-01` (Azure only) |
| **Model / Deployment Name** | `text-embedding-3-large` |
| **Embedding Dimensions** | `3072` |

3. Click **Add Model**.

Model actions: **Start**, **Stop**, **Restart** (via buttons in the table).

### Transform Pipelines

Transform Pipelines let you chain multiple processing steps:

1. Click **Transform Pipelines** in the sidebar.
2. Click **Create Pipeline**.
3. Enter a name and description.
4. Click **Add Step** to add processing stages (e.g., text extraction → chunking → embedding).
5. For each step, select a model and configure options.
6. Click **Save Pipeline**.

The pipeline can then be selected in Step 3 of the ingestion pipeline wizard.

### Logs

Click **Logs** in the sidebar, select a model from the dropdown, and click **Load Logs** to view execution output.

### System

The System page shows health metrics — CPU, memory, request rates, and component statuses.

---

## 11. Walkthrough: End-to-End Example

This example creates a complete pipeline that reads text files from Azure Blob Storage, generates embeddings using Azure OpenAI, and stores vectors in CosmosDB.

### Prerequisites
- An Azure Storage Account with a blob container containing text documents
- An Azure CosmosDB account with a database and vector-enabled container
- An Azure OpenAI deployment with an embedding model
- The OmniVec managed identity must have:
  - **Storage Blob Data Reader** on the storage account
  - **Cosmos DB Built-in Data Contributor** on the CosmosDB account

### Step 1 — Register an Embedding Model

1. Go to **Models** (under DocGrok Engine).
2. Click **Add External Model**.
3. Fill in:
   - Provider Name: `azure-openai-prod`
   - Provider Type: `azure-openai`
   - Endpoint URL: `https://your-resource.openai.azure.com`
   - API Key: your key
   - API Version: `2024-06-01`
   - Model: `text-embedding-3-large`
   - Dimensions: `3072`
4. Click **Add Model**.
5. Verify the model shows as "Running" in the table.

### Step 2 — Create a Source

1. Go to **Sources**.
2. Click **Add Source**.
3. Fill in:
   - Name: `Production Docs`
   - Type: `Azure Blob Storage`
   - Account URL: `https://youraccount.blob.core.windows.net`
   - Container: `documents`
4. Authentication: select **Managed Identity**.
5. Click **Test Connection** — confirm it succeeds and shows the document count.
6. Click **Add Source**.

### Step 3 — Create a Destination

1. Go to **Vector Stores**.
2. Click **Add Destination**.
3. Fill in:
   - Name: `Production Vectors`
   - Type: `Azure CosmosDB (Vector)`
   - Endpoint: `https://youraccount.documents.azure.com`
   - Database: `vectors`
   - Container: `embeddings`
4. Authentication: select **Managed Identity**.
5. Click **Test Connection** — confirm it succeeds.
6. Click **Add Destination**.

### Step 4 — Create a Pipeline

1. Go to **Pipelines**.
2. Click **Create Pipeline**.
3. **General**:
   - Name: `Docs → Vectors`
   - Check "Process existing documents on creation"
   - Click **Next**
4. **Source**:
   - Select `Production Docs` from the dropdown
   - Click **Test** to verify
   - Click **Next**
5. **Transform**:
   - Toggle to **Single Model**
   - Select `external:azure-openai-prod:text-embedding-3-large`
   - Click **Next**
6. **Destination**:
   - Select `Production Vectors` from the dropdown
   - Click **Test** to verify
   - Click **Create Pipeline**

### Step 5 — Monitor Processing

1. The pipeline now appears in the **Pipelines** table with status "Active".
2. Go to the **Dashboard** — watch the "Events Processed" counter increase.
3. Click the pipeline row to see detailed stats and job progress.
4. If processing is slow, go to **Deployments** and scale the worker up (click **+**).

### Step 6 — Search Your Vectors

1. Go to **Vector Search** (under Playground).
2. Select `Production Vectors` as the Vector Index.
3. Type a search query (e.g., "How do I configure authentication?").
4. Set Top K to 5.
5. Click **Search**.
6. Review the ranked results with similarity scores and text excerpts.

---

## 12. Troubleshooting

| Problem | Solution |
|---------|----------|
| **Test Connection hangs or times out** | Check that the URL is correct with no extra spaces. Verify the managed identity has the required role on the target resource. |
| **"Connection timed out after 10s"** | The target resource may be unreachable from the cluster. Verify the endpoint URL, check network/firewall rules, and ensure the resource exists. |
| **Pipeline is Active but no jobs appear** | Make sure "Process existing documents" is checked, or upload new documents to trigger event-driven processing. Check that the controller deployment is running (Operations page). |
| **Jobs stuck in PENDING** | The worker may be stopped or overloaded. Go to Deployments and verify the worker is running. Scale up if needed. |
| **Jobs failing** | Click the pipeline to see error details. Common causes: model API key expired, destination out of capacity, document format not supported. |
| **Search returns no results** | Verify the pipeline has completed processing (check job count). Ensure you selected the correct Vector Index in the search form. |
| **Operations page shows "Degraded"** | One or more pods are not ready. Check the pod table for restart counts or error states. Try a rolling restart. |
| **Cannot scale deployments** | The API service account may lack Kubernetes RBAC permissions. Contact your platform administrator. |
