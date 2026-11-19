### At a glance

Rank: the 95% interval of the tool's rank by pre-fix detection over the project bootstrap (ties share the mean rank). Pre-fix detection: bugs with a witness that meets the three conditions. Fixed program: the two-condition witness (comparable with Just et al. 2014). R(10), S(3): pre-fix witnesses within 10 random emissions or 3 random survivors. Emissions: the median number of in-scope emissions per bug.

| tool | rank | pre-fix detection (95% CI) | fixed program | R(10) | S(3) | emissions / bug |
|---|---|---|---|---|---|---|
| mutation | 1–2 | 24% (15%–33%) | 62% | 8% | 11% | 130 |
| universalmutator | 1–5 | 19% (13%–26%) | 57% | 8% | 7% | 55 |
| poodle | 2–6 | 17% (10%–25%) | 55% | 9% | 9% | 38 |
| irradiate | 2–8 | 16% (10%–23%) | 47% | 6% | 7% | 38 |
| mutmut | 2–8 | 16% (10%–22%) | 44% | 7% | 8% | 34 |
| cosmic-ray | 3–9 | 14% (9%–20%) | 43% | 8% | 7% | 40 |
| deployproof | 3–8 | 14% (8%–21%) | 44% | 9% | 9% | 21 |
| fest | 4–9 | 13% (7%–19%) | 41% | 8% | 9% | 23 |
| balamut | 6–9 | 11% (7%–16%) | 40% | 7% | 7% | 24 |
| pytest-leela | 10–11 | 7% (4%–11%) | 29% | 5% | 5% | 9 |
| pytest-gremlins | 10–11 | 6% (3%–11%) | 25% | 5% | 5% | 8 |
| coverage only | 1–2 | 31% (25%–37%) | – | – | – | – |

Coverage only: the bugs whose fail-to-pass tests execute a line of the scope that no context of the historical suite executed (line coverage alone points at the gap; retrospective, every fail-to-pass test counts, feature tests too). Its rank is the interval it would take among the tools.

### Detection

Operational rates: a bug on which a tool is unavailable (its generator failed or its in-scope emissions mostly have no faithful source text) counts as not detected; the conditional rate is over the bugs the tool is available on. Intervals: 95% percentile bootstrap over projects, pointwise; BCa beside it as a sensitivity check.

| tool | pre-fix bugs | rate (95% CI) | BCa | project mean | leave one project out | without arrow | conditional rate (bugs) | unavailable |
|---|---|---|---|---|---|---|---|---|
| mutation | 77/325 | 24% (15%–33%) | 16%–34% | 22% | 21%–25% | 21% | 24% (325) | 0 |
| universalmutator | 62/325 | 19% (13%–26%) | 14%–27% | 18% | 17%–20% | 19% | 19% (325) | 0 |
| poodle | 56/325 | 17% (10%–25%) | 11%–27% | 16% | 15%–18% | 15% | 17% (325) | 0 |
| irradiate | 52/325 | 16% (10%–23%) | 10%–25% | 15% | 14%–17% | 17% | 16% (325) | 0 |
| mutmut | 51/325 | 16% (10%–22%) | 11%–22% | 15% | 14%–16% | 16% | 16% (325) | 0 |
| cosmic-ray | 46/325 | 14% (9%–20%) | 10%–22% | 13% | 12%–15% | 14% | 14% (325) | 0 |
| deployproof | 46/325 | 14% (8%–21%) | 9%–23% | 13% | 12%–15% | 12% | 14% (325) | 0 |
| fest | 41/325 | 13% (7%–19%) | 8%–21% | 12% | 10%–13% | 10% | 13% (325) | 0 |
| balamut | 37/325 | 11% (7%–16%) | 7%–17% | 11% | 10%–12% | 12% | 11% (325) | 0 |
| pytest-leela | 22/325 | 7% (4%–11%) | 4%–12% | 6% | 5%–7% | 7% | 7% (325) | 0 |
| pytest-gremlins | 21/325 | 6% (3%–11%) | 3%–13% | 6% | 5%–7% | 7% | 6% (325) | 0 |

### Detection by witness location

