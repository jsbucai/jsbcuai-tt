import base64
import hashlib
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.product_images import (
    ProductImageCache, ProductImageUnavailable, _mime, canonical_image_url,
    image_metadata, persist_image_metadata,
)
from core.store import Store
from app import create_app


REAL_1X1_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/"
    "AAX+Av4N70a4AAAAAElFTkSuQmCC"
)
REAL_1X1_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAASABIAAD/4QBMRXhpZgAATU0AKgAAAAgAAYdpAAQAAAAB"
    "AAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAAaADAAQAAAABAAAAAQAA"
    "AAD/7QA4UGhvdG9zaG9wIDMuMAA4QklNBAQAAAAAAAA4QklNBCUAAAAAABDUHYzZ"
    "jwCyBOmACZjs+EJ+/8AAEQgAAQABAwEiAAIRAQMRAf/EAB8AAAEFAQEBAQEBAAAA"
    "AAAAAAABAgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAEEQUSITFB"
    "BhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNE"
    "RUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqi"
    "o6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz"
    "9PX29/j5+v/EAB8BAAMBAQEBAQEBAQEAAAAAAAABAgMEBQYHCAkKC//EALURAAIB"
    "AgQEAwQHBQQEAAECdwABAgMRBAUhMQYSQVEHYXETIjKBCBRCkaGxwQkjM1LwFWJy"
    "0QoWJDThJfEXGBkaJicoKSo1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpz"
    "dHV2d3h5eoKDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uLj5OXm5+jp6vLz9PX29/j5+v/bAEMAAgICAgICAwICAwUD"
    "AwMFBgUFBQUGCAYGBgYGCAoICAgICAgKCgoKCgoKCgwMDAwMDA4ODg4ODw8PDw8P"
    "Dw8PD//bAEMBAgICBAQEBwQEBxALCQsQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBA"
    "QEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEP/dAAQAAf/aAAwDAQACEQMRAD8A/f"
    "yiiigD/9k="
)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory(dir=ROOT / "runtime", prefix="image-test-") as path:
        yield Store(path)


def test_only_verified_qiniu_origin_is_accepted_and_forced_to_https():
    assert canonical_image_url("http://erp.qiniu.wsgjp.com/folder/a_pic1.png") == \
        "https://erp.qiniu.wsgjp.com/folder/a_pic1.png"
    assert canonical_image_url("https://erp.qiniu.wsgjp.com/folder/a.png?version=2").startswith("https://")
    assert canonical_image_url("https://127.0.0.1/private.png") == ""
    assert canonical_image_url("https://erp.qiniu.wsgjp.com.evil.test/a.png") == ""
    assert canonical_image_url("file:///etc/passwd") == ""


def test_known_image_magic_wins_over_vendor_pjpeg_header():
    assert _mime(REAL_1X1_PNG, "image/pjpeg") == "image/png"
    assert _mime(REAL_1X1_JPEG, "image/pjpeg") == "image/jpeg"


def test_mime_compatibility_still_rejects_non_images_and_non_image_headers():
    with pytest.raises(ProductImageUnavailable, match="格式"):
        _mime(b"<html>not an image</html>", "image/pjpeg")
    with pytest.raises(ProductImageUnavailable, match="格式"):
        _mime(REAL_1X1_PNG, "text/html")
    with pytest.raises(ProductImageUnavailable, match="格式"):
        _mime(REAL_1X1_PNG, "image/jpeg")
    with pytest.raises(ProductImageUnavailable, match="格式"):
        _mime(REAL_1X1_PNG, "image/webp")


def test_metadata_keeps_one_image_and_cached_content_is_reused(store):
    items, invalid = image_metadata("10", [{
        "ptypeid": "30", "picurl": "http://erp.qiniu.wsgjp.com/a_pic1.png",
    }, {"ptypeid": "31", "picurl": "https://example.test/not-allowed.png"}],
        "2020-09-29T12:00:00+08:00")
    assert len(items) == 1 and invalid == 1
    with store.connect(write=True) as database:
        persist_image_metadata(database, "10", items)
    png = b"\x89PNG\r\n\x1a\n" + b"safe-test-image"
    calls = []
    def fetch(url, headers):
        calls.append(url)
        return {"data": png, "mime_type": "image/png", "etag": '"abc"',
                "last_modified": "Tue, 29 Sep 2026 04:00:00 GMT"}
    cache = ProductImageCache(store, fetcher=fetch)
    listed = cache.list_for_product("30")
    assert len(listed["items"]) == 1
    first = cache.content(listed["items"][0]["id"])
    second = cache.content(listed["items"][0]["id"])
    assert first.path == second.path and first.path.read_bytes() == png
    assert calls == ["https://erp.qiniu.wsgjp.com/a_pic1.png"]
    assert first.etag == hashlib.sha256(png).hexdigest()


