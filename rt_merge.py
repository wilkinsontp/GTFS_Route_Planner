"""Step 6 — RT merge layer.

Patches RT delay offsets from RTStore onto static journey legs at query time.
Never modifies the SQLite data — all merging is done in-memory per request.
"""

# TODO: implement merge_delays(legs, rt_store) -> legs with rt_adjusted times