Where the witnesses of the fixed program lie relative to the fix (lines whose stripped text a mutant changes, against the fix's `-U0` hunks). Fixed program: the two conditions. Location existed: a witness on a line the fix rewrote or did not touch (an upper approximation of the third condition); untouched code: a witness the fix did not touch (a lower one); executed: such a witness on a line a historical test executes. Only inserted lines: every witness on lines the fix added, which no tool could show before the fix (share of the fixed-program detections). Pre-fix: the exact third condition, judged on the buggy parent.

| tool | fixed program | location existed | untouched code | untouched, executed | only inserted lines | pre-fix | pre-fix rank |
|---|---|---|---|---|---|---|---|
| mutation | 201 (61.8%) | 138 (42.5%) | 62 (19.1%) | 51 (15.7%) | 63 (31%) | 77 (23.7%) | 1–2 |
| universalmutator | 184 (56.6%) | 123 (37.8%) | 51 (15.7%) | 35 (10.8%) | 61 (33%) | 62 (19.1%) | 1–5 |
| poodle | 178 (54.8%) | 108 (33.2%) | 50 (15.4%) | 26 (8.0%) | 70 (39%) | 56 (17.2%) | 2–6 |
| irradiate | 152 (46.8%) | 95 (29.2%) | 42 (12.9%) | 35 (10.8%) | 57 (38%) | 52 (16.0%) | 2–8 |
| mutmut | 142 (43.7%) | 94 (28.9%) | 38 (11.7%) | 29 (8.9%) | 48 (34%) | 51 (15.7%) | 2–8 |
| cosmic-ray | 139 (42.8%) | 91 (28.0%) | 38 (11.7%) | 27 (8.3%) | 48 (35%) | 46 (14.2%) | 3–9 |
| deployproof | 143 (44.0%) | 84 (25.8%) | 38 (11.7%) | 17 (5.2%) | 59 (41%) | 46 (14.2%) | 3–8 |
| fest | 134 (41.2%) | 80 (24.6%) | 36 (11.1%) | 16 (4.9%) | 54 (40%) | 41 (12.6%) | 4–9 |
| balamut | 131 (40.3%) | 79 (24.3%) | 25 (7.7%) | 14 (4.3%) | 52 (40%) | 37 (11.4%) | 6–9 |
| pytest-leela | 93 (28.6%) | 52 (16.0%) | 20 (6.2%) | 10 (3.1%) | 41 (44%) | 22 (6.8%) | 10–11 |
| pytest-gremlins | 81 (24.9%) | 45 (13.8%) | 18 (5.5%) | 8 (2.5%) | 36 (44%) | 21 (6.5%) | 10–11 |
| all tools | 69.5% | 51.7% | 24.3% | 17.5% | – | 29.5% | – |

### Metric reliability

For each candidate headline measure: the pairs of tools (of all pairs) whose paired 95% interval excludes zero, the median width of those intervals, and the median Spearman correlation of the tools' order between two random halves of the projects (2,000 splits).

| measure | separable pairs | median paired CI width | split-half Spearman |
|---|---|---|---|
| pre-fix detection | 33/55 | 10.2 pp | 0.71 |
| fixed-program detection | 37/55 | 14.3 pp | 0.82 |
| location existed | 34/55 | 12.7 pp | 0.81 |
| untouched code | 31/55 | 8.9 pp | 0.75 |
| untouched code, executed | 37/55 | 6.0 pp | 0.89 |
| R(10), pre-fix | 10/55 | 6.6 pp | 0.10 |
| S(3), pre-fix | 15/55 | 7.8 pp | 0.30 |
| R(1), pre-fix | 6/55 | 2.5 pp | -0.11 |
| S(1), pre-fix | 5/55 | 5.1 pp | -0.04 |

### Rank intervals

95% intervals of each tool's rank over the project bootstrap, per measure (1 = highest).

| tool | pre-fix | fixed program | location existed | untouched code | R(10) | S(3) |
|---|---|---|---|---|---|---|
| mutation | 1–2 | 1–2 | 1–2 | 1–2 | 1–11 | 1–7 |
| universalmutator | 1–5 | 1–3 | 1–3 | 1–5 | 1–9 | 2–9 |
| poodle | 2–6 | 2–3 | 2–5 | 1–6 | 1–7 | 2–9 |
| irradiate | 2–8 | 4–8 | 3–8 | 2–8 | 2–11 | 2–11 |
| mutmut | 2–8 | 4–9 | 3–8 | 3–8 | 1–8 | 1–9 |
| cosmic-ray | 3–9 | 4–9 | 3–8 | 3–8 | 1–9 | 1–9 |
| deployproof | 3–8 | 4–8 | 4–9 | 3–8 | 1–8 | 1–8 |
| fest | 4–9 | 5–9 | 5–9 | 4–9 | 3–9 | 2–9 |
| balamut | 6–9 | 6–9 | 6–9 | 8–10 | 3–10 | 2–9 |
| pytest-leela | 10–11 | 10–11 | 10–11 | 9–11 | 8–11 | 8–11 |
| pytest-gremlins | 10–11 | 10–11 | 10–11 | 10–11 | 7–11 | 9–11 |

### Pairs (pre-fix detection)

Every pair of tools over all 325 bugs: bugs only the first or only the second detects, the difference of their rates with its 95% project-bootstrap interval, the two-sided bootstrap p-value and Holm's adjustment over all 55 pairs. Minimum detectable difference (half the median interval width): ±5.1 points.

| first | second | first only | second only | difference (95% CI) | p | Holm |
|---|---|---|---|---|---|---|
| mutation | universalmutator | 27 | 12 | +4.6 pp (-0.6 to +11.0) | 0.1 | 1 |
| mutation | irradiate | 31 | 6 | +7.7 pp (+2.4 to +15.3) | 0.0006 | 0.0204 |
| mutation | deployproof | 37 | 6 | +9.5 pp (+4.4 to +15.6) | <0.0001 | 0.0055 |
| mutation | fest | 41 | 5 | +11.1 pp (+6.6 to +16.2) | <0.0001 | 0.0055 |
| universalmutator | irradiate | 25 | 15 | +3.1 pp (-1.0 to +7.2) | 0.164 | 1 |
| universalmutator | deployproof | 32 | 16 | +4.9 pp (-1.9 to +11.2) | 0.163 | 1 |
| universalmutator | fest | 32 | 11 | +6.5 pp (+0.3 to +12.0) | 0.05 | 1 |
| mutation | poodle | 30 | 9 | +6.5 pp (+2.1 to +11.4) | 0.0012 | 0.0372 |
| universalmutator | poodle | 25 | 19 | +1.8 pp (-4.4 to +6.9) | 0.531 | 1 |
| poodle | irradiate | 21 | 17 | +1.2 pp (-4.7 to +9.0) | 0.823 | 1 |
| poodle | deployproof | 11 | 1 | +3.1 pp (+0.6 to +6.1) | 0.0148 | 0.385 |
| poodle | fest | 20 | 5 | +4.6 pp (+2.4 to +7.3) | <0.0001 | 0.0055 |
| poodle | pytest-leela | 37 | 3 | +10.5 pp (+5.4 to +16.9) | <0.0001 | 0.0055 |
| poodle | pytest-gremlins | 36 | 1 | +10.8 pp (+5.5 to +17.8) | <0.0001 | 0.0055 |
| irradiate | deployproof | 24 | 18 | +1.8 pp (-6.0 to +8.6) | 0.62 | 1 |
| mutation | mutmut | 34 | 8 | +8.0 pp (+1.3 to +16.3) | 0.0134 | 0.362 |
| universalmutator | mutmut | 27 | 16 | +3.4 pp (-1.4 to +8.1) | 0.195 | 1 |
| poodle | mutmut | 24 | 19 | +1.5 pp (-4.3 to +9.2) | 0.752 | 1 |
| irradiate | mutmut | 9 | 8 | +0.3 pp (-2.6 to +3.7) | 0.997 | 1 |
| mutmut | cosmic-ray | 24 | 19 | +1.5 pp (-2.9 to +6.2) | 0.561 | 1 |
| mutmut | deployproof | 23 | 18 | +1.5 pp (-5.8 to +7.4) | 0.642 | 1 |
| mutmut | fest | 28 | 18 | +3.1 pp (-4.3 to +8.7) | 0.377 | 1 |
| mutmut | pytest-leela | 35 | 6 | +8.9 pp (+5.0 to +12.9) | <0.0001 | 0.0055 |
| mutmut | pytest-gremlins | 35 | 5 | +9.2 pp (+5.7 to +13.0) | <0.0001 | 0.0055 |
| mutation | cosmic-ray | 41 | 10 | +9.5 pp (+3.2 to +17.1) | 0.001 | 0.032 |
| universalmutator | cosmic-ray | 25 | 9 | +4.9 pp (+2.0 to +7.8) | 0.0008 | 0.0264 |
| poodle | cosmic-ray | 26 | 16 | +3.1 pp (-2.1 to +9.6) | 0.321 | 1 |
| irradiate | cosmic-ray | 27 | 21 | +1.8 pp (-2.8 to +6.6) | 0.494 | 1 |
| cosmic-ray | deployproof | 20 | 20 | +0.0 pp (-6.9 to +5.9) | 0.998 | 1 |
| cosmic-ray | fest | 22 | 17 | +1.5 pp (-5.0 to +7.0) | 0.62 | 1 |
| cosmic-ray | pytest-leela | 28 | 4 | +7.4 pp (+3.9 to +11.0) | <0.0001 | 0.0055 |
| cosmic-ray | pytest-gremlins | 27 | 2 | +7.7 pp (+4.5 to +10.8) | <0.0001 | 0.0055 |
| irradiate | fest | 30 | 19 | +3.4 pp (-4.3 to +9.5) | 0.36 | 1 |
| deployproof | fest | 10 | 5 | +1.5 pp (-0.0 to +3.4) | 0.0846 | 1 |
| mutation | balamut | 44 | 4 | +12.3 pp (+6.0 to +19.8) | <0.0001 | 0.0055 |
| universalmutator | balamut | 32 | 7 | +7.7 pp (+4.3 to +11.2) | <0.0001 | 0.0055 |
| poodle | balamut | 29 | 10 | +5.8 pp (+1.1 to +12.3) | 0.0102 | 0.286 |
| irradiate | balamut | 23 | 8 | +4.6 pp (+0.6 to +8.8) | 0.0262 | 0.629 |
| mutmut | balamut | 20 | 6 | +4.3 pp (+0.9 to +7.9) | 0.0164 | 0.41 |
| cosmic-ray | balamut | 22 | 13 | +2.8 pp (-1.0 to +6.7) | 0.185 | 1 |
| deployproof | balamut | 23 | 14 | +2.8 pp (-2.1 to +9.0) | 0.366 | 1 |
| fest | balamut | 18 | 14 | +1.2 pp (-3.3 to +7.4) | 0.778 | 1 |
| balamut | pytest-leela | 18 | 3 | +4.6 pp (+2.1 to +7.3) | 0.0002 | 0.0072 |
| balamut | pytest-gremlins | 19 | 3 | +4.9 pp (+2.3 to +8.0) | <0.0001 | 0.0055 |
| mutation | pytest-leela | 57 | 2 | +16.9 pp (+10.1 to +24.8) | <0.0001 | 0.0055 |
| universalmutator | pytest-leela | 42 | 2 | +12.3 pp (+8.2 to +16.9) | <0.0001 | 0.0055 |
| irradiate | pytest-leela | 37 | 7 | +9.2 pp (+4.6 to +14.6) | <0.0001 | 0.0055 |
| deployproof | pytest-leela | 27 | 3 | +7.4 pp (+3.0 to +13.2) | <0.0001 | 0.0055 |
| fest | pytest-leela | 22 | 3 | +5.8 pp (+1.7 to +11.6) | 0.0026 | 0.078 |
| mutation | pytest-gremlins | 59 | 3 | +17.2 pp (+9.9 to +25.8) | <0.0001 | 0.0055 |
| universalmutator | pytest-gremlins | 43 | 2 | +12.6 pp (+8.5 to +17.0) | <0.0001 | 0.0055 |
| irradiate | pytest-gremlins | 37 | 6 | +9.5 pp (+5.2 to +14.6) | <0.0001 | 0.0055 |
| deployproof | pytest-gremlins | 27 | 2 | +7.7 pp (+2.6 to +14.5) | 0.0002 | 0.0072 |
| fest | pytest-gremlins | 23 | 3 | +6.2 pp (+1.4 to +12.7) | 0.004 | 0.116 |
| pytest-leela | pytest-gremlins | 5 | 4 | +0.3 pp (-1.4 to +2.2) | 0.889 | 1 |

### Budgets

R(q): q of the tool's in-scope emissions drawn without replacement (unmaterializable ones included). S(k): k of the unique artifacts of the tool that the reference oracle judges survivors (the tool's own engine may report others: S(k) rates the generator under a faithful engine). Both use min(budget, population) draws and are 0 for an empty population; witnesses are pre-fix ones unless marked fixed program. S(3) executed: only survivors on lines the historical suite executes (a test or the import); S(3) by line: lines with a survivor as the unit, a line with a witness as the hit. R(1) and S(1) are omitted: their tool order does not repeat between halves of the projects.

