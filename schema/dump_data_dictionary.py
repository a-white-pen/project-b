"""
Generates schema/data_dictionary.md from the live Cloud SQL database.

Functions:
  dump_data_dictionary() — connects to the DB, queries all table/column metadata
                           and comments, writes data_dictionary.md
"""

import os
import sys
import psycopg2


# Queries every user-defined table across all schemas, with table and column comments.
QUERY = """
SELECT
    n.nspname                          AS schema,
    c.relname                          AS table_name,
    obj_description(c.oid, 'pg_class') AS table_comment,
    a.attnum                           AS column_num,
    a.attname                          AS column_name,
    pg_catalog.format_type(a.atttypid, a.atttypmod) AS data_type,
    NOT a.attnotnull                   AS nullable,
    pg_get_expr(d.adbin, d.adrelid)    AS column_default,
    col_description(c.oid, a.attnum)   AS column_comment
FROM
    pg_catalog.pg_class     c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
    LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
WHERE
    c.relkind = 'r'
    AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    AND a.attnum > 0
    AND NOT a.attisdropped
ORDER BY
    n.nspname, c.relname, a.attnum
"""


def dump_data_dictionary():
    # Connects using DATABASE_URL from environment, writes data_dictionary.md.
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        print("ERROR: DATABASE_URL not set", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(database_url)
    cur = conn.cursor()
    cur.execute(QUERY)
    rows = cur.fetchall()
    conn.close()

    # Group by schema → table
    schemas: dict[str, dict[str, dict]] = {}
    for (schema, table, table_comment, _, col_name,
         data_type, nullable, col_default, col_comment) in rows:
        schemas.setdefault(schema, {})
        schemas[schema].setdefault(table, {"comment": table_comment, "columns": []})
        schemas[schema][table]["columns"].append({
            "name": col_name,
            "type": data_type,
            "nullable": nullable,
            "default": col_default,
            "comment": col_comment,
        })

    lines = ["# Data Dictionary\n",
             "_Auto-generated. Do not edit by hand. Run `python schema/dump_data_dictionary.py` to refresh._\n"]

    for schema_name, tables in sorted(schemas.items()):
        lines.append(f"\n## Schema: `{schema_name}`\n")
        for table_name, meta in sorted(tables.items()):
            lines.append(f"\n### `{schema_name}.{table_name}`\n")
            if meta["comment"]:
                lines.append(f"{meta['comment']}\n")
            lines.append("\n| Column | Type | Nullable | Default | Notes |\n")
            lines.append("|--------|------|----------|---------|-------|\n")
            for col in meta["columns"]:
                nullable_str = "yes" if col["nullable"] else "no"
                default_str = (col["default"] or "").replace("|", "\\|").replace("\n", " ")
                comment_str = (col["comment"] or "").replace("|", "\\|").replace("\n", " ")
                lines.append(
                    f"| `{col['name']}` | `{col['type']}` | {nullable_str} "
                    f"| {default_str} | {comment_str} |\n"
                )

    out_path = os.path.join(os.path.dirname(__file__), "data_dictionary.md")
    with open(out_path, "w") as f:
        f.writelines(lines)

    print(f"Written: {out_path}")
    table_count = sum(len(t) for t in schemas.values())
    print(f"  {len(schemas)} schemas, {table_count} tables")


if __name__ == "__main__":
    dump_data_dictionary()
