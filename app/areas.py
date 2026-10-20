"""Which filter area, and which side of town, a neighborhood belongs to.

area_of() keeps the rules of area_of() in the old app.py; region_of() is CC-73."""
import re

from .models import Area, Region

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


# CC-73: Chicago neighborhoods by side of town, as the product owner grouped them: the Loop and the
# neighborhoods around it are Downtown/Loop. Lower case; add new neighborhoods here (or fix one venue
# at a time on the Venues page).
CITY_REGIONS = {
    **dict.fromkeys(["loop", "downtown", "south loop", "west loop", "river north", "streeterville", "gold coast",
                     "near north side", "printers row", "magnificent mile", "new eastside", "fulton market"], Region.downtown),
    **dict.fromkeys(["andersonville", "edgewater", "irving park", "jefferson park", "lakeview", "lake view", "lincoln park",
                     "lincoln square", "north center", "old town", "rogers park", "uptown", "west ridge", "ravenswood",
                     "albany park", "avondale", "portage park", "wrigleyville", "boystown", "edison park", "norwood park",
                     "sauganash", "north park", "budlong woods", "bowmanville"], Region.north),
    **dict.fromkeys(["bucktown", "humboldt park", "logan square", "near west side", "north lawndale", "ukrainian village",
                     "university village", "west town", "wicker park", "pilsen", "little village", "east garfield park",
                     "west garfield park", "austin", "garfield park", "little italy", "noble square", "east village",
                     "hermosa", "belmont cragin", "galewood", "lower west side", "south lawndale", "tri-taylor"], Region.west),
    **dict.fromkeys(["bridgeport", "greater grand crossing", "hyde park", "kenwood", "woodlawn", "bronzeville", "chinatown",
                     "south shore", "chatham", "pullman", "beverly", "back of the yards", "douglas", "grand boulevard",
                     "washington park", "south side", "armour square", "mckinley park", "brighton park", "englewood",
                     "auburn gresham", "morgan park", "roseland", "hegewisch", "east side", "south chicago",
                     "near south side"], Region.south),
}


def region_of(neighborhood: str, area: Area | None = None) -> Region | None:
    """The side of town for a neighborhood: suburbs and out of town by area, city neighborhoods from the
    list above. None when a city neighborhood isn't in the list."""
    area = area or area_of(neighborhood)
    if area == Area.suburbs:
        return Region.suburbs
    if area == Area.out_of_town:
        return Region.out_of_town
    return CITY_REGIONS.get((neighborhood or "").strip().lower())
