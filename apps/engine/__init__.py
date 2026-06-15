"""Pure-Python analytical query engine over Parquet in object storage.

No Django imports live in this package. It is a standalone library that the
Django/Celery layer drives. The public entrypoint is `run_sql`.
"""

from .api import explain_sql, run_sql

__all__ = ["run_sql", "explain_sql"]