def test_new_catalog_observation_revalidates_with_vendor_etag_without_redownload(store):
    items, _ = image_metadata("10", [{
        "ptypeid": "30", "picurl": "http://erp.qiniu.wsgjp.com/a_pic1.png",
    }], "2020-09-29T12:00:00+08:00")
    with store.connect(write=True) as database:
        persist_image_metadata(database, "10", items)
    png = b"\x89PNG\r\n\x1a\n" + b"conditional-image"
    calls = []
    def fetch(url, headers):
        calls.append(dict(headers))
        if len(calls) == 1:
            return {"data": png, "mime_type": "image/png", "etag": '"v1"',
                    "last_modified": "Tue, 29 Sep 2026 04:00:00 GMT"}
        return {"not_modified": True, "etag": '"v1"'}
    cache = ProductImageCache(store, fetcher=fetch)
    cache.content(items[0]["image_id"])
    refreshed, _ = image_metadata("10", [{
        "ptypeid": "30", "picurl": "http://erp.qiniu.wsgjp.com/a_pic1.png",
    }], "2100-09-30T12:00:00+08:00")
    with store.connect(write=True) as database:
        persist_image_metadata(database, "10", refreshed)
    result = cache.content(items[0]["image_id"])
    assert result.path.read_bytes() == png
    assert calls[1]["If-None-Match"] == '"v1"'
    assert "If-Modified-Since" in calls[1]


def test_changed_source_replaces_metadata_without_serving_old_cache(store):
    first, _ = image_metadata("10", [{
        "ptypeid": "30", "picurl": "http://erp.qiniu.wsgjp.com/old.png",
    }], "2026-09-29T12:00:00+08:00")
    second, _ = image_metadata("10", [{
        "ptypeid": "30", "picurl": "http://erp.qiniu.wsgjp.com/new.png",
    }], "2026-09-30T12:00:00+08:00")
    with store.connect(write=True) as database:
        persist_image_metadata(database, "10", first)
        database.execute("UPDATE product_images SET status='ready',cache_path='old.bin',content_sha256='x'")
        persist_image_metadata(database, "10", second)
    with store.connect() as database:
        row = database.execute("SELECT * FROM product_images").fetchone()
    assert row["image_id"] == second[0]["image_id"]
    assert row["source_url"].endswith("/new.png")
    assert row["cache_path"] == "" and row["status"] == "pending"


def test_invalid_download_bytes_are_not_cached(store):
    items, _ = image_metadata("10", [{
        "ptypeid": "30", "picurl": "http://erp.qiniu.wsgjp.com/a.png",
    }], "2026-09-29T12:00:00+08:00")
    with store.connect(write=True) as database:
        persist_image_metadata(database, "10", items)
    cache = ProductImageCache(store, fetcher=lambda *_: {
        "data": b"not-an-image", "mime_type": "image/png",
    })
    with pytest.raises(Exception, match="格式"):
        cache.content(items[0]["image_id"])
    assert not list((store.runtime / "product-images").glob("*.bin"))


def test_authenticated_api_shape_lists_and_serves_cached_image(store):
    items, _ = image_metadata("10", [{
        "ptypeid": "30", "picurl": "http://erp.qiniu.wsgjp.com/a.png",
    }], "2026-09-29T12:00:00+08:00")
    png = b"\x89PNG\r\n\x1a\n" + b"api-route-image"
    directory = store.runtime / "product-images"
    directory.mkdir(exist_ok=True)
    filename = items[0]["image_id"] + ".bin"
    (directory / filename).write_bytes(png)
    digest = hashlib.sha256(png).hexdigest()
    with store.connect(write=True) as database:
        persist_image_metadata(database, "10", items)
        database.execute("""UPDATE product_images SET mime_type='image/png',etag=?,
            last_modified='Tue, 29 Sep 2026 04:00:00 GMT',content_sha256=?,
            content_length=?,cache_path=?,status='ready',fetched_at=last_seen_at""",
            (digest, digest, len(png), filename))
    app = create_app({"TESTING": True, "AUTO_IMPORT": False}, runtime_dir=store.runtime)
    client = app.test_client()
    listed = client.get("/api/v2/products/30/images")
    assert listed.status_code == 200 and len(listed.get_json()["items"]) == 1
    content = client.get(listed.get_json()["items"][0]["content_url"])
    assert content.status_code == 200 and content.data == png
    assert content.content_type == "image/png"
    assert content.headers["X-Content-Type-Options"] == "nosniff"
    assert "private" in content.headers["Cache-Control"]
