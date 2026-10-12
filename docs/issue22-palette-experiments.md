This initial experiment is followed by [five artistic prompt experiments and the GPT 6 luna switch](issue22-artistic-experiments.md). The local default retains this control prompt after the user reviewed that follow-up; measurements below describe the earlier prompt.

The local fix for [issue #22](https://github.com/kagelump/rpi_board/issues/22) produced accepted artwork in all eight matched test flows, compared with five of eight for the archived prompt. The existing 15% hue guardrail, vision checks, rejection provenance, and refusal to publish rejected art remain in place. This is a small controlled sample, not a production reliability estimate.

The Pi at `hinoka@192.168.1.38` was reachable. Its archived run `055a501a67e3475d8152bc17ea6ee3b0` supplied the exact scene, image prompt, requests, and seeds. The baseline reproduced the reported **19.35%** and **50.84%** off-palette fractions exactly. No remote files or display state were changed. The local checkout starts at `b175c03`; the archive records revision `2a4b8e8f60f79ea49ae67dbbf9a2a51c53750358`.

Changes implemented locally:

- `weather_image.txt` starts with a concise positive ink specification and concrete equivalents for night, glass, reflections, foliage, and lighting. It removes the subject's absolute priority and the long repeated prohibition list.
- `_inject_style_prompt` translates common incompatible scene cues before the first request and shortens the style block. Scene objects and composition stay in the prompt; known visual colours/materials become ink shapes. Colour substitutions are limited to surface descriptions so object names such as “blue jay”, “green tea”, and an “orange” remain intact. Weather phrases such as “cool light breeze” and “soft light rain” are protected.
- `weather_brief.txt` gives the brief model the same physical constraints while leaving the chosen style and palette emphasis to image generation.
- Palette retries rewrite unnormalized scene cues and replace the final rendering instruction, instead of simply appending another colour ban. Corrections for all earlier rejections are retained on subsequent retries. Text and collage checks continue to run normally.

The deterministic translator covers a bounded vocabulary. Unhandled materials still rely on the positive rendering contract and the unchanged guardrails. It adds no API calls. Original subjects and full actual provider requests remain available in generation-history snapshots.

Experiment design and results:

`fal-ai/flux-2/klein/9b`, 8 inference steps, requested 960×480, PNG. The provider returned 1024×512 images; measurements used these raw images, before resizing, composition, or quantization. The production OpenRouter vision checker (`openai/gpt-5.2`) checked text/collage. Each variant used the same two seed pairs: **910921837 → 1664093912** and **424242 → 424243**. Four scenarios covered glass/pixel art with Golden Light, glass/stained glass with Night Signal, rain/ukiyo-e with Red Signal, and foliage/linocut with Yellow Field.

| Measurement | Archived baseline | Final candidate |
| --- | ---: | ---: |
| First attempts over 15% hue limit | 3/8 | 0/8 |
| Retry samples over 15% hue limit | 4/8 | 0/8 |
| Mean first-attempt off-palette | 8.86% | 0.00% |
| Mean retry off-palette | 20.76% | 0.00% |
| Explicit first-attempt rejection, including vision | 4/8 | 2/8 |
| Accepted within two attempts | 5/8 | 8/8 |
| Fallback after both rejections | 3/8 | 0/8 |

Both final first-attempt rejections were **text flags**, not colour failures, in the pixel-art scene. Both retries passed every check. No unavailable checks or provider errors occurred in this matrix.

Both attempts were generated even when the first passed, to measure retry prompts at matched seeds. Those extra retries are diagnostic only: they do not change a successful flow's operational result. Fallback counts apply the normal first-then-retry policy; no previous accepted hero is assumed. Of the baseline's three fallbacks, two were colour failures and one was a text failure. `0.00%` means the current hue metric measured zero unsupported chromatic pixels; it does not mean the image contains only four exact RGB values. Existing display quantization remains necessary.

Other experiments:

- A compact palette-first prompt **without** subject translation fixed both original night seeds. Across the larger matrix, however, explicit green trees/blue sky still produced 32.81% and 16.35% off-palette first attempts. Its retries recovered both. This motivated translation before the first request.
- A one-off OpenRouter scene rewrite also reached 0% on both original seeds. It offered no measured colour advantage over the compact prompt on that case, so no extra rewrite call was added to production.
- Increasing inference steps to 16 produced HTTP 422 for both seeds. It is not a usable fix for this endpoint. The [fal API documentation](https://fal.ai/models/fal-ai/flux-2/klein/9b/api) describes this endpoint as a distilled model and lists 4 as the default step count; the existing setting of 8 was retained.
- In total, these bounded experiments generated **54 images**, plus two rejected 16-step requests. Provider billing was not returned; no cost estimate is asserted.

These prompt changes were tested together. The experiment does not isolate ordering, brevity, or positive wording individually as the cause. Different late style/palette instructions produced identical baseline glass images at matched seeds, suggesting those instructions were underweighted or truncated; that remains an inference, not a verified provider mechanism.

Measurements are preserved in [issue22-palette-results.json](issue22-palette-results.json). A final weather-adjective protection was added after the provider evaluation; all 16 evaluated candidate prompts were verified byte-identical with the final generator, and the protection passed the regression suite. Both generator hashes and this verification are recorded in the results. Full requests, raw images, vision verdicts, latency, and hashes are in `runtime/issue22-matrix/` and `runtime/issue22-final/`. The initial exploration is in `runtime/issue22-eval/`. Runtime artifacts are gitignored.

![Raw baseline reference on the left; accepted final candidate on the right](../runtime/issue22-final/previews/comparison.png)

The baseline images above are rejected reference samples where applicable, never published artwork. A composed 960×640 candidate board and its device-palette conversion are saved in `runtime/issue22-final/previews/board.png` and `board-quantized.png`. Preview copy is illustrative.

To reproduce with the configured key files, from the repository root:

```bash
python3 scripts/eval/eval_palette_prompts.py --out runtime/issue22-reproduction
```

This makes at most 32 generation calls and 32 vision calls, with two workers. Use a fresh output directory to preserve prior experiment provenance. `--seed-pairs 1` halves the calls; `--variants candidate` evaluates only the candidate. The final candidate was compared with the already measured baseline rather than generating identical baseline requests again. The harness never invokes display delivery or writes live hero/board/cache files.

Validation: **168 relevant tests passed** across brief/image generation, guardrails, palette analysis/quantization, and issue #22 regressions. The full suite yielded **501 passed, 3 failed**. The same three failures existed before these changes on this Mac: missing Linux `flock`, and two archived-copy tests whose expected text overflow depends on available fonts. The full log is `runtime/issue22-full-tests.log`. No Raspberry Pi deployment, GitHub write, threshold increase, or validation disablement was performed.
