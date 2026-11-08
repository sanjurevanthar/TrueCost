import pytest

from app.domain.geo import GeoService

geo = GeoService()


def test_same_point_is_zero():
    assert geo.distance_miles(42.9, -78.8, 42.9, -78.8) == 0.0


def test_known_distance_buffalo_to_niagara_falls():
    # Downtown Buffalo -> Niagara Falls, NY is roughly 17-19 miles in a straight line.
    d = geo.distance_miles(42.8864, -78.8784, 43.0962, -79.0377)
    assert 15 <= d <= 20


def test_symmetric():
    a, b = (42.8977, -78.8667), (43.17, -79.035)
    assert geo.distance_miles(*a, *b) == geo.distance_miles(*b, *a)


@pytest.mark.parametrize("args", [(None, -78, 42, -78), (42, None, 42, -78), (42, -78, None, -78), (42, -78, 42, None)])
def test_missing_coordinate_returns_none(args):
    assert geo.distance_miles(*args) is None


def test_zero_coordinate_is_valid_not_missing():
    assert geo.distance_miles(0, 0, 0, 1) is not None
