"""
Generates schema/data_dictionary.md from the live Cloud SQL database.

Functions:
  load_private_skiplist() — reads the gitignored skip-list of PRIVATE relations to exclude
  dump_data_dictionary()  — connects to the DB, queries all table/column metadata
                            and comments, writes data_dictionary.md

Includes both tables (relkind='r') and views (relkind='v'). Views have their SQL definition
emitted before the column table so runtime dependencies (e.g. b.latest_location used by the
food service for timezone resolution) are visible in the schema reference.

The repo is PUBLIC, so PRIVATE relations (B's personal data-visualisation views + device tables)
are EXCLUDED from the committed dictionary. The list lives in a gitignored file
(schema/private_relations.txt) — not hardcoded here — so no private names enter the public repo;
if the file is absent, nothing is skipped.
"""

import os
import sys
import psycopg2


# Queries every user-defined table and view across all schemas, with table and column comments.
QUERY = """
SELECT
    n.nspname                          AS schema,
    c.relname                          AS relation_name,
    c.relkind                          AS relkind,
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
    c.relkind IN ('r', 'v')
    AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    AND a.attnum > 0
    AND NOT a.attisdropped
ORDER BY
    n.nspname, c.relkind DESC, c.relname, a.attnum
"""

# Fetches the SQL definition for every view so readers can see what each view computes.
VIEW_DEF_QUERY = """
SELECT
    n.nspname  AS schema,
    c.relname  AS view_name,
    pg_get_viewdef(c.oid, true) AS definition
FROM
    pg_catalog.pg_class     c
    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE
    c.relkind = 'v'
    AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
ORDER BY n.nspname, c.relname
"""

# Gitignored file listing PRIVATE relations to EXCLUDE from the committed dictionary
# (one "schema.relation" per line; blank lines and # comments ignored). Kept out of the
# public repo so no private relation names are published; absent file => skip nothing.
_PRIVATE_SKIPLIST_FILE = os.path.join(os.path.dirname(__file__), "private_relations.txt")


# Loads the set of "schema.relation" names to exclude from the dictionary.
# Reads _PRIVATE_SKIPLIST_FILE if present (one name per line; '#' comments and blanks ignored).
# Returns an empty set when the file is absent, so a fresh checkout still dumps everything.
def load_private_skiplist() -> set[str]:
    skip: set[str] = set()
    if not os.path.exists(_PRIVATE_SKIPLIST_FILE):
        return skip
    with open(_PRIVATE_SKIPLIST_FILE) as f:
        for line in f:
            name = line.split("#", 1)[0].strip()
            if name:
                skip.add(name)
    return skip


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

    cur.execute(VIEW_DEF_QUERY)
    view_defs: dict[tuple[str, str], str] = {
        (schema, view): defn for schema, view, defn in cur.fetchall()
    }

    conn.close()

    # Group by schema → relation_name, preserving relkind. PRIVATE relations (the gitignored
    # skip-list) are excluded so the PUBLIC committed dictionary never publishes them.
    skip = load_private_skiplist()
    skipped: set[str] = set()
    schemas: dict[str, dict[str, dict]] = {}
    for (schema, rel_name, relkind, table_comment, _, col_name,
         data_type, nullable, col_default, col_comment) in rows:
        if f"{schema}.{rel_name}" in skip:
            skipped.add(f"{schema}.{rel_name}")
            continue
        schemas.setdefault(schema, {})
        schemas[schema].setdefault(rel_name, {"comment": table_comment, "relkind": relkind, "columns": []})
        schemas[schema][rel_name]["columns"].append({
            "name": col_name,
            "type": data_type,
            "nullable": nullable,
            "default": col_default,
            "comment": col_comment,
        })

    lines = ["# Data Dictionary\n",
             "_Auto-generated. Do not edit by hand. Run `python schema/dump_data_dictionary.py` to refresh._\n"]

    table_count = 0
    view_count = 0

    for schema_name, relations in sorted(schemas.items()):
        lines.append(f"\n## Schema: `{schema_name}`\n")
        for rel_name, meta in sorted(relations.items()):
            is_view = meta["relkind"] == "v"
            kind_label = "View" if is_view else "Table"
            lines.append(f"\n### {kind_label}: `{schema_name}.{rel_name}`\n")
            if meta["comment"]:
                lines.append(f"{meta['comment']}\n")
            if is_view:
                view_count += 1
                defn = view_defs.get((schema_name, rel_name), "")
                if defn:
                    lines.append("\n**View definition:**\n")
                    lines.append("```sql\n")
                    lines.append(defn.strip() + "\n")
                    lines.append("```\n")
            else:
                table_count += 1
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
    print(f"  {len(schemas)} schemas, {table_count} tables, {view_count} views")
    if skipped:
        print(f"  excluded {len(skipped)} private relation(s): {', '.join(sorted(skipped))}")


if __name__ == "__main__":
    dump_data_dictionary()
