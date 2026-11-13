"""Storage (ADR-008): InMemory, tabla `files` y contrato boto3 contra Garage (obligatorio en CI)."""

import io
import os
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError
from django.test import override_settings

from apps.files.models import File
from core.ids import new_id
from core.storage import MAX_PRESIGN_SECONDS, disposition_header, storage, tenant_key
from core.storage.memory import InMemoryStorage
from core.storage.s3 import S3CompatibleStorage, from_settings
from core.tenancy.context import TenantContext
from core.tenancy.scope import tenant_scope
from tests.test_tenancy import raw_count

CONTRACT = bool(os.environ.get("STORAGE_ENDPOINT_URL"))


def test_tenant_keys_and_short_presigned_urls() -> None:
    org, file_id = new_id(), new_id()
    key = tenant_key(org, "attachment", file_id, datetime(2026, 9, 30, tzinfo=UTC))
    assert key == f"org/{org}/attachment/2026/09/{file_id}"  # sin nombre original
    with pytest.raises(ValueError):
        tenant_key(org, "../etc", file_id)
    store = InMemoryStorage()
    info = store.put(key, io.BytesIO(b"hola"), content_type="text/plain", content_length=4)
    assert store.head(key) == info and store.open(key).read() == b"hola"
    for expires_in in (0, MAX_PRESIGN_SECONDS + 1):
        with pytest.raises(ValueError):
            store.presign_get(key, expires_in=expires_in, filename="a", content_type="text/plain")
    for bad in (f"org/{org}/attachment/../x", "otro/nombre.pdf", key + "/foto.png"):
        with pytest.raises(ValueError):
            store.put(bad, io.BytesIO(b""), content_type="text/plain", content_length=0)
    store.delete(key)
    assert store.head(key) is None


def test_content_disposition_is_safe() -> None:
    h = disposition_header('../"foto ñ".png', "inline", "image/png")
    assert h == "inline; filename=\"..foto .png\"; filename*=UTF-8''..%2F%22foto%20%C3%B1%22.png"
    for content_type in ("text/html", "image/svg+xml", "application/octet-stream"):  # activo
        assert disposition_header("x", "inline", content_type).startswith("attachment;")


def test_storage_backend_comes_from_settings() -> None:
    assert isinstance(storage(), InMemoryStorage)
    with override_settings(STORAGE_BACKEND="s3", STORAGE_BUCKET=""):
        pytest.raises(ImproperlyConfigured, storage)


def test_only_the_urls_for_the_browser_are_signed_with_the_public_endpoint() -> None:
    client, signer, k = MagicMock(), MagicMock(), key()
    s3 = S3CompatibleStorage(bucket="b", client=client, signer=signer)
    s3.put(k, io.BytesIO(b"x"), content_type="image/png", content_length=1)
    s3.open(k)
    s3.head(k)
    s3.delete(k)
    assert not signer.method_calls  # leer y escribir el objeto: siempre el endpoint interno
    s3.presign_get(k, expires_in=60, filename="a.png", content_type="image/png")
    s3.presign_put(k, expires_in=60, content_type="image/png", content_length=1, max_bytes=9)
    assert [c[1][0] for c in signer.method_calls] == ["get_object", "put_object"]
    assert not client.generate_presigned_url.called
    alone = MagicMock()  # sin endpoint público, firma el mismo cliente
    S3CompatibleStorage(bucket="b", client=alone).presign_get(
        k, expires_in=60, filename="a.png", content_type="image/png"
    )
    assert alone.generate_presigned_url.called


@pytest.mark.parametrize("public", ["http://localhost:3900", ""])
def test_the_public_endpoint_signs_and_the_internal_one_still_talks_to_the_bucket(
    public: str,
) -> None:
    store = {"STORAGE_BUCKET": "b", "STORAGE_ACCESS_KEY_ID": "k", "STORAGE_SECRET_ACCESS_KEY": "s"}
    with override_settings(
        STORAGE_ENDPOINT_URL="http://garage:3900", STORAGE_PUBLIC_ENDPOINT_URL=public, **store
    ):
        s3 = from_settings()  # firmar no toca la red
    url = s3.presign_get(key(), expires_in=60, filename="a.png", content_type="image/png")
    assert urlsplit(url).netloc == urlsplit(public or "http://garage:3900").netloc
    assert s3.client.meta.endpoint_url == "http://garage:3900"


