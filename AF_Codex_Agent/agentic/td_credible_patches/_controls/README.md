# Known-outcome controls

Not part of the 22 credible batch patches. These are patches whose outcome under
the perturbation has been measured directly, for validating that the harness
reports what it should.

| patch | container | verified outcome |
|---|---|---|
| APEXCORE-617-...KNOWN_PASS.diff | APEXCORE-617-testEmitTuplesOutsideStreamingWindow | PASS (before FAIL, after PASS) |

Run one with:

    ./check_patch.sh <container> --patch td_credible_patches/_controls/<file> --repeat 3

A harness that cannot turn a KNOWN_PASS control green is broken; trust nothing
else it reports until it does.
