#!/usr/bin/env bash
# Arrival/release order controls the test; elapsed time only bounds hangs.
gate="$RESIZE_GATE_ROOT/$RESIZE_EVENT_ID"
printf '%s' "$*" > "$gate.request"
touch "$gate.ready"
for ((attempt=0; attempt<600; attempt++)); do
    if [[ -f "$gate.release" ]]; then
        exit 0
    fi
    "$RESIZE_REAL_SLEEP" 0.05
done
touch "$gate.timed-out"
echo "resize gate $RESIZE_EVENT_ID timed out" >&2
exit 99
