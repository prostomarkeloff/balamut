Wall time: the median over a campaign's runs (smallest and largest in brackets), summed over the projects on which every campaign ran (cachetools); per campaign and first run in the second table. CPUs: those the container may use against those the tool's process sees (`os.cpu_count()`), from which tools size their parallelism. Peak tasks: the most processes and threads the container held (cgroup pids.peak, recorded from the repeated runs on). A peak memory at the container's 3 GiB limit measures a test that allocates without bound, not the tool. False kills: unconfirmed kills / (agree: killed + unconfirmed kills); false survivors: missed kills / (agree: survived + missed kills); not comparable: every other native mutant (unmapped, no reference verdict, the oracle's non-verdicts, the tool's timeouts, errors, skips and no-coverage verdicts, verdicts the tool reports only as a total). Peak memory: the container's memory peak in MiB (cgroup memory.peak, container lifetime (control run included)), the largest over the projects in the first table; it counts every process the campaign starts and the page cache of the files they read and write, so a mutant whose tests allocate without bound raises it to the container's memory limit. The CPU time in engine_track.json counts only the processes a campaign waited for.

| tool | projects run | native mutants | agree: killed | unconfirmed kills | agree: survived | missed kills | not comparable | false kills | false survivors | wall time | CPUs usable of visible | peak tasks | peak memory |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| balamut | 6/6 | 2097 | 1772 | 0 | 120 | 0 | 205 | 0.0% | 0.0% | 93 s (93–93, 5 runs) | 4 of 14 | 92 | 3072 MiB (limit reached: toolz) |
| cosmic-ray | 6/6 | 3242 | 2821 | 0 | 409 | 0 | 12 | 0.0% | 0.0% | 637 s (636–643, 5 runs) | 4 of 14 | 13 | 3072 MiB (limit reached: sqlparse) |
| deployproof | 4/6 | 944 | 444 | 0 | 122 | 63 | 315 | 0.0% | 34.1% | 134 s (134–134, 5 runs) | 4 of 14 | 13 | 338 MiB |
| fest | 6/6 | 1900 | 546 | 13 | 69 | 771 | 501 | 2.3% | 91.8% | 4 s (3–4, 5 runs) | 4 of 14 | 21 | 2914 MiB |
| irradiate | 5/6 | 3232 | 2201 | 0 | 280 | 277 | 474 | 0.0% | 49.7% | 38 s (34–40, 5 runs) | 4 of 14 | 41 | 1893 MiB |
| mutation | 5/6 | 10442 | 7852 | 539 | 328 | 1623 | 100 | 6.4% | 83.2% | 596 s (595–608, 5 runs) | 4 of 14 | 62 | 3072 MiB (limit reached: toolz, sqlparse) |
| mutmut | 3/6 | 2805 | 1294 | 0 | 159 | 10 | 1342 | 0.0% | 5.9% | 9 s (9–9, 5 runs) | 4 of 14 | 70 | 2573 MiB |
| poodle | 4/6 | 1914 | 1572 | 1 | 258 | 0 | 83 | 0.1% | 0.0% | 152 s (151–152, 5 runs) | 4 of 14 | 32 | 3072 MiB (limit reached: toolz, sqlparse) |
| pytest-gremlins | 6/6 | 847 | 762 | 0 | 80 | 0 | 5 | 0.0% | 0.0% | 165 s (164–165, 5 runs) | 4 of 14 | 13 | 304 MiB |
| pytest-leela | 4/6 | 655 | 439 | 0 | 34 | 182 | 0 | 0.0% | 84.3% | 13 s (12–13, 5 runs) | 4 of 14 | 13 | 300 MiB |
| universalmutator | 6/6 | 5453 | 4801 | 0 | 644 | 0 | 8 | 0.0% | 0.0% | 1289 s (1286–1314, 5 runs) | 4 of 14 | 30 | 3072 MiB (limit reached: cachetools, sqlparse) |

Outcomes: a campaign that ran and gave verdicts (ran), one that gave none or left no report (did not run out of the box), and among those that ran, the ones whose comparable verdicts all agree with the reference oracle.

