from __future__ import annotations

import aioboto3
from sluice_core.models import Message, QueueDepth


class SqsQueue:
    """Queue over AWS SQS. ack_token = ReceiptHandle; source = queue name."""

    def __init__(
        self,
        *,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        default_lease_s: int = 30,
    ) -> None:
        self._session = aioboto3.Session(
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name=region
        )
        self._kw = {"endpoint_url": endpoint_url} if endpoint_url else {}
        self._lease = default_lease_s
        self._urls: dict[str, str] = {}

    def _client(self):
        return self._session.client("sqs", **self._kw)

    async def _url(self, name: str) -> str:
        if name not in self._urls:
            async with self._client() as c:
                self._urls[name] = (await c.get_queue_url(QueueName=name))["QueueUrl"]
        return self._urls[name]

    async def ensure_queue(self, name: str) -> None:
        async with self._client() as c:
            self._urls[name] = (await c.create_queue(QueueName=name))["QueueUrl"]

    async def enqueue(self, dest: str, body: bytes, *, attributes: dict[str, str] | None = None) -> str:
        async with self._client() as c:
            resp = await c.send_message(QueueUrl=await self._url(dest), MessageBody=body.decode("latin-1"))
            return resp["MessageId"]

    async def receive(self, source: str, *, max_messages: int, wait_seconds: int) -> list[Message]:
        async with self._client() as c:
            resp = await c.receive_message(
                QueueUrl=await self._url(source),
                MaxNumberOfMessages=min(max_messages, 10),
                WaitTimeSeconds=min(wait_seconds, 20),
                VisibilityTimeout=self._lease,
                AttributeNames=["ApproximateReceiveCount"],
            )
            return [
                Message(
                    id=m["MessageId"],
                    body=m["Body"].encode("latin-1"),
                    ack_token=m["ReceiptHandle"],
                    receive_count=int(m.get("Attributes", {}).get("ApproximateReceiveCount", 1)),
                )
                for m in resp.get("Messages", [])
            ]

    async def ack(self, source: str, msg: Message) -> None:
        async with self._client() as c:
            await c.delete_message(QueueUrl=await self._url(source), ReceiptHandle=msg.ack_token)

    async def nack(self, source: str, msg: Message) -> None:
        async with self._client() as c:
            await c.change_message_visibility(
                QueueUrl=await self._url(source), ReceiptHandle=msg.ack_token, VisibilityTimeout=0
            )

    async def extend_lease(self, source: str, msg: Message, seconds: int) -> None:
        async with self._client() as c:
            await c.change_message_visibility(
                QueueUrl=await self._url(source), ReceiptHandle=msg.ack_token, VisibilityTimeout=seconds
            )

    async def depth(self, source: str) -> QueueDepth:
        async with self._client() as c:
            a = (
                await c.get_queue_attributes(
                    QueueUrl=await self._url(source),
                    AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
                )
            )["Attributes"]
            return QueueDepth(
                visible=int(a.get("ApproximateNumberOfMessages", 0)),
                in_flight=int(a.get("ApproximateNumberOfMessagesNotVisible", 0)),
            )
