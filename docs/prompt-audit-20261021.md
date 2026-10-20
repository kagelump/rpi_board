# Prompt audit, October 21

This revision preserves the richer after style the user preferred, including
London and Tokyo. The prompt edit itself involved no image generation, Pi
deployment, model switch, threshold change or cache reset. A subsequent
20-image evaluation is recorded below.
The earlier 20-image evaluation remains an immutable record of the preceding
draft, not a measurement of this revision.

| Conflict | Resolution |
|---|---|
| Briefs requested blurred backgrounds or sharp camera focus while the image prompt prohibited photographic blur. | Briefs now request overlapping forms and variation in drawn detail. Cached camera directions are normalized before injection. Reflections, translucent materials and subdued light remain valid drawing subjects. |
| Generic subject/viewpoint preservation competed with recipe staging. Recipes referred to an obsolete `SUBJECT` heading and constraints "below" them. | Explicit precedence: forecast conditions, scene lighting and available inks govern every layer; recipes set subject, setting and framing; scene mood and weather remain. References no longer depend on headings or position. |
| Paper Cutout requested "collage" while generic generation/retry instructions banned collage. | Unified paper-cut forms are explicitly allowed. Prohibitions name inserted photos, inset pictures and separate image panels. |
| De Stijl's "primary color blocks" could imply blue and demanded universal geometric flattening. | Specify red/yellow/white blocks and black structure as an illustrated vocabulary while retaining drawn material detail and depth. |
| The optional text-checking QA prompt described the target as "flat, minimal", contrary to the user's preference. | QA describes four-ink illustration; both QA prompts clarify that detailed illustrated backgrounds, drawn depth and unified paper-cut treatments are not themselves collage. Actual photographic insets remain covered. |
| Recent-history novelty rules demanded a new subject/composition at every refresh despite the fixed daily theme. | Distinguish novelty across forecast dates from continuity within one date. |
| Persona instructions could discourage useful numeric forecast details required by the headline rules. | State that factual/output/artwork rules take precedence over persona phrasing. |
| A legacy scene's night lighting could be treated as authoritative by recipe text. | Explicit scene-lighting direction takes precedence; black ink dominance remains a print choice. |

The production image template repeats the same sentence in three separated
positions: **Use only white, black, red and yellow inks.**

1. At the beginning, before the scene: the allowed colors apply to foreground,
   background, foliage, reflections and sky.
2. In the middle, after style guidance: give ink equivalents for foliage, water
   and glass while preserving veins, droplets, reflections, texture and depth;
   ink overlaps cannot introduce mixed hues.
3. At the end, after palette emphasis and recipe staging: check every region
   against the same four inks and replace other hues with drawn ink marks.

Palette emphasis chooses balance within that ink set; it does not expand it.
The repeated rules do not require all surfaces to become flat blocks or all four
inks to occupy equal area. Daylight, forecast accuracy and no-lettering
generation instructions continue to apply. Production text checking remains
configured off; the 15% raw-palette threshold, retry budget, quantizer and
rejected-image publication behavior are unchanged.

Regression coverage exercises the assembled prompt with recipes, dark palettes
and palette/collage retries; verifies that all three color reminders survive;
and checks that camera-cue normalization preserves material and weather details,
object names and existing negative camera constraints. Ten revised prompts
replayed from the earlier after briefs are saved under
`runtime/prompt-audit-20261021/` for inspection. These are prompt-only replays,
not newly generated images or new vision judgments.

## Repeat evaluation after the audit

The user requested the same five-location, two-seed before/after evaluation.
Twenty fresh images were generated with committed deployment `2c1d092e4939`
as before and the audited working tree as after. Archived weather/payloads,
the October 20 21:00 local run clock, styles, palettes and image seeds were
identical to the preceding run. Each cell received an independent new brief.
Sources and complete requests were frozen in the new output directory.

| Measure | Before | After |
|---|---:|---:|
| Images | 10 | 10 |
| Passed shared current guardrail and palette check | 10 | 8 |
| Raw palette failures over 15% | 0 | 2 |
| Classified daylight | 2 | 10 |
| Classified night / ambiguous | 7 / 1 | 0 / 0 |
| Blinded artistic preference wins | 3 | 7 |

Singapore's two after samples still exceeded the palette limit, at 18.06% and
19.26%. Remaining unsupported colors were visible in background foliage and
water. Every sample passed the shared current illustration check; no image
retries or hardware publication occurred. The same working-tree vision-check
prompt was used for both variants, so those verdicts should not be treated as
an isolated before/after test of the checker wording. Daylight classifications
and artistic preferences are subjective model judgments on device-quantized
artwork. This evaluates the combined changes, not color repetition alone.

[Repeat report](../runtime/daylight-eval-20261021-audited/report.md) ·
[Full-size gallery](../runtime/daylight-eval-20261021-audited/index.html) ·
[Results and provenance](../runtime/daylight-eval-20261021-audited/results.json)

## Approved production setting

After reviewing the repeated evaluation, the user selected every after sample
and authorized raising the raw off-palette limit from 15% to **40%**, committing,
pushing and updating the Pi. The shipped setting and missing-key runtime default
are now `0.40`; exactly 40% passes and anything above it is rejected. Final
display quantization remains restricted to white, black, red and yellow.
The evaluation results above retain their original 15% threshold for provenance;
both Singapore after samples are within the newly selected 40% budget.
