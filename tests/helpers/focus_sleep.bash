#!/usr/bin/env bash
# Replace only the handler's sleep; A and B are independently released.
gate="$FOCUS_GATE_ROOT/$FOCUS_GATE_ID"
printf '%s' "$*" > "$gate.request"
touch "$gate.entered"
for ((i=0; i<200; i++)); do
    if [[ -f "$gate.open" ]]; then
        touch "$gate.released"
        exit 0
    fi
    "$FOCUS_REAL_SLEEP" 0.05
done
touch "$gate.timed-out"
echo "focus gate $FOCUS_GATE_ID timed out" >&2
exit 99
