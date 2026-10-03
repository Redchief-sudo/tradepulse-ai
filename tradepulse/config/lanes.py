"""The single authority for supervised lane cadence and soak continuity.

The runtime schedules each lane at LANE_INTERVAL_SECONDS and the soak's
lane-continuity evidence (verification/soak.py) bounds silence with
LANE_MAX_GAP_SECONDS, so the two can never drift apart. A gap is one cycle's
duration plus the interval: the heartbeat fires at cycle end, and the next
cycle starts one interval after the previous one completes.
"""
LANE_INTERVAL_SECONDS = {"equity": 900, "crypto": 600, "option": 1200, "monitor": 30, "settle": 60, "reconcile": 60}

# Scan, settlement and reconciliation keep the historical 2 x interval + 120 s.
# The monitor (software-evaluated stops, see the Phase 4 broker-side stop spec)
# gets 30 s + a 90 s cycle budget: 4x the longest cycle observed (22.5 s across
# 676 cycles in two soaks) and several 20 s fill waits. Any protection stall
# over two minutes fails continuity. A cycle with four or more simultaneous slow
# exits could exceed it; that fails closed, and the soak is re-run.
LANE_MAX_GAP_SECONDS = {lane: 2 * interval + 120 for lane, interval in LANE_INTERVAL_SECONDS.items()}
LANE_MAX_GAP_SECONDS["monitor"] = 120