| tool | ran, all comparable verdicts agree | ran, some disagree | did not run out of the box |
|---|---|---|---|
| balamut | python-semver, cachetools, humanize, toolz, sqlparse, attrs | – | – |
| cosmic-ray | python-semver, cachetools, humanize, toolz, sqlparse, attrs | – | – |
| deployproof | sqlparse, attrs | python-semver, cachetools | humanize, toolz |
| fest | – | python-semver, cachetools, humanize, toolz, sqlparse, attrs | – |
| irradiate | – | cachetools, humanize, toolz, sqlparse, attrs | python-semver |
| mutation | – | python-semver, cachetools, humanize, toolz, sqlparse | attrs |
| mutmut | toolz | cachetools, sqlparse | python-semver, humanize, attrs |
| poodle | cachetools, humanize, sqlparse | toolz | python-semver, attrs |
| pytest-gremlins | python-semver, cachetools, humanize, toolz, sqlparse, attrs | – | – |
| pytest-leela | python-semver, humanize, attrs | cachetools | toolz, sqlparse |
| universalmutator | python-semver, cachetools, humanize, toolz, sqlparse, attrs | – | – |

Repeated campaigns: per repetition, the mutants it shares with the first run (by mutated file, else by file and native id) and how many of them changed verdict; over all runs, the mutants every run has whose verdicts differ, by the verdicts they got.

| project | tool | runs | common mutants | changed verdict | varied over all runs |
|---|---|---|---|---|---|
| cachetools | balamut | 5 | 518, 518, 518, 518 | 0, 0, 0, 1 | killed/survived 1 |
| cachetools | mutmut | 5 | 659, 659, 659, 659 | 0, 0, 0, 0 | – |
| cachetools | cosmic-ray | 5 | 576, 576, 576, 576 | 0, 0, 0, 1 | killed/survived 1 |
| cachetools | poodle | 5 | 545, 545, 545, 545 | 0, 0, 0, 1 | killed/survived 1 |
| cachetools | pytest-gremlins | 5 | 172, 172, 172, 172 | 0, 0, 0, 0 | – |
| cachetools | pytest-leela | 5 | 195, 195, 195, 195 | 0, 0, 0, 0 | – |
| cachetools | mutation | 5 | 2688, 2686, 2691, 2689 | 0, 0, 0, 0 | – |
| cachetools | universalmutator | 5 | 1180, 1180, 1180, 1180 | 1, 0, 0, 0 | killed/survived 1 |
| cachetools | fest | 5 | 319, 319, 319, 319 | 2, 6, 1, 5 | killed/survived 7 |
| cachetools | irradiate | 5 | 913, 913, 913, 913 | 13, 13, 15, 15 | killed/survived 1, killed/timeout 8, survived/timeout 23 |
| cachetools | deployproof | 5 | 98, 98, 98, 98 | 0, 0, 0, 0 | – |

