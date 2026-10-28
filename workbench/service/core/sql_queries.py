"""SQL pagination for the large stock and purchase datasets."""
import json
from datetime import datetime, timedelta
from .queries import Queries as BaseQueries, INVENTORY_IDENTITY
from .rules import normalized, text


def contains(field):
    return f"instr(replace(replace(lower(coalesce(json_extract(payload,'$.{field}'),'')),' ',''),'_',''),?)>0"


class Queries(BaseQueries):
    def sql_page(self, kind, args, clauses, values, order="rowid"):
        self.check_version(args)
        for field in INVENTORY_IDENTITY:
            if text(args.get(field)):
                clauses.append(f"cast(json_extract(payload,'$.{field}') AS TEXT)=?")
                values.append(text(args[field]))
        page = max(1, int(args.get("page", 1)))
        size = min(100, max(1, int(args.get("page_size", 50))))
        where = "kind=?" + (" AND " + " AND ".join(clauses) if clauses else "")
        params = [kind, *values]
        with self.store.connect() as db:
            aggregate = db.execute(f"SELECT count(*) AS count,coalesce(sum(quantity),0) AS quantity,coalesce(sum(amount),0) AS amount,count(DISTINCT sku) AS skus FROM records WHERE {where}", params).fetchone()
            rows = db.execute(f"SELECT payload FROM records WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", [*params, size, (page - 1) * size]).fetchall()
        items = [json.loads(row[0]) for row in rows]
        return {"items": items, "count": aggregate["count"], "total": aggregate["count"], "page": page, "page_size": size, "returned": len(items), "total_quantity": aggregate["quantity"], "total_amount": round(aggregate["amount"], 4), "sku_count": aggregate["skus"]}, where, params

    def inventory(self, args):
        # Also protect pre-fix published snapshots until the next full refresh.
        # Apply before pagination and aggregation, not only to returned cards.
        clauses = [f"lower(trim(cast(coalesce(json_extract(payload,'$.{field}'),'') AS TEXT))) NOT IN ('true','1')"
                   for field in ('is_deleted', 'deleted')]
        values = []
        if args.get("q"):
            clauses.append("instr(keywords,?)>0")
            values.append(normalized(args["q"]))
        if text(args.get('sku')):
            clauses.append('upper(trim(sku))=?')
            values.append(text(args['sku']).upper())
        for key, field in {"name": "product_name", "spec": "spec", "model": "model", "material": "material", "status": "status"}.items():
            if args.get(key):
                clauses.append(contains(field))
                values.append(normalized(args[key]))
        if text(args.get("only_available")).lower() in {"1", "true", "yes", "on"}:
            clauses.append("cast(replace(json_extract(payload,'$.available_stock'),',','') AS REAL)>0")
        result, where, params = self.sql_page("inventory", args, clauses, values)
        with self.store.connect() as db:
            total = db.execute(f"SELECT coalesce(sum(cast(replace(json_extract(payload,'$.stock_quantity'),',','') AS REAL)),0),coalesce(sum(cast(replace(json_extract(payload,'$.available_stock'),',','') AS REAL)),0) FROM records WHERE {where}", params).fetchone()
        return {**result, "total_stock": total[0], "total_available": total[1]}

    def purchases(self, args):
        self.check_version(args)
        days = min(3650, max(1, int(args.get("days", 180))))
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        coverage = self.purchase_coverage(days, cutoff)
        date_field = "coalesce(json_extract(payload,'$.posted_at'),'')" if coverage['available'] else 'record_date'
        clauses, values = [f"({date_field}='' OR {date_field}>=?)"], [cutoff]
        if args.get("q"):
            clauses.append("instr(keywords,?)>0")
            values.append(normalized(args["q"]))
        for key, field in {"sku": "sku", "name": "product_name", "spec": "spec", "model": "model", "party": "party", "document_no": "document_no"}.items():
            if args.get(key):
                clauses.append(contains(field))
                values.append(normalized(args[key]))
        if args.get("note"):
            clauses.append("(" + " OR ".join(contains(field) for field in ("note", "summary", "extra_note")) + ")")
            values.extend([normalized(args["note"])] * 3)
        result, where, params = self.sql_page("purchases", args, clauses, values, date_field + " DESC,rowid")
        with self.store.connect() as db:
            groups = {}
            for field in ("party", "sku"):
                rows = db.execute(f"SELECT coalesce(json_extract(payload,'$.{field}'),'-') AS key,count(*) AS count,coalesce(sum(quantity),0) AS quantity,coalesce(sum(amount),0) AS amount,max(substr({date_field},1,10)) AS latest_date FROM records WHERE {where} GROUP BY key ORDER BY latest_date DESC,quantity DESC", params).fetchall()
                groups[field] = [dict(row) for row in rows]
            latest = db.execute(f"SELECT max(substr({date_field},1,10)) FROM records WHERE {where}", params).fetchone()[0]
        return {**result, "coverage": coverage, "days": days, "party_count": len(groups["party"]), "party_summary": groups["party"][:30], "sku_summary": groups["sku"][:30], "latest_date": latest or ""}
