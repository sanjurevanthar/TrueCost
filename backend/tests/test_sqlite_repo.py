import sqlite3
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.repositories.sqlite_repo import SqlitePriceRepository

SCHEMA = """
CREATE TABLE prices(hospital TEXT, code TEXT, code_type TEXT, description TEXT, setting TEXT, payer TEXT, plan TEXT,
  rate_type TEXT, negotiated_rate REAL, cash_price REAL, gross_charge REAL, notes TEXT);
CREATE TABLE hospitals(hospital TEXT PRIMARY KEY, address TEXT, lat REAL, lng REAL);
CREATE TABLE codes(code TEXT, code_type TEXT, description TEXT, phase TEXT);
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    p = tmp_path / "t.db"
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    con.executemany("INSERT INTO hospitals VALUES (?,?,?,?)", [("Zeta", "z", 1.0, 2.0), ("Alpha", "a", 3.0, 4.0)])
    con.executemany("INSERT INTO codes VALUES (?,?,?,?)", [
        ("77066", "CPT", "mammo", "diagnostic"), ("19301", "CPT", "lumpectomy", "treatment"),
        ("582", "MS-DRG", "mastectomy", "treatment")])
    con.executemany("INSERT INTO prices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
        ("Alpha", "77066", "CPT", "m", "outpatient", "Univera", "PPO", "negotiated_dollar", 200.10, 100, 400, None),
        ("Alpha", "77066", "CPT", "m", "outpatient", "Univera", "HMO", "negotiated_dollar", 180, 100, 400, None),
        ("Alpha", "77066", "CPT", "m", "outpatient", "Highmark BCBS", "Blue", "negotiated_dollar", 250, 100, 400, None),
        ("Alpha", "77066", "CPT", "m", "outpatient", None, None, None, None, 100, 400, "gross"),
        ("Alpha", "19301", "CPT", "l", "outpatient", "Univera", None, "negotiated_dollar", 5000, None, None, None)])
    con.commit()
    con.close()
    return p


@pytest.fixture
def repo(db):
    return SqlitePriceRepository(db)


def test_missing_db_raises_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="build_db.py"):
        SqlitePriceRepository(tmp_path / "nope.db")


def test_hospitals_sorted_by_name(repo):
    assert [h.name for h in repo.list_hospitals()] == ["Alpha", "Zeta"]


def test_codes_all_phase_filter_and_unknown_phase(repo):
    assert len(repo.list_codes()) == 3
    assert {c.code for c in repo.list_codes("treatment")} == {"19301", "582"}
    assert repo.list_codes("nonsense") == []


def test_get_codes(repo):
    assert [c.code for c in repo.get_codes(["77066", "999"])] == ["77066"]
    assert repo.get_codes([]) == []


def test_find_prices_empty_codes(repo):
    assert repo.find_prices([]) == []


def test_find_prices_without_payer_returns_all_payers(repo):
    assert len(repo.find_prices(["77066"])) == 4


def test_find_prices_with_payer_includes_payerless_rows_only(repo):
    rows = repo.find_prices(["77066"], "Univera")
    assert {r.payer for r in rows} == {"Univera", None} and len(rows) == 3


def test_money_comes_back_as_decimal_not_float(repo):
    r = next(p for p in repo.find_prices(["77066"], "Univera") if p.plan == "PPO")
    assert r.negotiated_rate == D("200.1") and isinstance(r.negotiated_rate, D)
    assert r.is_dollar_rate


def test_null_money_stays_none(repo):
    r = next(p for p in repo.find_prices(["19301"]))
    assert r.cash_price is None and r.gross_charge is None


def test_list_plans_distinct_sorted_and_per_payer(repo):
    assert repo.list_plans("Univera") == ["HMO", "PPO"]
    assert repo.list_plans("Highmark BCBS") == ["Blue"]
    assert repo.list_plans("Aetna") == []


def test_database_is_opened_read_only(db, repo):
    repo.list_codes()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM prices")


def test_sql_injection_in_params_is_harmless(repo):
    assert repo.find_prices(["77066' OR '1'='1"]) == []
    assert repo.list_plans("x' OR 1=1 --") == []
