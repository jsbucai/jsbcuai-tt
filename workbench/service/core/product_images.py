"""Safe metadata and on-demand cache for the single ERP product image.

ERP credentials never enter image requests.  The live account's product image
host was verified separately; keeping a one-host allowlist also prevents an
ERP text field from becoming a general purpose server-side request primitive.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IMAGE_HOST = "erp.qiniu.wsgjp.com"
MAX_IMAGE_BYTES = 12 * 1024 * 1024


class ProductImageUnavailable(Exception):
    pass


def canonical_image_url(value):
    """Return the only approved canonical image URL, or an empty string."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    try:
        allowed_origin = (parsed.scheme in {"http", "https"} and
                          (parsed.hostname or "").lower() == IMAGE_HOST and
                          not parsed.username and not parsed.password and
                          parsed.port in {None, 443, 80})
    except ValueError:
        return ""
    if not allowed_origin:
        return ""
    if not parsed.path.startswith("/") or "\\" in parsed.path:
        return ""
    # The verified origin supports HTTPS even though ptype.list currently emits
    # an HTTP URL.  Fragments are client-only and never belong in cache identity.
    return urllib.parse.urlunsplit(("https", IMAGE_HOST, parsed.path, parsed.query, ""))


def image_id(profile_id, product_id, source_url):
    parts = f"product-image-v1\0{profile_id}\0{product_id}\0{source_url}"
    return hashlib.sha256(parts.encode()).hexdigest()[:32]


def image_metadata(profile_id, products, seen_at):
    output = []
    invalid = 0
    for product in products:
        product_id = str(product.get("ptypeid") or "").strip()
        raw = str(product.get("picurl") or "").strip()
        if not raw:
            continue
        source_url = canonical_image_url(raw)
        if not product_id or not source_url:
            invalid += 1
            continue
        output.append({
            "image_id": image_id(str(profile_id), product_id, source_url),
            "profile_id": str(profile_id),
            "product_id": product_id,
            "source_url": source_url,
            "last_seen_at": str(seen_at),
        })
    output.sort(key=lambda item: (item["product_id"], item["image_id"]))
    return output, invalid


def persist_image_metadata(database, profile_id, items):
    """Replace one profile's metadata while retaining unchanged cached bytes."""
    profile = str(profile_id)
    wanted = set()
    for item in items:
        if str(item.get("profile_id")) != profile:
            raise ValueError("商品图片账套身份不一致")
        ident = str(item["image_id"])
        wanted.add(ident)
        database.execute(
            """INSERT INTO product_images(
                image_id,profile_id,product_id,source_url,last_seen_at
            ) VALUES(?,?,?,?,?)
            ON CONFLICT(profile_id,product_id) DO UPDATE SET
                image_id=excluded.image_id,
                source_url=excluded.source_url,
                mime_type=CASE WHEN product_images.image_id=excluded.image_id THEN product_images.mime_type ELSE '' END,
                etag=CASE WHEN product_images.image_id=excluded.image_id THEN product_images.etag ELSE '' END,
                last_modified=CASE WHEN product_images.image_id=excluded.image_id THEN product_images.last_modified ELSE '' END,
                content_sha256=CASE WHEN product_images.image_id=excluded.image_id THEN product_images.content_sha256 ELSE '' END,
                content_length=CASE WHEN product_images.image_id=excluded.image_id THEN product_images.content_length ELSE 0 END,
                width=CASE WHEN product_images.image_id=excluded.image_id THEN product_images.width ELSE NULL END,
                height=CASE WHEN product_images.image_id=excluded.image_id THEN product_images.height ELSE NULL END,
                cache_path=CASE WHEN product_images.image_id=excluded.image_id THEN product_images.cache_path ELSE '' END,
                status=CASE WHEN product_images.image_id=excluded.image_id THEN product_images.status ELSE 'pending' END,
                last_seen_at=excluded.last_seen_at,
                fetched_at=CASE WHEN product_images.image_id=excluded.image_id THEN product_images.fetched_at ELSE NULL END,
                error=''
            """,
            (ident, profile, str(item["product_id"]), str(item["source_url"]),
             str(item["last_seen_at"])),
        )
    if wanted:
        marks = ",".join("?" for _ in wanted)
        database.execute(
            f"DELETE FROM product_images WHERE profile_id=? AND image_id NOT IN ({marks})",
            (profile, *sorted(wanted)),
        )
    else:
        database.execute("DELETE FROM product_images WHERE profile_id=?", (profile,))