| tool | R(10) (95% CI) | R(100) | S(3) (95% CI) | S(10) | S(3) executed | S(3) by line | R(10) fixed program | S(3) fixed program | emissions / bug | survivors / bug |
|---|---|---|---|---|---|---|---|---|---|---|
| mutation | 8% (3%–15%) | 18% | 11% (5%–17%) | 16% | 11% | 13% | 22% | 35% | 130 | 18 |
| universalmutator | 8% (4%–12%) | 16% | 7% (5%–11%) | 14% | 8% | 10% | 26% | 33% | 55 | 10 |
| poodle | 9% (5%–15%) | 16% | 9% (5%–15%) | 14% | 9% | 11% | 31% | 36% | 38 | 8 |
| irradiate | 6% (3%–10%) | 14% | 7% (3%–11%) | 12% | 7% | 9% | 24% | 30% | 38 | 7 |
| mutmut | 7% (4%–11%) | 14% | 8% (4%–11%) | 12% | 7% | 10% | 23% | 27% | 34 | 6 |
| cosmic-ray | 8% (4%–11%) | 13% | 7% (4%–11%) | 11% | 6% | 10% | 22% | 26% | 40 | 8 |
| deployproof | 9% (5%–15%) | 14% | 9% (5%–15%) | 12% | 8% | 10% | 29% | 33% | 21 | 4 |
| fest | 8% (4%–13%) | 12% | 9% (5%–14%) | 12% | 6% | 9% | 27% | 33% | 23 | 3 |
| balamut | 7% (3%–10%) | 11% | 7% (4%–10%) | 11% | 5% | 9% | 24% | 30% | 24 | 3 |
| pytest-leela | 5% (3%–8%) | 7% | 5% (3%–8%) | 7% | 3% | 6% | 21% | 24% | 9 | 1 |
| pytest-gremlins | 5% (2%–9%) | 6% | 5% (2%–8%) | 6% | 3% | 5% | 20% | 21% | 8 | 1 |