@pytest.mark.usefixtures("tenant_db")
def test_files_rows_are_tenant_scoped_and_keys_carry_the_tenant(orgs: dict[str, UUID]) -> None:
    def row(key: str, purpose: str = "attachment", sha256: str = "0" * 64) -> File:
        return File(storage_key=key, original_name="a.pdf", mime_type="application/pdf",
                    size_bytes=4, sha256=sha256, purpose=purpose)  # fmt: skip

    with tenant_scope(TenantContext(orgs["A"], "test")):
        row(tenant_key(orgs["A"], "attachment", new_id())).save()
    for bad in (
        row(tenant_key(orgs["B"], "attachment", new_id())),  # prefijo de otro tenant
        row(f"org/{orgs['A']}/attachment/../x"),  # forma libre / traversal
        row(tenant_key(orgs["A"], "kb", new_id())),  # purpose de la clave distinto de la columna
        row(tenant_key(orgs["A"], "attachment", new_id()), sha256="nope"),
    ):
        with pytest.raises(IntegrityError), tenant_scope(TenantContext(orgs["A"], "test")):
            bad.save()  # CHECKs en BD
    with tenant_scope(TenantContext(orgs["B"], "test")):
        assert raw_count("files") == 0  # RLS


@pytest.fixture
def s3() -> Iterator[S3CompatibleStorage]:
    if not CONTRACT:
        if os.environ.get("CI"):
            pytest.fail("CI sin STORAGE_ENDPOINT_URL: la suite de contrato es obligatoria")
        pytest.skip("Sin emulador S3 (STORAGE_ENDPOINT_URL)")
    yield from_settings()


def key() -> str:
    return tenant_key(new_id(), "attachment", new_id())


def fetch(url: str, method: str = "GET", data: bytes | None = None, **headers: str) -> Any:
    request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
    return urllib.request.urlopen(request, timeout=10)  # noqa: S310 — emulador local/CI


def test_contract_create_bucket(s3: S3CompatibleStorage) -> None:
    bucket = f"crm-contract-{new_id().hex[:12]}"
    s3.client.create_bucket(Bucket=bucket)
    s3.client.head_bucket(Bucket=bucket)
    s3.client.delete_bucket(Bucket=bucket)


def test_contract_put_get_head_delete_and_content_type(s3: S3CompatibleStorage) -> None:
    k = key()
    s3.put(k, io.BytesIO(b"%PDF-1.7"), content_type="application/pdf", content_length=8,
           metadata={"sha256": "abc"})  # fmt: skip
    info = s3.head(k)
    assert info is not None and (info.size, info.content_type) == (8, "application/pdf")
    assert info.metadata == {"sha256": "abc"} and s3.open(k).read() == b"%PDF-1.7"
    s3.delete(k)
    assert s3.head(k) is None


def test_contract_list_objects_v2(s3: S3CompatibleStorage) -> None:
    prefix = f"org/{new_id()}/"
    keys = sorted(f"{prefix}kb/2026/09/{new_id()}" for _ in range(3))
    for k in keys:
        s3.put(k, io.BytesIO(b"x"), content_type="text/plain", content_length=1)
    listed = s3.client.list_objects_v2(Bucket=s3.bucket, Prefix=prefix)
    assert [o["Key"] for o in listed["Contents"]] == keys
    for k in keys:
        s3.delete(k)


