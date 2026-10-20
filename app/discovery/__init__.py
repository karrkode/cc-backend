"""Event discovery (CC-70): check venues' own pages now and then for events we don't list yet.

    fetch      polite page fetching: robots.txt (including AI opt-outs), "changed since?", limits
    pagetext   a page's readable text, with its links
    readers    turn pages into found events: the Art Institute's API, or Claude Haiku 5.5
    rules      what to leave out (classes, rentals, members-only, recurring generic programs)
    store      the holding list: new items, "already listed", proposed updates
    run        which sources are due, checking them, backing off, pre-opening checks

Nothing here writes to the live catalog. Run it with `python -m scripts.discover`.
"""