### Witnesses and coverage

What the historical suite executes on the lines of each witness of the fixed program: a test (`test`), only the import of the module or the class body (`import`), or nothing (`never`); then the bugs by the best of the tool's witnesses. Engines that report a mutant on a line no test executes as "no coverage" show the user no survivor there.

| tool | witnesses: test / import / never | bugs: best witness under a test | on an import line | only on never-executed lines (share of fixed-program detections) | pre-fix under a test |
|---|---|---|---|---|---|
| mutation | 75% / 25% / 0% | 176 | 25 | 0 (0%) | 62 (19.1%) |
| universalmutator | 37% / 6% / 57% | 139 | 14 | 31 (17%) | 45 (13.8%) |
| poodle | 32% / 22% / 46% | 125 | 26 | 27 (15%) | 30 (9.2%) |
| irradiate | 46% / 0% / 54% | 139 | 4 | 9 (6%) | 43 (13.2%) |
| mutmut | 40% / 2% / 58% | 113 | 5 | 24 (17%) | 39 (12.0%) |
| cosmic-ray | 39% / 5% / 55% | 108 | 8 | 23 (17%) | 33 (10.2%) |
| deployproof | 27% / 37% / 36% | 86 | 26 | 31 (22%) | 22 (6.8%) |
| fest | 32% / 23% / 45% | 84 | 17 | 33 (25%) | 18 (5.5%) |
| balamut | 36% / 4% / 60% | 84 | 5 | 42 (32%) | 23 (7.1%) |
| pytest-leela | 41% / 3% / 56% | 54 | 2 | 37 (40%) | 12 (3.7%) |
| pytest-gremlins | 39% / 3% / 58% | 45 | 3 | 33 (41%) | 10 (3.1%) |

