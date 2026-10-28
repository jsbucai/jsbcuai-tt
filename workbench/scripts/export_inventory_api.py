"""Download one stable published inventory generation from the public UL API."""
import argparse
import getpass
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request(base, path, token=None, payload=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + path, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=45, context=ssl.create_default_context()) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="https://ul.yingyinghome.com:8443/api/v2")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    password = getpass.getpass("UL administrator password: ")
    status, auth = request(args.base, "/auth/login", payload={"password": password})
    password = None
    if status != 200 or not auth.get("token"):
        raise SystemExit("登录失败")
    token = auth["token"]
    rows = []
    try:
        status, first = request(args.base, "/inventory?page=1&page_size=100", token)
        if status != 200:
            raise SystemExit(f"读取库存失败：HTTP {status}")
        version = first.get("data_version")
        total = int(first.get("total", first.get("count", 0)))
        rows.extend(first.get("items", []))
        page = 2
        while len(rows) < total:
            query = urllib.parse.urlencode({
                "page": page,
                "page_size": 100,
                "expected_data_version": version,
            })
            status, body = request(args.base, "/inventory?" + query, token)
            if status != 200:
                raise SystemExit(f"库存分页失败：HTTP {status}，未保存半批数据")
            if body.get("data_version") != version or int(body.get("total", -1)) != total:
                raise SystemExit("读取期间数据版本或总数发生变化，未保存半批数据")
            items = body.get("items")
            if not isinstance(items, list) or not items:
                raise SystemExit("库存分页提前结束，未保存半批数据")
            rows.extend(items)
            page += 1
        if len(rows) != total or len({str(row.get("id")) for row in rows}) != total:
            raise SystemExit("库存记录数量或身份未闭合，未保存")
        document = {
            "source": args.base + "/inventory",
            "data_version": version,
            "source_updated_at": first.get("source_updated_at"),
            "total": total,
            "rows": rows,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        temporary.replace(args.output)
        print(json.dumps({"total": total, "data_version": version,
                          "source_updated_at": first.get("source_updated_at")}, ensure_ascii=False))
    finally:
        request(args.base, "/auth/logout", token, {})


if __name__ == "__main__":
    main()
