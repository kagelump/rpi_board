User selection after reviewing the comparisons: **keep the control prompt**. The control template and style wording have been restored exactly; GPT 6 luna remains selected. Integration onto the Pi's newer main revision preserves its existing artwork recipes, forecast protections, and production text-check opt-out. The benchmark harness explicitly enables text checking, as in the recorded experiments. The texture/targeted results and recommendation below are retained as experiment history, not the active configuration.

GPT 6 luna is now the local art-checking and image-tool orchestration default. Five prompt experiments found that style-led texture can recover artistic detail while keeping the existing 15% palette guardrail. The **control** variant is the selected image prompt; the experiment variants remain saved for comparison. No Raspberry Pi files or display state were changed.

GPT 6 luna verification:

[OpenAI's model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna) confirms image input, JSON/structured output, and Responses tool support. The [live OpenRouter catalog](https://openrouter.ai/api/v1/models), captured on October 12, lists both model IDs and these rates:

| Per million tokens | GPT 5.2 | GPT 6 luna | Reduction |
| --- | ---: | ---: | ---: |
| Input | $1.75 | $0.10 | 17.5× lower |
| Output | $14.00 | $0.50 | 28× lower |

Luna passed six task controls using the actual production `inspect_art` code: four clean generated images, artwork with visible `RAIN 24C` lettering, and a realistic night-building inset inside a graphic frame. All six returned usable JSON and the expected verdicts. A second comparison ran three identical samples through both models. GPT 5.2 billed **$0.00785925** in total; Luna billed **$0.00044070**, a **94.4% saving** for those calls, with matching verdicts. This reduces the checking/orchestration cost; fal image generation charges are unchanged.

The local `openrouter.image_tool_model` setting changed from `openai/gpt-5.2` to `openai/gpt-6-luna`. `art_guardrail` inherits that setting. Unsupported `temperature` is omitted for GPT-6 reasoning requests in the production checker and existing evaluation judge, following the [migration guidance](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-6-astra). The DeepSeek brief model and fal image model remain as configured.

Luna also successfully invoked the optional OpenRouter image-generation tool. The **upstream image request failed** because the configured Gemini endpoint rejects `aspect_ratio: "2:1"`. This was a provider-parameter failure, not a Luna tool-calling failure. The existing parameter was not changed during this experiment; the active fal route works normally. The smallest separate repair is to choose a supported Gemini ratio and verify the resulting crop/composition. No successful end-to-end OpenRouter image generation is claimed here.

Model checks are limited: Luna accepted two abstract pixel-art frames that the earlier GPT 5.2 run had flagged as lettering-like shapes. Clear lettering and the deliberate inset were correctly rejected by both. These samples support the switch for this workload, but do not establish identical sensitivity on every ambiguous image.

Five hypotheses:

All variants retained the same four-ink vocabulary, no-text instruction, vision checks, raw-pixel hue metric, 15% limit, and one retry. The image provider was `fal-ai/flux-2/klein/9b`, 8 steps, requested 960×480; returned images were 1024×512. Each hypothesis used four scene/style combinations and two first seeds (**910921837**, **424242**), with retry seeds **1664093912** and **424243**. Only explicit rejection triggered a retry. Existing control images were rechecked with Luna, so the model change was not confused with the prompt comparison.

| Hypothesis | What changed | First palette failures | Peak first off-palette | Raw artistic wins vs control | Accepted flows |
| --- | --- | ---: | ---: | ---: | ---: |
| 1. Concise contract | Remove global sky/glass/foliage mappings and all-surfaces-flat language; keep translated subject | 0/8 | 0.01% | 6/8 | 8/8 |
| 2. Style first | Move the same style instruction before the concise contract | 0/8 | 0.02% | 7/8 | 8/8 |
| 3. Texture | Add style-led hatching, carved gaps, dot fields, irregular edges, and tone from mark density | 0/8 | 0.02% | 7/8 | 8/8 |
| 4. Preserve materials | Keep original glass, reflection, faint-form and atmosphere descriptions; translate explicit surface hues only | 1/8 | 53.87% | 8/8 | 8/8 after one retry |
| 5. Targeted ink plan | Keep hypothesis 4, adding ink guidance only for glass architecture at night | 0/8 | 4.68% | 8/8 | 8/8 |

There were **zero fallbacks** and no unavailable validations in this round. Hypothesis 4's failing pixel-art night scene recovered to 0% on retry. Four daytime flows in hypothesis 5 were unchanged from hypothesis 4 and reused its images and judgments; four night flows were newly generated. This round generated **37 new fal images**, including the single retry. First-attempt raw measurements were never taken after quantization.

Art comparisons hid the hypothesis name and balanced image order. Luna judged style fidelity, expressive detail and scene fidelity on a 1–5 scale, with a separate overall artistic preference. These are **subjective model judgments on a small sample**, not a human vote or a reliability estimate. All 32 initial comparisons and four new fifth-hypothesis comparisons completed. The fourth/fifth hypotheses' artistic results refer to accepted artwork, including the recovered retry where applicable.

The most useful finding is scene contamination from global examples. The previous prompt mentioned black night sky, white moon/stars and leaf mappings even when the scene was a rainy street. In the saved control, the rainy scene became a clear night with a moon and stars. Removing those global examples restored rain strokes and a heavy sky. That association is supported by the images; it is not a proof of the model's internal mechanism. Moving style first alone made little visible difference in several matched samples. Explicit permission for print texture made the larger improvement.

Validation on the panel palette:

The unchanged production quantizer converted the texture and targeted variants plus their controls into the device's exact white/black/red/yellow RGB palette. Only the art region was compared; headline/date/temperature overlays were excluded. This was image validation, not a physical hardware refresh.

| After device quantization | Texture | Targeted ink plan |
| --- | ---: | ---: |
| Artistic wins vs control | 8/8 | 7/8 |
| Mean expressive-detail gain, 1–5 scale | +1.12 | +1.62 |
| Mean scene-fidelity gain, 1–5 scale | +1.00 | +1.38 |

The targeted variant has richer detail and stronger scene fidelity in these ratings, while texture has almost no hue drift and the more consistent device preference. **Texture was the experiment recommendation; the user subsequently selected control.** The targeted experiment remains available for a preference toward richer, subtler material rendering. Both kept every first attempt within the 15% colour limit. Gray/tonal dithering remains visible and can suit print texture; this study did not add a new tonal-noise metric or change the quantizer.

![Device-palette comparison: previous control, texture, targeted](../runtime/issue22-artistic/device-comparison.png)

[All five raw variants and control](../runtime/issue22-artistic/raw-comparison.png) are also saved. The [evidence JSON](issue22-artistic-results.json) contains catalog prices, capability controls, billed costs, per-attempt metrics, verdicts and paired judgments. Full prompts, provider requests, images and comparison details are in `runtime/issue22-artistic/`. The original phase's report remains in [issue22-palette-experiments.md](issue22-palette-experiments.md).

Reproduce from the repository root using the configured key files:

```bash
python3 scripts/eval/eval_artistic_prompts.py --out runtime/issue22-artistic-reproduction --device
```

The default is all five hypotheses: 40 first generations, up to 40 retries, and their checking/comparison calls. To run just the chosen default, use `--variants texture`, which bounds image generation to 16 calls. Use a fresh output directory. The control defaults to the retained `runtime/issue22-final/candidate` images from the first phase. The actual run saved the four initial hypotheses under `matrix/`, the fifth under `targeted/`, and quantized comparisons under `device/`; unaffected fifth-hypothesis flows were reused rather than billed again.

Local changes are limited to model configuration/compatibility, the selected prompt, regression checks, the experiment templates/harness, and reports. The production prompt builder has been restored to the evaluated control configuration. **171 relevant tests passed**, including GPT-6 request compatibility, retained rejection handling, and a new regression that rainy scenes do not receive invented moon/star instructions. No deployment, threshold change, guardrail disablement, or rejected-art publication occurred.