### Mutant accounting (unique in-scope mutants, bugs the tool is available on)

Unmaterializable: in-scope emissions for which the tool's adapter found no faithful source text; they count in R(q) and are never judged.

| tool | in-scope emissions | unmaterializable | unique | killed | survived | of which witnesses | of which pre-fix | invalid | equivalent (TCE) | no-op | unresolved |
|---|---|---|---|---|---|---|---|---|---|---|---|
| mutation | 106243 | 1519 | 101501 | 87559 | 13612 | 1552 | 609 | 77 | 200 | 0 | 53 |
| universalmutator | 46564 | 0 | 46564 | 38319 | 8171 | 1342 | 562 | 0 | 3 | 0 | 71 |
| irradiate | 37679 | 0 | 37617 | 30847 | 6701 | 1092 | 348 | 0 | 36 | 1 | 32 |
| cosmic-ray | 35586 | 1 | 35585 | 27203 | 7987 | 1202 | 680 | 351 | 22 | 0 | 22 |
| mutmut | 28937 | 0 | 28929 | 23073 | 5823 | 834 | 340 | 0 | 6 | 0 | 27 |
| poodle | 28511 | 0 | 28104 | 20410 | 6698 | 980 | 431 | 978 | 2 | 0 | 16 |
| balamut | 19945 | 0 | 19680 | 16666 | 2999 | 530 | 198 | 2 | 0 | 0 | 13 |
| fest | 16979 | 0 | 16928 | 12684 | 3925 | 609 | 259 | 96 | 208 | 9 | 6 |
| deployproof | 16897 | 2 | 16894 | 12264 | 4597 | 720 | 373 | 0 | 20 | 0 | 13 |
| pytest-leela | 7444 | 0 | 7262 | 6187 | 1069 | 249 | 96 | 0 | 0 | 0 | 6 |
| pytest-gremlins | 6284 | 1 | 6282 | 5007 | 1268 | 236 | 85 | 0 | 0 | 0 | 7 |

### Witness flow (unique artifacts)

A first-judgement witness counts when the re-judgement from fresh trees reproduces it (re-judgements without an outcome are unknown, the others not reproduced), the same fail-to-pass test is red with an outcome of its own in both judgements (red only through a collection or startup failure of the whole run is synthetic) and that test checks the fault (otherwise feature only). Pre-fix: of those, the witnesses whose counterpart in the buggy parent survives the historical suite; undecided: the counterpart's verdict is missing or unresolved, or no faithful counterpart could be written.

| tool | first-judgement witnesses | re-judgement unknown | not reproduced | synthetic | feature only | witnesses | in untouched / rewritten / inserted lines | pre-fix | pre-fix undecided |
|---|---|---|---|---|---|---|---|---|---|
| mutation | 1629 | 0 | 14 | 0 | 63 | 1552 | 511 / 494 / 547 | 609 | 2 |
| universalmutator | 1383 | 0 | 4 | 0 | 37 | 1342 | 517 / 382 / 443 | 562 | 4 |
| poodle | 996 | 0 | 1 | 0 | 15 | 980 | 377 / 280 / 323 | 431 | 0 |
| irradiate | 1116 | 0 | 2 | 0 | 22 | 1092 | 316 / 325 / 451 | 348 | 0 |
| mutmut | 847 | 0 | 1 | 0 | 12 | 834 | 278 / 291 / 265 | 340 | 0 |
| cosmic-ray | 1229 | 0 | 9 | 0 | 18 | 1202 | 593 / 312 / 297 | 680 | 0 |
| deployproof | 741 | 0 | 0 | 0 | 21 | 720 | 313 / 205 / 202 | 373 | 0 |
| fest | 633 | 0 | 2 | 0 | 22 | 609 | 230 / 169 / 210 | 259 | 0 |
| balamut | 558 | 0 | 1 | 0 | 27 | 530 | 165 / 189 / 176 | 198 | 0 |
| pytest-leela | 259 | 0 | 1 | 0 | 9 | 249 | 86 / 76 / 87 | 96 | 0 |
| pytest-gremlins | 246 | 0 | 0 | 0 | 10 | 236 | 79 / 59 / 98 | 85 | 0 |

