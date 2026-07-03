import httpx
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential


class OllamaError(Exception):
    """Raised when the Ollama API returns an error or is unreachable."""


class OllamaClient:
    """Async HTTP client for the Ollama local LLM API.

    Wraps the /api/generate endpoint with retries and structured error handling.
    Keeps a single httpx.AsyncClient alive for connection reuse.
    """

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3") -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=120.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        reraise=True,
    )
    async def generate(self, prompt: str, system: str = "") -> str:
        """Send a prompt to Ollama and return the full text response.

        Uses non-streaming mode so the entire response is returned at once.
        Retries up to 3 times on transient failures.
        """
        payload: dict = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
        }
        if system:
            payload["system"] = system

        logger.debug(f"Calling Ollama model={self._model}, prompt_len={len(prompt)}")

        try:
            response = await self._client.post("/api/generate", json=payload)
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise OllamaError(
                f"Cannot connect to Ollama at {self._base_url}. "
                "Make sure Ollama is running (`ollama serve`)."
            ) from e
        except httpx.HTTPStatusError as e:
            raise OllamaError(
                f"Ollama returned HTTP {e.response.status_code}: {e.response.text}"
            ) from e

        data = response.json()
        text: str = data.get("response", "")
        logger.debug(f"Ollama response length: {len(text)} chars")
        return text

    async def check_health(self) -> bool:
        """Return True if Ollama is reachable and the target model is available."""
        try:
            resp = await self._client.get("/api/tags")
            resp.raise_for_status()
            models = [m["name"].split(":")[0] for m in resp.json().get("models", [])]
            if self._model not in models:
                logger.warning(
                    f"Model '{self._model}' not found in Ollama. "
                    f"Available: {models}. Pull it with: ollama pull {self._model}"
                )
                return False
            return True
        except Exception as e:
            logger.error(f"Ollama health check failed: {e}")
            return False
