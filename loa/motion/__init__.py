"""motion — the PIR daemon (loa-motion, GPIO17).

`python -m loa.motion`. One process per sense so a hung sensor cannot
deafen the eye. Publishes its readings on the `pir` topic.
"""