### Undecided outcomes

Artifacts that could still be pre-fix witnesses: an unresolved historical run, a survivor whose fail-to-pass run gave no outcome, a witness whose re-judgement gave none, or a witness whose buggy-side counterpart is undecided. The optimistic rate counts every bug with one of them as detected.

| tool | undecided artifacts | bugs undetected with an undecided artifact | rate | optimistic rate |
|---|---|---|---|---|
| mutation | 56 | 8 | 24% | 28% |
| universalmutator | 75 | 8 | 19% | 24% |
| poodle | 16 | 4 | 17% | 20% |
| irradiate | 32 | 3 | 16% | 19% |
| mutmut | 27 | 4 | 16% | 18% |
| cosmic-ray | 24 | 6 | 14% | 17% |
| deployproof | 13 | 6 | 14% | 16% |
| fest | 6 | 4 | 13% | 14% |
| balamut | 13 | 5 | 11% | 14% |
| pytest-leela | 6 | 5 | 7% | 8% |
| pytest-gremlins | 7 | 5 | 6% | 8% |

### By stratum (pre-fix detection)

Region: inside functions, in class-body or module-level statements, both. Coverage: the fail-to-pass run executes a changed line and the historical suite executed every such line (covered), not every one (gap), or the fail-to-pass run executes none (unrun); every fail-to-pass test counts, feature tests too. Composition: every hunk only adds lines (insertion), every hunk removes lines (rewrite), or both. AI marker: a `Co-authored-by` or `Assisted-by` trailer, a generated-with line or a merged agent branch naming an AI tool.

Where the fix is: function (220 bugs), statement (38 bugs), mixed (67 bugs)

| tool | function | statement | mixed |
|---|---|---|---|
| mutation | 22% | 37% | 21% |
| universalmutator | 20% | 16% | 18% |
| poodle | 15% | 26% | 19% |
| irradiate | 16% | 8% | 19% |
| mutmut | 15% | 11% | 22% |
| cosmic-ray | 14% | 5% | 21% |
| deployproof | 11% | 26% | 16% |
| fest | 10% | 24% | 15% |
| balamut | 11% | 3% | 16% |
| pytest-leela | 7% | 3% | 7% |
| pytest-gremlins | 7% | 0% | 9% |

Coverage of the changed lines (retrospective): covered (222 bugs), gap (97 bugs), unrun (6 bugs)

| tool | covered | gap | unrun |
|---|---|---|---|
| mutation | 20% | 34% | 0% |
| universalmutator | 14% | 30% | 17% |
| poodle | 14% | 24% | 17% |
| irradiate | 12% | 26% | 0% |
| mutmut | 12% | 25% | 0% |
| cosmic-ray | 9% | 26% | 0% |
| deployproof | 12% | 20% | 17% |
| fest | 9% | 22% | 17% |
| balamut | 9% | 19% | 0% |
| pytest-leela | 5% | 12% | 0% |
| pytest-gremlins | 4% | 13% | 0% |

What the fix's hunks do: insertion (52 bugs), rewrite (163 bugs), mixed (110 bugs)

| tool | insertion | rewrite | mixed |
|---|---|---|---|
| mutation | 21% | 21% | 28% |
| universalmutator | 13% | 18% | 23% |
| poodle | 17% | 15% | 20% |
| irradiate | 13% | 15% | 19% |
| mutmut | 12% | 15% | 19% |
| cosmic-ray | 10% | 12% | 20% |
| deployproof | 12% | 12% | 18% |
| fest | 12% | 9% | 18% |
| balamut | 10% | 11% | 13% |
| pytest-leela | 6% | 6% | 9% |
| pytest-gremlins | 6% | 6% | 8% |

Lines the fix inserted or rewrote: 1–4 lines (151 bugs), 5–12 lines (110 bugs), 13 or more lines (64 bugs)

