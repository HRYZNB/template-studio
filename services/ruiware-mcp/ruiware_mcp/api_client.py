from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any


class RuiWareApiError(RuntimeError):
    """A non-successful response from the local RuiWare API."""


class RuiWareApiClient:
    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or os.getenv("RUIWARE_API_URL") or "http://127.0.0.1:8010/api/v1").rstrip("/")

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, payload: dict[str, Any]) -> Any:
        return self._request("POST", path, payload)

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("detail", body)
            except json.JSONDecodeError:
                detail = body
            raise RuiWareApiError(f"RuiWare API {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise RuiWareApiError(f"无法连接 RuiWare API（{self.base_url}）：{error.reason}") from error
