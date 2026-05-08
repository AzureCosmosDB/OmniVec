"""
DocGrok Transformation Pipelines
"""
import asyncio  # lgtm[py/unused-import]
import httpx
import base64
import io
import os
import json  # lgtm[py/unused-import]
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from enum import Enum


class StepType(str, Enum):
    LOCAL = "local"
    API = "api"
    EXTERNAL = "external"


class PipelineStep(BaseModel):
    id: str
    type: StepType
    function: Optional[str] = None
    model: Optional[str] = None  # model_id for API/external steps
    config: Dict[str, Any] = {}
    depends_on: List[str] = []


class Pipeline(BaseModel):
    name: str
    description: str
    steps: List[PipelineStep]


# Storage for pipelines
PIPELINES: Dict[str, Pipeline] = {}

# Available local functions
LOCAL_FUNCTIONS = {
    "pdf_to_images": "Convert PDF pages to images",
    "ocr_extract": "Extract text via OCR",
    "extract_text_from_pdf": "Extract text directly from PDF",
    "resize_images": "Resize images",
    "split_text": "Split text into chunks",
    "passthrough": "Pass data unchanged"
}

# Available models
MODELS = {
    "dse-qwen2": {"type": "vision", "description": "Visual document embeddings"},
    "clip": {"type": "vision", "description": "Image embeddings"},
    "bge": {"type": "text", "description": "Text embeddings"}
}


async def execute_local_step(function: str, data: Any, config: dict) -> Any:
    """Execute a local transformation step"""

    if function == "pdf_to_images":
        try:
            from pdf2image import convert_from_bytes
            dpi = config.get("dpi", 150)
            images = convert_from_bytes(data, dpi=dpi)
            result = []
            for img in images:
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                result.append(buf.getvalue())
            return result
        except Exception as e:
            raise RuntimeError(f"pdf_to_images failed: {e}")

    elif function == "ocr_extract":
        try:
            from PIL import Image
            import pytesseract
            if isinstance(data, list):
                texts = []
                for img_data in data:
                    img = Image.open(io.BytesIO(img_data))
                    texts.append(pytesseract.image_to_string(img))
                return "\n\n".join(texts)
            else:
                img = Image.open(io.BytesIO(data))
                return pytesseract.image_to_string(img)
        except Exception as e:
            raise RuntimeError(f"ocr_extract failed: {e}")

    elif function == "extract_text_from_pdf":
        try:
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            texts = [page.get_text() for page in doc]
            return "\n\n".join(texts)
        except Exception as e:
            raise RuntimeError(f"extract_text_from_pdf failed: {e}")

    elif function == "resize_images":
        from PIL import Image
        max_size = config.get("max_size", 1024)
        result = []
        for img_data in (data if isinstance(data, list) else [data]):
            img = Image.open(io.BytesIO(img_data))
            img.thumbnail((max_size, max_size))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            result.append(buf.getvalue())
        return result

    elif function == "split_text":
        chunk_size = config.get("chunk_size", 512)
        chunks = []
        for i in range(0, len(data), chunk_size):
            chunks.append(data[i:i+chunk_size])
        return chunks

    elif function == "passthrough":
        return data

    else:
        raise ValueError(f"Unknown function: {function}")


class PipelineExecutor:
    def __init__(self, http_client: httpx.AsyncClient, model_urls: dict, providers: dict):
        self.client = http_client
        self.model_urls = model_urls
        self.providers = providers

    async def call_api(self, model: str, data: Any) -> Any:
        """Call internal model API"""
        url = self.model_urls.get(model)
        if not url:
            raise ValueError(f"Unknown model: {model}")

        if isinstance(data, list) and all(isinstance(d, bytes) for d in data):
            images_b64 = [base64.b64encode(img).decode() for img in data]
            payload = {"images": images_b64}
        elif isinstance(data, bytes):
            payload = {"data": base64.b64encode(data).decode()}
        elif isinstance(data, str):
            payload = {"text": data}
        else:
            payload = {"data": data}

        resp = await self.client.post(f"{url}/embed", json=payload, timeout=300)
        resp.raise_for_status()
        result = resp.json()
        return result.get("pages") or result.get("embeddings") or result

    async def call_external(self, provider: str, model: str, data: Any) -> Any:
        """Call external provider API"""
        pconfig = self.providers.get(provider, {})
        endpoint = pconfig.get("endpoint")
        api_key = pconfig.get("api_key") or os.environ.get(f"PROVIDER_{provider.upper().replace('-','_')}_API_KEY")
        api_version = pconfig.get("api_version", "2024-06-01")

        if pconfig.get("type") == "azure-openai":
            deployment = pconfig.get("models", {}).get(model, {}).get("deployment", model)
            url = f"{endpoint}/openai/deployments/{deployment}/embeddings?api-version={api_version}"
            headers = {"api-key": api_key, "Content-Type": "application/json"}
        else:
            url = f"{endpoint}/embeddings"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

        input_text = [data] if isinstance(data, str) else data
        payload = {"input": input_text}
        if pconfig.get("type") != "azure-openai":
            payload["model"] = model

        resp = await self.client.post(url, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        return [item["embedding"] for item in resp.json().get("data", [])]

    async def execute(self, pipeline: Pipeline, input_data: Any) -> dict:
        """Execute pipeline"""
        results = {"_input": input_data}
        executed = set()

        pending = list(pipeline.steps)

        while pending:
            ready = [s for s in pending if all(d in executed for d in s.depends_on)]

            if not ready:
                raise RuntimeError(f"Stuck - remaining: {[s.id for s in pending]}")

            for step in ready:
                step_input = results[step.depends_on[0]] if step.depends_on else input_data

                if step.type == StepType.LOCAL:
                    result = await execute_local_step(step.function, step_input, step.config)
                elif step.type == StepType.API:
                    result = await self.call_api(step.model, step_input)
                elif step.type == StepType.EXTERNAL:
                    result = await self.call_external(step.provider, step.model, step_input)

                results[step.id] = result
                executed.add(step.id)
                pending.remove(step)

        final = pipeline.steps[-1]
        return {"pipeline": pipeline.name, "output": results[final.id]}


# Default pipelines
def init_default_pipelines():
    """Initialize with empty pipeline registry. Pipelines are now managed dynamically via API."""
    global PIPELINES
    PIPELINES = {}

init_default_pipelines()