@dataclass(frozen=True)
class ImageContent:
    path: Path
    mime_type: str
    etag: str
    last_modified: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _public_addresses(host):
    try:
        values = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except OSError as error:
        raise ProductImageUnavailable("商品图片域名暂时无法解析") from error
    if not values:
        raise ProductImageUnavailable("商品图片域名暂时无法解析")
    for value in values:
        address = ipaddress.ip_address(value)
        if (address.is_private or address.is_loopback or address.is_link_local or
                address.is_multicast or address.is_reserved or address.is_unspecified):
            raise ProductImageUnavailable("商品图片地址不属于允许的公网来源")
    return values


def _mime(data, header):
    value = str(header or "").split(";", 1)[0].strip().lower()
    detected = ""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif data.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        detected = "image/webp"
    # The ERP image CDN can return a valid PNG body with the legacy
    # ``image/pjpeg`` header.  The fixed, allowlisted origin and the file magic
    # remain the security boundary; a known image-ish header is only a hint and
    # must not override the detected type.
    supported_headers = {
        "image/png": {"", "application/octet-stream", "image/png", "image/pjpeg"},
        "image/jpeg": {"", "application/octet-stream", "image/jpeg", "image/pjpeg"},
        "image/webp": {"", "application/octet-stream", "image/webp"},
    }
    if not detected or value not in supported_headers[detected]:
        raise ProductImageUnavailable("ERP 商品图片格式无效")
    return detected


def fetch_https_image(url, headers=None):
    canonical = canonical_image_url(url)
    if not canonical or canonical != url:
        raise ProductImageUnavailable("商品图片来源未通过验证")
    _public_addresses(IMAGE_HOST)
    request = urllib.request.Request(canonical, headers={
        "Accept": "image/png,image/jpeg,image/webp",
        "User-Agent": "UL-Workbench-Image-Cache/1",
        **(headers or {}),
    })
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=15) as response:
            if response.status != 200:
                raise ProductImageUnavailable("ERP 商品图片返回异常")
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_IMAGE_BYTES:
                raise ProductImageUnavailable("ERP 商品图片超过大小限制")
            data = response.read(MAX_IMAGE_BYTES + 1)
            if len(data) > MAX_IMAGE_BYTES:
                raise ProductImageUnavailable("ERP 商品图片超过大小限制")
            return {
                "data": data,
                "mime_type": _mime(data, response.headers.get("Content-Type")),
                "etag": str(response.headers.get("ETag") or ""),
                "last_modified": str(response.headers.get("Last-Modified") or ""),
            }
    except urllib.error.HTTPError as error:
        if error.code == 304:
            return {"not_modified": True, "etag": str(error.headers.get("ETag") or ""),
                    "last_modified": str(error.headers.get("Last-Modified") or "")}
        raise ProductImageUnavailable("ERP 商品图片返回异常") from error
    except ProductImageUnavailable:
        raise
    except (urllib.error.URLError, ValueError, OSError) as error:
        raise ProductImageUnavailable("ERP 商品图片暂时不可用") from error


