"""Which filter area a neighborhood belongs to. Same rules as area_of() in the old app.py."""
import re

from .models import Area

FAR_IL = {"Rockford, IL"}       # Illinois, but not a Chicago suburb
SUBURB_NO_STATE = {"Niles"}     # suburbs entered without ", IL"


def area_of(neighborhood: str) -> Area:
    hood = (neighborhood or "").strip()
    if hood in SUBURB_NO_STATE:
        return Area.suburbs
    m = re.search(r",\s*([A-Z]{2})$", hood)
    if not m:
        return Area.city  # a plain name ("Lincoln Park") is a city neighborhood
    return Area.suburbs if m.group(1) == "IL" and hood not in FAR_IL else Area.out_of_town
