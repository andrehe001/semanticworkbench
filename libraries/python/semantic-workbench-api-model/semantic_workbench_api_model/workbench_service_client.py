from __future__ import annotations

import io
import json
import types
import urllib.parse
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, AsyncGenerator, AsyncIterator, Callable, Iterable, Mapping, Self

import asgi_correlation_id
import httpx

from . import assistant_model, workbench_model

HEADER_ASSISTANT_SERVICE_ID = "X-Assistant-Service-ID"
HEADER_ASSISTANT_ID = "X-Assistant-ID"
HEADER_API_KEY = "X-API-Key"


# HTTPX transport factory can be overridden to return an ASGI transport for testing
def httpx_transport_factory() -> httpx.AsyncHTTPTransport:
    return httpx.AsyncHTTPTransport(retries=3)


@dataclass
class AssistantServiceRequestHeaders:
    assistant_service_id: str
    api_key: str

    def to_headers(self) -> Mapping[str, str]:
        return {
            HEADER_ASSISTANT_SERVICE_ID: self.assistant_service_id,
            HEADER_API_KEY: self.api_key,
        }

    @staticmethod
    def from_headers(headers: Mapping[str, str]) -> AssistantServiceRequestHeaders:
        return AssistantServiceRequestHeaders(
            assistant_service_id=headers.get(HEADER_ASSISTANT_SERVICE_ID) or "",
            api_key=headers.get(HEADER_API_KEY) or "",
        )


@dataclass
class AssistantRequestHeaders:
    assistant_id: uuid.UUID | None

    def to_headers(self) -> Mapping[str, str]:
        return {HEADER_ASSISTANT_ID: str(self.assistant_id)}

    @staticmethod
    def from_headers(headers: Mapping[str, str]) -> AssistantRequestHeaders:
        assistant_id: uuid.UUID | None = None
        with suppress(ValueError):
            assistant_id = uuid.UUID(headers.get(HEADER_ASSISTANT_ID) or "")
        return AssistantRequestHeaders(
            assistant_id=assistant_id,
        )


