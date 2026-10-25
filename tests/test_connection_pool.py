from concurrent.futures import ThreadPoolExecutor

import pytest

from adapters.postgres import PostgresRepository
from alli_core.profile import Profile


def test_pool_reuses_bounded_connections_and_rolls_back_failed_transactions(database):
    repo=PostgresRepository(database,Profile.load('profiles/alli.toml'))
    repo.open(max_size=2)
    try:
        repo.initialize()
        with repo.connection() as db:db.execute('CREATE TABLE pool_probe(value int)')
        with pytest.raises(RuntimeError):
            with repo.connection() as db:
                db.execute('INSERT INTO pool_probe VALUES(1)')
                raise RuntimeError('Transaction interrupted')
        def read(_):
            with repo.connection() as db:
                row=db.execute('SELECT pg_backend_pid() AS pid,(SELECT count(*) FROM pool_probe) AS total').fetchone()
                assert row['total']==0
                return row['pid']
        with ThreadPoolExecutor(max_workers=8) as workers:pids=list(workers.map(read,range(32)))
        assert len(set(pids))<=2
        assert repo.pool.get_stats()['requests_num']>=34
    finally:repo.close()
    assert repo.pool is None
