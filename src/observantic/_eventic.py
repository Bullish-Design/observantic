"""observantic._eventic
======================
The Eventic seam: the stable import boundary between observantic and eventic.

Contract with Eventic 1.1.0 (the rewritten, declaration-based release):

* ``App`` / ``Stream`` / ``Subscription`` are frozen values; constructing
  them performs no I/O (I4).
* ``App.bind(store)`` returns a ``Runtime``; all writes go through
  ``runtime[stream]`` (a ``Collection``) with compare-and-swap (I5, I7).
* Stores: ``eventic.sql.SQLite`` (dev/test) and ``eventic.sql.Postgres``
  (production). Bare ``postgresql://`` URLs are translated to
  ``postgresql+psycopg://`` (eventic's ``[postgres]`` extra ships psycopg3).
  ``Store.close()`` is idempotent.
* Delivery: ``Inline`` (best-effort, in-process) or ``Outbox`` (durable,
  drained by ``eventic worker`` / ``eventic.worker.Worker``).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from eventic import App, NoMeta, Stream
from eventic.errors import NotFound, RevisionConflict
from eventic.runtime import Collection
from eventic.subscription import Subscription

from .config import DB_URL as DEFAULT_DB_URL
from .exceptions import ConfigurationException

logger = logging.getLogger("observantic.eventic")

__all__ = [
    "DEFAULT_DB_URL",
    "build_app",
    "make_store",
    "persist_row",
    "sqlite_aggregate_key",
]


def make_store(url_or_path: str | None = None, *, create_tables: bool = True) -> Any:
    """Build an eventic store from a URL or bare path.

    ``sqlite://`` (or a bare path like ``"obs.db"``) -> ``SQLite``;
    ``postgresql://`` -> ``Postgres``. ``url_or_path`` defaults to the
    settings snapshot ``DEFAULT_DB_URL`` (``OBSERVANTIC_DB_URL`` /
    ``DB_URL``).
    """
    url = url_or_path if url_or_path is not None else DEFAULT_DB_URL
    if not isinstance(url, str) or not url:
        raise ConfigurationException("store URL must be a non-empty string")
    try:
        if url.startswith("postgresql"):
            from eventic.sql import Postgres

            if url.startswith("postgresql://"):
                # SQLAlchemy's bare postgresql:// dialect defaults to
                # psycopg2, but eventic's [postgres] extra ships psycopg3
                # (psycopg[binary]). Translate so the documented URL works.
                url = "postgresql+psycopg" + url[len("postgresql") :]
            return Postgres(url, create_tables=create_tables)
        if url.startswith("sqlite"):
            from eventic.sql import SQLite

            return SQLite(url, create_tables=create_tables)
        if "://" not in url:
            # bare path — SQLite, matching eventic's own loader
            from eventic.sql import SQLite

            return SQLite(url, create_tables=create_tables)
    except ConfigurationException:
        raise
    except Exception as exc:  # missing driver, malformed URL, ...
        raise ConfigurationException(
            f"cannot create eventic store from {url!r}: {exc} "
            "(install eventic[postgres] for postgresql:// URLs)"
        ) from exc
    raise ConfigurationException(
        f"unsupported database URL scheme: {url!r} "
        "(expected sqlite://, postgresql://, or a bare path)"
    )


def build_app(
    id: str,
    streams: Sequence[Stream[Any]] = (),
    subscriptions: Sequence[Subscription[Any, Any]] = (),
    meta: Any = NoMeta,
    on_inline_error: str = "raise",
) -> App:
    """Assemble an eventic ``App`` from watcher streams and subscriptions.

    A thin passthrough so core/ and users never import eventic directly.
    The returned ``App`` can be bound to a store
    (``app.bind(store)``) and passed to the ``eventic`` CLI
    (``--app module:attr``).
    """
    return App(
        id=id,
        streams=streams,
        subscriptions=subscriptions,
        meta=meta,
        on_inline_error=on_inline_error,
    )


def sqlite_aggregate_key(table: str, row_id: int | str | None) -> UUID:
    """Deterministic aggregate id for one SQLite row.

    ``uuid5(NAMESPACE_URL, f"observantic:sqlite:{table}:{row_id}")`` — stable
    across processes and restarts, so updates and deletes append to the same
    aggregate (durable revision history per row).
    """
    return uuid5(NAMESPACE_URL, f"observantic:sqlite:{table}:{row_id}")


def persist_row(collection: Collection[Any], state: Any, *, keyed: bool) -> None:
    """Commit one emitted row state through the collection.

    ``keyed=False`` (legacy): every event is a fresh aggregate (revision 0).
    ``keyed=True``: inserts -> ``create(state, id=key)``; updates and deletes
    -> ``replace`` on the head (delete states are already tombstones:
    ``row_data=None``, ``operation="deleted"``). Rowid reuse after a delete
    falls back to replace on the existing aggregate; a pre-existing row that
    was never emitted (first snapshot) creates on first change/delete.
    """
    if not keyed:
        collection.create(state)
        return
    key = sqlite_aggregate_key(
        getattr(state, "table_name", ""), getattr(state, "row_id", None)
    )
    if getattr(state, "operation", "inserted") == "inserted":
        try:
            collection.create(state, id=key)
        except RevisionConflict:
            collection.replace(collection.get(key), state)  # rowid reused
        return
    try:
        head = collection.get(key)
    except NotFound:
        collection.create(state, id=key)  # row pre-existed at start
        return
    collection.replace(head, state)