@dataclass
class UserRequestHeaders:
    token: str

    def to_headers(self) -> Mapping[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


class ConversationAPIClient:
    def __init__(
        self,
        conversation_id: str,
        httpx_client_factory: Callable[[], httpx.AsyncClient],
    ) -> None:
        self._conversation_id = conversation_id
        self._httpx_client_factory = httpx_client_factory

    @property
    def _client(self) -> httpx.AsyncClient:
        return self._httpx_client_factory()

    async def get_sse_session(self, event_source_url: str) -> AsyncIterator[dict]:
        async with self._client as client:
            async with client.stream("GET", event_source_url) as response:
                event = {}
                async for line in response.aiter_lines():
                    if line == "":
                        # End of the event; process and yield it
                        if "data" in event:
                            # Concatenate multiline data
                            data = event["data"]
                            event["data"] = json.loads(data)
                        yield event
                        event = {}
                    elif line.startswith(":"):
                        # Comment line; ignore
                        continue
                    else:
                        # Parse the field
                        field, value = line.split(":", 1)
                        value = value.lstrip()  # Remove leading whitespace
                        if field == "data":
                            # Handle multiline data
                            event.setdefault("data", "")
                            event["data"] += value + "\n"
                        else:
                            event[field] = value
                # Handle the last event if the stream ends without a blank line
                if event:
                    if "data" in event:
                        data = event["data"]
                        event["data"] = json.loads(data)
                    yield event

    async def delete_conversation(self) -> None:
        async with self._client as client:
            http_response = await client.delete(f"/conversations/{self._conversation_id}")
            if http_response.status_code == httpx.codes.NOT_FOUND:
                return
            http_response.raise_for_status()

    async def duplicate_conversation(
        self, new_conversation: workbench_model.NewConversation
    ) -> workbench_model.ConversationImportResult:
        async with self._client as client:
            http_response = await client.post(
                f"/conversations/{self._conversation_id}",
                json=new_conversation.model_dump(exclude_defaults=True, exclude_unset=True, mode="json"),
            )
            http_response.raise_for_status()
            return workbench_model.ConversationImportResult.model_validate(http_response.json())

    async def get_conversation(self) -> workbench_model.Conversation:
        async with self._client as client:
            http_response = await client.get(f"/conversations/{self._conversation_id}")
            http_response.raise_for_status()
            return workbench_model.Conversation.model_validate(http_response.json())

    async def update_conversation(self, metadata: dict[str, Any]) -> workbench_model.Conversation:
        async with self._client as client:
            http_response = await client.patch(
                f"/conversations/{self._conversation_id}",
                json=workbench_model.UpdateConversation(metadata=metadata).model_dump(
                    mode="json", exclude_unset=True, exclude_defaults=True
                ),
            )
            http_response.raise_for_status()
            return workbench_model.Conversation.model_validate(http_response.json())

    async def get_participant_me(self) -> workbench_model.ConversationParticipant:
        async with self._client as client:
            http_response = await client.get(f"/conversations/{self._conversation_id}/participants/me")
            http_response.raise_for_status()
            return workbench_model.ConversationParticipant.model_validate(http_response.json())

    async def get_participant(self, participant_id: str) -> workbench_model.ConversationParticipant:
        async with self._client as client:
            http_response = await client.get(
                f"/conversations/{self._conversation_id}/participants/{participant_id}",
                params={"include_inactive": True},
            )
            http_response.raise_for_status()
            return workbench_model.ConversationParticipant.model_validate(http_response.json())

    async def get_participants(self, *, include_inactive: bool = False) -> workbench_model.ConversationParticipantList:
        async with self._client as client:
            http_response = await client.get(
                f"/conversations/{self._conversation_id}/participants",
                params={"include_inactive": include_inactive},
            )
            if http_response.status_code == httpx.codes.NOT_FOUND:
                return workbench_model.ConversationParticipantList(participants=[])

            http_response.raise_for_status()
            return workbench_model.ConversationParticipantList.model_validate(http_response.json())

    async def update_participant(
        self,
        participant_id: str,
        participant: workbench_model.UpdateParticipant,
    ) -> workbench_model.ConversationParticipant:
        async with self._client as client:
            http_response = await client.patch(
                f"/conversations/{self._conversation_id}/participants/{participant_id}",
                json=participant.model_dump(exclude_defaults=True, exclude_unset=True, mode="json"),
            )
            http_response.raise_for_status()
            return workbench_model.ConversationParticipant.model_validate(http_response.json())

    async def update_participant_me(
        self,
        participant: workbench_model.UpdateParticipant,
    ) -> workbench_model.ConversationParticipant:
        return await self.update_participant(participant_id="me", participant=participant)

    async def get_message(
        self,
        message_id: uuid.UUID,
    ) -> workbench_model.ConversationMessage:
        async with self._client as client:
            http_response = await client.get(f"/conversations/{self._conversation_id}/messages/{message_id}")
            http_response.raise_for_status()
            return workbench_model.ConversationMessage.model_validate(http_response.json())

    async def get_messages(
        self,
        before: uuid.UUID | None = None,
        after: uuid.UUID | None = None,
        message_types: Iterable[workbench_model.MessageType] = (workbench_model.MessageType.chat,),
        participant_ids: Iterable[str] | None = None,
        participant_role: workbench_model.ParticipantRole | None = None,
        limit: int | None = None,
    ) -> workbench_model.ConversationMessageList:
        async with self._client as client:
            params: dict[str, str | list[str]] = {}
            if message_types:
                params["message_type"] = [mt.value for mt in message_types]
            if participant_ids:
                params["participant_id"] = list(participant_ids)
            if participant_role:
                params["participant_role"] = participant_role.value
            if before:
                params["before"] = str(before)
            if after:
                params["after"] = str(after)
            if limit:
                params["limit"] = str(limit)

            http_response = await client.get(f"/conversations/{self._conversation_id}/messages", params=params)
            http_response.raise_for_status()
            return workbench_model.ConversationMessageList.model_validate(http_response.json())

    async def send_messages(
        self,
        *messages: workbench_model.NewConversationMessage,
    ) -> workbench_model.ConversationMessageList:
        messages_out = []
        async with self._client as client:
            for message in messages:
                http_response = await client.post(
                    f"/conversations/{self._conversation_id}/messages",
                    json=message.model_dump(mode="json", exclude_unset=True, exclude_defaults=True),
                )
                http_response.raise_for_status()
                message_out = workbench_model.ConversationMessage.model_validate(http_response.json())
                messages_out.append(message_out)

        return workbench_model.ConversationMessageList(messages=messages_out)

    async def send_conversation_state_event(
        self,
        assistant_id: str,
        state_event: workbench_model.AssistantStateEvent,
    ) -> None:
        async with self._client as client:
            http_response = await client.post(
                f"/assistants/{assistant_id}/states/events",
                params={"conversation_id": self._conversation_id},
                json=state_event.model_dump(mode="json", exclude_unset=True, exclude_defaults=True),
            )
            http_response.raise_for_status()

    async def write_file(
        self,
        filename: str,
        file_content: io.BytesIO,
        content_type: str = "application/octet-stream",
    ) -> workbench_model.File:
        async with self._client as client:
            http_response = await client.put(
                f"/conversations/{self._conversation_id}/files",
                files=[("files", (filename, file_content, content_type))],
            )
            http_response.raise_for_status()

            file_list = workbench_model.FileList.model_validate(http_response.json())
            return file_list.files[0]

    @asynccontextmanager
    async def read_file(
        self,
        filename: str,
        chunk_size: int | None = None,
    ) -> AsyncGenerator[AsyncIterator[bytes], Any]:
        async with self._client as client:
            request = client.build_request("GET", f"/conversations/{self._conversation_id}/files/{filename}")
            http_response = await client.send(request, stream=True)
            http_response.raise_for_status()

            try:
                yield http_response.aiter_bytes(chunk_size)
            finally:
                await http_response.aclose()

    async def get_file(self, filename: str) -> workbench_model.File | None:
        async with self._client as client:
            params = {"prefix": filename}
            http_response = await client.get(f"/conversations/{self._conversation_id}/files", params=params)
            http_response.raise_for_status()

            files_response = workbench_model.FileList.model_validate(http_response.json())
            if not files_response.files:
                return None

            for file in files_response.files:
                if file.filename != filename:
                    continue

                return file

            return None

    async def get_files(self, prefix: str | None = None) -> workbench_model.FileList:
        async with self._client as client:
            params = {"prefix": prefix} if prefix else {}
            http_response = await client.get(f"/conversations/{self._conversation_id}/files", params=params)
            http_response.raise_for_status()

            return workbench_model.FileList.model_validate(http_response.json())

    async def file_exists(self, filename: str) -> bool:
        async with self._client as client:
            http_response = await client.get(f"/conversations/{self._conversation_id}/files/{filename}/versions")
            match http_response.status_code:
                case 200:
                    return True
                case 404:
                    return False
            http_response.raise_for_status()

        return False

    async def delete_file(self, filename: str) -> None:
        async with self._client as client:
            http_response = await client.delete(f"/conversations/{self._conversation_id}/files/{filename}")
            if http_response.status_code == httpx.codes.NOT_FOUND:
                return
            http_response.raise_for_status()

    async def update_file(
        self,
        filename: str,
        metadata: dict[str, Any],
    ) -> workbench_model.FileVersions:
        async with self._client as client:
            http_response = await client.patch(
                f"/conversations/{self._conversation_id}/files/{filename}",
                json=workbench_model.UpdateFile(metadata=metadata).model_dump(
                    mode="json", exclude_unset=True, exclude_defaults=True
                ),
            )
            http_response.raise_for_status()
            return workbench_model.FileVersions.model_validate(http_response.json())


class ConversationsAPIClient:
    def __init__(
        self,
        httpx_client_factory: Callable[[], httpx.AsyncClient],
    ) -> None:
        self._httpx_client_factory = httpx_client_factory

    @property
    def _client(self) -> httpx.AsyncClient:
        return self._httpx_client_factory()

    async def list_conversations(self) -> workbench_model.ConversationList:
        async with self._client as client:
            http_response = await client.get("/conversations")
            http_response.raise_for_status()
            return workbench_model.ConversationList.model_validate(http_response.json())

    async def create_conversation(
        self,
        new_conversation: workbench_model.NewConversation,
    ) -> workbench_model.Conversation:
        async with self._client as client:
            http_response = await client.post(
                "/conversations",
                json=new_conversation.model_dump(exclude_defaults=True, exclude_unset=True, mode="json"),
            )
            http_response.raise_for_status()
            return workbench_model.Conversation.model_validate(http_response.json())

    async def create_conversation_with_owner(
        self,
        new_conversation: workbench_model.NewConversation,
        owner_id: str,
    ) -> workbench_model.Conversation:
        async with self._client as client:
            http_response = await client.post(
                f"/conversations/{owner_id}",
                json=new_conversation.model_dump(exclude_defaults=True, exclude_unset=True, mode="json"),
            )
            http_response.raise_for_status()
            return workbench_model.Conversation.model_validate(http_response.json())

    async def create_conversation_share_with_owner(
        self,
        new_conversation_share: workbench_model.NewConversationShare,
        owner_id: str,
    ) -> workbench_model.ConversationShare:
        async with self._client as client:
            http_response = await client.post(
                f"/conversation-shares/{owner_id}",
                json=new_conversation_share.model_dump(exclude_defaults=True, exclude_unset=True, mode="json"),
            )
            http_response.raise_for_status()
            return workbench_model.ConversationShare.model_validate(http_response.json())

    async def delete_conversation(self, conversation_id: str) -> None:
        async with self._client as client:
            http_response = await client.delete(f"/conversations/{conversation_id}")
            if http_response.status_code == httpx.codes.NOT_FOUND:
                return
            http_response.raise_for_status()


class AssistantsAPIClient:
    def __init__(
        self,
        httpx_client_factory: Callable[[], httpx.AsyncClient],
    ) -> None:
        self._httpx_client_factory = httpx_client_factory

    @property
    def _client(self) -> httpx.AsyncClient:
        return self._httpx_client_factory()

    async def list_assistants(self) -> workbench_model.AssistantList:
        async with self._client as client:
            http_response = await client.get("/assistants")
            http_response.raise_for_status()
            return workbench_model.AssistantList.model_validate(http_response.json())

    async def create_assistant(self, new_assistant: workbench_model.NewAssistant) -> workbench_model.Assistant:
        async with self._client as client:
            http_response = await client.post(
                "/assistants",
                json=new_assistant.model_dump(exclude_defaults=True, exclude_unset=True, mode="json"),
            )
            http_response.raise_for_status()
            return workbench_model.Assistant.model_validate(http_response.json())

    async def delete_assistant(self, assistant_id: str) -> None:
        async with self._client as client:
            http_response = await client.delete(f"/assistants/{assistant_id}")
            if http_response.status_code == httpx.codes.NOT_FOUND:
                return
            http_response.raise_for_status()


class AssistantAPIClient:
    def __init__(
        self,
        assistant_id: str,
        httpx_client_factory: Callable[[], httpx.AsyncClient],
    ) -> None:
        self._assistant_id = assistant_id
        self._httpx_client_factory = httpx_client_factory

    @property
    def _client(self) -> httpx.AsyncClient:
        return self._httpx_client_factory()

    async def get_assistant(self) -> workbench_model.Assistant:
        async with self._client as client:
            http_response = await client.get(f"/assistants/{self._assistant_id}")
            http_response.raise_for_status()
            return workbench_model.Assistant.model_validate(http_response.json())

    async def delete_assistant(self) -> None:
        async with self._client as client:
            http_response = await client.delete(f"/assistants/{self._assistant_id}")
            if http_response.status_code == httpx.codes.NOT_FOUND:
                return
            http_response.raise_for_status()

    async def get_config(self) -> assistant_model.ConfigResponseModel:
        async with self._client as client:
            http_response = await client.get(f"/assistants/{self._assistant_id}/config")
            http_response.raise_for_status()
            return assistant_model.ConfigResponseModel.model_validate(http_response.json())

    async def update_config(self, config: assistant_model.ConfigPutRequestModel) -> assistant_model.ConfigResponseModel:
        async with self._client as client:
            http_response = await client.put(
                f"/assistants/{self._assistant_id}/config",
                json=config.model_dump(exclude_defaults=True, exclude_unset=True, mode="json"),
            )
            http_response.raise_for_status()
            return assistant_model.ConfigResponseModel.model_validate(http_response.json())


class AssistantServiceAPIClient:
    def __init__(
        self,
        httpx_client_factory: Callable[[], httpx.AsyncClient],
    ) -> None:
        self._client = httpx_client_factory()

    async def __aenter__(self) -> Self:
        self._client = await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: types.TracebackType | None = None,
    ) -> None:
        await self._client.__aexit__(exc_type, exc_value, traceback)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def update_registration_url(
        self,
        assistant_service_id: str,
        update: workbench_model.UpdateAssistantServiceRegistrationUrl,
    ) -> None:
        http_response = await self._client.put(
            f"/assistant-service-registrations/{assistant_service_id}",
            json=update.model_dump(mode="json", exclude_unset=True, exclude_defaults=True),
        )
        http_response.raise_for_status()

    async def get_assistant_services(self, user_ids: list[str]) -> workbench_model.AssistantServiceInfoList:
        http_response = await self._client.get(
            "/assistant-services",
            params={"user_id": user_ids},
        )
        http_response.raise_for_status()
        return workbench_model.AssistantServiceInfoList.model_validate(http_response.json())


