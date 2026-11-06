# Selecting the mobile AI model on Railway

In `pleasing-nurturing → production → Resume-Matcher → Variables`, set:

| Variable | Value |
| --- | --- |
| `LLM_PROVIDER` | `openrouter` |
| `LLM_MODEL` | Exact model ID copied from OpenRouter, such as `publisher/model-id` |
| `LLM_API_KEY` | Your OpenRouter API key; enter it directly in Railway |
| `REASONING_EFFORT` | `medium` for the initial balanced setting |

Apply the variable changes with **Deploy**. A model change needs no Draftbit
release. Model IDs are text values; Railway does not provide a model catalog.

## Reasoning effort

In mobile mode, an explicitly supplied `REASONING_EFFORT` takes precedence over
any saved effort, independently of provider/model overrides. Supported values
are `minimal`, `low`, `medium` and `high`. Use a blank value to select **Auto**
(omit the effort parameter and let the model/provider use its default).
Removing the variable restores the saved effort if one exists. Non-mobile
installations retain saved-configuration priority, so the reference web app
can still be configured from its Settings page.

Edit this value in Railway Variables and deploy to change the level. The
application forwards the selected effort on supported requests; actual
reasoning behavior depends on the model, provider and available output budget.
Higher effort can increase latency and billed tokens and does not establish
better CV quality by itself. Set the reference web app to **Medium** for the
initial comparison with this deployment.

## Selection priority

With `MOBILE_API_ENABLED=true`, a nonblank `LLM_MODEL` overrides the saved model.
An explicitly supplied `LLM_PROVIDER` overrides the saved provider only together
with a nonblank `LLM_MODEL`. This also prevents the Docker startup script's
injected `LLM_PROVIDER=openai` default from replacing a saved selection.
Implicit Settings defaults never replace a saved selection. When omitted, the existing
saved configuration remains in effect. Non-mobile installations retain their
existing settings behavior.

Change both provider and model together when switching providers. A provider
variable without an explicit model leaves the saved selection in effect.
Provider-specific saved endpoints, shared keys and
reasoning options are not reused across a provider switch; set the credentials
and any required endpoint for the new provider separately. Local providers
continue to reject the generic cloud-key fallback.

The existing mobile OpenRouter rule makes `LLM_API_KEY` take precedence over
an expired saved OpenRouter key. The runtime selection does not overwrite
`config.json`; removing the selection variables and redeploying restores the
saved selection.

## Verify before adopting a model

Use a model that supports text completion and the structured output needed by
the engine. Compare the same CV and job description for factual accuracy,
completeness, wording quality, runtime and cost. Changing the model does not
change the full-tailoring defaults, page limits or processing budget, and does
not by itself establish output quality. Ordinary deterministic tests make no
paid AI calls.

No administrative configuration routes are opened by this change. API keys
remain server-side.
