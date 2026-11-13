"""CC-199: spreadsheet cells that start like a formula stay text."""
import pytest

from app.csvsafe import cell, to_csv


@pytest.mark.parametrize("lead", ["=", "+", "-", "@", "\t", "\r"])
def test_formula_leads_get_a_quote(lead):
    assert cell(f"{lead}SUM(A1)") == f"'{lead}SUM(A1)"


def test_ordinary_cells_are_untouched():
    assert [cell(v) for v in ["Hamlet", "$25", "2027-01-10", "", "a=b", " =x"]] == ["Hamlet", "$25", "2027-01-10", "", "a=b", " =x"]


def test_non_strings_are_stringified():
    assert (cell(5), cell(None), cell(-5)) == ("5", "", "'-5")  # a negative number is text once it is a string


def test_header_and_rows_both_go_through_it():
    out = to_csv(["=h", "b"], [["@x", "ok, fine"]])
    assert out.splitlines() == ["'=h,b", "'@x,\"ok, fine\""]