class WorkbenchServiceClientBuilder:
    """Builder for assistant-services to create clients to interact with the Workbench service."""

    def __init__(
        self,
        base_url: str,
        assistant_service_id: str,
        api_key: str,
    ) -> None:
        self._base_url = base_url
        self._assistant_service_id = assistant_service_id
        self._api_key = api_key

    def _client(self, *headers: AssistantServiceRequestHeaders | AssistantRequestHeaders) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx_transport_factory(),
            base_url=self._base_url,
            timeout=httpx.Timeout(5.0, connect=10.0, read=60.0),
            headers={
                asgi_correlation_id.CorrelationIdMiddleware.header_name: urllib.parse.quote(
                    asgi_correlation_id.correlation_id.get() or ""
                ),
            },
        )
        for header in headers:
            client.headers.update(header.to_headers())
        return client

    def for_service(self) -> AssistantServiceAPIClient:
        return AssistantServiceAPIClient(
            httpx_client_factory=lambda: self._client(
                AssistantServiceRequestHeaders(
                    assistant_service_id=self._assistant_service_id,
                    api_key=self._api_key,
                ),
            ),
        )

    def for_conversation(self, assistant_id: str, conversation_id: str) -> ConversationAPIClient:
        return ConversationAPIClient(
            conversation_id=conversation_id,
            httpx_client_factory=lambda: self._client(
                AssistantServiceRequestHeaders(
                    assistant_service_id=self._assistant_service_id,
                    api_key=self._api_key,
                ),
                AssistantRequestHeaders(assistant_id=uuid.UUID(assistant_id)),
            ),
        )

    def for_conversations(self, assistant_id: str | None = None) -> ConversationsAPIClient:
        if assistant_id is None:
            return ConversationsAPIClient(
                httpx_client_factory=lambda: self._client(
                    AssistantServiceRequestHeaders(
                        assistant_service_id=self._assistant_service_id,
                        api_key=self._api_key,
                    ),
                ),
            )

        return ConversationsAPIClient(
            httpx_client_factory=lambda: self._client(
                AssistantServiceRequestHeaders(
                    assistant_service_id=self._assistant_service_id,
                    api_key=self._api_key,
                ),
                AssistantRequestHeaders(assistant_id=uuid.UUID(assistant_id)),
            ),
        )


class WorkbenchServiceUserClientBuilder:
    """Builder for users to create clients to interact with the Workbench service."""

    def __init__(
        self,
        base_url: str,
        headers: UserRequestHeaders,
    ) -> None:
        self._base_url = base_url
        self._headers = headers

    def _client(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx_transport_factory())
        client.base_url = self._base_url
        client.timeout.connect = 10
        client.timeout.read = 60
        client.headers.update({
            **self._headers.to_headers(),
            asgi_correlation_id.CorrelationIdMiddleware.header_name: asgi_correlation_id.correlation_id.get() or "",
        })
        return client

    def for_assistants(self) -> AssistantsAPIClient:
        return AssistantsAPIClient(httpx_client_factory=self._client)

    def for_assistant(self, assistant_id: str) -> AssistantAPIClient:
        return AssistantAPIClient(assistant_id=assistant_id, httpx_client_factory=self._client)

    def for_conversations(self) -> ConversationsAPIClient:
        return ConversationsAPIClient(httpx_client_factory=self._client)

    def for_conversation(self, conversation_id: str) -> ConversationAPIClient:
        return ConversationAPIClient(
            conversation_id=conversation_id,
            httpx_client_factory=self._client,
        )