| project | tool | campaign | wall time | peak memory | native verdicts | comparison | control run |
|---|---|---|---|---|---|---|---|
| python-semver | balamut | ran | 9 s | 909 MiB | error 21, killed 333, no_coverage 29, survived 9 | agree (no coverage) 9, agree: killed 333, agree: survived 9, error 21, no coverage, killable 20 | 508 passed, 1 skipped in 0.36s |
| python-semver | mutmut | no verdicts (exit 1: Stopping early, because we could not find any test case for any mutant. It seems that the selected tests do not cover any code that we mutated.) | 2 s | 196 MiB | skipped 578 | skipped 578 | 508 passed, 1 skipped in 0.38s |
| python-semver | cosmic-ray | ran | 683 s | 168 MiB | killed 699, survived 63 | agree: killed 699, agree: survived 63 | 508 passed, 1 skipped in 0.38s |
| python-semver | poodle | no verdicts (exit 3: E   ModuleNotFoundError: No module named 'coerce') | 1 s | 95 MiB |  |  | 508 passed, 1 skipped in 0.37s |
| python-semver | pytest-gremlins | ran | 170 s | 130 MiB | killed 184, survived 7 | agree: killed 184, agree: survived 7 | 508 passed, 1 skipped in 0.35s |
| python-semver | pytest-leela | ran | 19 s | 101 MiB | killed 198, survived 7 | agree: killed 198, agree: survived 7 | 508 passed, 1 skipped in 0.37s |
| python-semver | mutation | ran | 733 s | 685 MiB | killed 1388, survived 1050 | agree: killed 1295, agree: survived 71, missed kill 977, unconfirmed kill 92, unmapped 3 | 508 passed, 1 skipped in 0.38s |
| python-semver | universalmutator | ran | 979 s | 136 MiB | killed 964, survived 118 | agree: killed 964, agree: survived 118 | 508 passed, 1 skipped in 0.38s |
| python-semver | fest | ran | 5 s | 172 MiB | killed 143, no_coverage 70, survived 174 | agree (no coverage) 5, agree: killed 141, agree: survived 5, missed kill 169, no coverage, killable 14, oracle equivalent_tce 27, oracle invalid 24, unconfirmed kill 2 | 508 passed, 1 skipped in 0.39s |
| python-semver | irradiate | no verdicts (exit 1: Exit codes: 3=internal error, 4=usage error, 5=no tests collected.) | 2 s | 512 MiB |  |  | 508 passed, 1 skipped in 0.36s |
| python-semver | deployproof | ran | 282 s | 111 MiB | killed 307, survived 37 | agree: killed 307, agree: survived 33, missed kill 4 | 508 passed, 1 skipped in 0.36s |
| cachetools | balamut | ran | 93 s | 853 MiB | error 12, killed 468, no_coverage 4, survived 26, timeout 9 | agree (no coverage) 1, agree: killed 468, agree: survived 26, error 12, no coverage, killable 3, timeout 9 | 391 passed, 24 subtests passed in 0.22s |
| cachetools | mutmut | ran | 9 s | 392 MiB | killed 585, survived 74 | agree: killed 585, agree: survived 67, missed kill 7 | 391 passed, 24 subtests passed in 0.21s |
| cachetools | cosmic-ray | ran | 643 s | 139 MiB | killed 489, survived 87 | agree: killed 489, agree: survived 87 | 391 passed, 24 subtests passed in 0.20s |
| cachetools | poodle | ran | 151 s | 244 MiB | killed 494, survived 74, timeout 5 | agree: killed 465, agree: survived 74, oracle invalid 29, timeout 5 | 391 passed, 24 subtests passed in 0.20s |
| cachetools | pytest-gremlins | ran | 165 s | 123 MiB | killed 161, survived 10, timeout 1 | agree: killed 161, agree: survived 10, timeout 1 | 391 passed, 24 subtests passed in 0.20s |
| cachetools | pytest-leela | ran | 12 s | 88 MiB | survived 195 | agree: survived 13, missed kill 182 | 391 passed, 24 subtests passed in 0.20s |
| cachetools | mutation | ran | 608 s | 674 MiB | killed 2868 | agree: killed 2621, oracle invalid 4, unconfirmed kill 232, unmapped 11 | 391 passed, 24 subtests passed in 0.24s |
| cachetools | universalmutator | ran | 1314 s | 3072 MiB | killed 1037, survived 143 | agree: killed 1036, agree: survived 143, oracle unresolved 1 | 391 passed, 24 subtests passed in 0.20s |
| cachetools | fest | ran | 4 s | 158 MiB | error 4, killed 218, no_coverage 64, survived 35 | agree (no coverage) 22, agree: killed 207, missed kill 35, no coverage, killable 16, oracle equivalent_tce 17, oracle invalid 13, unconfirmed kill 11 | 391 passed, 24 subtests passed in 0.20s |
| cachetools | irradiate | ran | 39 s | 344 MiB | killed 215, no_coverage 393, survived 309, timeout 10 | agree (no coverage) 41, agree: killed 215, agree: survived 61, missed kill 248, no coverage, killable 352, timeout 10 | 391 passed, 24 subtests passed in 0.20s |
| cachetools | deployproof | ran | 134 s | 97 MiB | error 1, killed 161, survived 97 | agree: survived 37, error, count only 1, killed, count only 161, missed kill 59, unmapped 1 | 391 passed, 24 subtests passed in 0.20s |
| humanize | balamut | ran | 5 s | 539 MiB | killed 191, no_coverage 105, survived 26 | agree (no coverage) 21, agree: killed 191, agree: survived 26, no coverage, killable 84 | 744 passed, 134 skipped in 0.74s |
| humanize | mutmut | no verdicts (exit 1: Failed to run clean test) | 3 s | 182 MiB | skipped 379 | skipped 379 | 744 passed, 134 skipped in 0.73s |
| humanize | cosmic-ray | ran | 845 s | 272 MiB | killed 678, survived 100 | agree: killed 677, agree: survived 100, oracle invalid 1 | 744 passed, 134 skipped in 0.72s |
| humanize | poodle | ran | 179 s | 388 MiB | killed 449, survived 98 | agree: killed 427, agree: survived 98, oracle invalid 22 | 744 passed, 134 skipped in 0.72s |
| humanize | pytest-gremlins | ran | 133 s | 132 MiB | killed 128, survived 24 | agree: killed 128, agree: survived 24 | 744 passed, 134 skipped in 0.80s |
| humanize | pytest-leela | ran | 13 s | 89 MiB | killed 156, survived 14 | agree: killed 156, agree: survived 14 | 744 passed, 134 skipped in 0.73s |
| humanize | mutation | ran | 423 s | 775 MiB | killed 1932 | agree: killed 1696, unconfirmed kill 215, unmapped 21 | 744 passed, 134 skipped in 0.78s |
| humanize | universalmutator | ran | 819 s | 109 MiB | killed 785, survived 104 | agree: killed 785, agree: survived 103, oracle equivalent_tce 1 | 744 passed, 134 skipped in 0.72s |
| humanize | fest | ran | 4 s | 168 MiB | error 1, killed 22, no_coverage 116, survived 221 | agree (no coverage) 55, agree: killed 22, agree: survived 17, missed kill 203, no coverage, killable 52, oracle equivalent_tce 9, oracle invalid 1, oracle noop 1 | 744 passed, 134 skipped in 0.72s |
| humanize | irradiate | ran | 23 s | 765 MiB | killed 528, survived 73, timeout 4 | agree: killed 528, agree: survived 64, missed kill 9, timeout 4 | 744 passed, 134 skipped in 0.72s |
| humanize | deployproof | no verdicts (exit 2: ERROR at setup of test_apnumber \| ERROR at teardown of test_apnumber) | 15 s | 108 MiB |  |  | 744 passed, 134 skipped in 0.72s |
| toolz | balamut | ran | 117 s | 3072 MiB | error 2, killed 313, no_coverage 4, survived 13 | agree: killed 312, agree: survived 13, error 2, no coverage, killable 4, oracle unresolved 1 | 188 passed in 0.41s |
| toolz | mutmut | ran | 19 s | 2573 MiB | killed 445, survived 30, timeout 6 | agree: killed 445, agree: survived 30, oracle unresolved 1, timeout 5 | 188 passed in 0.39s |
| toolz | cosmic-ray | ran | 1629 s | 161 MiB | killed 487, survived 61 | agree: killed 487, agree: survived 61 | 188 passed in 0.39s |
| toolz | poodle | ran | 171 s | 3072 MiB | killed 422, survived 32, timeout 5 | agree: killed 415, agree: survived 32, oracle invalid 5, oracle unresolved 1, timeout 5, unconfirmed kill 1 | 188 passed in 0.39s |
| toolz | pytest-gremlins | ran | 210 s | 135 MiB | killed 136, survived 5, timeout 3 | agree: killed 136, agree: survived 5, timeout 3 | 188 passed in 0.37s |
| toolz | pytest-leela | failed (the tool's command ran past the 7200 s limit) | n/a | n/a |  |  |  |
| toolz | mutation | ran | 741 s | 3072 MiB | killed 1818, survived 113 | agree: killed 1775, agree: survived 105, missed kill 8, oracle unresolved 3, unmapped 40 | 188 passed in 0.42s |
| toolz | universalmutator | ran | 1449 s | 130 MiB | killed 1043, survived 93 | agree: killed 1043, agree: survived 93 | 188 passed in 0.39s |
| toolz | fest | ran | 5 s | 444 MiB | killed 60, no_coverage 74, survived 221 | agree (no coverage) 1, agree: killed 60, agree: survived 9, missed kill 212, no coverage, killable 37, oracle equivalent_tce 36 | 188 passed in 0.39s |
| toolz | irradiate | ran | 108 s | 1552 MiB | killed 755, survived 55, timeout 13 | agree: killed 755, agree: survived 46, missed kill 9, timeout 13 | 188 passed in 0.38s |
| toolz | deployproof | no verdicts (exit 2: overage.exceptions.CoverageWarning: Dynamic contexts aren't supported with core=sysmon; context data may be incomplete (no-sysmon-context); see https://coverage) | 4 s | 104 MiB |  |  | 188 passed in 0.40s |
| sqlparse | balamut | ran | 345 s | 1492 MiB | error 6, killed 195, no_coverage 8, survived 29 | agree (no coverage) 6, agree: killed 195, agree: survived 29, error 6, no coverage, killable 2 | 537 passed, 2 xfailed, 1 xpassed in 1.18s |
| sqlparse | mutmut | ran | 27 s | 1585 MiB | killed 264, survived 65, timeout 9 | agree: killed 264, agree: survived 62, missed kill 3, oracle unresolved 1, timeout 8 | 537 passed, 2 xfailed, 1 xpassed in 1.17s |
| sqlparse | cosmic-ray | ran | 999 s | 3072 MiB | killed 282, survived 73 | agree: killed 281, agree: survived 73, oracle unresolved 1 | 537 passed, 2 xfailed, 1 xpassed in 1.14s |
| sqlparse | poodle | ran | 205 s | 3072 MiB | killed 269, survived 54, timeout 12 | agree: killed 265, agree: survived 54, oracle invalid 3, oracle unresolved 2, timeout 11 | 537 passed, 2 xfailed, 1 xpassed in 1.19s |
| sqlparse | pytest-gremlins | ran | 110 s | 156 MiB | killed 61, survived 18, timeout 1 | agree: killed 61, agree: survived 18, timeout 1 | 537 passed, 2 xfailed, 1 xpassed in 1.16s |
| sqlparse | pytest-leela | failed (the tool's command ran past the 7200 s limit) | n/a | n/a |  |  |  |
| sqlparse | mutation | ran | 700 s | 3072 MiB | killed 478, survived 795 | agree: killed 465, agree: survived 152, missed kill 638, oracle unresolved 6, unmapped 12 | 537 passed, 2 xfailed, 1 xpassed in 1.18s |
| sqlparse | universalmutator | ran | 1840 s | 3072 MiB | killed 478, survived 97 | agree: killed 472, agree: survived 97, oracle unresolved 6 | 537 passed, 2 xfailed, 1 xpassed in 1.18s |
| sqlparse | fest | ran | 233 s | 2914 MiB | error 1, killed 54, no_coverage 23, survived 96, timeout 65 | agree (no coverage) 4, agree: killed 54, agree: survived 24, missed kill 72, no coverage, killable 9, oracle equivalent_tce 6, oracle invalid 5, oracle unresolved 1, timeout 64 | 537 passed, 2 xfailed, 1 xpassed in 1.17s |
| sqlparse | irradiate | ran | 199 s | 1893 MiB | killed 429, no_coverage 19, survived 86, timeout 26 | agree (no coverage) 4, agree: killed 429, agree: survived 85, missed kill 1, no coverage, killable 15, oracle unresolved 4, timeout 22 | 537 passed, 2 xfailed, 1 xpassed in 1.16s |
| sqlparse | deployproof | ran | 299 s | 138 MiB | error 1, killed 151, survived 33 | agree: survived 33, error, count only 1, killed, count only 151 | 537 passed, 2 xfailed, 1 xpassed in 1.20s |
| attrs | balamut | ran | 14 s | 1120 MiB | killed 273, no_coverage 4, survived 17 | agree (no coverage) 1, agree: killed 273, agree: survived 17, no coverage, killable 3 | 1407 passed, 4 skipped, 2 xfailed in 4.53s |
| attrs | mutmut | no verdicts (exit 1: Failed to run clean test) | 7 s | 324 MiB | skipped 370 | skipped 370 | 1407 passed, 4 skipped, 2 xfailed in 4.51s |
| attrs | cosmic-ray | ran | 792 s | 355 MiB | killed 198, survived 25 | agree: killed 188, agree: survived 25, oracle invalid 10 | 1407 passed, 4 skipped, 2 xfailed in 4.37s |
| attrs | poodle | no verdicts (exit 3: Clean Run Failed) | 2 s | 328 MiB |  |  | 1407 passed, 4 skipped, 2 xfailed in 4.44s |
| attrs | pytest-gremlins | ran | 302 s | 304 MiB | killed 92, survived 16 | agree: killed 92, agree: survived 16 | 1407 passed, 4 skipped, 2 xfailed in 4.40s |
| attrs | pytest-leela | ran | 20 s | 300 MiB | killed 85 | agree: killed 85 | 1407 passed, 4 skipped, 2 xfailed in 4.56s |
| attrs | mutation | no verdicts (exit 2: ERROR I tried the following command: `python3 -m pytest -p mutation --exitfirst --no-header --tb=no --quiet --assert=plain tests --cov=. --cov-branch --no-cov-o) | 12 s | 2680 MiB |  |  | 1407 passed, 4 skipped, 2 xfailed in 4.65s |
| attrs | universalmutator | ran | 2334 s | 328 MiB | killed 501, survived 90 | agree: killed 501, agree: survived 90 | 1407 passed, 4 skipped, 2 xfailed in 4.49s |
| attrs | fest | ran | 16 s | 414 MiB | error 7, killed 62, no_coverage 72, survived 97 | agree (no coverage) 31, agree: killed 62, agree: survived 14, error 7, missed kill 80, no coverage, killable 37, oracle equivalent_tce 5, oracle noop 2 | 1407 passed, 4 skipped, 2 xfailed in 4.30s |
| attrs | irradiate | ran | 56 s | 557 MiB | killed 274, no_coverage 9, survived 34 | agree: killed 274, agree: survived 24, missed kill 10, no coverage, killable 9 | 1407 passed, 4 skipped, 2 xfailed in 4.51s |
| attrs | deployproof | ran | 617 s | 338 MiB | killed 137, survived 19 | agree: killed 137, agree: survived 19 | 1407 passed, 4 skipped, 2 xfailed in 4.45s |
