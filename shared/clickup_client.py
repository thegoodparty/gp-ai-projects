import os
from typing import Optional, List, Dict, Any
from enum import Enum
import httpx
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from shared.logger import get_logger

load_dotenv()


class TaskStatus(Enum):
    OPEN = "Open"
    IN_PROGRESS = "in progress"
    REVIEW = "review"
    COMPLETE = "complete"
    CLOSED = "Closed"


class TaskPriority(Enum):
    URGENT = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4


class ClickUpTask(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[int] = None
    assignees: List[Dict[str, Any]] = Field(default_factory=list)
    tags: List[Dict[str, Any]] = Field(default_factory=list)
    due_date: Optional[str] = None
    start_date: Optional[str] = None
    time_estimate: Optional[int] = None
    custom_fields: List[Dict[str, Any]] = Field(default_factory=list)
    list_id: Optional[str] = Field(default=None, alias="list")
    folder_id: Optional[str] = Field(default=None, alias="folder")
    space_id: Optional[str] = Field(default=None, alias="space")
    url: Optional[str] = None
    parent: Optional[str] = None

    class Config:
        populate_by_name = True


class ClickUpComment(BaseModel):
    id: str
    comment_text: str
    user: Dict[str, Any]
    date: str


class ClickUpList(BaseModel):
    id: str
    name: str
    folder_id: Optional[str] = Field(default=None, alias="folder")
    space_id: Optional[str] = Field(default=None, alias="space")
    status: Optional[Dict[str, Any]] = None


class ClickUpSpace(BaseModel):
    id: str
    name: str
    statuses: List[Dict[str, Any]] = Field(default_factory=list)
    features: Optional[Dict[str, Any]] = None


class ClickUpClient:
    BASE_URL = "https://api.clickup.com/api/v2"

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        base_delay: float = 1.0
    ):
        self.api_key = api_key or os.getenv("CLICKUP_API_KEY")
        if not self.api_key:
            raise ValueError("ClickUp API key is required. Set CLICKUP_API_KEY env var or pass api_key parameter.")

        self.timeout = timeout
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.logger = get_logger(__name__)

        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json"
            },
            timeout=timeout
        )

        self.logger.info("ClickUp client initialized")

    def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        import time

        last_exception = None
        for attempt in range(self.max_retries):
            try:
                response = self._client.request(
                    method=method,
                    url=endpoint,
                    params=params,
                    json=json_data
                )

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", self.base_delay * (2 ** attempt)))
                    self.logger.warning(f"Rate limited. Waiting {retry_after}s before retry {attempt + 1}/{self.max_retries}")
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                last_exception = e
                self.logger.warning(f"HTTP error {e.response.status_code} on attempt {attempt + 1}/{self.max_retries}: {str(e)}")

                if e.response.status_code >= 500:
                    delay = self.base_delay * (2 ** attempt)
                    time.sleep(delay)
                    continue
                raise

            except httpx.RequestError as e:
                last_exception = e
                self.logger.warning(f"Request error on attempt {attempt + 1}/{self.max_retries}: {str(e)}")
                delay = self.base_delay * (2 ** attempt)
                time.sleep(delay)
                continue

        raise RuntimeError(f"Failed after {self.max_retries} attempts: {str(last_exception)}")

    def get_authorized_user(self) -> Dict[str, Any]:
        return self._request("GET", "/user")

    def get_authorized_teams(self) -> List[Dict[str, Any]]:
        response = self._request("GET", "/team")
        return response.get("teams", [])

    def get_spaces(self, team_id: str, archived: bool = False) -> List[ClickUpSpace]:
        response = self._request(
            "GET",
            f"/team/{team_id}/space",
            params={"archived": str(archived).lower()}
        )
        return [ClickUpSpace(**space) for space in response.get("spaces", [])]

    def get_folders(self, space_id: str, archived: bool = False) -> List[Dict[str, Any]]:
        response = self._request(
            "GET",
            f"/space/{space_id}/folder",
            params={"archived": str(archived).lower()}
        )
        return response.get("folders", [])

    def get_lists(self, folder_id: str, archived: bool = False) -> List[ClickUpList]:
        response = self._request(
            "GET",
            f"/folder/{folder_id}/list",
            params={"archived": str(archived).lower()}
        )
        return [ClickUpList(**lst) for lst in response.get("lists", [])]

    def get_folderless_lists(self, space_id: str, archived: bool = False) -> List[ClickUpList]:
        response = self._request(
            "GET",
            f"/space/{space_id}/list",
            params={"archived": str(archived).lower()}
        )
        return [ClickUpList(**lst) for lst in response.get("lists", [])]

    def get_task(self, task_id: str, include_subtasks: bool = False) -> ClickUpTask:
        response = self._request(
            "GET",
            f"/task/{task_id}",
            params={"include_subtasks": str(include_subtasks).lower()}
        )
        return ClickUpTask(**response)

    def get_tasks(
        self,
        list_id: str,
        archived: bool = False,
        page: int = 0,
        order_by: str = "created",
        reverse: bool = False,
        subtasks: bool = False,
        statuses: Optional[List[str]] = None,
        include_closed: bool = False,
        assignees: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        due_date_gt: Optional[int] = None,
        due_date_lt: Optional[int] = None,
        date_created_gt: Optional[int] = None,
        date_created_lt: Optional[int] = None,
        date_updated_gt: Optional[int] = None,
        date_updated_lt: Optional[int] = None
    ) -> List[ClickUpTask]:
        params = {
            "archived": str(archived).lower(),
            "page": page,
            "order_by": order_by,
            "reverse": str(reverse).lower(),
            "subtasks": str(subtasks).lower(),
            "include_closed": str(include_closed).lower()
        }

        if statuses:
            params["statuses[]"] = statuses
        if assignees:
            params["assignees[]"] = assignees
        if tags:
            params["tags[]"] = tags
        if due_date_gt:
            params["due_date_gt"] = due_date_gt
        if due_date_lt:
            params["due_date_lt"] = due_date_lt
        if date_created_gt:
            params["date_created_gt"] = date_created_gt
        if date_created_lt:
            params["date_created_lt"] = date_created_lt
        if date_updated_gt:
            params["date_updated_gt"] = date_updated_gt
        if date_updated_lt:
            params["date_updated_lt"] = date_updated_lt

        response = self._request("GET", f"/list/{list_id}/task", params=params)
        return [ClickUpTask(**task) for task in response.get("tasks", [])]

    def get_all_tasks(
        self,
        list_id: str,
        **kwargs
    ) -> List[ClickUpTask]:
        all_tasks = []
        page = 0

        while True:
            tasks = self.get_tasks(list_id, page=page, **kwargs)
            if not tasks:
                break
            all_tasks.extend(tasks)
            if len(tasks) < 100:
                break
            page += 1

        self.logger.info(f"Retrieved {len(all_tasks)} tasks from list {list_id}")
        return all_tasks

    def create_task(
        self,
        list_id: str,
        name: str,
        description: Optional[str] = None,
        assignees: Optional[List[int]] = None,
        tags: Optional[List[str]] = None,
        status: Optional[str] = None,
        priority: Optional[int] = None,
        due_date: Optional[int] = None,
        start_date: Optional[int] = None,
        time_estimate: Optional[int] = None,
        notify_all: bool = True,
        parent: Optional[str] = None,
        custom_fields: Optional[List[Dict[str, Any]]] = None
    ) -> ClickUpTask:
        data = {"name": name, "notify_all": notify_all}

        if description:
            data["description"] = description
        if assignees:
            data["assignees"] = assignees
        if tags:
            data["tags"] = tags
        if status:
            data["status"] = status
        if priority:
            data["priority"] = priority
        if due_date:
            data["due_date"] = due_date
        if start_date:
            data["start_date"] = start_date
        if time_estimate:
            data["time_estimate"] = time_estimate
        if parent:
            data["parent"] = parent
        if custom_fields:
            data["custom_fields"] = custom_fields

        response = self._request("POST", f"/list/{list_id}/task", json_data=data)
        self.logger.info(f"Created task '{name}' in list {list_id}")
        return ClickUpTask(**response)

    def update_task(
        self,
        task_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[int] = None,
        due_date: Optional[int] = None,
        start_date: Optional[int] = None,
        time_estimate: Optional[int] = None,
        assignees_add: Optional[List[int]] = None,
        assignees_remove: Optional[List[int]] = None,
        archived: Optional[bool] = None
    ) -> ClickUpTask:
        data = {}

        if name is not None:
            data["name"] = name
        if description is not None:
            data["description"] = description
        if status is not None:
            data["status"] = status
        if priority is not None:
            data["priority"] = priority
        if due_date is not None:
            data["due_date"] = due_date
        if start_date is not None:
            data["start_date"] = start_date
        if time_estimate is not None:
            data["time_estimate"] = time_estimate
        if archived is not None:
            data["archived"] = archived

        if assignees_add or assignees_remove:
            data["assignees"] = {}
            if assignees_add:
                data["assignees"]["add"] = assignees_add
            if assignees_remove:
                data["assignees"]["rem"] = assignees_remove

        response = self._request("PUT", f"/task/{task_id}", json_data=data)
        self.logger.info(f"Updated task {task_id}")
        return ClickUpTask(**response)

    def delete_task(self, task_id: str) -> bool:
        self._request("DELETE", f"/task/{task_id}")
        self.logger.info(f"Deleted task {task_id}")
        return True

    def get_task_comments(self, task_id: str, start: int = 0, start_id: Optional[str] = None) -> List[ClickUpComment]:
        params = {"start": start}
        if start_id:
            params["start_id"] = start_id

        response = self._request("GET", f"/task/{task_id}/comment", params=params)
        return [ClickUpComment(**comment) for comment in response.get("comments", [])]

    def create_task_comment(
        self,
        task_id: str,
        comment_text: str,
        assignee: Optional[int] = None,
        notify_all: bool = True
    ) -> ClickUpComment:
        data = {
            "comment_text": comment_text,
            "notify_all": notify_all
        }
        if assignee:
            data["assignee"] = assignee

        response = self._request("POST", f"/task/{task_id}/comment", json_data=data)
        self.logger.info(f"Added comment to task {task_id}")
        return ClickUpComment(**response)

    def search_tasks(
        self,
        team_id: str,
        query: str,
        page: int = 0,
        include_closed: bool = False
    ) -> List[ClickUpTask]:
        params = {
            "query": query,
            "page": page,
            "include_closed": str(include_closed).lower()
        }

        response = self._request("GET", f"/team/{team_id}/task", params=params)
        return [ClickUpTask(**task) for task in response.get("tasks", [])]

    def get_task_by_custom_id(self, team_id: str, custom_task_id: str) -> ClickUpTask:
        response = self._request(
            "GET",
            f"/task/{custom_task_id}",
            params={"custom_task_ids": "true", "team_id": team_id}
        )
        return ClickUpTask(**response)

    def get_custom_fields(self, list_id: str) -> List[Dict[str, Any]]:
        response = self._request("GET", f"/list/{list_id}/field")
        return response.get("fields", [])

    def set_custom_field_value(
        self,
        task_id: str,
        field_id: str,
        value: Any
    ) -> Dict[str, Any]:
        data = {"value": value}
        return self._request("POST", f"/task/{task_id}/field/{field_id}", json_data=data)

    def add_tag_to_task(self, task_id: str, tag_name: str) -> Dict[str, Any]:
        return self._request("POST", f"/task/{task_id}/tag/{tag_name}")

    def remove_tag_from_task(self, task_id: str, tag_name: str) -> Dict[str, Any]:
        return self._request("DELETE", f"/task/{task_id}/tag/{tag_name}")

    def close(self):
        self._client.close()
        self.logger.info("ClickUp client closed")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