| tool | 1–4 lines | 5–12 lines | 13 or more lines |
|---|---|---|---|
| mutation | 21% | 26% | 27% |
| universalmutator | 15% | 21% | 25% |
| poodle | 15% | 21% | 17% |
| irradiate | 13% | 17% | 20% |
| mutmut | 12% | 17% | 22% |
| cosmic-ray | 11% | 15% | 20% |
| deployproof | 11% | 19% | 14% |
| fest | 9% | 17% | 12% |
| balamut | 7% | 14% | 17% |
| pytest-leela | 5% | 8% | 9% |
| pytest-gremlins | 5% | 7% | 9% |

When the fix was committed: 2021–2025 (112 bugs), 2026 January–June (48 bugs), 2026 July–November (165 bugs)

| tool | 2021–2025 | 2026 January–June | 2026 July–November |
|---|---|---|---|
| mutation | 26% | 15% | 25% |
| universalmutator | 17% | 17% | 21% |
| poodle | 18% | 15% | 18% |
| irradiate | 12% | 17% | 18% |
| mutmut | 14% | 19% | 16% |
| cosmic-ray | 11% | 12% | 17% |
| deployproof | 17% | 10% | 13% |
| fest | 17% | 8% | 11% |
| balamut | 8% | 15% | 13% |
| pytest-leela | 5% | 6% | 8% |
| pytest-gremlins | 4% | 6% | 8% |

An explicit AI-tool marker in the fix commit: AI marker (27 bugs), no marker (298 bugs)

| tool | AI marker | no marker |
|---|---|---|
| mutation | 11% | 25% |
| universalmutator | 15% | 19% |
| poodle | 7% | 18% |
| irradiate | 7% | 17% |
| mutmut | 4% | 17% |
| cosmic-ray | 4% | 15% |
| deployproof | 7% | 15% |
| fest | 11% | 13% |
| balamut | 7% | 12% |
| pytest-leela | 4% | 7% |
| pytest-gremlins | 4% | 7% |

### Secondary measures

Only this tool: bugs no other tool detects (pre-fix). Re-creates the fault: single-file fixes where a mutant whose edit touches a line the fix changed turns the fixed file into the buggy one (equal ASTs). Witness by any fail-to-pass test: the fixed-program witness without the purity filter.

| tool | only this tool | re-creates the fault (applicable bugs) | witness by any fail-to-pass test |
|---|---|---|---|
| mutation | 2 | 2 (296) | 62% |
| universalmutator | 4 | 2 (296) | 57% |
| poodle | 1 | 2 (296) | 55% |
| irradiate | 0 | 0 (296) | 47% |
| mutmut | 1 | 3 (296) | 44% |
| cosmic-ray | 1 | 2 (296) | 43% |
| deployproof | 0 | 0 (296) | 44% |
| fest | 0 | 1 (296) | 42% |
| balamut | 0 | 3 (296) | 41% |
| pytest-leela | 0 | 1 (296) | 29% |
| pytest-gremlins | 0 | 1 (296) | 26% |

Bugs: 325 in 26 projects; detected by at least one tool: 30% (pre-fix), 70% (fixed program). Scope regions left out as holding no change of the program: 36 import, 10 metadata, 17 no code change in 46 bugs (`excluded_regions.json`).

### Corpus

Fix commits by year (committer date): 2021 14, 2022 22, 2023 15, 2024 32, 2025 29, 2026 213. With an explicit AI-tool marker: 27 (inflect 14, marshmallow 1, more-itertools 2, packaging 5, pyparsing 1, python-dotenv 2, python-semver 1, tomlkit 1). Fix composition: insertion 52, mixed 110, rewrite 163; median lines inserted or rewritten: 5.

### Oracle shortcuts

1000 kills by a single test or the covering subset, drawn uniformly without replacement (seed 20261105) from 158040 over 210 bugs, judged again by the whole historical suite in canonical order alone: killed 999, survived 0, other 1 ({"killed": 999, "unresolved": 1}). Killed of the decided outcomes: 100.0% (999/999); of the sample: 99.9% (999/1000). By the first judgement's stage: {"killer": {"killed": 865, "unresolved": 1}, "subset": {"killed": 134}}.

| bug | artifact | first stage | first killed by | whole suite | fault tests red | tools |
|---|---|---|---|---|---|---|
| cattrs@39a4d6ebe1ab | 995fe31a91e5 | killer | tests/test_self.py::test_self_roundtrip_namedtuple[False] | unresolved (<external-kill>) | – | irradiate, mutation, mutmut |

### Oracle code equivalence

1000 judged artifacts drawn uniformly without replacement (seed 20261111) from 222278 over 230 bugs, judged again in bug mode by oracle code 6dde30737b28f496; by the code that recorded the first verdict: {"6dde30737b28f496": {"same": 12}, "ecd9f95dcc8ee860": {"different": 1, "same": 986, "undecided in one judgement": 1}}. The same verdict for 99.9% of the decided pairs; a survivor's verdict includes its witness outcome.

| bug | artifact | recorded | re-judged | recorded killed by | re-judged killed by |
|---|---|---|---|---|---|
| rich@0752ff047295 | b16ff7c0933d | killed | survived: not a witness | tests/test_console.py::test_brokenpipeerror | – |