class ProductImageCache:
    def __init__(self, store, fetcher=fetch_https_image):
        self.store = store
        self.fetcher = fetcher
        self.cache_dir = (store.runtime / "product-images").resolve()
        if not self.cache_dir.is_relative_to(store.runtime.resolve()):
            raise ValueError("商品图片缓存目录无效")
        self.cache_dir.mkdir(mode=0o700, exist_ok=True)

    def list_for_product(self, product_id):
        product = str(product_id or "").strip()
        if not product:
            raise ValueError("商品 ID 不能为空")
        with self.store.connect() as database:
            rows = database.execute(
                """SELECT * FROM product_images WHERE product_id=?
                   ORDER BY last_seen_at DESC LIMIT 1""", (product,),
            ).fetchall()
        return {
            "product_id": product,
            "items": [{
                "id": row["image_id"],
                "product_id": row["product_id"],
                "content_url": f'/api/v2/product-images/{row["image_id"]}/content',
                "mime_type": row["mime_type"],
                "width": row["width"],
                "height": row["height"],
                "etag": row["etag"] or row["content_sha256"],
                "updated_at": row["fetched_at"] or row["last_seen_at"],
            } for row in rows],
        }

    def _cached(self, row):
        if not row["cache_path"] or not row["content_sha256"]:
            return None
        path = (self.cache_dir / row["cache_path"]).resolve()
        if not path.is_relative_to(self.cache_dir) or not path.is_file():
            return None
        data = path.read_bytes()
        if len(data) != row["content_length"] or hashlib.sha256(data).hexdigest() != row["content_sha256"]:
            return None
        # Use our content hash for HTTP validation.  Vendor ETags are retained
        # as metadata but are opaque and are not guaranteed to satisfy
        # Werkzeug's response-header grammar.
        return ImageContent(path, row["mime_type"], row["content_sha256"],
                            row["last_modified"])

    @staticmethod
    def _seen_after_fetch(row):
        if not row["fetched_at"]:
            return True
        try:
            seen = datetime.fromisoformat(row["last_seen_at"])
            fetched = datetime.fromisoformat(row["fetched_at"])
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            if fetched.tzinfo is None:
                fetched = fetched.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            return seen > fetched
        except (TypeError, ValueError):
            return True

    def content(self, identity):
        image = str(identity or "").strip()
        with self.store.connect() as database:
            row = database.execute("SELECT * FROM product_images WHERE image_id=?", (image,)).fetchone()
        if row is None:
            raise KeyError("商品图片不存在")
        cached = self._cached(row)
        if cached and not self._seen_after_fetch(row):
            return cached
        try:
            conditional = {}
            if cached and row["etag"]:
                conditional["If-None-Match"] = row["etag"]
            if cached and row["last_modified"]:
                conditional["If-Modified-Since"] = row["last_modified"]
            fetched = self.fetcher(row["source_url"], conditional)
            now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
            if fetched.get("not_modified") is True:
                if cached is None:
                    raise ProductImageUnavailable("ERP 商品图片缓存不完整")
                with self.store.connect(write=True) as database:
                    database.execute(
                        """UPDATE product_images SET fetched_at=?,status='ready',error='',
                           etag=CASE WHEN ?<>'' THEN ? ELSE etag END,
                           last_modified=CASE WHEN ?<>'' THEN ? ELSE last_modified END
                           WHERE image_id=?""",
                        (now, str(fetched.get("etag") or ""), str(fetched.get("etag") or ""),
                         str(fetched.get("last_modified") or ""),
                         str(fetched.get("last_modified") or ""), image),
                    )
                return cached
            data = bytes(fetched["data"])
            mime_type = _mime(data, fetched.get("mime_type"))
            digest = hashlib.sha256(data).hexdigest()
            filename = image + ".bin"
            target = (self.cache_dir / filename).resolve()
            if not target.is_relative_to(self.cache_dir):
                raise ProductImageUnavailable("商品图片缓存路径无效")
            temporary = target.with_name(target.name + ".tmp-" + os.urandom(6).hex())
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, target)
            finally:
                if temporary.exists():
                    temporary.unlink()
            with self.store.connect(write=True) as database:
                result = database.execute(
                    """UPDATE product_images SET mime_type=?,etag=?,last_modified=?,
                       content_sha256=?,content_length=?,cache_path=?,status='ready',
                       fetched_at=?,error='' WHERE image_id=? AND source_url=?""",
                    (mime_type, str(fetched.get("etag") or ""),
                     str(fetched.get("last_modified") or ""), digest, len(data),
                     filename, now, image, row["source_url"]),
                )
                if result.rowcount != 1:
                    target.unlink(missing_ok=True)
                    raise ProductImageUnavailable("商品图片元数据已更新，请重新打开")
            return ImageContent(target, mime_type, digest,
                                str(fetched.get("last_modified") or ""))
        except ProductImageUnavailable as error:
            # A valid old file is preferred to a transient upstream failure.
            cached = self._cached(row)
            if cached:
                return cached
            with self.store.connect(write=True) as database:
                database.execute(
                    "UPDATE product_images SET status='failed',error=? WHERE image_id=?",
                    (str(error)[:300], image),
                )
            raise
