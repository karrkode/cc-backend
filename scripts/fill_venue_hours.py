"""Fill in venues' opening hours and usual show times from what discovery already found (CC-78),
without reading any website. Discovery does this itself after each check; this catches up at once.
Sources added before their venue existed are linked to it first. An admin's hours are never replaced.

    python -m scripts.fill_venue_hours
"""
from sqlalchemy import select

from app.db import SessionLocal
from app.discovery.hours import apply_open_hours, apply_show_times, link_venue
from app.models import DiscoverySource
from app.stats import today_in_chicago


def main():
    with SessionLocal() as db:
        for source in db.scalars(select(DiscoverySource).order_by(DiscoverySource.name)):
            if link_venue(db, source):
                print(f"{source.name}: linked to its venue, {source.venue.name} ({source.venue.neighborhood})")
            if source.venue is None:
                print(f"{source.name}: no venue linked, skipped")
                continue
            done = [what for what, changed in (("opening hours", apply_open_hours(source)),
                                                ("show times", apply_show_times(db, source, today_in_chicago()))) if changed]
            print(f"{source.name}: " + (", ".join(done) + " filled in" if done else "nothing new"))
        db.commit()


if __name__ == "__main__":
    main()
