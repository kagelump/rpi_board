# Daylight art direction draft, October 20

This local draft restores the pre-October-12 emphasis on subject, atmosphere,
expressive drawing and recognizable style while retaining the four-ink display
contract. It has not been deployed to the Raspberry Pi. A five-location,
20-image evaluation completed on October 21 using local key files; see the
[comparison report](../runtime/daylight-eval-20261021/report.md) and
[full-size gallery](../runtime/daylight-eval-20261021/index.html). Existing
October 12 experiments remain historical evidence, not measurements of this draft.

The new draft was classified as daylight in 10/10 images, versus 1/10 with the
deployed prompt, and won 7/10 blinded model artistic comparisons after device
quantization. However, only 6/10 draft images passed the existing first-attempt
guardrails, versus 10/10 before: Singapore's two samples had 39.66% and 33.95%
off-palette hues, and one Reykjavik and one London sample were rejected for
mixing illustrated foregrounds and realistic backgrounds. Tokyo lost both
artistic comparisons as added texture weakened the minimal ukiyo-e style.
These are small-sample measurements and subjective model judgments. The draft
needs refinement before deployment; daylight is promising, but material freedom
needs more targeted color guidance and style-dependent texture.

The user subsequently preferred the after artwork, including London and Tokyo;
that preference takes precedence over the model's style rankings. The subsequent
[prompt audit](prompt-audit-20261021.md) resolves conflicting instructions and
repeats the four-ink rule at the beginning, middle and end of the image prompt.
It also clarifies that detailed illustrated backgrounds and unified paper-cut
forms are not themselves collage. The numbers above describe the archived
20-image draft. A subsequent [audited repeat evaluation](../runtime/daylight-eval-20261021-audited/report.md)
generated 20 fresh images: the working-tree variant passed 8/10 shared checks,
was classified as daylight in 10/10 and won 7/10 model artistic comparisons.
Both remaining failures were Singapore palette measurements over 15%.
The user then selected all after samples and approved a 40% raw off-palette
limit for production. The 15% values in the evaluation sections remain historical;
the shipped configuration and runtime default now use `0.40`.

## Proposed behavior

- Keep white, black, red and yellow as the display inks. Use the user-approved
  40% raw off-palette budget, while retaining the collage check, configured
  text-check preference, retry budget, quantizer and rejected-image fallback.
- Let each style control texture, line quality, detail and depth. Remove the
  blanket demand for flat surfaces and crisp geometric shapes. Use separated
  ink marks for tone instead of smooth gradients or unsupported colors.
- Preserve materials and atmosphere on the first generation. Normalize explicit
  incompatible surface hues, protecting object names such as blue jay and green
  tea. Only a rejected palette attempt gets stricter material translation.
- Give both brief and image generation an explicit artwork context independent
  of the run clock. At 21:00, write an advance briefing and illustrate the next
  forecast day's daytime weather. At 08:00, keep that daytime board unless the
  normal forecast gate requests a change. At 13:00, show afternoon daylight.
- Remove positive global night-sky/moon/star examples and the after-dark visual
  rotation entry. Keep black-heavy print treatments, including the existing
  Night Signal palette identity, but describe them as ink balance rather than
  night lighting. This draft defaults all scheduled scenes to daylight.
- Do not make daylight synonymous with sunshine: overcast and rainy days stay
  overcast and rainy. Night-only rain stays in forecast advice rather than
  appearing as rain in a daytime picture. Daylight instructions explicitly
  override stale night cues in old scene descriptions or recipes.

For example, tomorrow's warm, mostly dry day with a late sprinkle should show
a daytime persimmon branch or neighborhood scene; mention the late sprinkle
in the copy. A daytime facade should not acquire a moon from generic palette
examples. A gray sky is normalized to an overcast sky, not a black sky.

## Review and rollout

The completed evaluation used the five configured locations (Reykjavik,
Phoenix, Singapore, London and Tokyo), two matched image seeds per variant,
and live weather archived once per city. Each cell had an independent brief;
this tests the combined brief/image changes rather than rendering in isolation.
Both variants simulated October 20 at 21:00 local time, targeting October 21.
No image retries were run, so the sample is exactly 20 generated images.
Raw palette metrics, production vision verdicts, source snapshots, complete
requests/prompts and blinded paired reviews are retained with the report.
Rejected images are diagnostic references only. Text checking retained the
production opt-out. Re-run or resume with:

```bash
python3 -u scripts/eval/eval_daylight.py --out runtime/daylight-eval-20261021 --workers 4
```

A fresh output directory spends on a new evaluation; the existing directory
reuses saved outputs. The runner reads `~/.openrouter.key` and `~/.fai.key`
without copying secrets into artifacts. No sample weather fallback is used.

When deployment is requested, use one full forced refresh so a cached night
brief or hero cannot mask the new direction. Subsequent scheduled updates keep
the existing cache and daily-style locking behavior. The draft does not change
the image model, inference steps or text model.