class AsyncClickUpClient:
    BASE_URL = "https://api.clickup.com/api/v2"

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 3,
        base_delay: float = 1.0
    ):
        self.api_key = api_key or os.getenv("CLICKUP_API_KEY")
        if not self.api_key:
            raise ValueError("ClickUp API key is required. Set CLICKUP_API_KEY env var or pass api_key parameter.")

        self.timeout = timeout
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.logger = get_logger(__name__)

        self._client = httpx.AsyncClient(
            base_url=self.BASE_URL,
            headers={
                "Authorization": self.api_key,
                "Content-Type": "application/json"
            },
            timeout=timeout
        )

        self.logger.info("Async ClickUp client initialized")

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        import asyncio

        last_exception = None
        for attempt in range(self.max_retries):
            try:
                response = await self._client.request(
                    method=method,
                    url=endpoint,
                    params=params,
                    json=json_data
                )

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", self.base_delay * (2 ** attempt)))
                    self.logger.warning(f"Rate limited. Waiting {retry_after}s before retry {attempt + 1}/{self.max_retries}")
                    await asyncio.sleep(retry_after)
                    continue

                response.raise_for_status()
                return response.json()

            except httpx.HTTPStatusError as e:
                last_exception = e
                self.logger.warning(f"HTTP error {e.response.status_code} on attempt {attempt + 1}/{self.max_retries}: {str(e)}")

                if e.response.status_code >= 500:
                    delay = self.base_delay * (2 ** attempt)
                    await asyncio.sleep(delay)
                    continue
                raise

            except httpx.RequestError as e:
                last_exception = e
                self.logger.warning(f"Request error on attempt {attempt + 1}/{self.max_retries}: {str(e)}")
                delay = self.base_delay * (2 ** attempt)
                await asyncio.sleep(delay)
                continue

        raise RuntimeError(f"Failed after {self.max_retries} attempts: {str(last_exception)}")

    async def get_task(self, task_id: str, include_subtasks: bool = False) -> ClickUpTask:
        response = await self._request(
            "GET",
            f"/task/{task_id}",
            params={"include_subtasks": str(include_subtasks).lower()}
        )
        return ClickUpTask(**response)

    async def get_tasks(
        self,
        list_id: str,
        archived: bool = False,
        page: int = 0,
        order_by: str = "created",
        reverse: bool = False,
        subtasks: bool = False,
        statuses: Optional[List[str]] = None,
        include_closed: bool = False
    ) -> List[ClickUpTask]:
        params = {
            "archived": str(archived).lower(),
            "page": page,
            "order_by": order_by,
            "reverse": str(reverse).lower(),
            "subtasks": str(subtasks).lower(),
            "include_closed": str(include_closed).lower()
        }

        if statuses:
            params["statuses[]"] = statuses

        response = await self._request("GET", f"/list/{list_id}/task", params=params)
        return [ClickUpTask(**task) for task in response.get("tasks", [])]

    async def update_task(
        self,
        task_id: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[int] = None
    ) -> ClickUpTask:
        data = {}

        if name is not None:
            data["name"] = name
        if description is not None:
            data["description"] = description
        if status is not None:
            data["status"] = status
        if priority is not None:
            data["priority"] = priority

        response = await self._request("PUT", f"/task/{task_id}", json_data=data)
        self.logger.info(f"Updated task {task_id}")
        return ClickUpTask(**response)

    async def create_task_comment(
        self,
        task_id: str,
        comment_text: str,
        notify_all: bool = True
    ) -> ClickUpComment:
        data = {
            "comment_text": comment_text,
            "notify_all": notify_all
        }

        response = await self._request("POST", f"/task/{task_id}/comment", json_data=data)
        self.logger.info(f"Added comment to task {task_id}")
        return ClickUpComment(**response)

    async def close(self):
        await self._client.aclose()
        self.logger.info("Async ClickUp client closed")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
