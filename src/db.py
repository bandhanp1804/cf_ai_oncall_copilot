"""Small wrapper over Durable Object SQLite (ctx.storage.sql).

The Python SDK wraps ctx.storage, so exec() results may arrive as a JS cursor
or as already-converted Python values depending on the SDK version. rows()
handles both and always returns a list of plain dicts.
"""


def _to_py(value):
    return value.to_py() if hasattr(value, "to_py") else value


class Db:
    def __init__(self, sql):
        self.sql = sql

    def run(self, query, *args):
        return self.sql.exec(query, *args)

    def rows(self, query, *args):
        cursor = self.sql.exec(query, *args)
        if hasattr(cursor, "toArray"):
            cursor = cursor.toArray()
        out = []
        for row in _to_py(cursor) or []:
            row = _to_py(row)
            out.append(dict(row) if not isinstance(row, dict) else {k: row[k] for k in row})
        return out

    def one(self, query, *args):
        found = self.rows(query, *args)
        return found[0] if found else None

    def script(self, ddl):
        for statement in ddl.split(";"):
            if statement.strip():
                self.sql.exec(statement)