### Cost and the Lite subset

Judging cost: the oracle's recorded seconds of every artifact of the current unions (first judgement), in worker-hours; a worker is one pytest process at a time. Lite: per project up to 5 bugs drawn with seed 20261105, projects whose median historical suite runs longer than 60 s left out (cattrs, packaging, pyparsing): 111 bugs, 74.0 worker-hours. Against the full corpus (pre-fix detection): Spearman 0.982, largest rate difference 3.3 points; over 1000 draws by the same rule, Spearman median 0.938 (5th percentile 0.833), largest difference median 3.9 points (95th percentile 6.6). The Lite bug ids are in `lite.json`.

| project | bugs | judging worker-hours | per bug (median) | historical suite (median s) | in Lite |
|---|---|---|---|---|---|
| arrow | 14 | 5.0 | 0.26 | 8 | 5 |
| attrs | 15 | 12.7 | 0.41 | 12 | 5 |
| boltons | 15 | 3.9 | 0.13 | 4 | 5 |
| cachetools | 9 | 1.5 | 0.09 | 5 | 5 |
| cattrs | 15 | 91.2 | 2.80 | 149 | 0 |
| click | 15 | 5.1 | 0.17 | 8 | 5 |
| humanize | 12 | 5.2 | 0.18 | 7 | 5 |
| inflect | 15 | 20.8 | 0.16 | 7 | 5 |
| jinja2 | 7 | 12.6 | 0.21 | 11 | 5 |
| marshmallow | 15 | 2.1 | 0.08 | 4 | 5 |
| mistune | 15 | 4.8 | 0.12 | 4 | 5 |
| more-itertools | 15 | 3.1 | 0.17 | 18 | 5 |
| packaging | 15 | 14.0 | 0.41 | 94 | 0 |
| parse | 14 | 11.7 | 0.39 | 3 | 5 |
| portion | 4 | 4.2 | 0.99 | 10 | 4 |
| pyflakes | 7 | 15.1 | 0.61 | 12 | 5 |
| pyparsing | 15 | 46.8 | 1.34 | 77 | 0 |
| python-dotenv | 12 | 1.4 | 0.10 | 7 | 5 |
| python-pathspec | 11 | 5.2 | 0.54 | 2 | 5 |
| python-semver | 13 | 2.3 | 0.16 | 4 | 5 |
| python-tabulate | 14 | 21.8 | 1.22 | 9 | 5 |
| pyupgrade | 11 | 1.6 | 0.09 | 7 | 5 |
| rich | 15 | 17.9 | 0.54 | 11 | 5 |
| sqlparse | 15 | 8.7 | 0.50 | 4 | 5 |
| tomlkit | 15 | 3.2 | 0.17 | 3 | 5 |
| toolz | 2 | 0.0 | 0.02 | 4 | 2 |
| **total** | **325** | **321.9** | | | **111** |

What judging a tool alone would add: the artifacts only that tool emitted (no other tool has the same mutated file) and their judging cost; a new tool pays for its artifacts no published tool has.

| tool | artifacts only this tool emitted | worker-hours |
|---|---|---|
| pytest-leela | 242 | 0.2 |
| pytest-gremlins | 1082 | 1.2 |
| deployproof | 1123 | 2.0 |
| balamut | 1964 | 3.5 |
| fest | 5056 | 4.5 |
| poodle | 8648 | 14.2 |
| mutmut | 8279 | 19.8 |
| cosmic-ray | 12828 | 25.9 |
| irradiate | 25390 | 29.6 |
| universalmutator | 21753 | 33.8 |
| mutation | 64695 | 71.8 |

### Generator and engine side by side

S(k) rates the generator under a faithful engine: the survivors it draws are the reference oracle's. A tool's own engine shows its user its own survivors; the engine track measures how many of them the oracle kills (false survivors) on its six projects, and on how many of them the campaign gave no verdict.

| tool | pre-fix detection | location existed | S(3) | engine false survivors | campaigns without verdicts |
|---|---|---|---|---|---|
| mutation | 24% | 42% | 11% | 83.2% | 1 of 6 |
| universalmutator | 19% | 38% | 7% | 0.0% | 0 of 6 |
| poodle | 17% | 33% | 9% | 0.0% | 2 of 6 |
| irradiate | 16% | 29% | 7% | 49.7% | 1 of 6 |
| mutmut | 16% | 29% | 8% | 5.9% | 3 of 6 |
| cosmic-ray | 14% | 28% | 7% | 0.0% | 0 of 6 |
| deployproof | 14% | 26% | 9% | 34.1% | 2 of 6 |
| fest | 13% | 25% | 9% | 91.8% | 0 of 6 |
| balamut | 11% | 24% | 7% | 0.0% | 0 of 6 |
| pytest-leela | 7% | 16% | 5% | 84.3% | 2 of 6 |
| pytest-gremlins | 6% | 14% | 5% | 0.0% | 0 of 6 |
