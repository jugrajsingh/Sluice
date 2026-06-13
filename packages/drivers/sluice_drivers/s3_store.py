from __future__ import annotations

import aioboto3
from botocore.exceptions import ClientError
from sluice_core.errors import KeyNotFound


class S3ObjectStore:
    """ObjectStore over S3 / S3-compatible (MinIO via endpoint_url)."""

    def __init__(
        self,
        *,
        bucket: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
    ) -> None:
        self._bucket = bucket
        self._session = aioboto3.Session(
            aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name=region
        )
        self._kw = {"endpoint_url": endpoint_url} if endpoint_url else {}

    def _client(self):
        return self._session.client("s3", **self._kw)

    async def ensure_bucket(self) -> None:
        async with self._client() as c:
            try:
                await c.create_bucket(Bucket=self._bucket)
            except ClientError:
                pass

    async def put(self, key: str, data: bytes, *, content_type: str | None = None) -> None:
        async with self._client() as c:
            await c.put_object(
                Bucket=self._bucket, Key=key, Body=data, **({"ContentType": content_type} if content_type else {})
            )

    async def get(self, key: str) -> bytes:
        async with self._client() as c:
            try:
                resp = await c.get_object(Bucket=self._bucket, Key=key)
            except ClientError as e:
                if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
                    raise KeyNotFound(key) from e
                raise
            async with resp["Body"] as stream:
                return await stream.read()

    async def exists(self, key: str) -> bool:
        async with self._client() as c:
            try:
                await c.head_object(Bucket=self._bucket, Key=key)
                return True
            except ClientError:
                return False

    async def delete(self, key: str) -> None:
        async with self._client() as c:
            await c.delete_object(Bucket=self._bucket, Key=key)

    async def signed_url(self, key: str, *, method: str = "GET", expires_s: int) -> str:
        op = "put_object" if method.upper() == "PUT" else "get_object"
        async with self._client() as c:
            return await c.generate_presigned_url(op, Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=expires_s)

    async def list_keys(self, prefix: str) -> list[str]:
        async with self._client() as c:
            out: list[str] = []
            paginator = c.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                out += [o["Key"] for o in page.get("Contents", [])]
            return sorted(out)
