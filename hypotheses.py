#!/usr/bin/env python3
"""Declared hypotheses for hypothesis_gate.

Every rule ever tested lives here, survivors and corpses alike, so the
registry's comparison count stays honest and a disproof cannot be quietly
re-derived under a new name.

Run with:  python3 hypothesis_gate.py --run "<name>"
"""
from hypothesis_gate import hypothesis


@hypothesis(name="sig_combined level >= 10", band=(0.02, 0.05))
def sig_combined_level(row):
    """settle_bot's rule. Scored +0.0300 on the maker basis and was shipped;
    live it returned -0.0255 over 47 fills because resting bids are adversely
    selected. Expected to DIE here on the taker basis, flagged fill_dependent
    -- if it does not, the harness is wrong, not the world."""
    sc = row.get("sig_combined")
    if sc is None:
        return None
    return "YES" if sc >= 10 else "NO" if sc <= -10 else None


@hypothesis(name="follow momentum |mom|>=30", band=(0.02, 0.09))
def follow_momentum(row):
    """The strongest signal found (2026-08-10): +0.0567, t=+4.49, 3/3 windows
    AT THE SIGNAL TICK. Half the edge is gone within 5s and the feature log's
    own cadence is 5.1s. Expected to DIE on latency, flagged latency_dependent."""
    mom = row.get("momentum")
    if mom is None or abs(mom) < 30:
        return None
    return "YES" if mom > 0 else "NO"


@hypothesis(name="fade momentum |mom|>=30", band=(0.02, 0.09))
def fade_momentum(row):
    """Mean reversion. Measured -0.0917, t=-7.25, 0/3 windows -- these markets
    are momentum-persistent over 5-11 minutes, not mean-reverting. A known
    corpse, kept so the registry refuses to re-derive it."""
    mom = row.get("momentum")
    if mom is None or abs(mom) < 30:
        return None
    return "NO" if mom > 0 else "YES"


@hypothesis(name="buy the cheap band", band=(0.02, 0.10))
def cheap_band(row):
    """Buying longshots. Settles 7.4% against a 13.4c ask: -0.060 +- 0.021.
    Classic favourite-longshot bias, in the losing direction."""
    a = row.get("yes_ask")
    if a is None:
        return None
    return "YES" if 0.01 < a < 0.20 else None


@hypothesis(name="NULL control: always YES", band=(0.02, 0.10))
def null_control(row):
    """Buys YES unconditionally. Must DIE. If this ever survives, the harness
    has a bug -- it is the smoke detector for the whole protocol."""
    return "YES" if row.get("yes_ask") is not None else None