def test_contract_multipart_upload_and_abort(s3: S3CompatibleStorage) -> None:
    k, c = key(), s3.client
    upload = c.create_multipart_upload(Bucket=s3.bucket, Key=k, ContentType="application/zip")
    parts = []
    for number, body in enumerate((b"a" * 5 * 1024 * 1024, b"fin"), start=1):
        part = c.upload_part(Bucket=s3.bucket, Key=k, UploadId=upload["UploadId"],
                             PartNumber=number, Body=body)  # fmt: skip
        parts.append({"ETag": part["ETag"], "PartNumber": number})
    c.complete_multipart_upload(Bucket=s3.bucket, Key=k, UploadId=upload["UploadId"],
                                MultipartUpload={"Parts": parts})  # fmt: skip
    info = s3.head(k)
    assert info is not None and info.size == 5 * 1024 * 1024 + 3
    s3.delete(k)
    aborted = c.create_multipart_upload(Bucket=s3.bucket, Key=key())
    c.abort_multipart_upload(Bucket=s3.bucket, Key=aborted["Key"], UploadId=aborted["UploadId"])
    pending = c.list_multipart_uploads(Bucket=s3.bucket).get("Uploads", [])
    assert aborted["UploadId"] not in {u["UploadId"] for u in pending}


def test_contract_presigned_get_and_put(s3: S3CompatibleStorage) -> None:
    k, png = key(), {"content_type": "image/png", "max_bytes": 1024}
    for declared in (0, 2048):  # vacío o por encima de la política: no se emite URL
        with pytest.raises(ValueError):
            s3.presign_put(k, expires_in=60, content_length=declared, **png)  # type: ignore[arg-type]
    upload = s3.presign_put(k, expires_in=60, content_length=4, **png)  # type: ignore[arg-type]
    assert upload.headers == {"Content-Type": "image/png", "Content-Length": "4"}
    with pytest.raises(urllib.error.HTTPError, match="403"):  # tamaño firmado ≠ enviado
        fetch(upload.url, "PUT", b"x" * 5000, **{**upload.headers, "Content-Length": "5000"})
    assert s3.head(k) is None
    with fetch(upload.url, "PUT", b"\x89PNG", **upload.headers) as response:
        assert response.status == 200
    info = s3.head(k)  # "finalize": segunda barrera sobre el tamaño y el tipo reales
    assert info is not None and (info.size, info.content_type) == (4, "image/png")
    url = s3.presign_get(k, expires_in=60, filename="foto.png", content_type="image/png")
    with fetch(url) as response:
        assert response.read() == b"\x89PNG" and response.headers["Content-Type"] == "image/png"
        assert response.headers["Content-Disposition"].startswith('attachment; filename="foto.png"')
        assert response.headers["Cache-Control"] == "private, no-store"  # firmado en la URL
    with pytest.raises(ValueError):
        s3.presign_get(k, expires_in=3600, filename="foto.png", content_type="image/png")
    s3.delete(k)


def test_contract_a_url_signed_for_the_public_host_works_only_through_that_host(
    s3: S3CompatibleStorage,
) -> None:
    """SigV4 firma `Host`: Garage acepta la URL por el host público y la rechaza por otro."""
    internal = urlsplit(settings.STORAGE_ENDPOINT_URL)
    alias = {"127.0.0.1": "localhost", "localhost": "127.0.0.1"}[internal.hostname or ""]
    public = f"{internal.scheme}://{alias}:{internal.port}"
    with override_settings(STORAGE_PUBLIC_ENDPOINT_URL=public):
        browser = from_settings()
    k = key()
    browser.put(k, io.BytesIO(b"\x89PNG"), content_type="image/png", content_length=4)
    url = browser.presign_get(k, expires_in=60, filename="a.png", content_type="image/png")
    assert urlsplit(url).netloc == f"{alias}:{internal.port}"
    with fetch(url) as response:
        assert response.read() == b"\x89PNG"
    elsewhere = urlunsplit(urlsplit(url)._replace(netloc=internal.netloc))
    with pytest.raises(urllib.error.HTTPError, match="403"):  # el mismo URL por otro host
        fetch(elsewhere)
    s3.delete(k)


def test_contract_active_content_is_never_served_inline(s3: S3CompatibleStorage) -> None:
    k = key()  # el cliente subió HTML; files.mime_type (magic bytes) decide cómo se sirve
    s3.put(k, io.BytesIO(b"<script>"), content_type="text/html", content_length=8)
    url = s3.presign_get(k, expires_in=60, filename="x.html", content_type="text/plain",
                         disposition="inline")  # fmt: skip
    with fetch(url) as response:
        assert response.headers["Content-Type"] == "text/plain"
        assert response.headers["Content-Disposition"].startswith("attachment;")
    s3.delete(k)
