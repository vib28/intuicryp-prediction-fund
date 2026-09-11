#!/bin/bash
# Fund cycle — run one full pass of the four agents, then emit ONLY if
# something notable happened (booking, settlement, survival change, audit
# failure). Steady-state "no edge found" stays silent by design.
set -u

FUND_DIR=/root/fund
cd "$FUND_DIR" || { echo "FUND: cannot cd to $FUND_DIR"; exit 1; }

# Hard time cap so a hung upstream can never wedge the scheduler.
timeout 600 /usr/bin/python3 cycle.py >/tmp/fund_cycle.out 2>/tmp/fund_cycle.err
rc=$?

if [ $rc -ne 0 ] && [ $rc -ne 1 ]; then
    echo "🚨 FUND CYCLE ERROR (rc=$rc)"
    tail -5 /tmp/fund_cycle.err
    exit 1
fi

/usr/bin/python3 notify.py
